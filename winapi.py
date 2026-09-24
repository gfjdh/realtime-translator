"""Windows 原生 API 的 ctypes 封装。

只包这个工具真正用到的部分：DPI 感知、窗口枚举与客户区坐标、显示器定位、
鼠标穿透、全局热键。刻意不依赖 pywin32 —— 这些调用总共不到 200 行，
不值得为它多装一个十几 MB 的包。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable, NamedTuple, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- 常量 ---
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


# 64 位下 GetWindowLongPtrW 的默认返回类型是 c_int，会把扩展样式截断，
# 必须显式声明 restype。32 位 Windows 上没有这个符号，退回 GetWindowLongW。
_HAS_LONG_PTR = hasattr(user32, "GetWindowLongPtrW")
if _HAS_LONG_PTR:
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
else:
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]

user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(ctypes.c_ulong)]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [POINT]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wt.HWND, ctypes.c_ulong]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint]
user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint, wt.WPARAM, wt.LPARAM]

_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
_MONITOR_PROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), wt.LPARAM
)


def set_dpi_aware() -> None:
    """必须在任何窗口/抓屏调用之前执行。

    不做这一步，GetClientRect 返回的是被系统缩放过的逻辑坐标，而 dxcam 返回
    物理像素 —— 两者对不上，抓到的区域会整体偏移，在高 DPI 笔记本上尤其明显。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _get_exstyle(hwnd: int) -> int:
    h = wt.HWND(hwnd)
    if _HAS_LONG_PTR:
        return int(user32.GetWindowLongPtrW(h, GWL_EXSTYLE))
    return int(user32.GetWindowLongW(h, GWL_EXSTYLE))


def _set_exstyle(hwnd: int, style: int) -> None:
    h = wt.HWND(hwnd)
    if _HAS_LONG_PTR:
        user32.SetWindowLongPtrW(h, GWL_EXSTYLE, style)
    else:
        user32.SetWindowLongW(h, GWL_EXSTYLE, style)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        raise OSError(f"GetWindowRect 失败 (hwnd={hwnd})")
    return r.left, r.top, r.right, r.bottom


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    """客户区在屏幕坐标系里的 (left, top, right, bottom)，物理像素。

    用客户区而不是整个窗口，是为了把标题栏和边框排除在抓取范围之外。
    """
    r = RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r)):
        raise OSError(f"GetClientRect 失败 (hwnd={hwnd})")
    pt = POINT(0, 0)
    if not user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt)):
        raise OSError(f"ClientToScreen 失败 (hwnd={hwnd})")
    return pt.x, pt.y, pt.x + r.right, pt.y + r.bottom


DWMWA_EXTENDED_FRAME_BOUNDS = 9


def dwm_frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口可见边框在屏幕坐标下的矩形（不含阴影，也不含不可见的拖拽边框）。

    Graphics Capture 按窗口抓取时，拿到的正是这个范围而不是客户区 —— 实测
    Tk 窗口客户区 1125x375、窗口 1145x424，而 WGC 给的是 1127x415。所以
    WGC 帧必须减去这个矩形的左上角、再按客户区裁一刀，--region 的比例
    才能和 dxcam/mss 保持同一套含义。
    """
    r = RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    )
    if hr != 0:
        raise OSError(f"DwmGetWindowAttribute 失败 (hwnd={hwnd}, hr={hr})")
    return r.left, r.top, r.right, r.bottom


def list_monitors() -> list[tuple[int, int, int, int]]:
    """按 EnumDisplayMonitors 的顺序返回各显示器矩形。"""
    found: list[tuple[int, int, int, int]] = []

    def cb(hmon, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, _MONITOR_PROC(cb), 0)
    return found


def monitor_rect_of_window(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在的显示器矩形（物理像素）。"""
    hmon = user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    target = int(hmon) if hmon else 0
    rects = list_monitors()
    if not rects:
        raise OSError("没有检测到任何显示器")
    # 用窗口中心点落进哪个显示器来判断，比直接比 hmon 句柄更稳
    l, t, r, b = window_rect(hwnd)
    cx, cy = (l + r) // 2, (t + b) // 2
    for rect in rects:
        if rect[0] <= cx < rect[2] and rect[1] <= cy < rect[3]:
            return rect
    return rects[0]


class WindowInfo(NamedTuple):
    hwnd: int
    title: str
    pid: int
    exe: str

    @property
    def exe_name(self) -> str:
        return self.exe.rsplit("\\", 1)[-1] if self.exe else ""

    def __str__(self) -> str:
        name = self.exe_name
        return f"{self.title}  [{name}]" if name else self.title


