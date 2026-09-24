"""抓帧。三个后端，按「能不能拿到真内容」排序：

  wgc    Windows Graphics Capture，按窗口句柄抓窗口自己的内容。窗口被遮挡、
         被拖到屏幕外都无所谓，也不需要游戏在前台 —— 默认走这条。
  dxcam  DXGI Desktop Duplication，抓合成后的桌面画面。窗口被盖住就会抓到
         遮挡物的内容，这是它最大的坑（会明确报警）。
  mss    GDI BitBlt，同上，最后兜底。

前两个后端都有「没有新帧就返回 None」的能力，这正是我们要的变化检测，
不用自己算帧差，静止的对话框不会白白跑 OCR。mss 没有这个能力，用下采样
指纹自己算。wgc 虽然只在画面变化时才回调，但它照样会在背景动画时一直出帧，
所以也用指纹再筛一道，让三个后端的行为保持一致。
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

import winapi


class CaptureError(RuntimeError):
    pass


class WindowCapture:
    """抓取某个窗口客户区（可只抓其中一块）的连续帧。

    WGC 后端另有自己的抓帧线程，但 grab() 只在单个线程里调用 —— mss 实例
    不是线程安全的，dxcam 相机也有内部状态。
    """

    def __init__(
        self,
        hwnd: int,
        title: str = "",
        region: Optional[tuple[float, float, float, float]] = None,
        backend: str = "auto",
        output_idx: Optional[int] = None,
        device_idx: int = 0,
        change_threshold: float = 1.0,
        fps: float = 6.0,
    ):
        self.hwnd = hwnd
        self.title = title
        self.region = region
        self.change_threshold = change_threshold
        self._cam = None
        self._sct = None
        self._wgc = None
        self._wgc_control = None
        self._prev_print: Optional[np.ndarray] = None
        self._rect: Optional[tuple[int, int, int, int]] = None
        self.backend_note = ""

        self.monitor = winapi.monitor_rect_of_window(hwnd)

        order = {
            "auto": ["wgc", "dxcam", "mss"],
            "wgc": ["wgc"],
            "dxcam": ["dxcam"],
            "mss": ["mss"],
        }[backend]
        problems = []
        for name in order:
            try:
                if name == "wgc":
                    self._init_wgc(fps)
                elif name == "dxcam":
                    self._init_dxcam(output_idx, device_idx)
                else:
                    self._init_mss()
            except Exception as e:
                if backend == name:  # 明确点名要它，失败了就不许悄悄换
                    raise CaptureError(f"{name} 初始化失败: {e}") from e
                problems.append(f"{name} 不可用（{e}）")
                continue
            self.backend = name
            break
        else:
            raise CaptureError("没有可用的抓帧后端：" + "；".join(problems))
        if problems:
            self.backend_note = "；".join(problems) + f"，已改用 {self.backend}"

    # --- 后端初始化 ---

    def _init_dxcam(self, output_idx: Optional[int], device_idx: int) -> None:
        import dxcam

        if output_idx is None:
            # 让 dxcam 自己按显示器序号对齐：EnumDisplayMonitors 的顺序与
            # DXGI 输出顺序在单显卡多屏下是一致的。多显卡时需要 --output 指定。
            rects = winapi.list_monitors()
            try:
                output_idx = rects.index(self.monitor)
            except ValueError:
                output_idx = 0
        # output_color="BGR" 是因为 rapidocr 按 OpenCV 约定吃 BGR
        cam = dxcam.create(
            device_idx=device_idx,
            output_idx=output_idx,
            output_color="BGR",
            backend="dxgi",
        )
        if cam is None:
            raise CaptureError("dxcam.create 返回 None")
        self._cam = cam
        self.output_idx = output_idx

    def _init_mss(self) -> None:
        import mss

        self._sct = mss.mss()

    def _init_wgc(self, fps: float) -> None:
        """按窗口句柄建一路 Graphics Capture。窗口被遮挡也照样拿得到真内容。

        库是回调式的（它自己开线程，画面一变就回调），而这边是 grab() 拉取式的，
        中间用一个「最新一帧」的槽位对接。
        """
        from windows_capture import WindowsCapture

        self._wgc_lock = threading.Lock()
        self._wgc_frame: Optional[np.ndarray] = None
        self._wgc_error: Optional[str] = None
        self._wgc_closed = False

        # minimum_update_interval 能从源头把出帧率压到我们的 fps，省掉多余的
        # GPU→CPU 拷贝。但它是 Win11 才有的能力 —— 在 Win10 19045 上实测会抛
        # "Setting a minimum update interval is not supported"，所以先试带节流
        # 的，不行再去掉。Win10 上因此只剩回调里那道指纹过滤在控频。
        interval = int(1000 / fps) if fps and fps > 0 else None
        attempts = ([{"minimum_update_interval": interval}] if interval else []) + [{}]

        last: Optional[Exception] = None
        for extra in attempts:
            try:
                cap = WindowsCapture(cursor_capture=False, window_hwnd=self.hwnd, **extra)
                cap.frame_handler = self._on_wgc_frame
                cap.closed_handler = self._on_wgc_closed
                control = cap.start_free_threaded()
            except Exception as e:
                last = e
                continue
            self._wgc, self._wgc_control = cap, control
            return
        raise last if last is not None else RuntimeError("WindowsCapture 启动失败")

    def _on_wgc_frame(self, frame, _control) -> None:
        """跑在抓帧线程上。

        frame.frame_buffer 是映射纹理上的零拷贝视图，回调一返回就失效 ——
        裁剪和拷贝必须在这里一次做完，不能把视图存出去。
        """
        try:
            bgra = frame.frame_buffer
            fh, fw = bgra.shape[:2]
            # WGC 给的是 DWM 的「可见边框」，不是客户区，要减掉两者的偏移；
            # 不然 --region 的比例会整体偏掉一个标题栏的高度。
            fl, ft, _, _ = winapi.dwm_frame_rect(self.hwnd)
            cl, ct, cr, cb = winapi.client_rect_on_screen(self.hwnd)
            x0, y0 = max(0, cl - fl), max(0, ct - ft)
            cw, ch = min(cr - cl, fw - x0), min(cb - ct, fh - y0)
            if cw <= 0 or ch <= 0:
                return
            rx0, ry0, rx1, ry1 = self._region_box(cw, ch)
            box = bgra[y0 + ry0:y0 + ry1, x0 + rx0:x0 + rx1, :3]
            with self._wgc_lock:
                self._wgc_frame = np.ascontiguousarray(box)
            self._rect = (fl + x0 + rx0, ft + y0 + ry0, fl + x0 + rx1, ft + y0 + ry1)
        except Exception as e:  # 回调里抛出去会掀掉整个会话，得咽在这里
            self._wgc_error = f"WGC 抓帧出错: {e}"

    def _on_wgc_closed(self) -> None:
        self._wgc_closed = True

    # --- 抓帧 ---

    def _region_box(self, w: int, h: int) -> tuple[int, int, int, int]:
        """把 region 比例换算成 (x0, y0, x1, y1) 像素框。比例的唯一出处。"""
        if not self.region:
            return 0, 0, w, h
        rx0, ry0, rx1, ry1 = self.region
        return int(w * rx0), int(h * ry0), int(w * rx1), int(h * ry1)

    def _crop_rect(self) -> Optional[tuple[int, int, int, int]]:
        """客户区（按比例裁剪后）与所在显示器的交集，物理像素坐标。"""
        try:
            l, t, r, b = winapi.client_rect_on_screen(self.hwnd)
        except OSError:
            return None
        x0, y0, x1, y1 = self._region_box(r - l, b - t)
        l, t, r, b = l + x0, t + y0, l + x1, t + y1

        # dxcam/mss 的 region 不能越过输出边界，窗口跨屏或被拖到屏幕外时必须裁掉
        ml, mt, mr, mb = self.monitor
        l, t = max(l, ml), max(t, mt)
        r, b = min(r, mr), min(b, mb)
        # DXGI 的 region 要求偶数宽高以外的值也能用，但留 2px 余量避免边界抖动
        if r - l < 8 or b - t < 8:
            return None
        return int(l), int(t), int(r), int(b)

    def grab(self) -> Optional[np.ndarray]:
        """返回 BGR 的 ndarray；画面没有新帧时返回 None。"""
        if self.backend == "wgc":
            return self._grab_wgc()

        rect = self._crop_rect()
        if rect is None:
            return None
        self._rect = rect

        if self.backend == "dxcam":
            return self._grab_dxcam(rect)
        return self._grab_mss(rect)

    def _grab_wgc(self) -> Optional[np.ndarray]:
        if self._wgc_error:
            raise CaptureError(self._wgc_error)
        if self._wgc_closed:
            raise CaptureError("目标窗口已关闭")

        # 取走最新的那一帧。回调一直在覆盖它，所以这里拿到的永远是当前的画面
        with self._wgc_lock:
            frame, self._wgc_frame = self._wgc_frame, None
        if frame is None:
            return None
        return frame if self._changed(frame) else None

    def _changed(self, frame: np.ndarray) -> bool:
        """和上次送出去的那帧比，变了才为 True。会顺手更新指纹。"""
        fp = fingerprint(frame)
        if self._prev_print is not None and fp.shape == self._prev_print.shape:
            if float(np.abs(fp - self._prev_print).mean()) < self.change_threshold:
                return False
        self._prev_print = fp
        return True

    def _grab_dxcam(self, rect) -> Optional[np.ndarray]:
        # new_frame_only 默认为 True：没有新帧就返回 None，省掉一次 OCR
        frame = self._cam.grab(region=rect, copy=True, new_frame_only=True)
        if frame is None:
            return None
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise CaptureError(f"dxcam 返回了意外的形状: {frame.shape}")
        return frame

    def _grab_mss(self, rect) -> Optional[np.ndarray]:
        l, t, r, b = rect
        raw = self._sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        arr = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        frame = np.ascontiguousarray(arr[:, :, :3])  # BGRA -> BGR

        # mss 没有“有没有新帧”的概念，只能自己按下采样指纹判断画面是否变了
        return frame if self._changed(frame) else None

    def current_rect(self) -> Optional[tuple[int, int, int, int]]:
        return self._rect

    def release(self) -> None:
        """释放抓帧资源。调用后这个对象不能再用了。

        注意 dxcam 的 duplicator.release() 会重复释放同一个 COM 指针，抛出
        "access violation" —— 这是上游的 bug（已实测定位到 release 内部），
        不影响功能。main.py 会装一个 unraisablehook 把这条例外静音掉。
        """
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                cam.release()
            except Exception:
                pass
            del cam
        control, self._wgc_control = self._wgc_control, None
        self._wgc = None
        if control is not None:
            try:
                control.stop()
                control.wait()
            except Exception:
                pass
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


def fingerprint(frame: np.ndarray) -> np.ndarray:
    """把一帧压成 16x32 的块均值，用来做廉价的画面变化判断。

    直接比较原始像素对压缩噪声太敏感，块均值能滤掉抖动又不至于漏掉文字变化。
    """
    h, w = frame.shape[:2]
    ys, xs = max(1, h // 16), max(1, w // 32)
    hh, ww = (h // ys) * ys, (w // xs) * xs
    small = frame[:hh, :ww].reshape(hh // ys, ys, ww // xs, xs, -1).mean(axis=(1, 3))
    return small.astype(np.float32)
