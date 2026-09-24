"""验证「按窗口抓取」这条路，以及它和现有后端的坐标能不能对上。

两个问题：
  1. 窗口被完全遮挡时，WGC（Graphics Capture，按 HWND）还能不能拿到真内容？
  2. WGC 的帧和 dxcam 抓的客户区是不是同一块画面？—— 是的话 --region 的
     比例在各个后端之间含义一致，用户之前校准好的选区不用重调。

遮挡用本进程起的全屏纯色窗口造成，不碰用户桌面上的任何窗口。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path("d:/dev/python/translator")
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import winapi
from PIL import Image

PY = str(ROOT / ".venv/Scripts/python.exe")
OUT = ROOT / ".scratch"


def wgc(hwnd: int, timeout: float = 6.0) -> np.ndarray | None:
    """用 Graphics Capture API 按 HWND 抓一帧，返回 BGRA。"""
    from windows_capture import WindowsCapture

    holder: dict = {}
    done = threading.Event()
    # draw_border 必须留空：开关捕获边框是 Win11 才有的能力，
    # 在 Win10 上传 False 会直接报 "not supported by the Graphics Capture API"
    cap = WindowsCapture(cursor_capture=False, window_hwnd=hwnd)

    @cap.event
    def on_frame_arrived(frame, control):
        # frame_buffer 是映射纹理上的零拷贝视图，回调返回后就失效，必须拷出来
        holder["bgra"] = np.ascontiguousarray(frame.frame_buffer)
        done.set()
        control.stop()

    @cap.event
    def on_closed():
        done.set()

    control = cap.start_free_threaded()
    done.wait(timeout)
    try:
        control.stop()
        control.wait()
    except Exception:
        pass
    return holder.get("bgra")


def crop_to_client(bgra: np.ndarray, hwnd: int) -> np.ndarray:
    """把 WGC 帧从「可见边框」换算并裁剪到「客户区」，BGRA -> BGR。"""
    fl, ft, _, _ = winapi.dwm_frame_rect(hwnd)
    cl, ct, cr, cb = winapi.client_rect_on_screen(hwnd)
    x0, y0 = cl - fl, ct - ft
    box = bgra[y0:y0 + (cb - ct), x0:x0 + (cr - cl)]
    return np.ascontiguousarray(box[:, :, :3])


def show(name: str, img: np.ndarray | None, ocr) -> None:
    if img is None:
        print(f"  {name:<22} 拿不到画面")
        return
    h, w = img.shape[:2]
    b, g, r = img.reshape(-1, 3).mean(axis=0)
    print(f"  {name:<22} {w}x{h}  平均RGB=({r:.0f},{g:.0f},{b:.0f})")
    Image.fromarray(img[:, :, ::-1]).save(OUT / f"probe_{name.replace(' ', '_')}.png")
    try:
        text = ocr.read(img)
    except Exception as e:
        text = f"<OCR 失败: {e}>"
    print(f"  {'':<22} OCR: {text!r}")


def main() -> int:
    winapi.set_dpi_aware()
    from ocr import Ocr
    from capture import WindowCapture
    from main import _grab_with_retry

    game = subprocess.Popen([PY, str(ROOT / ".scratch/fake_game.py")], cwd=str(ROOT))
    cover = None
    try:
        time.sleep(3.0)
        matches = winapi.match_windows("FakeGameDialogue")
        if not matches:
            print("没找到假游戏窗口")
            return 1
        hwnd = matches[0].hwnd
        cl, ct, cr, cb = winapi.client_rect_on_screen(hwnd)
        wl, wt_, wr, wb = winapi.window_rect(hwnd)
        fl, ft, fr, fb = winapi.dwm_frame_rect(hwnd)
        print(f"假游戏 hwnd={hwnd}")
        print(f"  窗口矩形 {wr - wl}x{wb - wt_}   客户区 {cr - cl}x{cb - ct}   "
              f"DWM 可见边框 {fr - fl}x{fb - ft}")

        ocr = Ocr()

        # --- 1. 没遮挡时：WGC 裁剪到客户区 vs dxcam 抓客户区，应当是同一块画面 ---
        bgra = wgc(hwnd)
        if bgra is None:
            print("WGC 拿不到帧，后面的对比没意义")
            return 1
        print(f"\n[未遮挡] WGC 原始帧 {bgra.shape[1]}x{bgra.shape[0]}"
              f"（DWM 可见边框是 {fr - fl}x{fb - ft}，"
              f"{'吻合' if (bgra.shape[1], bgra.shape[0]) == (fr - fl, fb - ft) else '不吻合'}）")
        wgc_client = crop_to_client(bgra, hwnd)

        cap = WindowCapture(hwnd, title="FakeGameDialogue", backend="dxcam")
        dx = _grab_with_retry(cap, tries=10)
        show("WGC 裁到客户区", wgc_client, ocr)
        show("dxcam 客户区", dx, ocr)
        if dx is not None and dx.shape == wgc_client.shape:
            diff = float(np.abs(dx.astype(np.int16) - wgc_client.astype(np.int16)).mean())
            print(f"  {'':<22} 与 dxcam 逐像素平均差 {diff:.2f} / 255"
                  f"（{'同一块画面' if diff < 6 else '对不上！'}）")
        else:
            print(f"  {'':<22} 形状不一致: WGC {wgc_client.shape} vs dxcam "
                  f"{None if dx is None else dx.shape}")

        # --- 2. 盖住它，再看各路径拿到什么 ---
        import tkinter as tk

        cover = tk.Tk()
        cover.title("ProbeCover")
        cover.configure(bg="#ff00ff")
        cover.attributes("-topmost", True)
        cover.state("zoomed")
        tk.Label(cover, text="COVER WINDOW — 这是遮挡物", bg="#ff00ff",
                 fg="#000000", font=("Arial", 40)).pack(expand=True)
        cover.update()
        time.sleep(1.5)

        pt = winapi.POINT((wl + wr) // 2, (wt_ + wb) // 2)
        top = int(ctypes.windll.user32.WindowFromPoint(pt) or 0)
        root = int(ctypes.windll.user32.GetAncestor(wt.HWND(top), winapi.GA_ROOT) or top)
        print(f"\n[已遮挡] 中心点最顶层 hwnd={root}，目标 hwnd={hwnd}"
              f" → {'确实盖住了' if root != hwnd else '没盖住，探测无效'}")
        show("WGC 裁到客户区", crop_to_client(wgc(hwnd), hwnd), ocr)
        show("dxcam 客户区", _grab_with_retry(cap, tries=10), ocr)
        cap.release()
    finally:
        if cover is not None:
            try:
                cover.destroy()
            except Exception:
                pass
        game.terminate()
        try:
            game.wait(timeout=5)
        except subprocess.TimeoutExpired:
            game.kill()
        print("\n已清理（假游戏和遮挡窗口都关掉了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
