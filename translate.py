"""翻译：多个免费端点组成降级链 + 内存/磁盘双层缓存。

为什么是一条链而不是写死一个端点：Google 的免费端点（translate_a/single）
质量最好也最快，但它在不少网络环境里会直接返回 429/403 —— 实测本机就是
如此，四个 Google 端点全部被挡。MyMemory 免费、不需要 key，本机可用，
但单次查询限 500 字符。

所以顺序是：先试上次成功的那个（省掉一次注定失败的请求），失败就沿链降级。
游戏里同一句台词会反复出现，缓存能同时省掉流量和几百毫秒延迟。
"""

from __future__ import annotations

import json
import threading
import time
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


def _http_get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
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


class MyMemoryProvider:
    name = "MyMemory"
    max_chunk = 480  # 实测 500 是硬上限，超了返回 responseStatus 403

    def __init__(self, source: str, target: str, timeout: float):
        self.source, self.target, self.timeout = source, target, timeout

    def translate(self, text: str) -> str:
        q = urllib.parse.urlencode(
            {"q": text, "langpair": f"{self.source}|{self.target}"}
        )
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
        if status != 200:
            detail = data.get("responseDetails") or f"responseStatus={status}"
            raise TranslateError(f"MyMemory: {detail}")

        out = (data.get("responseData") or {}).get("translatedText") or ""
        if not out:
            raise TranslateError("MyMemory 返回空译文")
        if "MYMEMORY WARNING" in out.upper():
            raise TranslateError("MyMemory 今日免费额度已用尽")
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
    ):
        self.source, self.target = source, target
        self.timeout = timeout
        self.max_cache = max_cache
        self.last_provider = ""
        self._lock = threading.Lock()
        self._mem: dict[str, str] = {}
        self._dirty = 0
        self._cache_path = Path(cache_path) if cache_path else None

        self._providers = [GoogleProvider(source, target, timeout), MyMemoryProvider(source, target, timeout)]
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
            try:
                out = self._via(provider, text)
            except TranslateError as e:
                errors.append(f"{provider.name}: {e}")
                continue
            if out.strip():
                self._preferred = idx  # 下次先试这个
                self.last_provider = provider.name
                self._remember(key, out)
                return out, False
            errors.append(f"{provider.name}: 返回空译文")

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
