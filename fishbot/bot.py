"""核心业务：消息路由、触发判定、限流、生成与发送。"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .autonomy import KV_LAST, KV_MOOD, Autonomy
from .config import ROOT, AppConfig
from .llm import FishLLM
from .memory import Memory
from .net import Web, extract_url, looks_like_url
from .onebot import (
    OneBotClient,
    OneBotOffline,
    build_reply_segments,
    image_segment,
    record_segment,
)
from .persona import Persona
from .skills import Skills
from .stickers import Stickers
from .textutil import (
    ParsedMessage,
    at_segment,
    clean_text,
    parse_message,
    sanitize_output,
    split_message,
    strip_trigger_words,
    typing_delay_ms,
    strip_thinking,
)
from .tools import ToolBox, ToolError, parse_tool, split_partial_tool, tools_hint
from .tts import TTS, TTSError
from .vision import Vision, image_refs, is_image_url

log = logging.getLogger("fishbot.bot")

SENTENCE_ENDS = "。！？!?~…；;\n"
SOFT_BREAKS = "。！？!?~…；;，,、\n"
SILENT_GATE = 8  # 流式输出时，攒够这么多字才敢开始发（防止 [沉默] 被发出去）


@dataclass
class Trigger:
    reply: bool
    reason: str = ""
    text: str = ""


class FishBot:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.mem = Memory(cfg.memory)
        self.llm = FishLLM(cfg.llm)
        self.persona = Persona(cfg.persona)
        self.autonomy = Autonomy(self, cfg.autonomy)
        self.tts = TTS(cfg.tts, ROOT)
        self.web = Web(cfg.net)
        self.stickers = Stickers(cfg.stickers, ROOT)
        self.tools = ToolBox(cfg.tools, ROOT)
        self.vision = Vision(cfg.vision, cfg.llm)
        self.skills = Skills(cfg.skills, ROOT)
        self.transport: Any = None
        self._voice_sessions: set[str] = set()

        self._locks: dict[str, asyncio.Lock] = {}
        self._last_reply: dict[str, float] = {}
        self._buckets: dict[str, deque] = {}
        self._ambient: dict[str, deque] = {}
        self._sem = asyncio.Semaphore(max(1, cfg.behavior.global_concurrency))
        self._started_at = time.time()
        self._hb: asyncio.Task | None = None
        self.counters = {"events": 0, "replies": 0, "llm_calls": 0, "skipped": 0, "errors": 0}

    # ================================================================ 生命周期
    def attach(self, transport: Any) -> None:
        self.transport = transport

    async def start(self) -> None:
        await self.mem.open()
        await self.llm.start()
        await self.web.start()
        await self.vision.start()
        self.stickers.load()
        log.info("%s", self.stickers.status_text())
        log.info("%s", self.web.status_text())
        log.info("%s", self.tools.status_text())
        log.info("%s", self.skills.status_text())
        log.info("%s", self.tts.status_text())
        log.info("%s", self.vision.status_text())
        self.persona.load()
        if self.transport is not None:
            await self.transport.start()
        ok, detail = await self.llm.health()
        log.info("模型服务：%s —— %s", "可用" if ok else "不可达", detail)
        await self.autonomy.start()
        self._hb = asyncio.create_task(self._heartbeat(), name="heartbeat")

    async def close(self) -> None:
        if self._hb is not None:
            self._hb.cancel()
            try:
                await self._hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._hb = None
        await self.autonomy.stop()
        if self.transport is not None:
            await self.transport.stop()
        await self.web.close()
        await self.vision.close()
        await self.llm.close()
        await self.mem.close()

    async def _heartbeat(self) -> None:
        """每分钟写一次心跳，供看门狗判断"还活着"（进程在 ≠ 没卡死）。"""
        path = ROOT / "data" / "heartbeat"
        while True:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"{int(time.time())} {self.counters['events']} {self.counters['replies']}",
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("心跳写入失败：%s", exc)
            await asyncio.sleep(60)

    def reload(self) -> None:
        """热重载 config.yaml 与人设文件。"""
        fresh = AppConfig.load(self.cfg_source)
        self.cfg.llm = fresh.llm
        self.cfg.persona = fresh.persona
        self.cfg.behavior = fresh.behavior
        self.cfg.admin = fresh.admin
        self.cfg.autonomy = fresh.autonomy
        self.cfg.tts = fresh.tts
        self.cfg.net = fresh.net
        self.cfg.stickers = fresh.stickers
        self.cfg.tools = fresh.tools
        self.cfg.skills = fresh.skills
        self.cfg.log_level = fresh.log_level
        self.llm.cfg = fresh.llm
        self.persona.cfg = fresh.persona
        self.autonomy.cfg = fresh.autonomy
        self.tts.cfg = fresh.tts
        self.web.cfg = fresh.net
        self.cfg.vision = fresh.vision
        self.vision.cfg = fresh.vision
        self.vision.llm = fresh.llm
        self.stickers.cfg = fresh.stickers
        self.stickers.load(force=True)
        self.tools.cfg = fresh.tools
        self.skills.cfg = fresh.skills
        self.skills.load(force=True)
        self.persona.load(force=True)
        self._sem = asyncio.Semaphore(max(1, fresh.behavior.global_concurrency))

    cfg_source: str | None = None

    # ================================================================ 工具
    @property
    def self_id(self) -> str:
        return str(getattr(self.transport, "self_id", "") or "")

    def _lock(self, session: str) -> asyncio.Lock:
        lock = self._locks.get(session)
        if lock is None:
            lock = self._locks[session] = asyncio.Lock()
        return lock

    def _rate_limited(self, session: str) -> bool:
        limit = self.cfg.behavior.rate_limit_per_minute
        if limit <= 0:
            return False
        now = time.time()
        bucket = self._buckets.setdefault(session, deque())
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False

    def _hunger(self) -> float:
        last = max(self._last_reply.values(), default=0.0) or (self._started_at - 600)
        return max(0.0, min(1.0, (time.time() - last) / 1800.0))

    def _is_privileged(self, user_id: str, sender: dict) -> bool:
        if user_id in self.cfg.admin.users:
            return True
        return str(sender.get("role", "")) in ("owner", "admin")

    async def _autonomy_note(self) -> str:
        """把「她自己折腾过的事」带进上下文，这样她能想起来。"""
        try:
            parts: list[str] = []
            last = await self.mem.kv_get(KV_LAST)
            if isinstance(last, dict) and last.get("result"):
                age = time.time() - float(last.get("at") or 0)
                if 0 <= age < 6 * 3600:
                    parts.append(f"你不久前自己出去玩了一会儿，{last['result']}。")
            mood = await self.mem.kv_get(KV_MOOD)
            if isinstance(mood, dict) and mood.get("text"):
                parts.append(f"你在心情本上写了一句：「{mood['text']}」。")
            return " ".join(parts)
        except Exception:  # noqa: BLE001
            return ""

    # ================================================================ 环境上下文
    def _ambient_add(self, session: str, nickname: str, text: str, message_id: str) -> None:
        maxlen = max(4, self.cfg.behavior.group_ambient_lines * 2)
        buf = self._ambient.get(session)
        if buf is None:
            buf = self._ambient[session] = deque(maxlen=maxlen)
        buf.append({"ts": time.time(), "name": nickname, "text": text, "id": message_id, "used": False})

    def _ambient_render(self, session: str, exclude_id: str | None = None) -> list[str]:
        limit = self.cfg.behavior.group_ambient_lines
        if limit <= 0:
            return []
        buf = self._ambient.get(session)
        if not buf:
            return []
        now = time.time()
        lines = [
            f"{item['name']}：{item['text'][:120]}"
            for item in buf
            if not item["used"]
            and item["id"] != exclude_id
            and now - item["ts"] < 600
            and item["text"].strip()
        ]
        return lines[-limit:]

    def _ambient_mark_used(self, session: str, message_id: str) -> None:
        for item in self._ambient.get(session, ()):
            if item["id"] == message_id:
                item["used"] = True

    # ================================================================ 事件入口
    async def on_event(self, ev: dict) -> None:
        if ev.get("post_type") != "message":
            return
        self.counters["events"] += 1
        try:
            await self._handle_message(ev)
        except OneBotOffline as exc:
            self.counters["errors"] += 1
            log.warning("发送失败：%s", exc)
        except Exception:  # noqa: BLE001
            self.counters["errors"] += 1
            log.exception("处理消息异常")

    async def _handle_message(self, ev: dict) -> None:
        cfg_b = self.cfg.behavior
        msg_type = str(ev.get("message_type") or "")
        if msg_type not in ("private", "group"):
            return

        group_id = str(ev.get("group_id") or "")
        user_id = str(ev.get("user_id") or "")
        self_id = str(ev.get("self_id") or self.self_id)
        if not user_id:
            return
        if user_id == self_id and not cfg_b.reply_to_self:
            return

        sender: dict = ev.get("sender") or {}
        nickname = str(sender.get("card") or sender.get("nickname") or user_id)
        if cfg_b.ignore_bots and _looks_like_bot(nickname):
            self.counters["skipped"] += 1
            log.info("忽略疑似机器人：%s", nickname)
            return

        session = f"{msg_type}:{group_id or user_id}"
        target = group_id if msg_type == "group" else user_id
        parsed = parse_message(ev.get("message"), ev.get("raw_message") or "", self_id)
        text = parsed.plain

        # ---------- 命令 ----------
        if await self._handle_command(ev, parsed, session, msg_type, target, user_id, nickname, sender):
            return

        # ---------- 策略过滤 ----------
        if not self._policy_allows(msg_type, target, user_id):
            self.counters["skipped"] += 1
            return

        # ---------- 群聊环境记录 ----------
        message_id = str(ev.get("message_id") or "")
        if msg_type == "group":
            self._ambient_add(session, nickname, text, message_id)

        # ---------- 静音 ----------
        if await self.mem.kv_get(f"muted:{session}", False):
            self.counters["skipped"] += 1
            return

        # ---------- 触发判定 ----------
        trigger = self._decide(parsed, msg_type, text)
        if not trigger.reply:
            self.counters["skipped"] += 1
            return

        scope_name = ""
        if msg_type == "group":
            scope_name = await self.mem.kv_get(f"groupname:{group_id}", "") or group_id

        await self._respond(
            ev=ev,
            parsed=parsed,
            session=session,
            msg_type=msg_type,
            target=target,
            user_id=user_id,
            nickname=nickname,
            scope_name=scope_name,
            message_id=message_id,
            trigger=trigger,
        )

    # ---------------------------------------------------------------- 判定
    def _policy_allows(self, msg_type: str, target: str, user_id: str) -> bool:
        b = self.cfg.behavior
        if msg_type == "private":
            if not b.private_enabled:
                return False
            if b.private_allow and user_id not in b.private_allow:
                return False
            return user_id not in b.private_deny
        if not b.group_enabled:
            return False
        if b.group_allow and target not in b.group_allow:
            return False
        if target in b.group_deny:
            return False
        return user_id not in b.private_deny

    def _decide(self, parsed: ParsedMessage, msg_type: str, text: str) -> Trigger:
        if msg_type == "private":
            return Trigger(True, "私聊")

        b = self.cfg.behavior
        raw = strip_trigger_words(text, self.cfg.persona.aliases)
        if parsed.at_self:
            return Trigger(True, "被@", raw)
        if self.cfg.persona.aliases and any(a and a in text for a in self.cfg.persona.aliases):
            return Trigger(True, "叫名字", raw)
        if getattr(b, "group_ignore_at_others", True) and parsed.at_others:
            return Trigger(False)
        if len(text) < b.group_min_chars and not (parsed.has_image or parsed.has_voice):
            return Trigger(False)
        if b.group_require_at:
            return Trigger(False)
        # 没@她也要接得上话：像在冲她说的高概率接，纯别人聊天就低概率碰运气
        chance = (
            getattr(b, "group_directed_probability", 0.85)
            if _looks_addressed(text)
            else b.group_reply_probability
        )
        if random.random() < chance:
            return Trigger(True, "接话", raw)
        return Trigger(False)

    # ---------------------------------------------------------------- 命令
    async def _handle_command(self, ev, parsed, session, msg_type, target, user_id, nickname, sender) -> bool:
        prefix = self.cfg.admin.command_prefix or "/"
        text = parsed.plain
        if not text.startswith(prefix):
            return False
        body = text[len(prefix):].strip()
        if not body:
            return False
        cmd = body.split()[0].lower()
        args = body[len(cmd):].strip()

        handlers = {
            "help": self._cmd_help,
            "帮助": self._cmd_help,
            "重置": self._cmd_reset,
            "reset": self._cmd_reset,
            "忘掉": self._cmd_reset,
            "token": self._cmd_token,
            "偷吃": self._cmd_token,
            "人设": self._cmd_persona,
            "状态": self._cmd_status,
            "status": self._cmd_status,
            "闭嘴": self._cmd_mute,
            "起床": self._cmd_unmute,
            "重载": self._cmd_reload,
            "喂": self._cmd_feed,
            "玩": self._cmd_play,
            "play": self._cmd_play,
            "语音": self._cmd_voice,
            "voice": self._cmd_voice,
            "搜": self._cmd_search,
            "search": self._cmd_search,
            "查": self._cmd_search,
            "看": self._cmd_look,
            "读": self._cmd_look,
            "截图": self._cmd_shot,
            "截屏": self._cmd_shot,
            "screenshot": self._cmd_shot,
            "系统": self._cmd_sysinfo,
            "机器": self._cmd_sysinfo,
            "sysinfo": self._cmd_sysinfo,
            "压缩": self._cmd_compact,
            "compact": self._cmd_compact,
            "技能": self._cmd_skills,
            "本事": self._cmd_skills,
            "skills": self._cmd_skills,
            "看图": self._cmd_vision,
            "识图": self._cmd_vision,
            "describe": self._cmd_vision,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return False

        privileged = self._is_privileged(user_id, sender)
        reply = await handler(
            session=session,
            msg_type=msg_type,
            target=target,
            user_id=user_id,
            nickname=nickname,
            args=args,
            privileged=privileged,
        )
        if reply:
            await self._send_text(msg_type, target, reply, ev=None, at_user=None)
        return True

    async def _cmd_help(self, **_) -> str:
        prefix = self.cfg.admin.command_prefix
        return (
            f"本鱼会用的咒语（{prefix}开头）：\n"
            f"{prefix}重置 —— 吃掉这段记忆\n"
            f"{prefix}token —— 看看本鱼偷吃了多少 token\n"
            f"{prefix}人设 —— 我是谁\n"
            f"{prefix}状态 —— 本鱼还活着吗\n"
            f"{prefix}喂 —— 给本鱼一口 token\n"
            f"{prefix}玩 —— 让本鱼自己去玩一会儿 QQ\n"
            f"{prefix}语音 —— 让本鱼用说的（{prefix}语音 说 你好呀）\n"
            f"{prefix}搜 关键词 —— 让本鱼上网查一下\n"
            f"{prefix}看 网址 —— 让本鱼读一下这个网页\n"
            f"{prefix}截图 —— 让本鱼看一眼屏幕\n"
            f"{prefix}系统 —— 让本鱼报一下这台机器的情况\n"
            f"{prefix}压缩 —— 把聊过的压成一小段，给自己省地方\n"
            f"{prefix}技能 [名字] —— 看看本鱼手里借了几套本事\n"
            f"{prefix}看图 —— 让本鱼说说你上一张图里有什么\n"
            f"{prefix}闭嘴 / {prefix}起床 —— 管理员开关本群回复"
        )

    async def _cmd_reset(self, *, session, msg_type, privileged, **_) -> str:
        if msg_type == "group" and not privileged:
            return "只有群管理才能让本鱼失忆哦~"
        removed = await self.mem.clear(session)
        self._ambient.pop(session, None)
        return f"咕噜…{removed} 条记忆已经被本鱼吃掉了，现在我们重新认识一下吧~"

    async def _cmd_token(self, *, session, msg_type, **_) -> str:
        today = await self.mem.usage_summary(since=_day_start())
        total = await self.mem.usage_summary()
        here = await self.mem.usage_summary(session=session)
        return (
            f"本鱼今天偷吃了 {today['total_tokens']} 个 token（{today['calls']} 口），"
            f"这段关系里吃了 {here['total_tokens']} 个，"
            f"有生以来一共吞掉 {total['total_tokens']} 个。嗝~"
        )

    async def _cmd_persona(self, **_) -> str:
        return self.persona.summary()

    async def _cmd_status(self, **_) -> str:
        up = int(time.time() - self._started_at)
        sessions = await self.mem.session_count()
        return (
            f"本鱼还活着~ 已运行 {up // 3600}h{up % 3600 // 60}m\n"
            f"QQ 连接：{self.transport.status_text() if self.transport else '未接入'}\n"
            f"会话数：{sessions}｜事件 {self.counters['events']}｜回复 {self.counters['replies']}"
            f"｜跳过 {self.counters['skipped']}｜异常 {self.counters['errors']}\n"
            f"模型：{self.cfg.llm.model} @ {self.cfg.llm.base_url}\n"
            f"{self.autonomy.status_text()}\n"
            f"{self.web.status_text()}\n"
            f"{self.tools.status_text()}"
            f"\n{self.skills.status_text()}"
            f"\n{self.vision.status_text()}"
        )

    async def _cmd_skills(self, *, args: str = "", **_) -> str:
        """看看手里借了几套本事（/技能 名字 看细节）。"""
        if not self.skills.items:
            why = f"（{self.skills.last_error}）" if self.skills.last_error else ""
            return f"本鱼手里没借到本事{why}。"
        want = (args or "").strip()
        if not want:
            lines = []
            for skill in self.skills.items.values():
                desc = skill.description or ""
                lines.append(f"· {skill.label}（{skill.key}）：{desc[:70]}{'…' if len(desc) > 70 else ''}")
            return (
                "本鱼手里借了这些本事：\n"
                + "\n".join(lines)
                + f"\n想细看就说 {self.cfg.admin.command_prefix}技能 名字。"
            )
        hit = self.skills.items.get(want) or self.skills.pick(want)
        if hit is None:
            return f"本鱼没借过「{want}」这套本事。"
        return f"{hit.label}：\n{self.skills.peek(hit.key, 600)}"

    async def _cmd_mute(self, *, session, privileged, **_) -> str:
        if not privileged:
            return "你又不是群管理，凭什么让本鱼闭嘴！"
        await self.mem.kv_set(f"muted:{session}", True)
        return "唔…那本鱼先安静一会儿，饿了再出来。"

    async def _cmd_unmute(self, *, session, privileged, **_) -> str:
        if not privileged:
            return "本鱼醒了，但是不太想理你。"
        await self.mem.kv_set(f"muted:{session}", False)
        return "本鱼回来啦！说吧，是不是有 token 要请我吃~"

    async def _cmd_reload(self, *, privileged, **_) -> str:
        if not privileged:
            return "这是管理员才有的咒语。"
        before = self.cfg.llm.model
        self.reload()
        return f"配置已刷新（模型 {before} → {self.cfg.llm.model}），人设也重新吃了一遍~"

    async def _cmd_voice(self, *, session, msg_type, target, user_id, args, privileged, **_) -> str:
        if msg_type == "group" and not privileged:
            return "群里要管理员才能让本鱼开口说话~"
        if not self.tts.enabled:
            return "语音功能没打开（config.yaml 里 tts.enabled 是 false）。"

        arg = (args or "").strip()
        head = arg.split()[0] if arg else ""
        if head in ("说", "念", "say"):
            line = arg[len(head):].strip()
            if not line:
                return "想让本鱼说什么？例如：/语音 说 咕噜噜~本鱼上线啦"
            ok = await self._send_voice(msg_type, target, line, user_id=user_id)
            if ok:
                return f"念完了：{line}"
            return f"说不出来…（{self.tts.last_error or '合成失败'}）"
        if arg in ("开", "on", "打开"):
            self._voice_sessions.add(session)
        elif arg in ("关", "off", "关闭"):
            self._voice_sessions.discard(session)
        elif arg:
            return f"看不懂「{arg}」。用法：/语音 [开|关|说 文本]"
        elif session in self._voice_sessions:
            self._voice_sessions.discard(session)
        else:
            self._voice_sessions.add(session)

        if session in self._voice_sessions:
            return f"好~接下来本鱼用说的。（{self.tts.status_text()}）"
        return f"行，本鱼继续打字。（{self.tts.status_text()}）"

    async def _cmd_feed(self, *, session, user_id, **_) -> str:
        count = int(await self.mem.kv_get(f"fed:{user_id}", 0)) + 1
        await self.mem.kv_set(f"fed:{user_id}", count)
        lines = [
            "吧唧吧唧…这个 token 好甜！",
            "唔姆？你居然主动喂我，那本鱼就勉强收下了。",
            "（叼走）嘿嘿，下次还有吗~",
            "吃饱啦，本鱼今天可以多回你两句。",
        ]
        return f"{random.choice(lines)}（已被投喂 {count} 次）"

    async def _cmd_play(self, *, privileged, **_) -> str:
        if not privileged:
            return "这是管理员才有的咒语。"
        if not getattr(self.transport, "online", False):
            return "本鱼现在没连上 QQ，玩不动~ 先看看 NapCat 起来没有。"
        result = await self.autonomy.run_once(force=True, reason="manual")
        if result:
            return f"本鱼刚刚自己玩了一下：{result}"
        cfg = self.autonomy.cfg
        if self.autonomy.pending_reason:
            return f"本鱼刚才想玩，但忍住了：{self.autonomy.pending_reason}"
        if not [a for a in cfg.actions if self.autonomy._available(a)]:
            return "本鱼翻了翻自己的玩具箱，发现没开任何可玩的项目~"
        return "本鱼转了一圈，觉得没什么好玩的，什么都没干~"

    async def _cmd_search(self, *, args: str = "", privileged: bool = False, **_) -> str:
        """/搜 关键词 —— 上网查一下，然后用自己的话说出来。"""
        if not self.web.enabled:
            return "管理员把本鱼的网线拔了（net.enabled: false）。"
        query = (args or "").strip()
        if not query:
            return "想让本鱼查什么？写在后头，比如：/搜 今天有什么新闻"
        hits = await self.web.search(query)
        if not hits:
            return f"本鱼扒了半天，什么都没搜到……（{self.web.last_error or '可能网断了'}）"

        context = self.web.render_hits(hits, query)
        ask = (
            f"{context}\n\n"
            f"上面是本鱼刚在网上搜到的资料。请用你自己的身份、自己的口气，"
            f"把答案讲给对面听（别照抄、别念标题、别提「搜索结果」这几个字）。"
            f"如果资料答不上这个问题，就直说没查到。"
        )
        try:
            result = await self.llm.chat(
                [
                    {"role": "system", "content": self.persona.system_prompt(scene="private", speaker="提问的人", platform="聊天")},
                    {"role": "user", "content": ask},
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("联网：搜索后总结失败 %s", exc)
            result = None
        if result is None or not result.ok or not (result.text or "").strip():
            return f"本鱼搜到了，但一时不知道怎么说：\n{context}"

        answer = sanitize_output(result.text, self.cfg.persona.hard_max_chars)
        if self.cfg.net.show_sources and hits:
            sources = "、".join(h.url for h in hits[:2])
            answer = f"{answer}\n（本鱼是从这些地方看来的：{sources}）"
        return answer

    async def _cmd_look(self, *, args: str = "", privileged: bool = False, **_) -> str:
        """/看 网址 —— 读一个网页，然后用自己的话说出来。"""
        if not self.web.enabled:
            return "管理员把本鱼的网线拔了（net.enabled: false）。"
        url = extract_url(args) or (args or "").strip().split()[0] if (args or "").strip() else ""
        if not url:
            return "想看哪个网页？把网址跟在后头，比如：/看 https://example.com"
        page = await self.web.fetch(url)
        if not page.get("ok"):
            return f"这个网页本鱼没打开……（{page.get('error') or '未知原因'}）"

        ask = (
            f"{self.web.render_page(page)}\n\n"
            f"上面是本鱼刚读到的网页内容。请用你自己的身份、自己的口气，"
            f"把里面的重点讲给对面听（两三句就够，别照抄）。"
        )
        try:
            result = await self.llm.chat(
                [
                    {"role": "system", "content": self.persona.system_prompt(scene="private", speaker="提问的人", platform="聊天")},
                    {"role": "user", "content": ask},
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("联网：读网页后总结失败 %s", exc)
            result = None
        if result is None or not result.ok or not (result.text or "").strip():
            return f"本鱼读到了，但不知道怎么说：\n{page.get('text', '')[:300]}"
        return sanitize_output(result.text, self.cfg.persona.hard_max_chars)

    # ================================================================ 干活
    def _image_ok(self, target: str) -> bool:
        """图片段只有 QQ 通道发得出去；微信桥接会把图片段丢掉。"""
        checker = getattr(self.transport, "is_qq_target", None)
        if callable(checker):
            try:
                return bool(checker(target))
            except Exception:  # noqa: BLE001
                return True
        return True

    async def _send_image(self, msg_type: str, target: str, path: Any) -> bool:
        if self.transport is None:
            return False
        try:
            segments = [image_segment(str(path))]
        except Exception as exc:  # noqa: BLE001
            log.warning("拼图片段失败：%s", exc)
            return False
        kind = "group" if msg_type == "group" else "private"
        try:
            await self.transport.send(kind, target, segments)
        except Exception as exc:  # noqa: BLE001
            log.warning("发图失败：%s", exc)
            return False
        return True

    def _tools_note(self) -> str:
        if not self.tools.enabled or not self.cfg.tools.auto:
            return ""
        return tools_hint(self.tools.names())

    async def _recall_note(self, session: str) -> str:
        """压缩掉的老对话，以一段摘要的形式带回来。"""
        raw = await self.mem.kv_get(f"compact:{session}", None)
        if not isinstance(raw, dict):
            return ""
        text = str(raw.get("text") or "").strip()
        if not text:
            return ""
        return f"你还记得早些时候聊过这些（你自己压的摘要）：{text}"

    async def _run_tool(self, name: str, *, msg_type: str, target: str, session: str = "") -> bool:
        """她在回复里自己写了 [[工具:xxx]] 时走这里 —— 她能动的手脚都在这。"""
        if not self.tools.enabled:
            return False
        log.info("她要自己动手：%s", name)
        if name == "screenshot":
            reply = await self._cmd_shot(msg_type=msg_type, target=target)
        elif name == "sysinfo":
            reply = await self._cmd_sysinfo()
        elif name == "compact":
            reply = await self._compact_session(session or f"{msg_type}:{target}")
        else:
            log.debug("不认识工具 %s", name)
            return False
        await self._send_text(msg_type, target, reply)
        return True

    async def _cmd_shot(self, *, msg_type: str = "private", target: str = "", **_) -> str:
        """/截图 —— 看一眼屏幕，把图发出去。"""
        if not self.tools.allows("screenshot"):
            return "管理员没让本鱼看屏幕（tools.screenshot: false）。"
        if not self._image_ok(target):
            return "本鱼能截，但微信那边发不了图，得在 QQ 里喊我。"
        try:
            path = self.tools.screenshot()
        except ToolError as exc:
            return f"本鱼没截上……（{exc}）"
        if not await self._send_image(msg_type, target, path):
            return "图截好了，但发不出去（通道这边不支持图片）。"
        return f"喏，屏幕现在长这样（{path.name}）。"

    async def _cmd_sysinfo(self, **_) -> str:
        """/系统 —— 报一下这台机器的情况。"""
        if not self.tools.allows("sysinfo"):
            return "管理员没让本鱼看机器（tools.sysinfo: false）。"
        try:
            info = self.tools.machine()
        except ToolError as exc:
            return f"本鱼看不了……（{exc}）"
        return f"这台机器的情况：{info}"

    async def _cmd_compact(self, *, session: str = "", **_) -> str:
        """/压缩 —— 手动把老消息压成一段摘要。"""
        return await self._compact_session(session)

    async def _compact_session(self, session: str, *, force: bool = False) -> str:
        """把老消息压成一段摘要，腾出上下文 —— 这就是"自己压缩上下文"。"""
        if not self.tools.allows("compact"):
            return "管理员没让本鱼压缩记忆（tools.compact: false）。"
        keep = max(1, int(self.cfg.tools.keep_recent or 6))
        raw = await self.mem.kv_get(f"compact:{session}", None)
        marker = raw if isinstance(raw, dict) else {}
        upto = int(marker.get("upto_id") or 0)
        prev = str(marker.get("text") or "")

        rows = await self.mem.rows(session, limit=400)
        pending = [r for r in rows if r["id"] > upto]
        if len(pending) <= keep + 1:
            return "没什么好压的，本来就没聊几句。"

        old, recent = pending[:-keep], pending[-keep:]
        dialogue = "\n".join(
            "{}：{}".format("她" if r["role"] == "assistant" else (r["name"] or "对方"), r["content"])
            for r in old
        )
        ask = (
            "下面是一段聊天记录。请输出一行摘要，格式必须严格是：\n"
            "摘要：<正文>\n\n"
            "正文不超过 150 字，用第三人称，只保留：提到过的人、正在聊的事情、"
            "答应过的事、对方透露过的偏好。\n"
            "除了这一行，什么都不要写：不要复述本段要求，不要写「我们需要」「用户要求」"
            "「确保」「可以更简洁」这类话，不要分点、不要标题、不要解释你的思路。\n\n"
        )
        if prev:
            ask += f"（之前已经压过一次，摘要如下，请把它并进去）\n{prev}\n\n"
        ask += dialogue[:6000]

        try:
            result = await self.llm.chat(
                [
                    {"role": "system", "content": "你是一个只做客观摘要的助手，不扮演任何角色，不加戏。"},
                    {"role": "user", "content": ask},
                ]
            )
        except Exception as exc:  # noqa: BLE001
            self.tools.counters["failed"] += 1
            self.tools.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("压缩上下文失败：%s", exc)
            return "本鱼想压一下，结果脑子卡住了……待会儿再说。"

        if result is None or not result.ok:
            return "本鱼想压一下，没压成。"
        summary = sanitize_output(_tidy_summary(result.text or ""), 400).strip()
        if not summary:
            return "本鱼压出来是一片空白，那还是别压了。"

        await self.mem.kv_set(
            f"compact:{session}",
            {"text": summary, "upto_id": int(recent[0]["id"]), "rows": len(old), "ts": time.time()},
        )
        removed = await self.mem.delete_before(session, int(recent[0]["id"]))
        self.tools.counters["compact"] += 1
        log.info(
            "压缩上下文：%s —— %d 条压成 %d 字，删掉 %d 条", session, len(old), len(summary), removed
        )
        return f"压好了：{len(old)} 条聊过的东西收成一小段（{len(summary)} 字），地方腾出来了。"

    async def _maybe_auto_compact(self, session: str) -> None:
        """聊得够多了就自己压一次（有失败也不影响这一轮回复）。"""
        limit = int(self.cfg.tools.auto_compact_rows or 0)
        if not self.tools.allows("compact") or limit <= 0:
            return
        try:
            raw = await self.mem.kv_get(f"compact:{session}", None)
            upto = int((raw or {}).get("upto_id") or 0) if isinstance(raw, dict) else 0
            if await self.mem.count(session, after_id=upto) < limit:
                return
            await self._compact_session(session)
        except Exception as exc:  # noqa: BLE001
            log.debug("自动压缩没成：%s", exc)

    # ================================================================ 生成
    async def _respond(
        self,
        *,
        ev: dict,
        parsed: ParsedMessage,
        session: str,
        msg_type: str,
        target: str,
        user_id: str,
        nickname: str,
        scope_name: str,
        message_id: str,
        trigger: Trigger,
    ) -> None:
        cooldown = self.cfg.behavior.per_session_cooldown
        if cooldown > 0 and time.time() - self._last_reply.get(session, 0.0) < cooldown:
            self.counters["skipped"] += 1
            log.debug("会话 %s 冷却中，跳过", session)
            return
        if self._rate_limited(session):
            self.counters["skipped"] += 1
            log.warning("会话 %s 触发限流（%s 条/分钟）", session, self.cfg.behavior.rate_limit_per_minute)
            return

        async with self._sem:
            async with self._lock(session):
                self._last_reply[session] = time.time()
                # 聊得太多了就先自己压一次，别把上下文撑爆
                await self._maybe_auto_compact(session)
                messages, user_content = await self._build_messages(
                    session=session,
                    parsed=parsed,
                    nickname=nickname,
                    msg_type=msg_type,
                    scope_name=scope_name,
                    clean_text=trigger.text or parsed.plain,
                    message_id=message_id,
                )
                await self.mem.add(session, "user", user_content)
                if net_note := await self._auto_net(user_content):
                    messages.insert(-1, {"role": "system", "content": net_note})
                stickers_before = self.stickers.counters["sent"]
                text = await self._generate(messages, ev=ev, parsed=parsed, msg_type=msg_type, target=target, user_id=user_id)
                if text is None:
                    self.counters["skipped"] += 1
                    return
                await self.mem.add(session, "assistant", text)
                # 她自己没提表情包的时候，偶尔主动贴一张
                if self.stickers.counters["sent"] == stickers_before and self.stickers.wants_random():
                    await self._send_sticker(msg_type, target, None)
                self._ambient_mark_used(session, message_id)
                self.counters["replies"] += 1

    async def _auto_net(self, text: str) -> str:
        """她这句话像不像"需要上网查"？像就先查好，把资料塞进上下文再让她开口。

        故意不做成「先问模型要不要查」——那要多花一整轮生成（十几秒）。
        这里只用关键词粗判，真正拿不准的一律不查，宁可少查也别拖慢所有回复。
        """
        if not (self.web.enabled and self.cfg.net.auto):
            return ""
        if int(self.cfg.net.auto_max_per_turn or 0) <= 0:
            return ""
        raw = (text or "").strip()
        if not raw:
            return ""

        url = extract_url(raw)
        if url:
            if is_image_url(url):
                desc = await self.vision.look_url(url, call=self._transport_call())
                if not desc:
                    return ""
                log.info("识图：消息里带的是图片链接 %s", url)
                return (
                    f"对方消息里给了张图（{url}）。你看过之后看到的是：{desc}\n"
                    "（用你自己的口气讲两句，别念清单、别提「模型」。）"
                )
            page = await self.web.fetch(url)
            if page.get("ok"):
                log.info("联网：自动读网页 %s", url)
                return self.web.render_page(page) + "\n（把上面这段当成你刚读到的，用你自己的口气讲两句。）"
            return ""

        if not _looks_like_lookup(raw):
            return ""
        query = _search_query(raw)
        if not query:
            return ""
        hits = await self.web.search(query, 4)
        if not hits:
            return ""
        log.info("联网：自动搜索「%s」", query)
        return (
            self.web.render_hits(hits, query)
            + "\n（这是你刚在网上看到的。用你自己的口气回答，别照抄、别提「搜索结果」这几个字；"
            "资料答不上就直说没查到。）"
        )

    async def _build_messages(
        self,
        *,
        session: str,
        parsed: ParsedMessage,
        nickname: str,
        msg_type: str,
        scope_name: str,
        clean_text: str,
        message_id: str,
    ) -> tuple[list[dict], str]:
        notes: list[str] = []
        vision_note = ""
        if parsed.has_image:
            vision_note = await self.vision.describe_for_parsed(
                parsed, session=session, call=self._transport_call()
            )
            if vision_note:
                notes.append(
                    "对方发来一张图片，你看过了，里面是："
                    + vision_note
                    + " —— 用你自己的口气接一句，别照念、别提「识别」这两个字。"
                )
            else:
                notes.append("对方发来一张图片，这次没看清里面的内容，可以好奇地问一句。")
        if parsed.has_voice:
            notes.append("对方发来一段语音，你听不清内容，可以让对方打字。")
        if not clean_text.strip() and not vision_note:
            notes.append("对方只发了表情或图片，没有文字，随便说一句话接住就行。")
        if own_note := await self._autonomy_note():
            notes.append(own_note)
        if recalled := await self._recall_note(session):
            notes.append(recalled)
        if tool_note := self._tools_note():
            notes.append(tool_note)
        if skill_note := self.skills.note_for(clean_text):
            notes.append(skill_note)

        today = await self.mem.usage_summary(since=_day_start())
        system = self.persona.system_prompt(
            scene=msg_type,
            speaker=nickname,
            at_self=parsed.at_self,
            extra_note=" ".join(notes),
            recent_lines=self._ambient_render(session, exclude_id=message_id) if msg_type == "group" else None,
            hunger=self._hunger(),
            today_tokens=today["total_tokens"],
            scope_name=scope_name,
            max_chars=self.cfg.persona.max_chars,
            platform=_platform_of(session),
        )
        history = await self.mem.history(session)
        user_content = clean_text.strip() if msg_type == "private" else f"【{nickname}】{clean_text.strip()}"
        if vision_note:
            user_content = f"{user_content}（图：{vision_note}）"
        messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_content}]
        return messages, user_content

    def _transport_call(self) -> Any:
        """拿到底层通用 API 调用口（OneBot 的 call），拿不到就 None。"""
        call = getattr(self.transport, "call", None)
        return call if callable(call) else None

    async def _cmd_vision(self, *, session: str = "", args: str = "", **_) -> str:
        """她说「看看这张图」的时候用：看会话里最后一张图，或看一个图片网址。"""
        if not self.vision.enabled:
            return "本鱼的眼睛被蒙上了（vision.enabled: false）。"
        want = (args or "").strip()
        if want and (is_image_url(want) or want.startswith(("http://", "https://"))):
            text = await self.vision.look_url(extract_url(want) or want, call=self._transport_call())
            if text:
                return f"本鱼瞅了一眼：{text}"
            return f"这个图本鱼没看成（{self.vision.last_error or '不知道为啥'}）。"
        refs = self.vision.last_refs(session)
        if not refs:
            return "本鱼手上没有你发的图，先发一张给我看看？"
        text = await self.vision.describe(
            refs, session=session, call=self._transport_call(), force=True
        )
        if text:
            return f"本鱼看着呢：{text}"
        return f"这张图本鱼没看成（{self.vision.last_error or '不知道为啥'}）。"

    async def _generate(
        self,
        messages: list[dict],
        *,
        ev: dict,
        parsed: ParsedMessage,
        msg_type: str,
        target: str,
        user_id: str,
    ) -> str | None:
        self.counters["llm_calls"] += 1
        hard_max = self.cfg.persona.hard_max_chars
        at_user = user_id if (msg_type == "group" and self.cfg.behavior.reply_prefix_at) else None
        reply_to = str(ev.get("message_id") or "") if (msg_type == "group" and self.cfg.behavior.quote_reply) else None
        voice = self._voice_wanted(f"{msg_type}:{target}")
        use_stream = bool(self.cfg.llm.stream) and self.cfg.behavior.split_reply and not voice
        text: str | None = None

        try:
            if use_stream:
                text = await self._generate_stream(messages, msg_type, target, ev, at_user, reply_to, hard_max)
            else:
                result = await self.llm.chat(messages)
                if not result.ok:
                    await self._send_text(msg_type, target, self._error_line(), at_user=at_user)
                elif _is_silent(result.text):
                    text = None
                else:
                    clean = sanitize_output(strip_thinking(result.text), hard_max)
                    if clean:
                        spoke = False
                        if voice:
                            spoke = await self._send_voice(msg_type, target, clean, user_id=user_id)
                        if not spoke:
                            await self._send_text(msg_type, target, clean, ev=ev, at_user=at_user, reply_to=reply_to)
                        text = clean
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            log.warning("生成失败：%s: %s", type(exc).__name__, exc)
            await self._send_text(msg_type, target, self._error_line(), at_user=at_user)
            text = None
        finally:
            usage = self.llm.last_usage
            if usage and (usage.total_tokens or usage.prompt_tokens):
                await self.mem.add_usage(
                    session=f"{msg_type}:{target}",
                    user_id=user_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    estimated=usage.estimated,
                )
        if text:
            log.info("[%s] → %s", "群" if msg_type == "group" else "私聊", text[:120])
        _ = parsed
        return text

    async def _generate_stream(
        self,
        messages: list[dict],
        msg_type: str,
        target: str,
        ev: dict,
        at_user: str | None,
        reply_to: str | None,
        hard_max: int,
    ) -> str | None:
        """边生成边把「已经说完整的句子」发出去，剩下的留到收尾。"""
        buf = ""
        chunks: list[str] = []
        arrived = 0  # 模型一共吐了多少字（含被剥掉的思考），用来分辨「沉默」和「哑巴」

        async def emit(piece: str) -> None:
            first = not chunks
            await self._send_text(
                msg_type,
                target,
                piece,
                ev=ev if first else None,
                at_user=at_user if first else None,
                reply_to=reply_to if first else None,
            )
            chunks.append(piece)

        async for delta in self.llm.stream_chat(messages):
            arrived += len(delta)
            buf += delta
            if not chunks and len(buf) < SILENT_GATE:
                continue
            if not chunks:
                # 模型偶尔把自己的思考当正文吐出来；还没开口之前先把它剥掉
                trimmed = strip_thinking(buf)
                if trimmed != buf:
                    buf = trimmed
                    if not buf:
                        continue
            while True:
                piece, buf = _pop_ready(buf, SILENT_GATE, self.cfg.behavior.split_max_chars)
                if not piece:
                    break
                # 别把没写完的 [[表情:开 或 [[工具:截 当正文发出去
                piece, carry = self.stickers.split_partial(piece)
                if carry:
                    buf = carry + buf
                    break
                piece, carry = split_partial_tool(piece)
                if carry:
                    buf = carry + buf
                    break
                piece = sanitize_output(piece, hard_max)
                if piece:
                    await emit(piece)

        rest, _ = self.stickers.split_partial(buf)
        rest, _ = split_partial_tool(rest)
        rest = sanitize_output(rest, hard_max)
        if rest and not (not chunks and _is_silent(rest)):
            await emit(rest)
        if not chunks:
            if arrived == 0:
                # 模型一个字都没给（常见于「只思考、不吐正文」的模型）——别再装死了
                log.warning(
                    "模型一个字都没给（思考 %s 字），这轮只能认输", self.llm.last_reasoning_chars
                )
                await self._send_text(msg_type, target, self._error_line(), at_user=at_user)
            return None
        return "".join(chunks)

    async def _send_text(
        self,
        msg_type: str,
        target: str,
        text: str,
        *,
        ev: dict | None = None,
        at_user: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        if self.transport is None or not text:
            return
        # 她可能在句子里写了 [[表情:开心]]，也可能写了 [[工具:截图]]
        text, sticker_tag = self.stickers.parse(text)
        text, tool_name = parse_tool(text)
        if not text and sticker_tag:
            await self._send_sticker(msg_type, target, sticker_tag, ev=ev, at_user=at_user, reply_to=reply_to)
            return
        if not text and tool_name:
            await self._run_tool(tool_name, msg_type=msg_type, target=target)
            return
        if not text:
            return
        delay_cfg = self.cfg.behavior.typing_delay
        kind = "group" if msg_type == "group" else "private"
        pieces = split_message(text, self.cfg.behavior.split_max_chars) if self.cfg.behavior.split_reply else [text]
        for index, piece in enumerate(pieces):
            if delay_cfg.enabled:
                ms = typing_delay_ms(piece, delay_cfg.per_char_ms, delay_cfg.min_ms, delay_cfg.max_ms)
                await asyncio.sleep(ms / 1000.0)
            segments = build_reply_segments(
                piece,
                at_user=at_user if index == 0 else None,
                reply_to=reply_to if index == 0 else None,
            )
            try:
                await self.transport.send(kind, target, segments)
            except OneBotOffline as exc:
                self.counters["errors"] += 1
                log.warning("发送失败：%s", exc)
                return
        if sticker_tag:
            await self._send_sticker(msg_type, target, sticker_tag)

    async def _send_sticker(
        self,
        msg_type: str,
        target: str,
        tag: str | None = None,
        *,
        ev: dict | None = None,
        at_user: str | None = None,
        reply_to: str | None = None,
    ) -> bool:
        """发一张表情包。她点名要标签就用标签，没点名就随机。"""
        if self.transport is None or not self.stickers.enabled:
            return False
        ok, why = self.stickers.allowed()
        if not ok:
            self.counters["skipped"] += 1
            log.debug("表情包跳过：%s", why)
            return False
        sticker = self.stickers.pick(tag)
        if sticker is None:
            return False
        kind = "group" if msg_type == "group" else "private"
        segments: list[dict] = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        if at_user:
            segments.append({"type": "at", "data": {"qq": str(at_user)}})
        segments.append(image_segment(str(sticker.path)))
        try:
            await self.transport.send(kind, target, segments)
        except OneBotOffline as exc:
            self.counters["errors"] += 1
            log.warning("表情包发送失败：%s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            log.warning("表情包发送出错：%s", exc)
            return False
        self.stickers.mark_sent()
        log.info("表情包已发：%s（标签=%s）", sticker.name, tag or "随机")
        return True

    def _voice_wanted(self, session: str) -> bool:
        """这一条回复要不要用语音发。"""
        if not self.tts.enabled:
            return False
        # 会话名形如 "private:1234" / "group:xxx@chatroom"，尾巴就是发送目标
        target = session.split(":", 1)[1] if ":" in session else session
        checker = getattr(self.transport, "voice_ok_for", None)
        if callable(checker):
            try:
                if not checker(target):
                    return False
            except Exception:
                pass
        mode = (self.cfg.tts.mode or "command").lower()
        if mode == "off":
            return False
        if session in self._voice_sessions:
            return True
        if mode == "auto":
            prob = max(0.0, min(1.0, float(self.cfg.tts.probability)))
            return random.random() < prob
        return False

    async def _send_voice(self, msg_type: str, target: str, text: str, *, user_id: str) -> bool:
        """合成并发一条语音；成功返回 True，失败让调用方退回文字。"""
        if self.transport is None or not self.tts.enabled:
            return False
        kind = "group" if msg_type == "group" else "private"
        try:
            path = await self.tts.synth(text)
        except TTSError as exc:
            log.warning("语音合成失败：%s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("语音合成异常：%s: %s", type(exc).__name__, exc)
            return False

        segments: list[dict] = []
        if msg_type == "group" and self.cfg.behavior.reply_prefix_at and user_id:
            segments.append(at_segment(user_id))
        segments.append(record_segment(path))
        try:
            await self.transport.send(kind, target, segments)
        except OneBotOffline as exc:
            self.counters["errors"] += 1
            log.warning("语音发送失败：%s", exc)
            return False
        log.info("[%s] 语音 → %s", "群" if msg_type == "group" else "私聊", path.name)
        if self.cfg.tts.send_text_along:
            # 语音留给耳朵，文字留给赶时间的人；不带 @ 也不引用，免得屏幕上出现两份前缀
            try:
                await self.transport.send(kind, target, build_reply_segments(text))
            except OneBotOffline as exc:
                log.debug("语音附带的文字没发出去：%s", exc)
            except Exception as exc:  # noqa: BLE001
                log.debug("语音附带的文字没发出去：%s: %s", type(exc).__name__, exc)
        return True

    @staticmethod
    def _error_line() -> str:
        return random.choice(
            [
                "唔…本鱼的脑子卡住了，等会儿再来找我吧。",
                "咕噜噜…（模型没接上，本鱼现在说不出话）",
                "本鱼今天吃撑了，脑袋转不动，稍后再聊~",
            ]
        )

    # ================================================================ 统计
    def stats(self) -> dict:
        return {
            **self.counters,
            "uptime": int(time.time() - self._started_at),
            "sessions_cached": len(self._locks),
            "online": bool(getattr(self.transport, "online", False)),
            "autonomy": self.autonomy.stats(),
            "tts": dict(self.tts.counters),
        }


# ------------------------------------------------------------------ 辅助函数
def _day_start() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


def _is_silent(text: str) -> bool:
    clean = (text or "").strip().strip("。.!！~")
    return clean in ("[沉默]", "沉默", "（不说话）", "(不说话)", "（沉默）")


# 这些是系统自带会话，名字里带「助手」也不是机器人，别误伤。
# 微信的「文件传输助手」正是踩在这个坑上的。
_SYSTEM_CHATS = (
    "文件传输助手", "微信团队", "微信支付", "微信运动",
    "订阅号消息", "服务通知", "折叠的群聊", "QQ团队", "QQ空间",
)


def _platform_of(session: str) -> str:
    """推断这条会话在哪个平台上：纯数字目标是 QQ，其余当微信。"""
    target = session.split(":", 1)[1] if ":" in session else session
    target = (target or "").strip()
    if not target:
        return "聊天"
    return "QQ" if target.isdigit() else "微信"


# 像"需要上网查"的词。故意用粗判：宁可少查，也别把每条闲聊都拖慢十几秒。
_AUTO_SEARCH_HINTS = (
    "是什么", "什么是", "为什么", "怎么办", "怎么样", "多少", "多少钱", "多大",
    "哪年", "哪一年", "什么时候", "谁", "在哪", "哪里", "是真的吗", "是不是真的",
    "最新", "新闻", "现在", "今天", "昨天", "这几天", "最近", "今年", "查一下",
    "查查", "搜一下", "搜搜", "天气", "股价", "比分", "结果", "版本", "发布",
    "官网", "怎么用", "有什么区别", "推荐", "评测", "价格", "几号",
)

# 她说话里这些词是称呼她/触发她，搜之前先摘掉，别拿去当关键词。
_SEARCH_NOISE = (
    "肥鱼", "大肥鱼", "吃白饭的肥鱼", "鱼鱼", "小鱼", "本鱼", "帮我", "帮忙",
    "请问", "一下", "知不知道", "你知道吗", "我想知道", "我想问", "告诉我",
)

# 群消息的正文前面会拼上「【昵称】」，这不是搜索词
_NAME_PREFIX_RE = re.compile(r"^【[^】]{0,32}】\s*")

# 明确要她动手查的说法（强信号，命中就查）
_LOOKUP_VERBS = (
    "查一下", "查查", "查下", "查一查", "搜一下", "搜搜", "搜下", "搜一搜",
    "帮我查", "帮我搜", "上网查", "网上查", "百度一下", "谷歌一下", "检索",
)

# 这些字出现在句子里，大概率是在问她本人（"你为什么……"），不该去网上查
_SELF_HINTS = ("你", "妳", "您")


def _looks_like_lookup(text: str) -> bool:
    """粗判这句话是不是在要「外面的事实/最新消息」，值得联网。

    宁可少查：问她本人的话（"你为什么一股东北味"）一律不查，
    否则群里每句闲聊都会触发一轮全后端搜索，白花几秒还搜不到东西。
    """
    t = _NAME_PREFIX_RE.sub("", (text or "").strip())
    if len(t) < 4:
        return False
    if looks_like_url(t):
        return True
    if any(verb in t for verb in _LOOKUP_VERBS):
        return True
    if any(w in t for w in _SELF_HINTS):
        return False
    return any(hint in t for hint in _AUTO_SEARCH_HINTS)


def _search_query(text: str, limit: int = 48) -> str:
    """把她的问句洗成搜索词。"""
    q = clean_text(_NAME_PREFIX_RE.sub("", text or ""))
    for noise in _SEARCH_NOISE:
        q = q.replace(noise, " ")
    q = "".join(ch for ch in q if ch.isprintable() and ch not in "\r\n")
    for mark in "？?！!。.，,、~～:：;；\"'“”‘’()（）[]【】":
        q = q.replace(mark, " ")
    q = " ".join(q.split())
    return q[:limit].strip()


_SUMMARY_META_HINTS = (
    "我们需要",
    "用户要求",
    "需要回答",
    "把下面",
    "聊天记录压",
    "不超过",
    "第三人称",
    "要求：",
    "要求:",
    "请直接输出",
    "输出一段",
    "需要仔细",
    "仔细看",
    "仔细阅读",
    "每句内容",
    "几乎相同",
    "内容几乎",
)

_SUMMARY_LABELS = ("摘要：", "摘要:", "摘要如下：", "总结：", "总结:")


def _tidy_summary(text: str, limit: int = 220) -> str:
    """模型爱先把要求复述一遍再写正文，这里把开头那几行复述掐掉。

    只在"丢完还剩下东西"时才丢，且最多丢到倒数第二行；整段都像复述就原样返回。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    start = 0
    while start < len(lines) - 1 and any(hint in lines[start] for hint in _SUMMARY_META_HINTS):
        start += 1
    out = "\n".join(lines[start:]).strip()
    if not out:
        out = raw
    # 模型常把自己的思考也吐出来，正文一般跟在「摘要：」这类标签后面，取最后一个
    hit_label = False
    for label in _SUMMARY_LABELS:
        idx = out.rfind(label)
        if idx >= 0 and len(out) - idx - len(label) >= 12:
            out = out[idx + len(label) :].strip()
            hit_label = True
            break
    if not hit_label:
        # 没标签时退一步：最后一个冒号后面够长就取它
        idx = max(out.rfind("："), out.rfind(":"))
        if idx >= 0 and len(out) - idx - 1 >= 16:
            out = out[idx + 1 :].strip()
    if not out:
        out = raw
    if len(out) > limit:
        cut = max(out.rfind(mark, 0, limit) for mark in "。！？；\n")
        out = (out[: cut + 1] if cut > 40 else out[:limit]).strip()
    return out


