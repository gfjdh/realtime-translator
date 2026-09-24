"""摸 MyMemory 的边界：长度上限、错误返回格式、多行文本行为。"""
import json
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def mm(text):
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": text, "langpair": "en|zh-CN"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


print("=== 长度上限 ===")
for n in (200, 450, 480, 500, 520, 600):
    text = ("The ancient relic lies beyond the northern gate. " * 20)[:n]
    try:
        d = mm(text)
        st = d.get("responseStatus")
        ok = d.get("responseData", {}).get("translatedText", "")
        print(f"  {n} 字符 (字节 {len(text.encode())}): status={st} 译文{len(ok)}字  {ok[:40]}")
    except Exception as e:
        print(f"  {n} 字符: {type(e).__name__} {e}")

print("\n=== 多行文本 ===")
try:
    d = mm("Where is the relic?\nI must find it.\nPress E.")
    print("  返回:", repr(d["responseData"]["translatedText"]))
except Exception as e:
    print("  失败:", type(e).__name__, e)

print("\n=== 完整响应字段（找配额/错误线索）===")
d = mm("Hello, traveler.")
print("  顶层键:", list(d.keys()))
print("  responseStatus:", d.get("responseStatus"), "| quotaFinished:", d.get("quotaFinished"))
print("  responseDetails:", str(d.get("responseDetails"))[:120])
