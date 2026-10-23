"""OpenAI 兼容接口客户端：流式 / 非流式、重试、token 记账。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from .config import LLMConfig

log = logging.getLogger("fishbot.llm")


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LLMResult:
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    elapsed: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def estimate_tokens(text: str) -> int:
    """粗略估算：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    if not text:
        return 0
    total = 0.0
    for ch in text:
        total += 1.0 if ord(ch) > 0x2E80 else 0.25
    return max(1, int(round(total)))


# 思考里这些词一出现，就说明那一行是"心里盘算"，不是要说的话
_META_HINTS = (
    "用户", "对方", "要求", "保持", "字以内", "我应该", "我该", "我需要", "可以发", "可以带",
    "带个", "语气", "回复", "吐槽", "开场", "结尾", "注意", "别忘", "思考", "分析", "结合",
    "人设", "设定", "字数", "简洁", "自然", "方案", "直接说", "要显得", "得显得", "看起来",
    "判断", "标记", "工具", "表情包", "颜文字", "第一句", "第二句", "显得", "显得我",
)
_KAOMOJI_RE = re.compile(r"[（(][^（()）\n]{1,12}[)）]|[´`￢￣｡・ω]|[「“『][^」”』]{1,80}[」”』]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LABEL_RE = re.compile(r"^(?:回复|回答|回应|最终|结论|答案|要说的话|台词)\s*[:：]\s*")
_META_PAREN_RE = re.compile(r"[（(][^（()）]{0,12}(?:字|保持|要求|以内)[^（()）]{0,12}[)）]")
# 从回收的台词两端剃掉的东西：中英文引号与书名号之类
_TRIM_CHARS = "\"'“”‘’「」『』《》"


def _strip_residue(text: str) -> str:
    """去掉思考文本里残留的流程标记（end / </answer> / markdown 强调符等）。"""
    out = re.sub(r"(?i)(?<![a-z])(end|answer|final|done)(?![a-z])", "", text or "")
    out = re.sub(r"[*`]+", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip(" \t")


def _atoms(text: str) -> list[str]:
    """把一段思考切成最小的「可说单元」：先按行，再按句末标点。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        for part in re.split(r"(?<=[。！？!?~…])[ \t]*", line):
            piece = _strip_residue(part)
            if piece:
                out.append(piece)
    return out


_EXTRA_META = (
    # 2026-10-22：识图 prompt 被模型复述后混进正文，这里一并认出来
    "主要是什么东西",
    "有没有文字",
    "照抄关键",
    "大概什么颜色",
    "什么情绪",
    "不能有额外",
    "需要把之前",
    "要压缩到",
    "非常关键",
    "需要识别",
    "我们需要",
    "长度", "多字", "字数", "字以内", "以内", "够不够", "可以。", "可以，", "行。",
    "这样回答", "接了梗", "接梗", "也接", "回应", "调侃", "口吻", "语气", "风格",
    "符合", "保持", "控制", "直接给", "最终", "综上", "没问题，", "最后",
    # 现场抓到的残渣：模型在思考里写「加一句尾巴。「…」」，把指令和台词粘在了一起
    "加一句", "补一句", "再加一句", "加个", "补个", "换成", "改成",
)


def _is_meta(text: str) -> bool:
    """这句是不是在盘算「我该怎么答」，而不是真的在说。"""
    plain = text.strip()
    if not plain:
        return True
    if _LABEL_RE.match(plain) or _META_PAREN_RE.search(plain):
        return True
    if any(hint in plain for hint in _META_HINTS) or any(hint in plain for hint in _EXTRA_META):
        return True
    return bool(re.search(r"\d+\s*字", plain))


def answer_from_reasoning(reasoning: str) -> str:
    """正文是空的时候，从思考里捞回她本来要说的那句。

    思考通常是「分析 + 该怎么回 + 试写一版 + 自我检查」，真正的台词在最后。
    找不到像样的台词就返回空串 —— 宁可不说，也不把心里话发出去。
    """
    text = reasoning or ""
    if not text.strip():
        return ""

    units = _atoms(text)
    if not units:
        return ""

    def picked(unit: str) -> bool:
        body = unit.strip()
        if not body:
            return False
        if _is_meta(body):
            return False
        return 6 <= len(body) <= 160

    def tidy(unit: str) -> str:
        body = unit.strip().strip(_TRIM_CHARS)
        # 「我先写了：」「试写一版：」这类短标签，剃掉后剩下才是台词
        body = re.sub(r"^[^，,。；;！？!?]{1,12}[：:]\s*", "", body).strip()
        body = re.sub(r"^[，,、。；;：:~\s]+", "", body).strip()
        return body.strip(_TRIM_CHARS).strip()

    # ① 带颜文字的那句，几乎一定是对外的语气
    for unit in reversed(units):
        if _KAOMOJI_RE.search(unit) and picked(unit):
            return tidy(unit)

    # ② 从后往前找干净句子，最多连带上上一句（停顿更自然）
    tail: list[str] = []
    for unit in reversed(units):
        if not picked(unit):
            continue
        body = tidy(unit)
        if not body:
            continue
        # 别把「我先写了：X」和「X」之类的同一句粘两遍
        if any(body in seen or seen in body for seen in tail):
            continue
        tail.insert(0, body)
        if len(tail) >= 2:
            break
    if tail:
        merged = "".join(tail)
        if len(_CJK_RE.findall(merged)) >= 5:
            return merged

    # ③ 带「摘要：」「回复：」这类标签的，取标签后面的正文
    for unit in reversed(units):
        hit = _LABEL_RE.match(unit.strip())
        if hit:
            body = _strip_residue(unit.strip()[hit.end():]).strip()
            if len(_CJK_RE.findall(body)) >= 6 and not _is_meta(body):
                return body

    return ""


class LLMError(RuntimeError):
    pass


class FishLLM:
    """给鱼用的模型客户端。"""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.last_usage = Usage()
        self.last_reasoning_chars = 0  # 只记账：思考过程一个字都不许发出去
        self.last_reasoning_text = ""  # 攒着的思考原文，只用来救场，不外发
        self.last_source = ""  # 这次正文是哪儿来的：content / reasoning / 备用模型
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "FishLLM":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.timeout, connect=15.0),
                headers={
                    "Authorization": f"Bearer {self.cfg.api_key or 'sk-local'}",
                    "Content-Type": "application/json",
                },
                trust_env=False,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- payload
    def _model_chain(self) -> list[str]:
        """先主模型，再备用模型；主模型「只思考、不吐正文」时可以往后换。"""
        chain = [self.cfg.model]
        for name in self.cfg.fallback_models or []:
            name = str(name).strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    def _payload(self, messages: list[dict], stream: bool, model: str | None = None) -> dict:
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "top_p": self.cfg.top_p,
            "max_tokens": self.cfg.max_tokens,
            "stream": stream,
        }
        if stream:
            # 让服务端在最后一个 chunk 里带上 usage（不支持的服务端会忽略）
            payload["stream_options"] = {"include_usage": True}
        payload.update(self.cfg.extra_body or {})
        return payload

    # -------------------------------------------------------------- 非流式调用
    async def chat(self, messages: list[dict]) -> LLMResult:
        await self.start()
        assert self._client is not None
        started = time.perf_counter()
        last_error = ""
        chain = self._model_chain()
        for index, model in enumerate(chain):
            self.last_reasoning_chars = 0
            for attempt in range(self.cfg.retries + 1):
                try:
                    resp = await self._client.post(
                        self.cfg.chat_url, json=self._payload(messages, stream=False, model=model)
                    )
                    if resp.status_code >= 400:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.cfg.retries:
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        break
                    data = resp.json()
                    text, usage = self._parse_completion(data, messages)
                    self.last_usage = usage
                    self.last_source = self.last_source or "content"
                    if not text and index + 1 < len(chain):
                        last_error = f"{model} 只给思考不给正文"
                        log.warning(
                            "模型 %s 正文是空的（思考 %s 字），换 %s 再问一次",
                            model, self.last_reasoning_chars, chain[index + 1],
                        )
                        break
                    if index:
                        log.info("这次用的是备用模型 %s", model)
                    return LLMResult(
                        text=text,
                        usage=usage,
                        model=str(data.get("model", model)),
                        elapsed=time.perf_counter() - started,
                    )
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self.cfg.retries:
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue
                    break
        log.warning("模型请求失败：%s", last_error)
        self.last_usage = Usage()
        return LLMResult(error=last_error, elapsed=time.perf_counter() - started)

    # ---------------------------------------------------------------- 流式调用
    def _record_usage(
        self, usage: Usage | None, messages: list[dict], collected: list[str]
    ) -> Usage:
        """服务端没给 usage 时自己估一个，并记到 self.last_usage。"""
        if usage is None:
            usage = Usage(
                prompt_tokens=sum(estimate_tokens(m.get("content", "")) for m in messages),
                completion_tokens=estimate_tokens("".join(collected)),
                estimated=True,
            )
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        self.last_usage = usage
        return usage

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """逐段吐出增量文本；结束后 self.last_usage 为本次用量。

        主模型要是「只思考、不吐正文」，就换备用模型重来一次 —— 只要它还一个字
        都没吐出去，重来对用户是无感的。
        """
        await self.start()
        assert self._client is not None
        chain = self._model_chain()
        last_error = ""
        for index, model in enumerate(chain):
            payload = self._payload(messages, stream=True, model=model)
            usage: Usage | None = None
            collected: list[str] = []
            self.last_reasoning_chars = 0
            self.last_reasoning_text = ""
            self.last_source = ""
            try:
                async with self._client.stream("POST", self.cfg.chat_url, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        last_error = f"HTTP {resp.status_code}: {body[:300]}"
                        self._record_usage(usage, messages, collected)
                        continue
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data.get("usage"), dict):
                            usage = self._usage_from(data["usage"])
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("reasoning_content"):
                                # 思考流只记账，一个字都不往外吐（但要攒着，万一正文是空的）
                                piece_reason = str(delta["reasoning_content"])
                                self.last_reasoning_chars += len(piece_reason)
                                self.last_reasoning_text += piece_reason
                            piece = delta.get("content")
                            if piece is None and choice.get("message"):
                                piece = (choice["message"] or {}).get("content")
                            if piece:
                                collected.append(piece)
                                yield piece
            except (httpx.HTTPError, LLMError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("流式请求异常：%s", exc)
                if collected:
                    # 已经吐过东西了，就别再吐第二遍
                    self._record_usage(usage, messages, collected)
                    return
                self._record_usage(usage, messages, collected)
                continue
            if not collected:
                # 主模型只思考、不吐正文时，从思考里把最后那句人话捡回来
                saved = answer_from_reasoning(self.last_reasoning_text)
                if saved:
                    collected.append(saved)
                    self.last_source = "reasoning"
                    self._record_usage(usage, messages, collected)
                    log.info(
                        "模型 %s 只思考没正文，从思考里捡回一句（%s 字）：%s",
                        model, len(saved), saved[:40],
                    )
                    yield saved
                    return
            self._record_usage(usage, messages, collected)
            if collected:
                if not self.last_source:
                    self.last_source = "content"
                if index:
                    log.info("这次用的是备用模型 %s", model)
                return
            last_error = f"{model} 流式返回正文为空"
            log.warning(
                "模型 %s 流式只说思考不给正文（思考 %s 字）%s",
                model,
                self.last_reasoning_chars,
                "，换下一个模型重问" if index + 1 < len(chain) else "",
            )
        if last_error:
            log.warning("流式请求失败：%s", last_error)

    # ---------------------------------------------------------------- 解析工具
    def _parse_completion(self, data: dict, messages: list[dict]) -> tuple[str, Usage]:
        text = ""
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            if reasoning:
                # 思考过程只记账；只有正文真的空着时，才想办法从里面捡回那句话
                self.last_reasoning_chars += len(str(reasoning))
                self.last_reasoning_text = str(reasoning)
                if not text:
                    if self.cfg.allow_reasoning_fallback:
                        text = str(reasoning)
                    else:
                        text = answer_from_reasoning(str(reasoning))
                        if text:
                            self.last_source = "reasoning"
                            log.info("模型只思考没正文，从思考里捡回一句（%s 字）", len(text))
            if not text and choices[0].get("text"):
                text = choices[0]["text"]
        if isinstance(data.get("usage"), dict):
            usage = self._usage_from(data["usage"])
        else:
            prompt = sum(estimate_tokens(m.get("content", "")) for m in messages)
            completion = estimate_tokens(text)
            usage = Usage(prompt, completion, prompt + completion, estimated=True)
        return (text or "").strip(), usage

    @staticmethod
    def _usage_from(raw: dict) -> Usage:
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or (prompt + completion))
        return Usage(prompt, completion, total)

    # ---------------------------------------------------------------- 健康检查
    async def health(self) -> tuple[bool, str]:
        await self.start()
        assert self._client is not None
        try:
            resp = await self._client.get(self.cfg.models_url, timeout=10.0)
            if resp.status_code == 200:
                try:
                    ids = [m.get("id") for m in (resp.json().get("data") or [])]
                except ValueError:
                    ids = []
                hit = "（已包含目标模型）" if ids and self.cfg.model in ids else ""
                return True, f"/models 可用，共 {len(ids)} 个模型 {hit}".strip()
            return False, f"/models 返回 HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            return False, f"无法连接 {self.cfg.models_url}：{type(exc).__name__}: {exc}"
