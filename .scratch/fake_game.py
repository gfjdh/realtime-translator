"""冒充游戏窗口：深色背景 + 白色英文对话，尺寸和配色都贴近真实游戏对话框。"""
import tkinter as tk

root = tk.Tk()
root.title("FakeGameDialogue")
root.geometry("900x300+80+80")
root.configure(bg="#12141a")
# 必须置顶：Desktop Duplication 抓的是合成后的桌面画面，
# 窗口一旦被别的程序盖住，抓到的就是遮挡物的内容
root.attributes("-topmost", True)
root.lift()

c = tk.Canvas(root, width=900, height=300, bg="#12141a", highlightthickness=0)
c.pack()
c.create_text(30, 60, anchor="w", fill="#f0f0f0", font=("Arial", 26),
              text="Where is the ancient relic? I must find it")
c.create_text(30, 120, anchor="w", fill="#f0f0f0", font=("Arial", 26),
              text="before the sun sets, or all is lost forever.")
c.create_text(30, 190, anchor="w", fill="#c8c8c8", font=("Arial", 20),
              text="Press E to continue.")

root.mainloop()