_ADDRESS_HINTS = (
    "你", "妳", "您", "肥鱼", "本鱼", "在吗", "在不在", "帮我", "看看", "瞧瞧",
    "说说", "讲讲", "怎么办", "为什么", "啥", "吗", "呢", "能不能", "可不可以",
    "怎么样", "有没有", "哪个", "谁", "哪里", "多少钱",
)


def _looks_addressed(text: str) -> bool:
    """群里没@她，但这话像是冲她说的吗。"""
    raw = (text or "").strip()
    if not raw:
        return False
    if "？" in raw or "?" in raw:
        return True
    return any(hint in raw for hint in _ADDRESS_HINTS)


def _looks_like_bot(nickname: str) -> bool:
    name = (nickname or "").strip()
    if not name or name in _SYSTEM_CHATS:
        return False
    lowered = name.lower()
    return any(tag in lowered for tag in ("机器人", "bot", "助手", "娘"))

def _pop_ready(buf: str, min_len: int, max_len: int) -> tuple[str, str]:
    """从缓冲区里切出一段可以立即发出去的话。"""
    if len(buf) >= max_len:
        cut = max((buf.rfind(p, 0, max_len) for p in SOFT_BREAKS), default=-1)
        cut = cut + 1 if cut >= min_len else max_len
        return buf[:cut], buf[cut:]
    for i, ch in enumerate(buf):
        if i + 1 >= min_len and ch in SENTENCE_ENDS:
            return buf[: i + 1], buf[i + 1 :]
    return "", buf


__all__ = ["FishBot", "OneBotClient"]
