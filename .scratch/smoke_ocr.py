"""冒烟测试：验证 rapidocr 3.x 与 dxcam 0.3.0 的真实调用方式。

造一张模拟游戏对话框的图（深色底 + 白色描边文字），跑 OCR，
把 txts / scores / boxes 的形状和耗时打出来。
"""
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

print("=== dxcam ===")
try:
    import dxcam

    print("device_info:", dxcam.device_info().strip())
    print("output_info:", dxcam.output_info().strip())
except Exception as e:
    print("dxcam 失败:", type(e).__name__, e)

print("\n=== 造图 ===")
W, H = 720, 150
img = Image.new("RGB", (W, H), (18, 20, 26))
d = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 26)
except OSError:
    font = ImageFont.load_default()
d.text((20, 20), "Where is the ancient relic? I must find", font=font, fill=(240, 240, 240))
d.text((20, 62), "it before the sun sets, or all is lost.", font=font, fill=(240, 240, 240))
d.text((20, 104), "Press E to continue.", font=font, fill=(200, 200, 200))
img.save("d:/dev/python/translator/.scratch/sample.png")
bgr = np.asarray(img)[:, :, ::-1].copy()  # rapidocr 吃 BGR
print("图:", img.size, "BGR array:", bgr.shape, bgr.dtype)

print("\n=== RapidOCR 默认参数 ===")
from rapidocr import RapidOCR

t0 = time.perf_counter()
engine = RapidOCR()
print(f"初始化耗时 {time.perf_counter() - t0:.2f}s")

t0 = time.perf_counter()
res = engine(bgr)
dt = time.perf_counter() - t0
print(f"推理耗时 {dt * 1000:.0f}ms (engine 自报 {res.elapse * 1000:.0f}ms)")
print("类型:", type(res).__name__)
print("txts:", res.txts)
print("scores:", res.scores)
print("boxes 形状:", None if res.boxes is None else np.asarray(res.boxes).shape)
print("len():", len(res))

print("\n=== 关掉 cls 再测（游戏文本都是横排，cls 纯浪费） ===")
engine2 = RapidOCR(params={"Global.use_cls": False})
t0 = time.perf_counter()
res2 = engine2(bgr)
print(f"推理耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
print("txts:", res2.txts)
