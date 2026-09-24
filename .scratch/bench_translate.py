"""探哪些免费翻译端点在当前网络真的可用。

这是选端点的唯一依据 —— 不要凭猜。只有测出 [OK] 的才配写进 translate.py：
降级链里每多挂一个死端点，每句话就多等一次注定失败的请求。

分三类测：
  1. Google 官方端点 —— 本机已知全挂，留着当对照，哪天通了要能立刻看出来
  2. Google 前端镜像（Lingva / Mozhi）—— 用镜像自己的 IP 去调 Google，
     能绕开对 translate.googleapis.com 的封锁
  3. 自建模型的公共实例（LibreTranslate）和国内可直连的（有道 / 腾讯 / 百度 / Bing）
"""
import json
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TEXT = "Where is the ancient relic? I must find it before the sun sets."


def get(url, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def post(url, body, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def form(url, fields, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    h.update(headers or {})
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


# --- Google 官方 ---

def google_single(client):
    q = urllib.parse.urlencode(
        {"client": client, "sl": "en", "tl": "zh-CN", "dt": "t", "q": TEXT})
    return get("https://translate.googleapis.com/translate_a/single?" + q)


def _google_parse(body):
    data = json.loads(body)
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def g_gtx():
    return _google_parse(google_single("gtx"))


def g_dict_chrome():
    q = urllib.parse.urlencode(
        {"client": "dict-chrome-ex", "sl": "en", "tl": "zh-CN", "q": TEXT})
    return _google_parse(get("https://clients5.google.com/translate_a/t?" + q))


def g_webapp():
    return _google_parse(google_single("webapp"))


# --- Google 前端镜像 ---

def lingva(host):
    def call():
        url = f"https://{host}/api/v1/en/zh/{urllib.parse.quote(TEXT)}"
        return json.loads(get(url))["translation"]
    return call


def mozhi(host):
    def call():
        q = urllib.parse.urlencode(
            {"engine": "google", "from": "en", "to": "zh", "text": TEXT})
        return json.loads(get(f"https://{host}/api/translate?" + q))["translated-text"]
    return call


# --- LibreTranslate 公共实例（自建 Argos 模型，不依赖 Google）---

def libretranslate(host, key=""):
    def call():
        payload = json.dumps(
            {"q": TEXT, "source": "en", "target": "zh", "format": "text", "api_key": key}
        ).encode("utf-8")
        return json.loads(post(f"https://{host}/translate", payload))["translatedText"]
    return call


# --- 国内可直连 ---

def youdao():
    q = urllib.parse.urlencode({"doctype": "json", "type": "AUTO", "i": TEXT})
    data = json.loads(get("https://fanyi.youdao.com/translate?" + q))
    if data.get("errorCode") != 0:
        raise RuntimeError(f"errorCode={data.get('errorCode')}")
    return "".join(row["tgt"] for row in data["translateResult"][0])


def tencent():
    payload = json.dumps({
        "header": {"fn": "auto_translation", "client_key": "browser-chrome-120.0.0"},
        "type": "plain", "model_category": "normal",
        "source": {"lang": "en", "text_list": [TEXT]},
        "target": {"lang": "zh"},
    }).encode("utf-8")
    data = json.loads(post("https://transmart.qq.com/api/imt", payload))
    out = data.get("auto_translation") or []
    if not out:
        raise RuntimeError(str(data.get("header", {}))[:120])
    return out[0]


def baidu():
    return json.loads(form("https://fanyi.baidu.com/transapi",
                           {"from": "en", "to": "zh", "query": TEXT}))["data"][0]["dst"]


def bing():
    """Bing 要先用页面里的 IG/IID 换一个临时 token，两步。"""
    page = get("https://cn.bing.com/translator")
    import re
    ig = re.search(r'IG:"([^"]+)"', page)
    iid = re.search(r'data-iid="([^"]+)"', page)
    if not ig or not iid:
        raise RuntimeError("拿不到 IG/IID")
    body = urllib.parse.urlencode(
        {"fromLang": "en", "text": TEXT, "to": "zh-Hans"}).encode()
    url = f"https://cn.bing.com/ttranslatev3?isVertical=1&&IG={ig.group(1)}&IID={iid.group(1)}"
    h = {"Content-Type": "application/x-www-form-urlencoded",
         "Referer": "https://cn.bing.com/translator"}
    return json.loads(post(url, body, h))[0]["translations"][0]["text"]


TESTS = [
    ("google gtx", g_gtx),
    ("google dict-chrome-ex", g_dict_chrome),
    ("google webapp", g_webapp),
    ("lingva.ml", lingva("lingva.ml")),
    ("lingva.thedaviddelta.com", lingva("lingva.thedaviddelta.com")),
    ("lingva.garudalinux.org", lingva("lingva.garudalinux.org")),
    ("translate.plausibility.cloud", lingva("translate.plausibility.cloud")),
    ("mozhi.aryak.me", mozhi("mozhi.aryak.me")),
    ("translate.projectsegfau.lt", mozhi("translate.projectsegfau.lt")),
    ("libretranslate.de", libretranslate("libretranslate.de")),
    ("trans.zillyhuhn.com", libretranslate("trans.zillyhuhn.com")),
    ("lt.vern.cc", libretranslate("lt.vern.cc")),
    ("translate.fedilab.app", libretranslate("translate.fedilab.app")),
    ("有道 fanyi.youdao.com", youdao),
    ("腾讯 transmart.qq.com", tencent),
    ("百度 fanyi.baidu.com", baidu),
    ("Bing cn.bing.com", bing),
]


def main() -> int:
    ok = []
    for name, fn in TESTS:
        try:
            t0 = time.perf_counter()
            out = fn()
            dt = (time.perf_counter() - t0) * 1000
            print(f"[OK] {name}  {dt:.0f}ms")
            print(f"     {out[:120]}")
            ok.append((name, dt))
        except Exception as e:
            detail = str(e)[:110].replace("\n", " ")
            print(f"[!!] {name}: {type(e).__name__} {detail}")
        print()

    print("=" * 66)
    print(f"可用 {len(ok)}/{len(TESTS)}：")
    for name, dt in ok:
        print(f"  {name}  {dt:.0f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
