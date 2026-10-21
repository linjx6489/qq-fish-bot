"""借来的本事：把 ``.dsh/skills`` 里的 skill 当成她的参考书。

这些 skill 原本是给写代码/写文档的助手看的（人物蒸馏、情感军师……），
机器人要的是里面的**思路**，所以这里只做三件事：

1. 扫目录，读每个 skill 的 front matter（name / description）和正文；
2. 平时只把「手里借了哪几套本事」的一句话索引塞进上下文（很便宜）；
3. 聊天内容命中某套本事的领域时，再把它的正文截断后塞进去（贵，但按需）。

它不执行任何动作，也不替她说话——正文是给她看的参考，最后由她用自己的口气讲。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("fishbot.skills")

_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)

# 默认触发词：聊天里出现这些词，就把对应 skill 的正文递给她。
# 想改就在 config.yaml 的 skills.triggers 里覆盖同名的 key。
DEFAULT_TRIGGERS: dict[str, tuple[str, ...]] = {
    "goutoujunshi": (
        "喜欢", "暗恋", "表白", "追她", "追他", "追人", "暧昧", "约会", "心动",
        "对象", "女朋友", "男朋友", "前任", "分手", "复合", "吵架", "冷战",
        "感情", "相亲", "结婚", "出轨", "被拒", "怎么回", "聊天记录", "恋爱",
        "情绪价值", "冷淡", "不回我",
    ),
    "nuwa-skill": (
        "蒸馏", "造skill", "造个skill", "女娲", "造人", "思维方式", "思维框架",
        "人物skill", "思维顾问", "决策质量", "做个视角", "思考方式",
    ),
}


@dataclass
class Skill:
    """一套借来的本事。"""

    key: str  # 目录名，也当主键
    name: str  # front matter 里的 name
    title: str  # 正文里第一个一级标题
    description: str
    path: Path
    body: str = ""  # 去掉 front matter 的正文
    refs: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.title or self.name or self.key


class Skills:
    """技能目录的只读视图。"""

    def __init__(self, cfg: Any, root: Path):
        self.cfg = cfg
        self.root = root
        self.items: dict[str, Skill] = {}
        self.last_error = ""
        self.counters = {"loaded": 0, "hit": 0, "miss": 0, "trimmed": 0}
        self.load()

    # ------------------------------------------------------------------ 路径
    @property
    def dir_path(self) -> Path:
        raw = str(getattr(self.cfg, "dir", "") or "").strip()
        path = Path(raw) if raw else (self.root / "skills")
        return path if path.is_absolute() else (self.root / path)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True) and self.items)

    def names(self) -> list[str]:
        return list(self.items.keys())

    # ------------------------------------------------------------------ 加载
    def load(self, force: bool = False) -> int:  # noqa: ARG002 (接口和别处一致)
        self.items.clear()
        self.last_error = ""
        base = self.dir_path
        if not base.exists():
            self.last_error = f"目录不存在：{base}"
            log.warning("技能目录不存在：%s", base)
            self.counters["loaded"] = 0
            return 0
        allow = [str(x).strip() for x in (getattr(self.cfg, "allow", None) or []) if str(x).strip()]
        wanted = set(allow)
        loaded = 0
        try:
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError as exc:
            self.last_error = f"列目录失败：{exc}"
            log.warning("技能目录读不了：%s", exc)
            return 0
        for child in children:
            if wanted and child.name not in wanted:
                continue
            md = child / "SKILL.md"
            if not md.exists():
                continue
            try:
                skill = self._parse(child.name, md)
            except OSError as exc:
                log.warning("读技能 %s 失败：%s", child.name, exc)
                continue
            if skill is not None:
                self.items[child.name] = skill
                loaded += 1
        if wanted:
            missing = sorted(wanted - set(self.items))
            if missing:
                self.last_error = "没找到：" + "、".join(missing)
                log.warning("配置里点名要的 skill 没找到：%s", "、".join(missing))
        self.counters["loaded"] = loaded
        log.info("%s", self.status_text())
        return loaded

    def _parse(self, key: str, md: Path) -> Skill | None:
        raw = md.read_text(encoding="utf-8", errors="replace")
        meta: dict = {}
        body = raw
        m = _FRONT_RE.match(raw)
        if m:
            body = raw[m.end():]
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError as exc:
                log.debug("技能 %s 的 front matter 解析失败：%s", key, exc)
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        name = str(meta.get("name") or key).strip()
        desc = " ".join(str(meta.get("description") or "").split())
        head = _TITLE_RE.search(body)
        title = head.group(1).strip() if head else ""
        refs: list[str] = []
        ref_dir = md.parent / "references"
        if ref_dir.exists():
            refs = sorted(p.name for p in ref_dir.rglob("*.md"))[:40]
        body = body.strip()
        if not body:
            return None
        return Skill(key=key, name=name, title=title, description=desc, path=md, body=body, refs=refs)

    # ------------------------------------------------------------------ 取用
    def index_text(self) -> str:
        """一行一套本事的索引，便宜到可以每轮都带。"""
        preview = int(getattr(self.cfg, "index_preview", 60) or 60)
        lines = []
        for skill in self.items.values():
            desc = skill.description or "（没有说明）"
            if preview > 0 and len(desc) > preview:
                desc = desc[:preview].rstrip() + "…"
            lines.append(f"· {skill.label}（{skill.key}）：{desc}")
        return (
            "【你手里还借了几套本事，别人问到就用得上；"
            "用了也别说「技能」「skill」「资料」「参考」这些字，就当是你自己懂】\n"
            + "\n".join(lines)
        )

    def triggers_for(self, key: str) -> list[str]:
        cfg_triggers = getattr(self.cfg, "triggers", None) or {}
        words = cfg_triggers.get(key) if isinstance(cfg_triggers, dict) else None
        if not words:
            words = DEFAULT_TRIGGERS.get(key, ())
        return [str(w).strip() for w in (words or []) if str(w).strip()]

    def pick(self, text: str) -> Skill | None:
        """看这句话是不是撞上了某套本事的领域；撞上就返回分最高的那套。"""
        if not text or not self.items:
            return None
        low = text.lower()
        best: Skill | None = None
        best_score = 0
        for key, skill in self.items.items():
            words = self.triggers_for(key)
            words += [skill.name.lower(), skill.label.lower(), key.lower()]
            score = sum(1 for w in words if w and w.lower() in low)
            if score > best_score:
                best, best_score = skill, score
        return best

    def _limit_for(self, key: str) -> int:
        base = int(getattr(self.cfg, "max_chars", 4000) or 4000)
        per = getattr(self.cfg, "per_skill", None) or {}
        if isinstance(per, dict) and key in per:
            try:
                return max(300, int(per[key]))
            except (TypeError, ValueError):
                pass
        return base

    def trim(self, text: str, limit: int) -> str:
        """截断到 limit 字，尽量断在一个段落/标题边界上。"""
        text = (text or "").strip()
        if limit <= 0 or len(text) <= limit:
            return text
        cut = text[:limit]
        for sep in ("\n## ", "\n### ", "\n\n", "\n"):
            idx = cut.rfind(sep)
            if idx >= limit * 0.5:
                cut = cut[:idx]
                break
        self.counters["trimmed"] += 1
        return cut.rstrip() + "\n（后面还有，先看这些）"

    def render(self, key: str) -> str:
        """把某套本事的正文取出来，准备塞进上下文。"""
        skill = self.items.get(key)
        if skill is None:
            self.counters["miss"] += 1
            return ""
        body = self.trim(skill.body, self._limit_for(key))
        self.counters["hit"] += 1
        head = (
            f"【这会儿用得上「{skill.label}」的思路】下面这些是给你自己看的，"
            "照着它的思路想，但要用你自己的口气讲给对方听；别背原文、别报菜名。"
        )
        tail = ""
        if skill.refs:
            tail = "\n（这套本事还有这些细料，真需要就让人去翻：" + "、".join(skill.refs[:10]) + "）"
        return f"{head}\n{body}{tail}"

    def peek(self, key: str, limit: int = 600) -> str:
        """给人看的短预览（/技能 名字）。"""
        skill = self.items.get(key)
        if skill is None:
            return ""
        return self.trim(skill.body, limit)

    def note_for(self, text: str) -> str:
        """塞进 system 备注：索引常驻；命中领域时才带正文。"""
        if not self.enabled:
            return ""
        parts = [self.index_text()]
        if getattr(self.cfg, "auto", True):
            hit = self.pick(text)
            if hit is not None:
                body = self.render(hit.key)
                if body:
                    parts.append(body)
                    log.info("她用上了借来的本事：%s", hit.label)
        return "\n".join(parts)

    # ------------------------------------------------------------------ 状态
    def status_text(self) -> str:
        if not getattr(self.cfg, "enabled", True):
            return "借来的本事：关着（skills.enabled: false）"
        if not self.items:
            why = f"（{self.last_error}）" if self.last_error else ""
            return f"借来的本事：没借到{why}"
        labels = "、".join(s.label for s in self.items.values())
        c = self.counters
        return f"借来的本事：{len(self.items)} 套（{labels}）｜用上 {c['hit']} 次"

    def stats(self) -> dict:
        return {"dir": str(self.dir_path), **self.counters, "last_error": self.last_error}
