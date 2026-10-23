"""识图：把群里发来的图片取回来，交给视觉模型看一眼，拿回一句客观描述。

链路：OneBot 图片段（file / url）→ 本机文件或 HTTPS 下载 → base64 →
      视觉模型（OpenAI 兼容的 image_url 格式）→ 一句中文描述 → 注入她的上下文。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT

log = logging.getLogger("fishbot.vision")

# 模型声称没收到图（常是请求形状被上游丢了，而不是它真不会看）。短句里出现才算。
_DROPPED_IMAGE_RE = re.compile(
    r"没(?:有)?(?:收|看)到图|看不到图片|未(?:看到|收到)图|请(?:先)?上传|"
    r"无法(?:读取|查看|识别|看到)这张?图|图片(?:没|未)(?:有)?(?:上传|送达)|"
    r"haven'?t (?:received|seen)|no image|unable to (?:read|view|see) the image",
    re.I,
)

# 兜底的视觉模型：配置里 vision.model 留空时用它
DEFAULT_MODEL = "deepseek-v4.1-flash-free"

# 从思考里捞回来的东西如果是在复述要求（而不是真的描述了图），一律当没有正文看待
_PROMPT_ECHO_RE = re.compile(
    r"主要是什么东西|有没有文字|照抄关键|大概什么颜色|什么情绪|不要分点|不超过\s*\d+\s*字"
    r"|只输出|客观描述|用中文|别客套|需要.{0,6}(描述|识别|说明|回答)"
)

DEFAULT_PROMPT = (
    "看这几张图，用两三句中文说清里面有什么：主要是什么东西、有没有文字（有就照抄关键那句）、"
    "大概什么颜色、什么情绪。只说你真看到的，看不清就说不确定，不许客套、不许交代你在做什么。"
)

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class VisionError(RuntimeError):
    pass


def mime_of(name: str = "", body: bytes = b"") -> str:
    """按扩展名或魔数猜 MIME。"""
    ext = Path(str(name or "")).suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    if body[:4] == b"\x89PNG":
        return "image/png"
    if body[:2] == b"\xff\xd8":
        return "image/jpeg"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body[:2] == b"BM":
        return "image/bmp"
    return "image/png"


def image_refs(parsed: Any) -> list[dict]:
    """从解析好的消息里挑出图片段（保留 file / url / size）。"""
    refs: list[dict] = []
    for seg in getattr(parsed, "segments", None) or []:
        if not isinstance(seg, dict) or str(seg.get("type")) != "image":
            continue
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            continue
        refs.append(
            {
                "file": str(data.get("file") or ""),
                "url": str(data.get("url") or ""),
                "size": data.get("file_size"),
            }
        )
    return refs


def is_image_url(url: str) -> bool:
    """这个网址看着像不像一张图。"""
    raw = (url or "").strip().lower()
    if not raw:
        return False
    tail = raw.split("?", 1)[0].split("#", 1)[0]
    return tail.endswith(tuple(_MIME_BY_EXT))


class Vision:
    """看图。cfg 是 VisionConfig，llm 是 LLMConfig（借它的 base_url / api_key）。"""

    def __init__(self, cfg: Any, llm: Any) -> None:
        self.cfg = cfg
        self.llm = llm
        self.client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, str]] = {}
        self._last: dict[str, list[dict]] = {}
        self.counters = {"looked": 0, "failed": 0, "cached": 0, "too_big": 0, "no_image": 0}
        self.last_error = ""

    # ------------------------------------------------------------ 生命周期
    @property
    def dir_path(self) -> Path:
        raw = Path(str(self.cfg.dir or "data/vision"))
        return raw if raw.is_absolute() else (ROOT / raw)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def auto(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.auto)

    @property
    def model(self) -> str:
        return (self.cfg.model or "").strip() or DEFAULT_MODEL

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=float(self.cfg.timeout or 60.0), trust_env=False)
        return self.client

    async def start(self) -> None:
        _ = self._client()
        try:
            self.dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: PERF203
            log.warning("识图目录建不了（%s）：%s", self.dir_path, exc)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    # ------------------------------------------------------------ 对外接口
    def remember(self, session: str, refs: list[dict]) -> None:
        """记住这个会话最近一张图，方便 /看图 回头再看。"""
        if session and refs:
            self._last[session] = refs[: max(1, int(self.cfg.max_images or 1))]

    def last_refs(self, session: str) -> list[dict]:
        return list(self._last.get(session) or [])

    async def describe_for_parsed(
        self,
        parsed: Any,
        *,
        session: str = "",
        call: Any = None,
        force: bool = False,
    ) -> str:
        """收到消息时的入口：记下图片，按需自动看一眼。"""
        return await self.describe(image_refs(parsed), session=session, call=call, force=force)

    async def describe(
        self,
        refs: list[dict],
        *,
        session: str = "",
        call: Any = None,
        force: bool = False,
    ) -> str:
        """看一眼（多张合成一次请求）。没开自动时只有 force=True 才真的看。"""
        if not self.enabled or not refs:
            return ""
        limit = max(1, int(self.cfg.max_images or 1))
        picked = refs[:limit]
        self.remember(session, picked)
        if not (force or self.cfg.auto):
            return ""

        bodies: list[bytes] = []
        for ref in picked:
            body = await self._fetch(ref, call)
            if body:
                bodies.append(body)
        if not bodies:
            self.counters["failed"] += 1
            if not self.last_error:
                self.last_error = "图片没取到"
            return ""

        key = hashlib.sha1(b"|".join(hashlib.sha1(b).digest() for b in bodies)).hexdigest()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < float(self.cfg.cache_seconds or 0):
            self.counters["cached"] += 1
            return hit[1]

        text = await self._ask(bodies)
        if text:
            self._cache[key] = (time.time(), text)
            self.counters["looked"] += 1
        else:
            self.counters["failed"] += 1
        return text

    async def look_url(self, url: str, *, call: Any = None) -> str:
        """给她一个图片网址：下下来看一眼。"""
        if not self.enabled:
            return ""
        return await self.describe([{"file": "", "url": url}], call=call, force=True)

    # ------------------------------------------------------------ 取图
    async def _fetch(self, ref: dict, call: Any) -> bytes | None:
        size_hint = ref.get("size")
        try:
            if size_hint and int(size_hint) > int(self.cfg.max_bytes or 0):
                self.counters["too_big"] += 1
                log.info("识图：图太大（%s 字节），不看", size_hint)
                return None
        except (TypeError, ValueError):
            pass

        name = str(ref.get("file") or "").strip()
        url = str(ref.get("url") or "").strip()
        local: Path | None = Path(name) if name and Path(name).is_absolute() else None

        # NapCat 手上有本机缓存文件，先问它要路径（比走 HTTPS 快）
        if local is None and call is not None and name:
            try:
                got = await call("get_image", {"file": name})
            except Exception as exc:  # noqa: BLE001
                log.debug("get_image 调用失败：%s", exc)
                got = None
            if isinstance(got, dict):
                payload = got.get("data") if isinstance(got.get("data"), dict) else got
                path = str((payload or {}).get("file") or "")
                if path and Path(path).is_absolute():
                    local = Path(path)
                url = url or str((payload or {}).get("url") or "")

        body = b""
        try:
            if local is not None and local.exists():
                body = local.read_bytes()
            elif url:
                resp = await self._client().get(url)
                if resp.status_code == 200:
                    body = resp.content
                else:
                    self.last_error = f"下载图片 HTTP {resp.status_code}"
        except OSError as exc:
            self.last_error = f"读本地图失败：{exc}"
        except httpx.HTTPError as exc:
            self.last_error = f"下载图片失败：{type(exc).__name__}"

        if not body:
            return None
        if len(body) > int(self.cfg.max_bytes or 0) > 0:
            self.counters["too_big"] += 1
            log.info("识图：图太大（%d 字节），不看", len(body))
            return None
        self._save(body, local.name if local else name)
        return body

    def _save(self, body: bytes, name: str) -> None:
        if not self.cfg.keep_files:
            return
        digest = hashlib.sha1(body).hexdigest()[:16]
        ext = Path(name).suffix.lower() or ".png"
        if ext not in _MIME_BY_EXT:
            ext = ".png" if mime_of("", body) == "image/png" else ".jpg"
        path = self.dir_path / f"{digest}{ext}"
        try:
            if not path.exists():
                self.dir_path.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                self._prune()
        except OSError as exc:
            log.debug("存图失败：%s", exc)

    def _prune(self) -> None:
        keep = int(self.cfg.keep_recent or 0)
        if keep <= 0:
            return
        try:
            files = sorted(self.dir_path.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ 问模型
    async def _ask(self, bodies: list[bytes]) -> str:
        prompt = (str(self.cfg.prompt or "").strip() or DEFAULT_PROMPT)
        parts: list[dict] = [{"type": "text", "text": prompt}]
        for body in bodies:
            b64 = base64.b64encode(body).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime_of('', body)};base64,{b64}"}}
            )

        url = str(self.llm.base_url).rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.llm.api_key}"}
        models = [self.model] + [
            m for m in (self.cfg.fallback_models or []) if m and m != self.model
        ]
        last_err = ""
        for model in models:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是只做客观描述的助手，不扮演任何角色，不闲聊，不客套。"},
                    {"role": "user", "content": parts},
                ],
                "max_tokens": int(self.cfg.max_tokens or 300),
                "temperature": 0.2,
            }
            # 通道池很小：503 是真的「等会儿再来」，重试一次基本就好
            for attempt in (1, 2):
                started = time.time()
                try:
                    resp = await self._client().post(url, headers=headers, json=payload)
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}: {exc}"
                    break
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code} {resp.text[:120]}"
                    if resp.status_code >= 500 and attempt == 1:
                        log.info("识图：%s 打回 HTTP %s，等一下重试", model, resp.status_code)
                        await asyncio.sleep(1.5)
                        continue
                    break
                try:
                    data = resp.json()
                except ValueError:
                    last_err = "返回不是 JSON"
                    break
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                text = str(msg.get("content") or "").strip()
                reason = str(msg.get("reasoning_content") or "").strip()
                if not text and reason:
                    text = self._salvage(reason)
                    if text and _PROMPT_ECHO_RE.search(text):
                        log.warning("识图：%s 捞回来的是要求本身，换下一个（%r）", model, text[:60])
                        text = ""
                    elif text:
                        log.info("识图：%s 只给思考不给正文，从思考里捞了 %d 字", model, len(text))
                if text and _DROPPED_IMAGE_RE.search(text):
                    # 200 也可能是「图被上游静默丢了」，它会客客气气说自己没看到图
                    last_err = f"{model} 说它没看到这张图：{text[:60]}"
                    log.warning("识图：%s 声称没看到图，换下一个（%r）", model, text[:60])
                    break
                if text:
                    if model != self.model:
                        log.info("识图用的是备用模型：%s", model)
                    log.info("识图完成：%s %.1fs %d 字", model, time.time() - started, len(text))
                    self.last_error = ""
                    return text
                last_err = f"{model} 正文是空的（思考 {len(reason)} 字）"
                break
        self.last_error = last_err
        log.warning("识图失败：%s", last_err)
        return ""

    @staticmethod
    def _salvage(reason: str) -> str:
        """模型把描述写进思考里了，尽量捞回来。"""
        try:
            from .llm import answer_from_reasoning

            salvaged = (answer_from_reasoning(reason) or "").strip()
            if salvaged:
                return salvaged
        except Exception:  # noqa: BLE001
            pass
        lines = [line.strip() for line in (reason or "").splitlines() if line.strip()]
        return lines[-1][:200] if lines else ""

    # ------------------------------------------------------------ 状态
    def status_text(self) -> str:
        if not self.enabled:
            return "识图：关闭"
        c = self.counters
        line = (
            f"识图：{'开启' if self.cfg.auto else '手动'} —— 模型 {self.model}"
            f"／看过 {c['looked']} 张（缓存 {c['cached']}／失败 {c['failed']}）"
        )
        if self.last_error:
            line += f"｜最近出错：{self.last_error[:60]}"
        return line

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "auto": bool(self.cfg.auto),
            "model": self.model,
            "dir": str(self.dir_path),
            "files": len(list(self.dir_path.glob("*"))) if self.dir_path.exists() else 0,
            "cache": len(self._cache),
            **self.counters,
            "last_error": self.last_error,
        }
