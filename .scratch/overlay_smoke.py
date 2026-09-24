"""验证悬浮窗的显示逻辑：不跑 mainloop，手动投递消息并直接断言 Text 内容。"""
import sys

sys.path.insert(0, "d:/dev/python/translator")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import winapi
from config import Config
from overlay import Overlay

winapi.set_dpi_aware()
cfg = Config()
ui = Overlay(cfg, {"pause": lambda: None, "click_through": lambda: None, "quit": lambda: None})

ui.post(("result", ("Where is the relic?", "遗迹在哪里？", False)))
ui.post(("result", ("Press E.", "按 E。", True)))
ui.post(("status", "翻译 120ms · MyMemory · 20 字符"))
ui.post(("note", "等待识别到英文文本…"))
ui.post(("error", "翻译失败: 测试用的错误"))
ui._drain()

content = ui.text.get("1.0", "end-1c")
print("--- 窗口文本内容 ---")
print(content)
print("--- 状态栏 ---")
print("状态:", ui.state_label.cget("text"), "| 底部:", ui.status.cget("text"))

assert "遗迹在哪里？" in content, "译文没进窗口"
assert "Where is the relic?" in content, "原文没进窗口"
assert "按 E。" in content, "第二句译文没进窗口"
assert "翻译失败" in content, "错误消息没进窗口"

# 关掉原文，再投一条：不该出现英文
ui.var_source.set(False)
ui._toggle_source()
ui.post(("result", ("Second line.", "第二行。", False)))
ui._drain()
content2 = ui.text.get("1.0", "end-1c")
assert "Second line." not in content2, "关掉原文后仍在显示英文"
assert "第二行。" in content2, "关掉原文后译文也丢了"
print("\n[通过] 原文开关生效")

# 鼠标穿透：直接查扩展样式位
ui.set_click_through(True)
hwnd = winapi.root_hwnd(ui.root.winfo_id())
assert winapi._get_exstyle(hwnd) & winapi.WS_EX_TRANSPARENT, "穿透位没设上"
ui.set_click_through(False)
assert not (winapi._get_exstyle(hwnd) & winapi.WS_EX_TRANSPARENT), "穿透位没去掉"
print("[通过] 鼠标穿透开关改的是真实的扩展样式位")

# 长跑裁剪：防止挂机几小时后 Text 撑爆
for i in range(400):
    ui.post(("result", (f"line {i}", f"第 {i} 行", False)))
ui._drain()
lines = int(ui.text.index("end-1c").split(".")[0])
print(f"[{'通过' if lines < 900 else '失败'}] 400 条后行数 {lines}（应 < 900）")
assert lines < 900, f"裁剪没生效，行数 {lines}"

ui.root.destroy()
print("\n全部断言通过")
