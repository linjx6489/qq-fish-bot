"""消息文本处理：OneBot 消息段解析、CQ 码兼容、分句、输出清洗。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 消息段 -> 纯文本
# --------------------------------------------------------------------------

SEGMENT_PLACEHOLDER = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "mface": "[表情包]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "forward": "[合并转发]",
    "file": "[文件]",
    "music": "[音乐]",
    "poke": "[戳一戳]",
    "shake": "[窗口抖动]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}

_CQ_INLINE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


@dataclass
class ParsedMessage:
    """一条入站消息的结构化视图。"""

    text: str = ""
    at_self: bool = False
    at_others: list = field(default_factory=list)
    has_image: bool = False
    has_voice: bool = False
    reply_to: str | None = None
    segments: list = field(default_factory=list)
    raw: str = ""

    @property
    def plain(self) -> str:
        return self.text.strip()


def parse_cq(raw: str, self_id: str = "") -> list[dict]:
    """把 CQ 码字符串转成 OneBot 消息段数组。"""
    segments: list[dict] = []
    pos = 0
    for m in _CQ_INLINE.finditer(raw or ""):
        if m.start() > pos:
            segments.append({"type": "text", "data": {"text": raw[pos:m.start()]}})
        seg_type = m.group(1)
        data: dict = {}
        for pair in (m.group(2) or "").lstrip(",").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                data[k.strip()] = unescape_cq(v)
        segments.append({"type": seg_type, "data": data})
        pos = m.end()
    if pos < len(raw or ""):
        segments.append({"type": "text", "data": {"text": raw[pos:]}})
    return segments or [{"type": "text", "data": {"text": raw or ""}}]


def unescape_cq(value: str) -> str:
    return (
        value.replace("&#91;", "[").replace("&#93;", "]")
        .replace("&#44;", ",").replace("&amp;", "&")
    )


def escape_cq(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("[", "&#91;")
        .replace("]", "&#93;").replace(",", "&#44;")
    )


def parse_message(message, raw: str = "", self_id: str = "") -> ParsedMessage:
    """兼容消息段数组与 CQ 码字符串两种格式。"""
    if isinstance(message, str) or message is None:
        segments = parse_cq(message or raw or "", self_id)
    else:
        segments = list(message)

    out = ParsedMessage(segments=segments, raw=raw or "")
    buf: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", ""))
        data = seg.get("data") or {}
        if seg_type == "text":
            buf.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq in ("all", "0"):
                out.at_others.append("all")
                buf.append("@全体成员")
            elif self_id and qq == self_id:
                out.at_self = True
            else:
                out.at_others.append(qq)
                buf.append(f"@{data.get('name') or qq}")
        elif seg_type == "reply":
            out.reply_to = str(data.get("id", ""))
        else:
            if seg_type == "image":
                out.has_image = True
            elif seg_type == "record":
                out.has_voice = True
            buf.append(SEGMENT_PLACEHOLDER.get(seg_type, ""))

    out.text = "".join(buf)
    if not out.text.strip() and out.at_self:
        out.text = ""
    return out


def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def at_segment(qq: str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def reply_segment(message_id: str) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}


def clean_text(text: str) -> str:
    """洗掉孤立代理对（lone surrogate）。

    Windows 控制台按非 UTF-8 代码页解码、或 QQ 端发来半截 emoji 时，
    Python 字符串里会混进 U+DC80–U+DCFF 这类字符；它们无法编码成 UTF-8，
    写进 sqlite 会直接抛 UnicodeEncodeError。这里统一替换成 U+FFFD。
    """
    if not text:
        return ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


# --------------------------------------------------------------------------
# 输出清洗 / 分句
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?")
_LEADING_NAME = re.compile(r"^\s*(?:吃白饭的肥鱼|大肥鱼|肥鱼)\s*[:：]\s*")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{2,}")

# 图形 emoji（要清掉）；颜文字用的 ๑ •̀ ω ✧ 之类不在这些区段里
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 图形、表情、手势、旗帜、扩充-A
    "\U0001F1E6-\U0001F1FF"  # 区域指示符
    "\u2600-\u2604\u260e\u2611\u2614\u2615\u2618\u261d\u2620"
    "\u2622\u2623\u2626\u262a\u262e\u262f\u2638-\u263a\u2640\u2642"
    "\u2648-\u2653\u265f\u2660\u2663\u2666\u2668\u267b\u267e\u267f"
    "\u2692-\u2697\u2699\u269b\u269c\u26a0\u26a1\u26aa\u26ab\u26b0"
    "\u26b1\u26bd\u26be\u26c4\u26c5\u26c8\u26ce\u26cf\u26d1\u26d3"
    "\u26d4\u26e9\u26ea\u26f0-\u26f5\u26f7-\u26fa\u26fd"
    "\u2705\u2708-\u270d\u270f\u2712\u2714\u2716\u271d\u2721\u2728"
    "\u2733\u2734\u2744\u2747\u274c\u274e\u2753-\u2755\u2757\u2763"
    "\u2764\u2795-\u2797\u27a1\u27b0\u27bf\u2b05-\u2b07\u2b1b\u2b1c"
    "\u2b50\u2b55\u3030\u303d\u3297\u3299"
    "\ufe0f\u200d"  # 变体选择符 / 零宽连接符
    "]+"
)


def strip_emoji(text: str) -> str:
    """去掉图形 emoji。颜文字（(｡•ᴗ•｡) 这类）不受影响。"""
    if not text:
        return ""
    return _EMOJI.sub("", text)


# --------------------------------------------------------------------------
# 藏起思考过程
# --------------------------------------------------------------------------

_THINK_BLOCK = re.compile(
    r"<(think|thinking|reasoning|analysis|思考|分析)\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_THINK_LABEL = re.compile(
    r"^\s*(?:思考|思路|推理|分析|thinking|reasoning|analysis)\s*[:：]\s*", re.I
)
_THINK_LEAD_HINTS = (
    "用户说", "用户在问", "用户要求", "对方说", "对方问", "对方在问",
    "我们需要", "需要回答", "我需要先", "首先，我需要", "首先我需要",
    "让我想想", "让我先", "先分析", "我来看看", "让我梳理",
    "根据要求", "按照要求", "按照人设", "输出要求", "字数要求",
)


def strip_thinking(text: str, max_lead_lines: int = 8) -> str:
    """把模型「把自己的思考当成正文」的部分摘掉。

    只动开头：正经回复里出现「首先」「我来看看」是正常的，所以一旦遇到第一句
    像人话的内容就停手；也永远不会把整段清空（最坏情况原样返回）。
    """
    if not text:
        return ""
    out = _THINK_BLOCK.sub("", text)
    lines = out.split("\n")
    start = 0
    while start < len(lines) - 1 and start < max_lead_lines:
        line = lines[start].strip()
        if not line:
            start += 1
            continue
        if _THINK_LABEL.match(line) or any(hint in line for hint in _THINK_LEAD_HINTS):
            start += 1
            continue
        break
    if start:
        out = "\n".join(lines[start:]).strip() or out
    out = _THINK_LABEL.sub("", out, count=1)
    return out.strip()


def sanitize_output(text: str, max_chars: int = 400) -> str:
    """清掉模型常见的自报家门、代码围栏、图形 emoji，并防止 CQ 码注入。"""
    if not text:
        return ""
    text = _FENCE.sub("", text).replace("```", "")
    text = _LEADING_NAME.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_NEWLINE.sub("\n\n", text)
    text = _SPACES.sub(" ", text)
    text = strip_emoji(text)
    # 阻断 CQ 码注入：把方括号替换成全角，视觉上几乎无差别
    text = text.replace("[CQ:", "［CQ：").replace("[cq:", "［cq：")
    text = text.strip()
    if max_chars > 0 and len(text) > max_chars:
        cut = text[:max_chars]
        for punct in ("。", "！", "？", "~", "…", "\n", "，", ".", "!", "?"):
            idx = cut.rfind(punct)
            if idx >= max_chars * 0.6:
                cut = cut[: idx + 1]
                break
        text = cut.rstrip() + "…"
    return text


_SPLIT_RE = re.compile(r"(?<=[。！？!?；;~…])|(?<=\n)|\s{3,}")


def split_message(text: str, max_chars: int = 120) -> list[str]:
    """把长回复切成多条短消息，尽量保住句子边界。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    for chunk in _SPLIT_RE.split(text):
        if not chunk:
            continue
        if len(chunk) <= max_chars:
            pieces.append(chunk)
        else:  # 超长单句硬切
            pieces.extend(chunk[i : i + max_chars] for i in range(0, len(chunk), max_chars))

    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) + len(piece) <= max_chars and not merged[-1].endswith("\n"):
            merged[-1] += piece
        else:
            merged.append(piece)
    return [p.strip() for p in merged if p.strip()]


def typing_delay_ms(text: str, per_char_ms: int, min_ms: int, max_ms: int) -> int:
    if not text:
        return 0
    raw = len(text) * per_char_ms
    return int(max(min_ms, min(max_ms, raw)))


def strip_trigger_words(text: str, names: list[str]) -> str:
    """去掉开头/结尾的称呼，避免她一直听到自己被叫名字。"""
    out = text
    for name in sorted(set(names), key=len, reverse=True):
        if not name:
            continue
        out = re.sub(rf"^\s*@?{re.escape(name)}[\s,，、:：~]*", "", out)
        out = re.sub(rf"[\s,，、]*(?:在吗|在嘛)?[，,]?\s*@?{re.escape(name)}\s*$", "", out)
    return out.strip() or text.strip()