def process_exe_name(pid: int) -> str:
    """进程可执行文件的完整路径。拿不到就返回空串。

    按 exe 名字找窗口比按标题靠谱得多 —— 玩家记得住 exe 名字，
    而窗口标题可能带一堆后缀、会变、甚至整个是空的。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows() -> list[WindowInfo]:
    """所有可见的、有标题的顶层窗口，已排除本进程自己的窗口。"""
    out: list[WindowInfo] = []
    own_pid = kernel32.GetCurrentProcessId()

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        if _get_exstyle(hwnd) & WS_EX_TOOLWINDOW:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        try:
            l, t, r, b = window_rect(hwnd)
        except OSError:
            return True
        if r - l < 120 or b - t < 80:  # 太小的一律不是游戏窗口
            return True
        out.append(WindowInfo(int(hwnd), title, int(pid.value), ""))
        return True

    user32.EnumWindows(_ENUM_PROC(cb), 0)
    # exe 名字要开进程句柄，比枚举窗口贵得多，所以只对留下来的窗口查
    return [w._replace(exe=process_exe_name(w.pid)) for w in out]


def match_windows(term: str) -> list[WindowInfo]:
    """按标题**或 exe 名字**匹配，大小写不敏感。

    匹配优先级：标题全等 > exe 全等 > 标题子串 > exe 子串。
    前者有结果就不看后者，避免输入 "steam" 时既命中标题又命中一堆 helper 进程。
    """
    s = term.strip().lower()
    if not s:
        return []
    wins = list_windows()
    for key in (
        lambda w: w.title.lower(),
        lambda w: w.exe_name.lower(),
    ):
        exact = [w for w in wins if key(w) == s]
        if exact:
            return exact
    for key in (
        lambda w: w.title.lower(),
        lambda w: w.exe_name.lower(),
    ):
        partial = [w for w in wins if s in key(w)]
        if partial:
            return partial
    return []


def covering_window(hwnd: int) -> Optional[WindowInfo]:
    """如果目标窗口被别的窗口盖住，返回盖住它的那个窗口；没被盖住返回 None。

    抓帧抓的是合成后的桌面画面，窗口一旦被盖住，OCR 读到的就是遮挡物的内容。
    这是最容易让人以为"工具坏了"的情况，所以启动时先查一次并明确报出来。

    做法是在窗口中心点问一句"这个位置最顶层是谁"，比截屏比对便宜得多。
    """
    try:
        l, t, r, b = window_rect(hwnd)
    except OSError:
        return None
    pt = POINT((l + r) // 2, (t + b) // 2)
    top = int(user32.WindowFromPoint(pt) or 0)
    if not top:
        return None
    root = int(user32.GetAncestor(wt.HWND(top), GA_ROOT) or top)
    if root == hwnd:
        return None
    for w in list_windows():
        if w.hwnd == root:
            return w
    return None


def root_hwnd(widget_id: int) -> int:
    """从 tkinter 的 winfo_id() 拿到真正的顶层窗口句柄。"""
    h = user32.GetAncestor(wt.HWND(widget_id), GA_ROOT)
    return int(h) if h else widget_id


def set_click_through(hwnd: int, enabled: bool) -> None:
    """开关鼠标穿透。

    只加/去 WS_EX_TRANSPARENT，保留 tkinter 自己设的 WS_EX_LAYERED ——
    单独给一个非层叠窗口加 LAYERED 会让它不渲染，所以绝不能碰那一位。
    """
    style = _get_exstyle(hwnd)
    style = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
    _set_exstyle(hwnd, style)


# --- 全局热键 ---

_VK_NAMED = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "home": 0x24,
    "end": 0x23,
    "insert": 0x2D,
    "delete": 0x2E,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """"ctrl+alt+t" -> (modifiers, virtual_key)。"""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("热键为空")
    mods = MOD_NOREPEAT
    vk: Optional[int] = None
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif p in _VK_NAMED:
            vk = _VK_NAMED[p]
        elif len(p) == 1 and (p.isalnum()):
            vk = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            vk = 0x70 + int(p[1:]) - 1
        else:
            raise ValueError(f"无法识别的热键片段: {p!r}")
    if vk is None:
        raise ValueError(f"热键缺少主键（如 ctrl+alt+t）: {spec!r}")
    if mods == MOD_NOREPEAT:
        raise ValueError(f"热键至少要有一个修饰键（ctrl/alt/shift/win）: {spec!r}")
    return mods, vk


class HotkeyListener(threading.Thread):
    """在独立线程里跑 RegisterHotKey + GetMessage 循环。

    Windows 要求注册热键和接收 WM_HOTKEY 必须在同一个线程，而 tkinter 的主
    循环不跑 Windows 消息泵（它有自己的事件循环），所以只能单开一个线程。
    """

    daemon = True

    def __init__(self, bindings: dict[str, str], on_fire: Callable[[str], None]):
        super().__init__(name="hotkeys")
        self._on_fire = on_fire
        self._bindings: list[tuple[int, int, int, str]] = []
        self._tid: Optional[int] = None
        self._ready = threading.Event()
        self.failed: list[tuple[str, str]] = []  # (动作名, 原因)
        for i, (action, spec) in enumerate(bindings.items(), start=1):
            if not spec:
                continue
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as e:
                self.failed.append((action, str(e)))
                continue
            self._bindings.append((i, mods, vk, action))

    def run(self) -> None:
        self._tid = int(kernel32.GetCurrentThreadId())
        registered: dict[int, str] = {}
        for hid, mods, vk, action in self._bindings:
            if user32.RegisterHotKey(None, hid, mods, vk):
                registered[hid] = action
            else:
                self.failed.append((action, "已被其它程序占用"))
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                action = registered.get(int(msg.wParam))
                if action:
                    try:
                        self._on_fire(action)
                    except Exception:
                        pass
        for hid in registered:
            user32.UnregisterHotKey(None, hid)

    def wait_ready(self, timeout: float = 3.0) -> None:
        self._ready.wait(timeout)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
