"""翻译：多个免费端点组成降级链 + 内存/磁盘双层缓存。

端点的顺序和取舍全是实测出来的，不是拍脑袋（见 .scratch/bench_translate.py
和 .scratch/probe_mt_limits.py）：

  Google   质量最好也最快，但本机四个端点全被挡（429/403）。仍然放第一顺位 ——
           哪天网络通了它立刻就是最好的；失败一次 _preferred 就会让位，之后
           每句话不会再白试它。
  腾讯     实测 8/8 稳定、6000 字符仍可用、换行原样保留、~250ms。国内直连，
           不需要 key 也没有额度提示，所以它是事实上主力。
  有道     最快（~140ms）质量也可以，但限流很凶：8 连发只成功 2 次，而且失败
           时返回的是 HTTP 200 + errorCode=411，光看状态码会被骗成成功。
           放链尾兜底。
  MyMemory 稳定但要额度（匿名 5000 字符/天，带邮箱 50000）。腾讯和有道在前面
           扛着，它的额度通常根本不会动 —— 这正是把它排在后面的原因。

额度耗尽和临时故障必须分开：quotaFinished 是「今天不会再有了」，用一个闩锁
记住并跳过，免得每句话都白等一次往返；429 之类的临时故障不能锁，下一句照试。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class TranslateError(RuntimeError):
    pass


class QuotaExhausted(TranslateError):
    """今日免费额度已用尽。和临时故障不同 —— 这个当天不会恢复，要跳过。"""


def _request(req: urllib.request.Request, timeout: float) -> str:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            raise TranslateError(f"HTTP {e.code}（被限流或拒绝）") from e
        raise TranslateError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise TranslateError(f"网络不可达: {e.reason}") from e
    except TimeoutError as e:
        raise TranslateError("请求超时") from e


def _http_get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    return _request(req, timeout)


def _http_post(url: str, data: bytes, timeout: float) -> str:
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json"},
        method="POST",
    )
    return _request(req, timeout)


def _zh_variant(code: str, simplified: str, traditional: str) -> str:
    """把 zh-CN / zh-TW / zh-Hant 之类归一成某个端点自己的写法。"""
    c = code.lower()
    if c.startswith("zh"):
        return traditional if ("hant" in c or "tw" in c or "hk" in c) else simplified
    return c.split("-")[0]


class GoogleProvider:
    name = "Google"
    max_chunk = 1500

    def __init__(self, source: str, target: str, timeout: float):
        self.source, self.target, self.timeout = source, target, timeout

    def translate(self, text: str) -> str:
        q = urllib.parse.urlencode(
            {"client": "gtx", "sl": self.source, "tl": self.target, "dt": "t", "q": text}
        )
        body = _http_get("https://translate.googleapis.com/translate_a/single?" + q, self.timeout)
        try:
            data = json.loads(body)
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
        except (ValueError, TypeError, IndexError, KeyError) as e:
            raise TranslateError("Google 响应格式异常") from e


class TencentProvider:
    """腾讯交互翻译的公开接口。国内直连、不需要 key、实测最稳的一个。"""

    name = "腾讯"
    # 实测 6000 字符仍可用，但 6000 要 ~1.9s。我们的文本最长不到 400 字符，
    # 2000 足够覆盖，还留了余量
    max_chunk = 2000

    def __init__(self, source: str, target: str, timeout: float):
        self.source, self.target, self.timeout = source, target, timeout

    def translate(self, text: str) -> str:
        payload = json.dumps({
            "header": {"fn": "auto_translation", "client_key": "browser-chrome-120.0.0"},
            "type": "plain",
            "model_category": "normal",
            "source": {"lang": _zh_variant(self.source, "zh", "zh-CHT"), "text_list": [text]},
            "target": {"lang": _zh_variant(self.target, "zh", "zh-CHT")},
        }).encode("utf-8")
        body = _http_post("https://transmart.qq.com/api/imt", payload, self.timeout)
        try:
            data = json.loads(body)
        except ValueError as e:
            raise TranslateError("腾讯响应不是 JSON") from e

        out = data.get("auto_translation") or []
        if not out or not out[0]:
            # 出错时没有 auto_translation，只有 header 里的错误码
            header = json.dumps(data.get("header") or {}, ensure_ascii=False)
            raise TranslateError(f"腾讯未返回译文: {header[:120]}")
        return out[0]


class YoudaoProvider:
    """有道移动版的公开接口。最快，但限流很凶 —— 只配当链尾兜底。

    坑：errorCode 是**字符串** "0"，写 `!= 0` 会把成功当失败；而限流时返回的是
    HTTP 200 + errorCode=411，光看状态码会当成成功。两样都得解析 body 才知道。
    """

    name = "有道"
    # 实测 1200 字符触发 errorCode=103（文本过长），800 成功过，取 400 保守
    max_chunk = 400

    def __init__(self, source: str, target: str, timeout: float):
        self.source, self.target, self.timeout = source, target, timeout

    def translate(self, text: str) -> str:
        q = urllib.parse.urlencode({
            "q": text,
            "from": _zh_variant(self.source, "zh-CHS", "zh-CHT"),
            "to": _zh_variant(self.target, "zh-CHS", "zh-CHT"),
        })
        body = _http_get("https://aidemo.youdao.com/trans?" + q, self.timeout)
        try:
            data = json.loads(body)
        except ValueError as e:
            raise TranslateError("有道响应不是 JSON") from e

        try:
            code = int(data.get("errorCode") or 0)
        except (TypeError, ValueError):
            code = 0
        if code != 0:
            hint = "（请求过频）" if code == 411 else ""
            raise TranslateError(f"有道 errorCode={code}{hint}")

        return "".join(data.get("translation") or [])


class MyMemoryProvider:
    name = "MyMemory"
    max_chunk = 480  # 实测 500 是硬上限，超了返回 responseStatus 403

    def __init__(self, source: str, target: str, timeout: float, email: str = ""):
        self.source, self.target, self.timeout = source, target, timeout
        self.email = email.strip()

    def translate(self, text: str) -> str:
        params = {"q": text, "langpair": f"{self.source}|{self.target}"}
        # 带 de（邮箱）额度从 5000 字符/天提到 50000，官方明说不需要注册或验证
        if self.email:
            params["de"] = self.email
        q = urllib.parse.urlencode(params)
        body = _http_get("https://api.mymemory.translated.net/get?" + q, self.timeout)
        try:
            data = json.loads(body)
        except ValueError as e:
            raise TranslateError("MyMemory 响应不是 JSON") from e

        # 注意：出错时 HTTP 状态码仍是 200，错误只体现在 JSON 里
        status = data.get("responseStatus")
        try:
            status = int(status) if status is not None else 0
        except (TypeError, ValueError):
            status = 0

        # quotaFinished 是官方给的额度信号，比自己猜译文内容可靠
        if data.get("quotaFinished"):
            raise QuotaExhausted("MyMemory 今日免费额度已用尽")

        if status != 200:
            detail = data.get("responseDetails") or f"responseStatus={status}"
            if "QUOTA" in str(detail).upper() or "USED ALL" in str(detail).upper():
                raise QuotaExhausted(f"MyMemory: {detail}")
            raise TranslateError(f"MyMemory: {detail}")

        out = (data.get("responseData") or {}).get("translatedText") or ""
        if not out:
            raise TranslateError("MyMemory 返回空译文")
        # 老版本接口把额度提示塞在译文里，留一道保险
        if "MYMEMORY WARNING" in out.upper():
            raise QuotaExhausted("MyMemory 今日免费额度已用尽")
        return out


class Translator:
    """带缓存和降级链的翻译器。只在一个线程里调用。"""

    def __init__(
        self,
        source: str = "en",
        target: str = "zh-CN",
        cache_path: Optional[Path] = None,
        timeout: float = 8.0,
        max_cache: int = 5000,
        email: str = "",
    ):
        self.source, self.target = source, target
        self.timeout = timeout
        self.max_cache = max_cache
        self.last_provider = ""
        self._lock = threading.Lock()
        self._mem: dict[str, str] = {}
        self._dirty = 0
        self._cache_path = Path(cache_path) if cache_path else None

        self._providers = [
            GoogleProvider(source, target, timeout),
            TencentProvider(source, target, timeout),
            YoudaoProvider(source, target, timeout),
            MyMemoryProvider(source, target, timeout, email=email),
        ]
        # 今日额度用尽的端点，记住后当天不再试
        self._exhausted: set[int] = set()
        self._preferred = 0
        self._load_cache()

    # --- 缓存 ---

    def _load_cache(self) -> None:
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._mem = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            pass  # 缓存坏了不值得报错，当作没有就行

    def save_cache(self) -> None:
        if not self._cache_path or not self._dirty:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._mem) > self.max_cache:
                # dict 保持插入顺序，砍掉最旧的那批
                drop = len(self._mem) - self.max_cache
                for k in list(self._mem)[:drop]:
                    del self._mem[k]
            self._cache_path.write_text(
                json.dumps(self._mem, ensure_ascii=False), encoding="utf-8"
            )
            self._dirty = 0
        except OSError:
            pass

    def _remember(self, key: str, value: str) -> None:
        with self._lock:
            self._mem[key] = value
            self._dirty += 1
        if self._dirty >= 20:
            self.save_cache()

    # --- 翻译 ---

    def translate(self, text: str) -> tuple[str, bool]:
        """返回 (译文, 是否命中缓存)。失败抛 TranslateError。"""
        key = normalize(text)
        if not key:
            return "", True
        with self._lock:
            hit = self._mem.get(key)
        if hit is not None:
            return hit, True

        errors = []
        order = [self._preferred] + [i for i in range(len(self._providers)) if i != self._preferred]
        for idx in order:
            provider = self._providers[idx]
            if idx in self._exhausted:
                errors.append(f"{provider.name}: 今日额度已用尽，跳过")
                continue
            try:
                out = self._via(provider, text)
            except QuotaExhausted as e:
                # 锁上，之后当天不再碰它 —— 不锁的话每句话都要白等一次往返
                self._exhausted.add(idx)
                errors.append(f"{provider.name}: {e}")
                continue
            except TranslateError as e:
                errors.append(f"{provider.name}: {e}")
                continue
            if out.strip():
                self._preferred = idx  # 下次先试这个
                self.last_provider = provider.name
                self._remember(key, out)
                return out, False
            errors.append(f"{provider.name}: 返回空译文")

        if len(self._exhausted) == len(self._providers):
            raise QuotaExhausted("所有翻译端点的今日额度都已用尽，明天再试，或换个网络")
        raise TranslateError("；".join(errors) if errors else "没有可用的翻译端点")

    def _via(self, provider, text: str) -> str:
        parts = []
        for chunk in chunk_text(text, provider.max_chunk):
            parts.append(provider.translate(chunk))
        return "\n".join(p for p in parts if p)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]


def chunk_text(text: str, limit: int) -> list[str]:
    """按行切块，保证每块不超过 limit 字符；超长单行再按句子切。"""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        for piece in _split_long(line, limit):
            piece = piece.strip()
            if not piece:
                continue
            if not cur:
                cur = piece
            elif len(cur) + 1 + len(piece) <= limit:
                cur += "\n" + piece
            else:
                chunks.append(cur)
                cur = piece
    if cur:
        chunks.append(cur)
    return chunks


def _split_long(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    out, cur = [], ""
    for token in _sentences(line):
        if len(token) > limit:  # 单句仍然超长，只能硬切
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(token), limit):
                out.append(token[i : i + limit])
            continue
        if len(cur) + 1 + len(token) <= limit:
            cur += (" " if cur else "") + token
        else:
            out.append(cur)
            cur = token
    if cur:
        out.append(cur)
    return out


def _sentences(line: str) -> list[str]:
    out, cur = [], ""
    for ch in line:
        cur += ch
        if ch in ".!?;":
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def normalize(text: str) -> str:
    """用于去重和缓存的键：忽略大小写、标点和空白差异。"""
    return " ".join("".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text).split())
