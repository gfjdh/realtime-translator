"""一次性诊断脚本：把候选端点失败的原始响应打出来，判断是「真死」还是「姿势不对」。

第一轮结论（已定案，不用再试）：
  Lingva 全系      Cloudflare 的 "Just a moment..." JS 挑战，程序调不通
  百度 transapi    errno 1022，要 cookie 里的 sign/token
  libretranslate.de 返回官网 HTML，API 已迁走
  有道 fanyi.youdao.com  返回前端页面，老接口已下线 —— 但移动版 aidemo 有响应

第二轮就查这几个「姿势不对」的：语言码、响应结构。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TEXT = "Where is the ancient relic? I must find it before the sun sets."


def fetch(url, headers=None, data=None):
    h = {"User-Agent": CHROME, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


print("=== 有道移动版 aidemo：看完整结构 ===")
status, body = fetch("https://aidemo.youdao.com/trans?" + urllib.parse.urlencode(
    {"q": TEXT, "from": "en", "to": "zh-CHS"}))
print(f"HTTP {status}")
try:
    d = json.loads(body)
    print("字段:", list(d.keys()))
    for k in ("translation", "query", "errorCode", "l"):
        if k in d:
            print(f"  {k} = {str(d[k])[:150]}")
except ValueError:
    print(body[:300])
print()

print("=== Mozhi / SimplyTranslate：试各个中文语言码 ===")
for eng, param, host, path in [
    ("mozhi", "to", "mozhi.aryak.me", "/api/translate"),
    ("simplytranslate", "to", "simplytranslate.org", "/api/translate/"),
]:
    for code in ["zh-CN", "zh_HANS", "zh-Hans", "zh-CHS", "zh"]:
        url = f"https://{host}{path}?" + urllib.parse.urlencode(
            {"engine": "google", "from": "en", param: code, "text": TEXT})
        status, body = fetch(url)
        body = body.replace("\n", " ")
        mark = "OK " if status == 200 and "{" in body else "   "
        print(f"{mark}{eng:16s} {param}={code:8s} HTTP {status}  {body[:110]}")
    print()
