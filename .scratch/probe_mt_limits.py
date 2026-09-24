"""测各可用端点的限流行为和长度上限，决定它们配不配进降级链。

降级链里放一个「时不时就失败」的端点是有代价的：每句话都要多等一次注定失败的
往返。所以要看的不只是「能不能用」，还有「稳不稳」。

已知结论（保留下来免得重测）：
  腾讯   6000 字符仍可用，换行原样保留，~350-600ms
  有道   errorCode 103 = 文本过长（1500 字符触发），411 = 疑似限流，时好时坏
        另：errorCode 是字符串 "0"，比较前必须 int()
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SHORT = "Where is the ancient relic?"
FILLER = "The ancient relic must be hidden somewhere in this forgotten ruin. "


class Failed(Exception):
    pass


def youdao(text):
    q = urllib.parse.urlencode({"q": text, "from": "en", "to": "zh-CHS"})
    req = urllib.request.Request(f"https://aidemo.youdao.com/trans?{q}",
                                 headers={"User-Agent": CHROME})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise Failed(f"{type(e).__name__}") from e
    code = int(d.get("errorCode") or 0)
    if code != 0:
        raise Failed(f"errorCode={code}")
    return "".join(d.get("translation") or [])


def tencent(text):
    payload = json.dumps({
        "header": {"fn": "auto_translation", "client_key": "browser-chrome-120.0.0"},
        "type": "plain", "model_category": "normal",
        "source": {"lang": "en", "text_list": [text]},
        "target": {"lang": "zh"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://transmart.qq.com/api/imt", data=payload,
        headers={"User-Agent": CHROME, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise Failed(f"HTTP {e.code}") from e
    except Exception as e:
        raise Failed(f"{type(e).__name__}") from e
    out = d.get("auto_translation") or []
    if not out:
        raise Failed("无译文")
    return out[0]


def try_once(fn, text):
    t0 = time.perf_counter()
    try:
        out = fn(text)
        return (time.perf_counter() - t0) * 1000, out[:40].strip() or "(空)"
    except Failed as e:
        return (time.perf_counter() - t0) * 1000, f"FAIL {e}"


print("=== 连续短请求（不限速）看会不会被限 ===")
for name, fn in [("有道", youdao), ("腾讯", tencent)]:
    print(f"--- {name}")
    oks = 0
    for i in range(8):
        dt, out = try_once(fn, SHORT)
        oks += not out.startswith("FAIL")
        print(f"    #{i + 1}  {dt:6.0f}ms  {out}")
    print(f"    → {oks}/8 成功\n")

print("=== 有道：加 3 秒间隔重试同样 8 次 ===")
oks = 0
for i in range(8):
    dt, out = try_once(youdao, SHORT)
    oks += not out.startswith("FAIL")
    print(f"    #{i + 1}  {dt:6.0f}ms  {out}")
    time.sleep(3)
print(f"    → {oks}/8 成功\n")

print("=== 有道：慢慢加长，找 errorCode 103 的临界点 ===")
for n in [50, 100, 200, 300, 400, 600, 800, 1200]:
    dt, out = try_once(youdao, (FILLER * (n // len(FILLER) + 1))[:n])
    print(f"    {n:5d} 字符  {dt:6.0f}ms  {out}")
    time.sleep(3)
