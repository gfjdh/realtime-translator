"""探哪些免费翻译端点在当前网络真的可用。"""
import json
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TEXT = "Where is the ancient relic? I must find it before the sun sets."


def get(url, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read().decode("utf-8", "replace")


def google_single(client):
    q = urllib.parse.urlencode({"client": client, "sl": "en", "tl": "zh-CN", "dt": "t", "q": TEXT})
    return "https://translate.googleapis.com/translate_a/single?" + q


TESTS = [
    ("google gtx", lambda: google_single("gtx")),
    ("google dict-chrome-ex", lambda: "https://clients5.google.com/translate_a/t?" + urllib.parse.urlencode(
        {"client": "dict-chrome-ex", "sl": "en", "tl": "zh-CN", "q": TEXT})),
    ("google webapp", lambda: google_single("webapp")),
    ("google m 页面", lambda: "https://translate.google.com/m?" + urllib.parse.urlencode(
        {"sl": "en", "tl": "zh-CN", "q": TEXT})),
    ("mymemory", lambda: "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": TEXT, "langpair": "en|zh-CN"})),
]

for name, build in TESTS:
    try:
        t0 = time.perf_counter()
        status, body = get(build())
        dt = (time.perf_counter() - t0) * 1000
        print(f"[OK] {name}: HTTP {status}  {dt:.0f}ms  {len(body)} 字节")
        print("     ", body[:220].replace("\n", " "))
    except Exception as e:
        print(f"[!!] {name}: {type(e).__name__} {e}")
    print()
