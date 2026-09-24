"""测 OCR 稳态耗时，并验证 det 的 limit_type 猜测。

同时测翻译端点通不通。
"""
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "d:/dev/python/translator")
from rapidocr import RapidOCR

img = Image.new("RGB", (720, 150), (18, 20, 26))
d = ImageDraw.Draw(img)
font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 26)
d.text((20, 20), "Where is the ancient relic? I must find", font=font, fill=(240, 240, 240))
d.text((20, 62), "it before the sun sets, or all is lost.", font=font, fill=(240, 240, 240))
d.text((20, 104), "Press E to continue.", font=font, fill=(200, 200, 200))
bgr = np.asarray(img)[:, :, ::-1].copy()
TRUTH = 3

CONFIGS = {
    "默认 (det limit_type=min, 736)": {},
    "det max/1280 + 关cls": {"Global.use_cls": False, "Det.limit_type": "max", "Det.limit_side_len": 1280},
    "det max/960 + 关cls": {"Global.use_cls": False, "Det.limit_type": "max", "Det.limit_side_len": 960},
}

for name, params in CONFIGS.items():
    eng = RapidOCR(params=params) if params else RapidOCR()
    eng(bgr)  # 预热：第一次调用包含 onnx 图优化，不能计入
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        res = eng(bgr)
        times.append((time.perf_counter() - t0) * 1000)
    hit = len(res.txts or ())
    print(f"{name}: 稳态 {min(times):.0f}ms (中位 {sorted(times)[2]:.0f}ms) 识别 {hit}/{TRUTH} 行")

print("\n=== 翻译端点 ===")
q = urllib.parse.urlencode({
    "client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t",
    "q": "Where is the ancient relic? I must find it before the sun sets.",
})
req = urllib.request.Request(
    "https://translate.googleapis.com/translate_a/single?" + q,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
)
try:
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=8) as r:
        import json
        data = json.loads(r.read().decode("utf-8"))
    print(f"耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
    print("译文:", "".join(seg[0] for seg in data[0] if seg and seg[0]))
except Exception as e:
    print("翻译失败:", type(e).__name__, e)
