"""自主活动：让鲸鲸自己折腾 QQ，而不是干等着别人说话。

设计原则：**会打扰别人的动作必须显式开白名单**，改签名、记心情这类
自娱自乐的动作默认允许。所有结果都写进记忆库，下次聊天时她还能想起来。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any

from .config import AutonomyConfig
from .onebot import OneBotOffline, build_reply_segments
from .textutil import sanitize_output

log = logging.getLogger("fishbot.autonomy")

KV_SIGNATURE = "autonomy:signature"
KV_MOOD = "autonomy:mood"
KV_DIARY = "autonomy:diary"
KV_LAST = "autonomy:last"

SILENT_TOKENS = ("[沉默]", "沉默", "（不说话）", "(不说话)", "（沉默）", "[无语]")


class Autonomy:
    def __init__(self, bot: Any, cfg: AutonomyConfig):
        self.bot = bot
        self.cfg = cfg
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._hour_bucket: deque[float] = deque()
        self._recent: deque[dict] = deque(maxlen=20)
        self.counters = {"acts": 0, "failed": 0, "skipped": 0, "manual": 0}
        self.last_action: dict | None = None
        self.pending_reason: str = ""
        self._sig_at: float = 0.0  # 上次自己改签名的时间

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        if not self.cfg.enabled:
            log.info("自主活动：关闭（config.yaml 里 autonomy.enabled 打开即可）")
            return
        record = await self.bot.mem.kv_get(KV_SIGNATURE)
        self._sig_at = float(record.get("at") or 0.0) if isinstance(record, dict) else 0.0
        self._stopping = False
        self._task = asyncio.create_task(self._loop())
        log.info(
            "自主活动：开启 —— 每 %.0f 分钟 ±%d%%，每小时最多 %d 次，候选动作 %s",
            self.cfg.interval_minutes,
            int(self.cfg.jitter * 100),
            self.cfg.max_actions_per_hour,
            "、".join(self.cfg.actions) or "无",
        )

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        # 刚启动时先安分一会儿，别一上线就开始折腾
        await asyncio.sleep(max(20.0, min(180.0, self.cfg.interval_minutes * 60 * 0.2)))
        while not self._stopping:
            try:
                await self.run_once(reason="auto")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.counters["failed"] += 1
                log.exception("自主活动循环出错")
            try:
                await asyncio.sleep(self._next_delay())
            except asyncio.CancelledError:
                raise

    def _next_delay(self) -> float:
        base = self.cfg.interval_minutes * 60.0
        spread = base * self.cfg.jitter
        return max(30.0, random.uniform(base - spread, base + spread))

    # ---------------------------------------------------------------- 闸门
    def _in_quiet_hours(self) -> bool:
        window = self.cfg.parsed_quiet_hours()
        if window is None:
            return False
        start, end = window
        if start == end:
            return False
        hour = time.localtime().tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _hourly_ok(self) -> bool:
        limit = self.cfg.max_actions_per_hour
        if limit <= 0:
            return True
        now = time.time()
        while self._hour_bucket and now - self._hour_bucket[0] > 3600:
            self._hour_bucket.popleft()
        return len(self._hour_bucket) < limit

    def _idle_enough(self) -> bool:
        """刚有人跟她说过话，就先别自顾自玩。"""
        minutes = self.cfg.min_idle_minutes
        if minutes <= 0:
            return True
        last_reply = getattr(self.bot, "_last_reply", None) or {}
        if not last_reply:
            return True
        return (time.time() - max(last_reply.values())) >= minutes * 60

    def _available(self, action: str) -> bool:
        if action == "signature":
            return self.cfg.allow_profile_edit and self._signature_due()
        if action == "nickname":
            return self.cfg.allow_profile_edit and self.cfg.allow_nickname_change
        if action == "chat":
            return bool([t for t in self.cfg.proactive_private_allow if t and t != self.bot.self_id])
        if action == "group":
            return bool([g for g in self.cfg.proactive_group_allow if g])
        if action == "diary":
            return True
        return False

    def _signature_due(self) -> bool:
        """签名限流：`signature_interval_days` 天内只准她自己动一次。"""
        days = float(getattr(self.cfg, "signature_interval_days", 0) or 0)
        if days <= 0:
            return True
        return (time.time() - self._sig_at) >= days * 86400

    def signature_wait_text(self) -> str:
        left = self._sig_at + float(self.cfg.signature_interval_days or 0) * 86400 - time.time()
        if not self._sig_at or left <= 0:
            return ""
        return f"签名还要 {left / 86400:.1f} 天才轮得到再改"

    def _pick_action(self) -> str | None:
        pool = [a for a in self.cfg.actions if self._available(a)]
        if not pool:
            return None
        # 刚做过的动作降权，避免连续改两次签名
        if self.last_action and len(pool) > 1:
            pool = [a for a in pool if a != self.last_action.get("action")] or pool
        return random.choice(pool)

    # ---------------------------------------------------------------- 主流程
    async def run_once(self, *, force: bool = False, reason: str = "auto") -> str | None:
        self.pending_reason = ""
        transport = self.bot.transport
        if transport is None or not getattr(transport, "online", False):
            self.counters["skipped"] += 1
            self.pending_reason = "还没连上 QQ"
            return None
        if not force:
            if self._in_quiet_hours():
                self.counters["skipped"] += 1
                self.pending_reason = "现在是安静时段"
                return None
            if not self._hourly_ok():
                self.counters["skipped"] += 1
                self.pending_reason = "这个小时已经玩够了"
                return None
            if not self._idle_enough():
                self.counters["skipped"] += 1
                self.pending_reason = "刚有人跟她说过话，先不打扰"
                return None

        action = self._pick_action()
        if action is None:
            self.counters["skipped"] += 1
            self.pending_reason = "没开任何可玩的项目"
            return None
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            log.warning("未知的自主活动：%s", action)
            self.counters["skipped"] += 1
            self.pending_reason = f"不认识动作 {action}"
            return None

        try:
            result = await handler()
        except OneBotOffline as exc:
            result = None
            self.counters["failed"] += 1
            self.pending_reason = "QQ 连接断了"
            log.warning("自主活动[%s]失败：%s", action, exc)
        except Exception as exc:  # noqa: BLE001
            result = None
            self.counters["failed"] += 1
            self.pending_reason = f"{action} 出错：{exc}"
            log.warning("自主活动[%s]出错：%s: %s", action, type(exc).__name__, exc)

        if not result:
            if not self.pending_reason:
                self.pending_reason = f"{action} 这次没酝酿出内容"
            return None

        self._hour_bucket.append(time.time())
        self.counters["acts"] += 1
        if reason == "manual":
            self.counters["manual"] += 1
        record = {"action": action, "result": result, "at": time.time()}
        self.last_action = record
        self._recent.append(record)
        await self.bot.mem.kv_set(KV_LAST, record)
        log.info("自主活动[%s]：%s", action, result)
        return result

    # ---------------------------------------------------------------- 具体动作
    async def _act_signature(self) -> str | None:
        info = await self.bot.transport.call("get_login_info")
        nickname = str(info.get("nickname") or self.bot.persona.cfg.name)
        text = await self._ask(
            "写一句你现在的 QQ 个性签名，符合你的性格和此刻心情。"
            "别写跟白饭、吃饭、白吃白喝有关的内容（那是名字里的梗，天天念就不好笑了），"
            "写点别的：此刻的心情、看到的事情、想做的事、困了想睡都行。",
            max_chars=24,
        )
        if not text:
            return None
        # nickname 是必填字段，必须带上，否则昵称会被清空
        await self.bot.transport.call(
            "set_qq_profile", {"nickname": nickname, "personal_note": text}
        )
        await self.bot.mem.kv_set(KV_SIGNATURE, {"text": text, "at": time.time()})
        self._sig_at = time.time()
        return f"把个性签名改成「{text}」"

    async def _act_nickname(self) -> str | None:
        text = await self._ask("给自己换一个新昵称，还是「鲸鲸」的味道但换个说法。", max_chars=12)
        if not text:
            return None
        await self.bot.transport.call("set_qq_profile", {"nickname": text})
        return f"把昵称改成「{text}」"

    async def _act_diary(self) -> str | None:
        today = await self.bot.mem.usage_summary(since=_day_start())
        tokens = int(today.get("total_tokens") or 0)
        text = await self._ask(
            f"你今天已经偷吃了 {tokens} 个 token。写一句只有你自己看的心情记录。"
            "别写白饭、吃饭这类梗，写点真的：今天遇到的事、谁让你高兴或无语、你在琢磨什么。",
            max_chars=60,
        )
        if not text:
            return None
        await self.bot.mem.kv_set(KV_MOOD, {"text": text, "at": time.time(), "tokens": tokens})
        await self.bot.mem.kv_set(KV_DIARY, {"text": text, "at": time.time(), "tokens": tokens})
        return f"记了条心情：{text}"

    async def _resumable_tail(self, session: str) -> str | None:
        """判断这个会话现在适不适合接话，合适就返回话尾。

        - 从没聊过 → 不接（不贸然搭话）
        - 最后一句是自己说的 → 要等满 `follow_up_after_hours` 才回访（不追着刷屏）
        - 最后一句是对方说的 → 可以接（对方的话还悬着）
        """
        info = await self.bot.mem.last_role_and_age(session)
        if info is None:
            return None
        role, age = info
        if role == "assistant" and age < self.cfg.follow_up_after_hours * 3600:
            return None
        history = await self.bot.mem.history(session)
        if not history:
            return None
        return str(history[-1].get("content") or "")[:60] or None

    async def _act_chat(self) -> str | None:
        targets = [t for t in self.cfg.proactive_private_allow if t and t != self.bot.self_id]
        if not targets:
            return None
        random.shuffle(targets)
        for uid in targets:
            session = f"private:{uid}"
            tail = await self._resumable_tail(session)
            if not tail:
                continue
            history = await self.bot.mem.history(session)
            text = await self._ask(
                f"你想主动找对方说句话。对方最后说的是：「{tail}」",
                history=history,
                max_chars=self.bot.cfg.persona.max_chars,
            )
            if not text:
                continue
            await self.bot.transport.send_private(uid, build_reply_segments(text))
            await self.bot.mem.add(session, "assistant", text, name=self.bot.persona.cfg.name)
            return f"主动找 {uid} 搭话：{text}"
        return None

    async def _act_group(self) -> str | None:
        groups = [g for g in self.cfg.proactive_group_allow if g]
        if not groups:
            return None
        random.shuffle(groups)
        for gid in groups:
            session = f"group:{gid}"
            tail = await self._resumable_tail(session)
            if not tail:
                continue
            history = await self.bot.mem.history(session)
            text = await self._ask(
                f"你想在群里冒个泡。群里最后一句是：「{tail}」",
                history=history,
                max_chars=self.bot.cfg.persona.max_chars,
            )
            if not text:
                continue
            await self.bot.transport.send_group(gid, build_reply_segments(text))
            await self.bot.mem.add(session, "assistant", text, name=self.bot.persona.cfg.name)
            return f"在群 {gid} 冒泡：{text}"
        return None

    # ---------------------------------------------------------------- 内部
    async def _ask(
        self,
        instruction: str,
        *,
        history: list[dict] | None = None,
        max_chars: int = 60,
    ) -> str | None:
        system = self.bot.persona.system_prompt(
            scene="self",
            speaker="你自己",
            extra_note=f"{instruction} 只输出这句话本身，不要解释、不要引号、不要超过 {max_chars} 字。",
            hunger=self.bot._hunger(),
            max_chars=max_chars,
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        if history:
            messages.extend(history[-6:])
            messages.append({"role": "user", "content": f"（{instruction}）"})
        else:
            messages.append({"role": "user", "content": "（现在是你的自由活动时间，没人跟你说话）"})
        result = await self.bot.llm.chat(messages)
        if not result.ok:
            return None
        text = sanitize_output(result.text, max(max_chars * 2, 40))
        if not text or text.strip() in SILENT_TOKENS:
            return None
        return text

    # ---------------------------------------------------------------- 对外
    def recent(self, limit: int = 5) -> list[dict]:
        return list(self._recent)[-limit:]

    def status_text(self) -> str:
        if not self.cfg.enabled:
            return "自主活动：关闭"
        lines = [
            f"自主活动：开启（每 {self.cfg.interval_minutes:.0f} 分钟，每小时 ≤{self.cfg.max_actions_per_hour} 次）",
            f"候选动作：{'、'.join(self.cfg.actions) or '无'}",
            f"已折腾 {self.counters['acts']} 次，跳过 {self.counters['skipped']} 次，失败 {self.counters['failed']} 次",
        ]
        if wait := self.signature_wait_text():
            lines.append(wait)
        if self._in_quiet_hours():
            lines.append("现在处于静默时段，暂时不动")
        if self.last_action:
            lines.append(f"最近一次：{self.last_action['result']}")
        return "\n".join(lines)

    def stats(self) -> dict:
        return {**self.counters, "enabled": self.cfg.enabled, "recent": self.recent(3)}


def _day_start() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


__all__ = ["Autonomy"]
