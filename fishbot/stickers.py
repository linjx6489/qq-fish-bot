"""表情包：把 assets/stickers 里的图按标签发出去。

模型只要在回复里写一行 `[[表情:开心]]`，这里就把这句话拆成
「文字」+「一张图」。没有标签时（`[[表情]]`）随机挑一张。

图本身不生成，全部来自本地素材库（assets/stickers/ + manifest.json）。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("fishbot.stickers")

# [[表情:开心]] / [[表情包：累]] / [[sticker]] —— 标签可省，最长 12 个字
MARKER_RE = re.compile(r"\[\[\s*(?:表情包|表情|贴图|颜文字包|sticker)\s*[:：]?\s*([^\[\]]{0,12})\s*\]\]")

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_PARTIAL_PREFIXES = ("[[", "[[表", "[[表情", "[[表情:", "[[表情：", "[[表情包", "[[表情包:", "[[表情包：", "[[贴图", "[[贴图:", "[[sticker")


@dataclass
class Sticker:
    path: Path
    tags: list[str] = field(default_factory=list)
    desc: str = ""
    source: str = ""

    @property
    def name(self) -> str:
        return self.path.name


class Stickers:
    """表情包库：加载 manifest、按标签挑图、记录冷却。"""

    def __init__(self, cfg: Any, root: Path):
        self.cfg = cfg
        self.root = Path(root)
        self.items: list[Sticker] = []
        self._by_tag: dict[str, list[Sticker]] = {}
        self._last_sent = 0.0
        self._hour_bucket: list[float] = []
        self.counters = {"sent": 0, "miss": 0, "allowed": 0}
        self.last_error = ""

    # ---------------------------------------------------------------- 路径
    @property
    def dir_path(self) -> Path:
        raw = str(getattr(self.cfg, "dir", "assets/stickers") or "assets/stickers")
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    @property
    def manifest_path(self) -> Path:
        name = str(getattr(self.cfg, "manifest", "manifest.json") or "manifest.json")
        return self.dir_path / name

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False)) and bool(self.items)

    # ---------------------------------------------------------------- 加载
    def load(self, force: bool = False) -> int:
        """读 manifest.json；没有 manifest 就退化成「扫目录、用文件名当标签」。"""
        if self.items and not force:
            return len(self.items)
        self.items = []
        self._by_tag = {}
        self.last_error = ""

        folder = self.dir_path
        if not folder.exists():
            self.last_error = f"表情包目录不存在：{folder}"
            log.info("表情包：%s", self.last_error)
            return 0

        entries: list[dict] = []
        if self.manifest_path.exists():
            try:
                raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                entries = _manifest_entries(raw)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"manifest 读取失败：{exc}"
                log.warning("表情包：%s", self.last_error)

        if entries:
            for item in entries:
                st = _entry_to_sticker(folder, item)
                if st is not None:
                    self.items.append(st)
        else:
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix.lower() in _IMAGE_EXTS:
                    self.items.append(
                        Sticker(path=path, tags=_tags_from_name(path.stem), desc=path.stem)
                    )

        for st in self.items:
            for tag in st.tags:
                self._by_tag.setdefault(_norm(tag), []).append(st)

        if self.items:
            log.info(
                "表情包：加载 %d 张（%d 个标签）来自 %s",
                len(self.items),
                len(self._by_tag),
                folder,
            )
        else:
            self.last_error = self.last_error or f"目录里没有可用图片：{folder}"
            log.info("表情包：%s", self.last_error)
        return len(self.items)

    # ---------------------------------------------------------------- 挑图
    def tags(self) -> list[str]:
        return sorted(self._by_tag)

    def pick(self, tag: str | None = None) -> Sticker | None:
        if not self.items:
            return None
        want = _norm(tag or "")
        if want:
            hit = self._by_tag.get(want)
            if hit:
                return random.choice(hit)
            loose = [
                st
                for st in self.items
                if any(want in _norm(t) for t in st.tags) or want in _norm(st.desc)
            ]
            if loose:
                return random.choice(loose)
            self.counters["miss"] += 1
        return random.choice(self.items)

    def allowed(self) -> tuple[bool, str]:
        """冷却与每小时上限。返回 (能不能发, 原因)。"""
        now = time.time()
        gap = float(getattr(self.cfg, "cooldown_seconds", 0) or 0)
        if gap > 0 and self._last_sent and (now - self._last_sent) < gap:
            return False, f"距上张才 {int(now - self._last_sent)} 秒"
        cap = int(getattr(self.cfg, "max_per_hour", 0) or 0)
        if cap > 0:
            self._hour_bucket = [t for t in self._hour_bucket if now - t < 3600]
            if len(self._hour_bucket) >= cap:
                return False, f"这一小时已经发了 {len(self._hour_bucket)} 张"
        return True, ""

    def mark_sent(self) -> None:
        self._last_sent = time.time()
        self._hour_bucket.append(self._last_sent)
        self.counters["sent"] += 1

    def wants_random(self) -> bool:
        """没人点名、模型也没写标记时，按概率自己贴一张。"""
        prob = float(getattr(self.cfg, "probability", 0.0) or 0.0)
        return prob > 0 and random.random() < prob

    # ---------------------------------------------------------------- 文本
    @staticmethod
    def parse(text: str) -> tuple[str, str | None]:
        """把 `[[表情:开心]]` 从句子里摘出来。返回 (干净的句子, 标签或 None)。"""
        if not text or "[[" not in text:
            return text, None
        tag: str | None = None

        def _sub(m: re.Match) -> str:
            nonlocal tag
            if tag is None:
                tag = (m.group(1) or "").strip()
            return ""

        clean = MARKER_RE.sub(_sub, text)
        if tag is None:
            return text, None
        clean = re.sub(r"[ \t]{2,}", " ", clean)
        clean = re.sub(r"\n{2,}", "\n", clean).strip()
        return clean, tag

    @staticmethod
    def split_partial(text: str) -> tuple[str, str]:
        """切掉结尾那个还没写完的 `[[表情:开`，剩下的留到下一轮回合再拼。"""
        idx = text.rfind("[[")
        if idx < 0:
            return text, ""
        tail = text[idx:]
        if "]]" in tail:
            return text, ""
        if any(tail.startswith(p) for p in _PARTIAL_PREFIXES) or len(tail) <= 5:
            return text[:idx], tail
        return text, ""

    # ---------------------------------------------------------------- 展示
    def status_text(self) -> str:
        if not getattr(self.cfg, "enabled", False):
            return "表情包：关"
        if not self.items:
            return f"表情包：没素材（{self.last_error or '目录是空的'}）"
        return f"表情包：{len(self.items)} 张 / {len(self._by_tag)} 个标签"

    def stats(self) -> dict:
        return {
            "enabled": bool(getattr(self.cfg, "enabled", False)),
            "loaded": len(self.items),
            "tags": len(self._by_tag),
            "last_error": self.last_error,
            **self.counters,
        }


# ==================================================================== 辅助
def _norm(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "")


def _manifest_entries(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("stickers", "items", "list", "data"):
            val = raw.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def _tags_from_name(stem: str) -> list[str]:
    parts = re.split(r"[_\-\s]+", stem)
    return [p for p in parts if p and not p.isdigit()]


def _entry_to_sticker(folder: Path, item: dict) -> Sticker | None:
    raw = item.get("file") or item.get("path") or item.get("name") or ""
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        path = folder / path
    if not path.exists():
        return None

    tags: list[str] = []
    for key in ("tags", "keywords", "labels"):
        val = item.get(key)
        if isinstance(val, list):
            tags.extend(str(x) for x in val if str(x).strip())
        elif isinstance(val, str) and val.strip():
            tags.append(val)
    desc = str(item.get("desc") or item.get("title") or "").strip()
    if desc:
        tags.append(desc)
    if not tags:
        tags = _tags_from_name(path.stem)

    seen: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    return Sticker(path=path, tags=seen, desc=desc or path.stem, source=str(item.get("source") or ""))


__all__ = ["Sticker", "Stickers"]
