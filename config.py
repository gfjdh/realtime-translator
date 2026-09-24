"""运行参数：config.json 与命令行参数合并后的结果。

同一份配置同时被主流程和悬浮窗使用，所以单独放一个模块，避免
main 和 overlay 互相 import。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path(__file__).with_name("config.json")
CACHE_PATH = Path(__file__).with_name("cache") / "translations.json"


@dataclass
class Config:
    # 目标窗口标题子串（大小写不敏感）
    window: str = ""
    # [x0, y0, x1, y1]，相对客户区的比例；None 表示抓整个客户区
    region: Optional[list] = None
    # 抓帧频率上限。OCR 只在画面真的变了才跑，所以这个值不用太大
    fps: float = 6.0
    # 悬浮窗不透明度
    alpha: float = 0.92
    source_lang: str = "en"
    target_lang: str = "zh-CN"
    # MyMemory 的 de 参数。官方文档明说不需要注册或验证，填了额度从
    # 5000 字符/天提到 50000。留空就走匿名额度
    mt_email: str = ""
    show_source: bool = True
    font_size: int = 11
    width: int = 560
    height: int = 280
    # 显示器/显卡序号，None 表示自动跟随窗口所在显示器
    output_idx: Optional[int] = None
    device_idx: int = 0
    # auto / wgc / dxcam / mss。auto 按 wgc → dxcam → mss 依次尝试
    backend: str = "auto"
    # OCR 检测模型的长边上限。调小更快，调大能认更小的字
    det_side_len: int = 1280
    use_cls: bool = False
    min_score: float = 0.5
    # 默认用 F9~F11 而不是字母键：实测本机上 ctrl+alt+t/c/x/z/s 都被别的
    # 工具占着，RegisterHotKey 会直接失败，热键按了没反应还找不到原因。
    # 功能键 + ctrl+alt 的组合几乎不会被抢。
    hotkey_pause: str = "ctrl+alt+F9"
    hotkey_click: str = "ctrl+alt+F10"
    hotkey_quit: str = "ctrl+alt+F11"
    use_cache: bool = True
    debug: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    # --- 读写 ---

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path = CONFIG_PATH) -> None:
        data = dataclasses.asdict(self)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def apply_args(self, args) -> None:
        """命令行里显式给出的参数覆盖配置文件。"""
        for f in dataclasses.fields(self):
            if f.name == "extra":
                continue
            val = getattr(args, f.name, None)
            if val is not None:
                setattr(self, f.name, val)

    def validate(self) -> list[str]:
        problems = []
        if self.region is not None:
            if len(self.region) != 4 or not all(0.0 <= v <= 1.0 for v in self.region):
                problems.append("region 必须是 4 个 0~1 之间的数，形如 0.1,0.6,0.9,0.85")
            elif self.region[0] >= self.region[2] or self.region[1] >= self.region[3]:
                problems.append("region 的 x0 必须小于 x1、y0 必须小于 y1")
        if self.fps <= 0:
            problems.append("fps 必须大于 0")
        if not 0.2 <= self.alpha <= 1.0:
            problems.append("alpha 必须在 0.2~1.0 之间")
        return problems
