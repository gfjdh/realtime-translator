"""回归测试：目标窗口被完全遮挡时，各后端分别是什么结果。

这是「按窗口抓取」存在的唯一理由，所以要有一条能自动跑的断言：
  wgc   —— 必须照常识别出游戏文本，而且不能报遮挡警告（那对它是误报）
  dxcam —— 必须报警告，并且识别不出东西（它抓的就是遮挡物）

遮挡用本进程起的全屏纯色窗口造成，不碰用户桌面上的任何窗口。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("d:/dev/python/translator")
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PY = str(ROOT / ".venv/Scripts/python.exe")
EXPECT = "ancient relic"


def run_once(backend: str) -> tuple[str, int]:
    r = subprocess.run(
        [PY, "-u", str(ROOT / "main.py"), "-w", "FakeGameDialogue",
         "--once", "--backend", backend],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    return r.stdout + r.stderr, r.returncode


def main() -> int:
    game = subprocess.Popen([PY, str(ROOT / ".scratch/fake_game.py")], cwd=str(ROOT))
    cover = None
    failures = []
    try:
        time.sleep(3.0)

        import tkinter as tk

        cover = tk.Tk()
        cover.title("OcclusionTestCover")
        cover.configure(bg="#ff00ff")
        cover.attributes("-topmost", True)
        cover.state("zoomed")
        tk.Label(cover, text="COVER", bg="#ff00ff", fg="#000000",
                 font=("Arial", 40)).pack(expand=True)
        cover.update()
        time.sleep(1.5)

        for backend, should_read, should_warn in [
            ("wgc", True, False),
            ("dxcam", False, True),
        ]:
            out, code = run_once(backend)
            got_text = EXPECT in out
            got_warn = "警告:" in out
            ok = (got_text == should_read) and (got_warn == should_warn)
            print(f"\n{'=' * 70}")
            print(f"backend={backend}  期望: 读到={should_read} 警告={should_warn}")
            print(f"实际: 读到={got_text} 警告={got_warn}  退出码={code}  "
                  f"→ {'通过' if ok else '失败'}")
            print(f"{'=' * 70}")
            print(out.strip()[:900])
            if not ok:
                failures.append(backend)
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

    print(f"\n{'=' * 70}")
    if failures:
        print(f"失败的后端: {failures}")
        return 1
    print("全部通过：wgc 在遮挡下正常识别，dxcam 如实报警")
    return 0


if __name__ == "__main__":
    sys.exit(main())
