"""英文游戏实时翻译悬浮窗 —— 入口与流水线编排。

数据流：抓帧 → 变化检测 → RapidOCR → 稳定判定 → 翻译 → 悬浮窗

线程划分（tkinter 只允许主线程碰）：
  主线程      tkinter 主循环 + 定时从队列取消息刷界面
  pipeline    抓帧 + OCR，一个独立线程
  translate   网络翻译，另一个线程，避免慢请求把抓帧卡住
  hotkeys     RegisterHotKey 的消息循环，必须在自己的线程里
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import winapi
from capture import CaptureError, WindowCapture
from config import CACHE_PATH, Config
from ocr import Ocr, OcrError
from overlay import ACCENT, ERR, WARN, Overlay
from translate import TranslateError, Translator, normalize


# --- 命令行 ---

def parse_region(s: str) -> list[float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("需要 4 个数，形如 0.05,0.60,0.95,0.88")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"不是合法的数字: {s!r}") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="抓取英文游戏窗口的文字，机翻后显示在置顶悬浮窗里。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程：\n"
            "  python main.py --list                    看有哪些窗口\n"
            "  python main.py -w \"游戏名\" --snapshot .scratch/crop.png\n"
            "                                           先截一张抓取范围，确认选区对不对\n"
            "  python main.py -w \"游戏名\" --once         跑一次 OCR+翻译，不开窗口\n"
            "  python main.py -w \"游戏名\"                正式启动\n"
        ),
    )
    p.add_argument("-w", "--window",
                   help="目标窗口：标题或 exe 名字的一部分都行，不区分大小写")
    p.add_argument("--list", action="store_true", help="列出当前可见窗口后退出")
    p.add_argument("--region", type=parse_region,
                   help="只抓客户区的一块，比例形式 0.05,0.60,0.95,0.88（强烈建议设，能避开血条小地图）")
    p.add_argument("--fps", type=float, help="抓帧频率上限，默认 6")
    p.add_argument("--alpha", type=float, help="悬浮窗不透明度，默认 0.92")
    p.add_argument("--width", type=int, help="悬浮窗初始宽度")
    p.add_argument("--height", type=int, help="悬浮窗初始高度")
    p.add_argument("--font-size", type=int, dest="font_size", help="正文字号")
    p.add_argument("--output", type=int, dest="output_idx", help="抓帧用的显示器序号，默认自动")
    p.add_argument("--device", type=int, dest="device_idx", help="显卡序号，默认 0")
    p.add_argument("--backend", choices=["auto", "wgc", "dxcam", "mss"],
                   help="抓帧后端，默认 auto（wgc → dxcam → mss 依次尝试）")
    p.add_argument("--det-size", type=int, dest="det_side_len",
                   help="OCR 检测长边上限，默认 1280。调小更快，调大认得更小的字")
    p.add_argument("--min-score", type=float, dest="min_score", help="OCR 置信度下限，默认 0.5")
    p.add_argument("--use-cls", action="store_true", default=None, dest="use_cls",
                   help="开启文本方向分类（游戏横排文字不需要，开了更慢）")
    p.add_argument("--source-lang", dest="source_lang", help="源语言，默认 en")
    p.add_argument("--target-lang", dest="target_lang", help="目标语言，默认 zh-CN")
    p.add_argument("--show-source", action="store_true", default=None, dest="show_source",
                   help="在译文上方显示英文原文")
    p.add_argument("--no-show-source", action="store_false", dest="show_source")
    p.add_argument("--no-cache", action="store_false", dest="use_cache", help="不读写磁盘翻译缓存")
    p.add_argument("--hotkey-pause", dest="hotkey_pause", help="暂停/继续热键，默认 ctrl+alt+t")
    p.add_argument("--hotkey-click", dest="hotkey_click", help="鼠标穿透热键，默认 ctrl+alt+c")
    p.add_argument("--hotkey-quit", dest="hotkey_quit", help="退出热键，默认 ctrl+alt+q")
    p.add_argument("--debug", action="store_true", default=None, help="在状态栏显示 OCR 耗时")
    p.add_argument("--once", action="store_true", help="抓一帧做 OCR+翻译并打印，然后退出")
    p.add_argument("--snapshot", metavar="PNG路径", help="把抓取范围存成 PNG，用来校准选区")
    p.add_argument("--save-config", action="store_true", help="把本次参数写进 config.json")
    return p


def _setup_console() -> None:
    # Windows 控制台默认是 GBK，中文提示会变成乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _quiet_comtypes_teardown() -> None:
    """静音 dxcam 释放资源时那条已知的 comtypes 异常。

    dxcam 的 duplicator.release() 会对同一个 COM 指针重复调用 Release()，
    每次退出都打一段 "access violation" 的 traceback。它不影响任何功能，
    但看着像崩溃，会让人以为工具出错了。

    这里只过滤 traceback 里出现 comtypes 的 unraisable 异常，其它一律交给
    默认处理器 —— 不能把真正的问题一起吞掉。
    """

    def hook(unraisable) -> None:
        tb = unraisable.exc_traceback
        while tb is not None:
            if "comtypes" in tb.tb_frame.f_code.co_filename:
                return
            tb = tb.tb_next
        sys.__unraisablehook__(unraisable)

    sys.unraisablehook = hook


def _print_windows() -> None:
    wins = winapi.list_windows()
    if not wins:
        print("  （没有找到可见的窗口）")
        return
    for w in wins:
        print(f"  {w}")


# --- 后台线程 ---

class Pipeline(threading.Thread):
    """抓帧 + OCR。只负责产出"一段稳定的文本"，翻译交给另一个线程。"""

    daemon = True

    def __init__(self, capture: WindowCapture, ocr: Ocr, cfg: Config, ui: Overlay, trans_q: queue.Queue):
        super().__init__(name="pipeline")
        self.capture = capture
        self.ocr = ocr
        self.cfg = cfg
        self.ui = ui
        self.trans_q = trans_q
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self._pending = ""
        self._pending_norm = ""
        self._last_sent = ""

    def run(self) -> None:
        interval = 1.0 / self.cfg.fps
        errors = 0
        while not self.stopped.is_set():
            started = time.perf_counter()
            if self.paused.is_set():
                time.sleep(0.15)
                continue

            try:
                frame = self.capture.grab()
            except Exception as e:
                errors += 1
                self.ui.post(("error", f"抓帧失败: {e}"))
                time.sleep(min(2.0, 0.2 * errors))
                continue

            if frame is None:
                # 没有新帧 == 画面静止 == 刚识别出的那段文字已经稳定，可以翻了
                self._flush()
                time.sleep(interval)
                continue

            errors = 0
            try:
                text = self.ocr.read(frame)
            except OcrError as e:
                self.ui.post(("error", str(e)))
                time.sleep(interval)
                continue

            self._on_text(text)
            if self.cfg.debug:
                rect = self.capture.current_rect()
                size = f"{rect[2] - rect[0]}x{rect[3] - rect[1]}" if rect else "?"
                self.ui.post(("status", f"OCR {self.ocr.last_elapse * 1000:.0f}ms · 抓取 {size} · {len(text)} 字符"))

            time.sleep(max(0.0, interval - (time.perf_counter() - started)))

    def _on_text(self, text: str) -> None:
        norm = normalize(text)
        if not norm:
            # 对话结束、文字消失。清掉已发送记录，这样同一句话再次出现时
            # 还能重新显示（译文走缓存，几乎不花时间）
            self._pending = self._pending_norm = self._last_sent = ""
            return
        if norm == self._pending_norm:
            self._flush()  # 连续两次识别结果一致 -> 不是逐字打印动画，可以翻了
        else:
            self._pending, self._pending_norm = text, norm

    def _flush(self) -> None:
        if not self._pending_norm or self._pending_norm == self._last_sent:
            return
        self._last_sent = self._pending_norm
        enqueue_latest(self.trans_q, self._pending)


class TranslateWorker(threading.Thread):
    daemon = True

    def __init__(self, translator: Translator, trans_q: queue.Queue, ui: Overlay, debug: bool = False):
        super().__init__(name="translate")
        self.translator = translator
        self.trans_q = trans_q
        self.ui = ui
        self.debug = debug
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                text = self.trans_q.get(timeout=0.3)
            except queue.Empty:
                continue
            if text is None:
                break
            started = time.perf_counter()
            try:
                out, cached = self.translator.translate(text)
            except TranslateError as e:
                self.ui.post(("error", f"翻译失败: {e}"))
                self.ui.post(("status", "翻译失败，下一句会重试"))
                continue
            ms = (time.perf_counter() - started) * 1000
            self.ui.post(("result", (text, out, cached)))
            if self.debug:
                # 同时打到控制台。悬浮窗在游戏上面，出了问题时没法一边看游戏
                # 一边看窗口，能留一份控制台记录省很多事。
                print(f"[识别] {text}\n[译文] {out}\n", flush=True)
            if cached:
                self.ui.post(("status", f"翻译命中缓存 · {len(text)} 字符"))
            else:
                self.ui.post(("status", f"翻译 {ms:.0f}ms · {self.translator.last_provider} · {len(text)} 字符"))


def enqueue_latest(q: queue.Queue, item: str) -> None:
    """只保留最新一条待翻译文本 —— 翻译比抓帧慢，积压的请求已经没有意义了。"""
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


# --- 单次模式 ---

def _grab_with_retry(capture: WindowCapture, tries: int = 40, delay: float = 0.1):
    for _ in range(tries):
        frame = capture.grab()
        if frame is not None:
            return frame
        time.sleep(delay)
    return None


def _do_snapshot(capture: WindowCapture, path: str) -> int:
    frame = _grab_with_retry(capture)
    if frame is None:
        print("抓不到画面。窗口是不是最小化了？或者换个 --backend mss 试试。")
        return 1
    from PIL import Image

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(frame[:, :, ::-1]).save(out)  # BGR -> RGB
    rect = capture.current_rect()
    print(f"抓取范围（物理像素）: {rect}")
    print(f"画面尺寸: {frame.shape[1]}x{frame.shape[0]}")
    print(f"已保存: {out}")
    print("打开看看 —— 里面应该正好是你要翻译的那块文字。不对就调 --region。")
    return 0


def _do_once(capture: WindowCapture, cfg: Config) -> int:
    frame = _grab_with_retry(capture)
    if frame is None:
        print("抓不到画面。窗口是不是最小化了？或者换个 --backend mss 试试。")
        return 1
    print("初始化 OCR（首次要几秒）…")
    ocr = Ocr(min_score=cfg.min_score, det_side_len=cfg.det_side_len, use_cls=cfg.use_cls)
    text = ocr.read(frame)
    if not text:
        print(f"没识别到文字（OCR 耗时 {ocr.last_elapse * 1000:.0f}ms）。")
        print("用 --snapshot 看看抓到的到底是什么画面。")
        return 1
    print(f"\n识别到（{ocr.last_elapse * 1000:.0f}ms）：\n{text}\n")
    translator = Translator(
        cfg.source_lang, cfg.target_lang, cache_path=CACHE_PATH if cfg.use_cache else None
    )
    try:
        out, cached = translator.translate(text)
    except TranslateError as e:
        print(f"翻译失败: {e}")
        return 1
    print(f"译文（{'缓存' if cached else translator.last_provider}）：\n{out}")
    translator.save_cache()
    return 0


# --- 主流程 ---

def main(argv: Optional[list[str]] = None) -> int:
    _setup_console()
    _quiet_comtypes_teardown()
    args = build_parser().parse_args(argv)

    cfg = Config.load()
    cfg.apply_args(args)
    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"参数错误: {problem}")
        return 2

    winapi.set_dpi_aware()  # 必须在任何窗口/抓屏调用之前

    if args.list:
        print("当前可见窗口：")
        _print_windows()
        return 0

    if not cfg.window:
        print("要用 -w/--window 指定游戏窗口。标题或 exe 名字的一部分都行。当前可见窗口：")
        _print_windows()
        return 2

    matches = winapi.match_windows(cfg.window)
    if not matches:
        print(f"没找到匹配 {cfg.window!r} 的窗口（标题和 exe 名字都试过了）。当前可见窗口：")
        _print_windows()
        return 2
    if len(matches) > 1:
        print(f"有 {len(matches)} 个窗口都匹配 {cfg.window!r}，写得更具体一点：")
        for w in matches:
            print(f"  {w}")
        return 2
    target = matches[0]
    hwnd, title = target.hwnd, target.title
    print(f"目标窗口: {target}")

    if args.save_config:
        cfg.save()
        print(f"参数已写入 {Path(__file__).with_name('config.json')}")

    try:
        capture = WindowCapture(
            hwnd,
            title=title,
            region=tuple(cfg.region) if cfg.region else None,
            backend=cfg.backend,
            output_idx=cfg.output_idx,
            device_idx=cfg.device_idx,
            fps=cfg.fps,
        )
    except CaptureError as e:
        print(f"抓帧初始化失败: {e}")
        return 1

    note = getattr(capture, "backend_note", "")
    print(f"抓帧后端: {capture.backend}" + (f"（{note}）" if note else ""))
    if cfg.region:
        print(f"抓取区域: 客户区的 {cfg.region}")

    # 遮挡检查要尽早做：dxcam/mss 抓的是合成后的桌面画面，窗口被盖住时 OCR
    # 读到的就是遮挡物的内容。不提前说的话，用户只会看到一堆莫名其妙的译文。
    # WGC 抓的是窗口自己的内容，盖不盖住都一样，这时报警纯属误报。
    warning = None if capture.backend == "wgc" else _check_covered(hwnd)
    if warning:
        print(f"\n警告: {warning}\n")

    try:
        if args.snapshot:
            return _do_snapshot(capture, args.snapshot)
        if args.once:
            return _do_once(capture, cfg)
        return _run_gui(capture, cfg, warning)
    finally:
        capture.release()


def _check_covered(hwnd: int) -> Optional[str]:
    """目标窗口被别的窗口盖住时返回一句警告，否则返回 None。"""
    try:
        cover = winapi.covering_window(hwnd)
    except Exception:
        return None
    if cover is None:
        return None
    return (
        f"目标窗口被「{cover.title}」盖住了，现在抓到的会是它的内容。"
        "请把游戏切到前台再启动；如果游戏是独占全屏，改成无边框窗口。"
    )


def _run_gui(capture: WindowCapture, cfg: Config, warning: Optional[str] = None) -> int:
    print("初始化 OCR…")
    try:
        ocr = Ocr(min_score=cfg.min_score, det_side_len=cfg.det_side_len, use_cls=cfg.use_cls)
    except OcrError as e:
        print(f"OCR 初始化失败: {e}")
        return 1

    translator = Translator(
        cfg.source_lang, cfg.target_lang, cache_path=CACHE_PATH if cfg.use_cache else None
    )
    print(f"翻译端点（按优先级）: {' → '.join(translator.provider_names)}")

    trans_q: queue.Queue = queue.Queue(maxsize=1)
    commands: dict = {}
    ui = Overlay(cfg, commands)
    pipeline = Pipeline(capture, ocr, cfg, ui, trans_q)
    worker = TranslateWorker(translator, trans_q, ui, debug=cfg.debug)

    state = {"paused": False, "click": False}

    def toggle_pause() -> None:
        state["paused"] = not state["paused"]
        if state["paused"]:
            pipeline.paused.set()
            ui.set_state("已暂停", WARN)
            ui.post(("status", "已暂停，热键再按一次继续"))
        else:
            pipeline.paused.clear()
            ui.set_state("监听中", ACCENT)
            ui.post(("status", "继续监听"))

    def toggle_click() -> None:
        state["click"] = not state["click"]
        ui.set_click_through(state["click"])

    def quit_app() -> None:
        pipeline.stopped.set()
        worker.stopped.set()
        try:
            trans_q.put_nowait(None)
        except queue.Full:
            pass
        ui._quit()

    commands.update({"pause": toggle_pause, "click_through": toggle_click, "quit": quit_app})

    hotkeys = winapi.HotkeyListener(
        {"pause": cfg.hotkey_pause, "click_through": cfg.hotkey_click, "quit": cfg.hotkey_quit},
        on_fire=lambda action: ui.post(("cmd", action)),
    )
    hotkeys.start()
    hotkeys.wait_ready()
    for action, reason in hotkeys.failed:
        print(f"热键注册失败 [{action}]: {reason}")
        # 也要显示在窗口上。只在 stdout 说的话，悬浮窗一开就看不见了，
        # 用户只会觉得"这个热键按了没反应"。
        ui.post(("note", f"[热键失效] {action}: {reason}。可在 config.json 里换一组"))

    labels = {"pause": "暂停/继续", "click_through": "鼠标穿透", "quit": "退出"}
    spec_of = {"pause": cfg.hotkey_pause, "click_through": cfg.hotkey_click, "quit": cfg.hotkey_quit}
    hotkey_text = " | ".join(f"{spec_of[a]} {labels[a]}" for a in labels)
    print(f"热键: {hotkey_text}")
    print("悬浮窗也可以右键出菜单。Ctrl+C 复制全部，关掉窗口即退出。")

    pipeline.start()
    worker.start()
    ui.post(("status", f"监听中 · {capture.backend} · 等待画面变化"))
    if warning:
        ui.post(("note", f"[窗口被遮挡] {warning}"))
    elif cfg.show_source:
        ui.post(("note", f"热键 {hotkey_text}"))

    try:
        ui.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stopped.set()
        worker.stopped.set()
        hotkeys.stop()
        translator.save_cache()
        print("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
