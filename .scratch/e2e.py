"""端到端验证：起一个假游戏窗口，让 main.py 去抓它、识别、翻译。

这个脚本自己管理子进程，任何退出路径都会把假窗口杀掉，不留孤儿进程。
"""
import subprocess
import sys
import time
from pathlib import Path

# 控制台是 GBK，窗口标题里可能有零宽字符之类打不出来的东西
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path("d:/dev/python/translator")
PY = str(ROOT / ".venv/Scripts/python.exe")


def run(args, title, timeout=240):
    print(f"\n{'=' * 70}\n$ main.py {' '.join(args)}\n{'=' * 70}")
    r = subprocess.run(
        [PY, str(ROOT / "main.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    print(r.stdout.strip())
    if r.stderr.strip():
        print("--- stderr ---")
        print(r.stderr.strip()[:3000])
    print(f"[退出码 {r.returncode}]")
    return r.returncode


game = subprocess.Popen([PY, str(ROOT / ".scratch/fake_game.py")], cwd=str(ROOT))
try:
    time.sleep(3.0)  # 等窗口画出来并稳定

    run(["--list"], "列窗口")
    run(["-w", "FakeGame", "--snapshot", ".scratch/crop_full.png"], "抓整个客户区")
    run(["-w", "FakeGame", "--region", "0.02,0.05,0.98,0.75",
         "--snapshot", ".scratch/crop_region.png"], "抓指定区域")
    run(["-w", "FakeGame", "--once"], "单次 OCR + 翻译")

    # GUI 模式：跑一段时间看会不会崩（tkinter + 后台线程 + 热键线程一起跑）
    # 输出重定向到文件 + 子进程加 -u，否则 terminate() 会连同缓冲区一起丢掉
    print(f"\n{'=' * 70}\n$ main.py -w FakeGame  （GUI 模式，跑 15 秒）\n{'=' * 70}")
    log_path = ROOT / ".scratch/gui.log"
    with open(log_path, "w", encoding="utf-8") as log:
        gui = subprocess.Popen(
            [PY, "-u", str(ROOT / "main.py"), "-w", "FakeGame", "--debug"], cwd=str(ROOT),
            stdout=log, stderr=subprocess.STDOUT,
        )
        time.sleep(15)
        alive = gui.poll() is None
        if not alive:
            print(f"GUI 提前退出，退出码 {gui.returncode}")
        gui.terminate()
        try:
            gui.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gui.kill()
    print(log_path.read_text(encoding="utf-8").strip())
    print(f"[GUI {'存活 15 秒未崩溃' if alive else '崩了'}]")
finally:
    game.terminate()
    try:
        game.wait(timeout=5)
    except subprocess.TimeoutExpired:
        game.kill()
    print("\n假游戏窗口已关闭")
