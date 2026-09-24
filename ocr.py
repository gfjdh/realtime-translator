"""RapidOCR 封装：识别窗口文字并按阅读顺序拼成一段文本。

默认参数是实测调出来的。rapidocr 自带的 Det.limit_type 是 "min"，
意味着 limit_side_len 作用在**短边**上：一张 720x150 的对话条会被放大到
约 3500x736 再送进检测模型，单帧要 630ms。改成 "max" 后限制的是长边，
同样一张图实测 92ms，识别结果完全一致 —— 快了近 7 倍。

cls 模型是用来判断文本有没有旋转 180° 的。游戏对话永远是横排，
关掉它省一次推理。
"""

from __future__ import annotations

import statistics

import numpy as np

# 这些全角标点在游戏文本里很常见，直接丢掉可惜，先归一化成半角再交给翻译
_PUNCT_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


class OcrError(RuntimeError):
    pass


class Ocr:
    def __init__(
        self,
        min_score: float = 0.5,
        min_chars: int = 2,
        det_side_len: int = 1280,
        use_cls: bool = False,
    ):
        try:
            from rapidocr import RapidOCR
        except ImportError as e:
            raise OcrError(
                "没有装 rapidocr。请先跑 setup.bat，或用 "
                "pip install -r requirements.txt"
            ) from e
        try:
            self._engine = RapidOCR(
                params={
                    "Global.use_cls": use_cls,      # 游戏文本不会倒过来
                    "Global.log_level": "warning",  # 默认每次调用都刷 INFO，太吵
                    "Det.limit_type": "max",        # 见文件头注释，这是最大的性能开关
                    "Det.limit_side_len": det_side_len,
                }
            )
        except Exception as e:
            raise OcrError(f"RapidOCR 初始化失败: {e}") from e
        self.min_score = min_score
        self.min_chars = min_chars
        self.last_elapse = 0.0

    def read(self, image: np.ndarray) -> str:
        """image 必须是 BGR 的 ndarray（OpenCV 约定）。返回拼好的多行文本。"""
        if image is None or image.size == 0:
            return ""
        try:
            res = self._engine(image)
        except Exception as e:
            raise OcrError(f"OCR 推理失败: {e}") from e
        self.last_elapse = float(getattr(res, "elapse", 0.0) or 0.0)

        txts = getattr(res, "txts", None)
        if not txts:
            return ""
        boxes = getattr(res, "boxes", None)
        scores = getattr(res, "scores", None) or [1.0] * len(txts)

        items = []
        for i, raw in enumerate(txts):
            score = float(scores[i]) if i < len(scores) else 1.0
            if score < self.min_score:
                continue
            text = clean(raw)
            if _alpha_count(text) < self.min_chars:
                continue
            top, bottom, left = _box_bounds(boxes, i)
            items.append({"top": top, "bottom": bottom, "left": left, "text": text})
        if not items:
            return ""
        return _join_reading_order(items)


def _box_bounds(boxes, i: int) -> tuple[float, float, float]:
    """取出第 i 个框的上边、下边、左边。形状是 (N, 4, 2)。"""
    if boxes is None:
        return float(i * 1000), float(i * 1000), 0.0
    try:
        pts = np.asarray(boxes[i], dtype=np.float32)
        ys, xs = pts[:, 1], pts[:, 0]
        return float(ys.min()), float(ys.max()), float(xs.min())
    except (IndexError, TypeError, ValueError):
        return float(i * 1000), float(i * 1000), 0.0


def _join_reading_order(items: list[dict]) -> str:
    """把识别框按"先上下、再左右"排成文本行。

    单纯按 y 排序会把同一行的几段文字拆散，所以先按纵向重叠聚成行，
    再在行内按 x 排序。行高阈值取中位字高的 0.6 倍。
    """
    heights = sorted(it["bottom"] - it["top"] for it in items)
    median_h = heights[len(heights) // 2] if heights else 10.0
    tol = max(6.0, median_h * 0.6)

    items = sorted(items, key=lambda it: (it["top"], it["left"]))
    lines: list[list[dict]] = [[items[0]]]
    for it in items[1:]:
        ref = lines[-1][0]["top"]
        if abs(it["top"] - ref) <= tol:
            lines[-1].append(it)
        else:
            lines.append([it])

    out = []
    for line in lines:
        line.sort(key=lambda it: it["left"])
        text = " ".join(it["text"] for it in line).strip()
        # 游戏 UI 常把同一句话重复渲染两遍（描边/阴影层），去掉完全重复的行
        if text and (not out or out[-1] != text):
            out.append(text)
    return "\n".join(out)


def clean(text: str) -> str:
    """归一化标点 + 丢掉非 ASCII 的识别噪声。"""
    if not text:
        return ""
    buf = []
    for ch in text:
        ch = _PUNCT_MAP.get(ch, ch)
        if ch.isascii() and (ch.isprintable() or ch == " "):
            buf.append(ch)
        else:
            buf.append(" ")
    return " ".join("".join(buf).split())


def _alpha_count(text: str) -> int:
    return sum(1 for c in text if c.isalnum())
