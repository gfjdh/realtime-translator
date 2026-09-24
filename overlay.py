"""置顶悬浮窗：无边框、可拖动、可缩放、可选鼠标穿透。

tkinter 只能在主线程里碰，所以后台的抓帧/翻译线程一律通过 post() 往队列里
丢消息，由主线程的 after() 轮询搬到界面上。
"""

from __future__ import annotations

import queue
import sys
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

import winapi

BG = "#12141a"
BAR = "#1b1f28"
TEXT_BG = "#0e1015"
FG = "#e8eaf0"
SRC_FG = "#7f8a9e"
DIM = "#667085"
ACCENT = "#6cc4ff"
WARN = "#ffb37c"
ERR = "#ff8080"

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


class Overlay:
    def __init__(self, cfg, commands: dict[str, Callable[[], None]]):
        self.cfg = cfg
        self.commands = commands
        self.q: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("游戏翻译")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # alpha 一定要先设：tk 会因此把窗口标成 WS_EX_LAYERED，
        # 之后我们再加 WS_EX_TRANSPARENT 才不会让窗口消失
        self.root.attributes("-alpha", cfg.alpha)
        self.root.configure(bg=BG)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = int(cfg.width), int(cfg.height)
        x = max(0, sw - w - 48)
        y = max(0, sh - h - 160)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self._click_through = False
        self._at_bottom = True
        self._build()

        self.root.after(80, self._drain)
        self.root.after(2000, self._keep_topmost)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    # --- 界面搭建 ---

    def _pick_font(self) -> str:
        families = set(tkfont.families())
        for name in (UI_FONT, "微软雅黑", "SimHei", "Segoe UI"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _build(self) -> None:
        self.ui_font = self._pick_font()

        # 顶栏：拖动 + 状态 + 关闭
        bar = tk.Frame(self.root, bg=BAR, height=26)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        self.dot = tk.Label(bar, text="●", bg=BAR, fg=DIM, font=(self.ui_font, 9))
        self.dot.pack(side="left", padx=(8, 4))
        tk.Label(bar, text="游戏翻译", bg=BAR, fg=FG, font=(self.ui_font, 9)).pack(side="left")
        self.state_label = tk.Label(bar, text="监听中", bg=BAR, fg=ACCENT, font=(self.ui_font, 9))
        self.state_label.pack(side="left", padx=10)

        close = tk.Label(bar, text="✕", bg=BAR, fg=DIM, font=(self.ui_font, 10), cursor="hand2")
        close.pack(side="right", padx=8)
        close.bind("<Button-1>", lambda _e: self._quit())

        for widget in (bar, self.dot, self.state_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        # 正文
        body = tk.Frame(self.root, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        self.text = tk.Text(
            body,
            bg=TEXT_BG,
            fg=FG,
            wrap="word",
            relief="flat",
            highlightthickness=0,
            padx=10,
            pady=8,
            spacing1=1,
            spacing3=4,
            insertbackground=FG,
            font=(self.ui_font, self.cfg.font_size),
        )
        self.text.pack(side="left", fill="both", expand=True)

        scroll = tk.Scrollbar(body, command=self.text.yview, width=10)
        scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=self._on_scroll, state="disabled", cursor="arrow")

        self.text.tag_configure("src", foreground=SRC_FG, font=(self.ui_font, max(8, self.cfg.font_size - 1)))
        self.text.tag_configure("dst", foreground=FG)
        self.text.tag_configure("err", foreground=ERR)
        self.text.tag_configure("note", foreground=WARN, font=(self.ui_font, 9))

        # 底栏：状态 + 右下角缩放手柄
        bottom = tk.Frame(self.root, bg=BAR, height=20)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        self.status = tk.Label(
            bottom, text="启动中…", bg=BAR, fg=DIM, font=(self.ui_font, 8), anchor="w"
        )
        self.status.pack(side="left", fill="x", expand=True, padx=8)

        grip = tk.Label(bottom, text="◢", bg=BAR, fg=DIM, font=(self.ui_font, 8), cursor="size_nw_se")
        grip.pack(side="right", padx=2)
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

        # 右键菜单
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="暂停 / 继续", command=lambda: self.commands["pause"]())
        self.menu.add_command(label="清空", command=self.clear)
        self.menu.add_separator()
        self.var_source = tk.BooleanVar(value=self.cfg.show_source)
        self.menu.add_checkbutton(
            label="显示英文原文", variable=self.var_source, command=self._toggle_source
        )
        self.menu.add_command(label="鼠标穿透", command=lambda: self.commands["click_through"]())
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self._quit)

        for widget in (self.text, bar, bottom):
            widget.bind("<Button-3>", self._popup)
        self.text.bind("<MouseWheel>", lambda _e: self.root.after(30, self._check_bottom))

        self.root.bind("<Control-c>", lambda _e: self.copy_all())

    # --- 交互 ---

    def _popup(self, event) -> None:
        if self._click_through:
            return
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _resize_start(self, event) -> None:
        self._resize = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, event) -> None:
        x0, y0, w0, h0 = self._resize
        w = max(280, w0 + event.x_root - x0)
        h = max(120, h0 + event.y_root - y0)
        self.root.geometry(f"{w}x{h}")

    def _on_scroll(self, first, last) -> None:
        self.scrollbar_set = (first, last)
        self._at_bottom = float(last) >= 0.999

    def _check_bottom(self) -> None:
        self._at_bottom = self.text.yview()[1] >= 0.999

    def _keep_topmost(self) -> None:
        # 有些游戏/全屏程序会把置顶抢走，定期重新抢回来
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(2000, self._keep_topmost)

    def _toggle_source(self) -> None:
        self.cfg.show_source = bool(self.var_source.get())

    def copy_all(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.text.get("1.0", "end-1c"))
        except tk.TclError:
            pass

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def set_click_through(self, enabled: bool) -> None:
        self._click_through = enabled
        try:
            winapi.set_click_through(winapi.root_hwnd(self.root.winfo_id()), enabled)
        except Exception:
            pass
        if enabled:
            self.state_label.configure(text="穿透中（热键可关）", fg=WARN)
        else:
            self.state_label.configure(text="监听中", fg=ACCENT)

    def set_state(self, text: str, color: str = ACCENT) -> None:
        self.state_label.configure(text=text, fg=color)
        self.dot.configure(fg=color)

    # --- 供后台线程投递 ---

    def post(self, message: tuple) -> None:
        """message 形如 ("result", (原文, 译文, 是否缓存)) / ("status", "…")。

        参数是整条消息而不是 (kind, payload) 两段，调用点写起来更像是在发事件。
        """
        self.q.put(message)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _handle(self, kind: str, payload) -> None:
        if kind == "result":
            source, target, cached = payload
            self._append(source, target, cached)
        elif kind == "status":
            self.status.configure(text=payload)
        elif kind == "state":
            text, color = payload
            self.set_state(text, color)
        elif kind == "note":
            self._append_raw(payload + "\n", ("note",))
        elif kind == "error":
            self._append_raw(f"[错误] {payload}\n", ("err",))
        elif kind == "cmd":
            fn = self.commands.get(payload)
            if fn:
                fn()
        else:
            # 消息格式写错时一定要吵出来。静默忽略的后果是整个悬浮窗一片空白，
            # 而且不报任何错 —— 这正是之前踩过的坑。
            print(f"[overlay] 无法识别的消息: {kind!r} {payload!r}", file=sys.stderr)

    def _append(self, source: str, target: str, cached: bool) -> None:
        self.text.configure(state="normal")
        if self.cfg.show_source and source:
            self.text.insert("end", source + "\n", ("src",))
        suffix = "  ·缓存" if cached else ""
        self.text.insert("end", target + "\n", ("dst",))
        if suffix:
            self.text.insert("end", suffix.lstrip() + "\n", ("note",))
        self.text.insert("end", "\n", ())
        self._trim()
        self.text.configure(state="disabled")
        if self._at_bottom:
            self.text.see("end")

    def _append_raw(self, s: str, tags=()) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", s, tags)
        self._trim()
        self.text.configure(state="disabled")
        if self._at_bottom:
            self.text.see("end")

    def _trim(self) -> None:
        """只保留最近约 600 行，防止长时间挂机后 Text 越来越大越来越卡。"""
        try:
            lines = int(self.text.index("end-1c").split(".")[0])
        except (ValueError, tk.TclError):
            return
        if lines > 900:
            self.text.delete("1.0", f"{lines - 600}.0")

    # --- 生命周期 ---

    def _quit(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
