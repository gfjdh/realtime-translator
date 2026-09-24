"""回归测试：翻译降级链、额度闩锁、各端点的参数拼装。

不需要联网的部分（闩锁、URL、语言码、错误码解析）用假 provider 和假的 _http_get
测，必须确定性通过；真实端点只做一次端到端确认，失败不算红 —— 免费端点随时可能
挂，那是网络的事，不是代码的事。
"""
import sys
from pathlib import Path

ROOT = Path("d:/dev/python/translator")
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import translate
from translate import (
    MyMemoryProvider,
    QuotaExhausted,
    TranslateError,
    Translator,
    YoudaoProvider,
)

EMAIL = "2556123498@qq.com"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'通过' if cond else '失败'}  {name}" + ("" if cond else f"   <- {detail}"))
    if not cond:
        failures.append(name)


def stub_get(body: str):
    """把模块级 _http_get 换掉，同时把请求 URL 记下来。"""
    seen = {}

    def fake(url, timeout):
        seen["url"] = url
        return body

    return fake, seen


class Fake:
    def __init__(self, name: str, mode: str):
        self.name = name
        self.mode = mode
        self.calls = 0
        self.max_chunk = 1000

    def translate(self, text: str) -> str:
        self.calls += 1
        if self.mode == "fail":
            raise TranslateError("模拟故障")
        if self.mode == "quota":
            raise QuotaExhausted("模拟额度用尽")
        return f"[{self.name}]{text}"


orig_get = translate._http_get

print("=== MyMemory：de 参数 ===")
fake, seen = stub_get('{"responseStatus":200,"responseData":{"translatedText":"你好"}}')
translate._http_get = fake
try:
    out = MyMemoryProvider("en", "zh-CN", 8.0, email=EMAIL).translate("hello")
    check("带邮箱时请求里出现 de", "de=2556123498%40qq.com" in seen["url"], seen["url"])
    check("译文正常返回", out == "你好", out)

    MyMemoryProvider("en", "zh-CN", 8.0).translate("hello")
    check("不给邮箱时不带 de", "de=" not in seen["url"], seen["url"])
finally:
    translate._http_get = orig_get

print("=== MyMemory：quotaFinished 闩锁信号 ===")
fake, _ = stub_get('{"quotaFinished":true,"responseStatus":403,"responseDetails":"QUOTA"}')
translate._http_get = fake
try:
    MyMemoryProvider("en", "zh-CN", 8.0).translate("hello")
    check("quotaFinished=true 抛 QuotaExhausted", False, "没抛")
except QuotaExhausted:
    check("quotaFinished=true 抛 QuotaExhausted", True)
except TranslateError as e:
    check("quotaFinished=true 抛 QuotaExhausted", False, f"抛的是 {type(e).__name__}")
finally:
    translate._http_get = orig_get

print('=== 有道：errorCode 是字符串 "0" 的坑 ===')
fake, _ = stub_get('{"errorCode":"0","translation":["你好"]}')
translate._http_get = fake
try:
    out = YoudaoProvider("en", "zh-CN", 8.0).translate("hi")
    check('errorCode="0" 判为成功', out == "你好", out)
finally:
    translate._http_get = orig_get

fake, _ = stub_get('{"errorCode":"411","translation":[]}')
translate._http_get = fake
try:
    YoudaoProvider("en", "zh-CN", 8.0).translate("hi")
    check("errorCode=411 判为失败", False, "没抛")
except TranslateError as e:
    check("errorCode=411 判为失败", True)
    check("  错误里点明了是限流", "411" in str(e), str(e))
finally:
    translate._http_get = orig_get

print("=== 语言码映射 ===")
v = translate._zh_variant
check("zh-CN -> zh", v("zh-CN", "zh", "zh-CHT") == "zh")
check("zh-CN -> zh-CHS", v("zh-CN", "zh-CHS", "zh-CHT") == "zh-CHS")
check("zh-TW -> 繁体", v("zh-TW", "zh", "zh-CHT") == "zh-CHT")
check("zh-Hant -> 繁体", v("zh-Hant", "zh", "zh-CHT") == "zh-CHT")
check("en 原样", v("en", "zh", "zh-CHT") == "en")

print("=== 降级链与额度闩锁 ===")
t = Translator(cache_path=None)
a, b, c = Fake("A", "fail"), Fake("B", "quota"), Fake("C", "ok")
t._providers = [a, b, c]
t._exhausted, t._preferred = set(), 0

out, _ = t.translate("first sentence")
check("前两个挂了也能降级到第三个", out == "[C]first sentence", out)
check("额度端点在链尾被标记", t._exhausted == {1}, str(t._exhausted))
check("故障端点没被误标记", 0 not in t._exhausted and 2 not in t._exhausted)

out2, _ = t.translate("second sentence")
check("第二次跳过额度端点", b.calls == 1, f"被调用了 {b.calls} 次")
check("第二次仍走可用端点", out2 == "[C]second sentence", out2)
check("成功的端点被记住优先", t._preferred == 2, str(t._preferred))

print("=== 全部额度用尽 ===")
t2 = Translator(cache_path=None)
t2._providers = [Fake("A", "quota"), Fake("B", "quota")]
t2._exhausted, t2._preferred = set(), 0
try:
    t2.translate("anything")
    check("全用尽时抛 QuotaExhausted", False, "没抛")
except QuotaExhausted as e:
    check("全用尽时抛 QuotaExhausted", True)
    check("  提示里说了明天再试", "明天" in str(e), str(e))
except TranslateError as e:
    check("全用尽时抛 QuotaExhausted", False, f"抛的是 {type(e).__name__}: {e}")

print("=== 配置 → 命令行 → Translator 的接线 ===")
import main as main_mod
from config import Config

args = main_mod.build_parser().parse_args(["--mt-email", "other@example.com"])
check("--mt-email 能被解析", args.mt_email == "other@example.com", repr(args.mt_email))

cfg = Config()
cfg.apply_args(args)
check("命令行覆盖配置", cfg.mt_email == "other@example.com", repr(cfg.mt_email))

t3 = Translator(cache_path=None, email=cfg.mt_email)
check("Translator 把邮箱交给 MyMemory", t3._providers[-1].email == "other@example.com")
check("端点链顺序", [p.name for p in t3._providers] == ["Google", "腾讯", "有道", "MyMemory"],
      str([p.name for p in t3._providers]))
print(f"  （config.json 当前存的是: {Config.load().mt_email!r}）")

print("=== 真实端点（失败不算红，免费端点随时可能挂）===")
live = Translator(cache_path=None, email=EMAIL)
try:
    out, cached = live.translate(
        "Where is the ancient relic? I must find it before the sun sets."
    )
    print(f"  端点链: {' → '.join(live.provider_names)}")
    print(f"  实际命中: {live.last_provider}")
    print(f"  译文: {out}")
except TranslateError as e:
    print(f"  全部失败: {e}")

print()
if failures:
    print(f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("全部通过")
