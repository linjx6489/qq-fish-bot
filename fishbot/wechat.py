"""微信接入：把 wx_bridge 子进程的 JSON 行协议包成一个 OneBot 形状的传输层。

为什么是子进程：wechatauto 依赖 winsdk/uiautomation，只装得进 Python 3.12；
机器人本体跑在 3.14。所以把微信驱动挂在旁边，用 stdin/stdout 对话。

设计要点
--------
* 事件形状对齐 OneBot v11 —— 于是 ``FishBot._handle_message`` 一行都不用改，
  微信消息和 QQ 消息走完全同一套策略、记忆、人设、命令。
* 会话命名：私聊 ``private:<wxid>``、群聊 ``group:<xxx@chatroom>``。
  wxid 不是纯数字，所以 Router 用「是不是纯数字」就能分清 QQ 和微信。
* 微信侧暂时不发语音（``supports_voice = False``），语音段会被降级成文字。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, WeChatConfig

log = logging.getLogger("fishbot.wechat")

EventHandler = Callable[[dict], Any]


class WeChatOffline(RuntimeError):
    """桥接进程不可用。"""


def _clean_env() -> dict:
    """本机的 http_proxy / NO_PROXY 是坏的（端口残缺、大小写冲突），别传给子进程。"""
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class WeChatTransport:
    """保持一个 wx_bridge 子进程常驻，收发都走它。"""

    supports_voice = False  # 微信侧不发语音，交给 bot 决定降级

    def __init__(self, cfg: WeChatConfig, on_event: EventHandler) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.self_id: str = ""
        self.nick: str = ""
        self.sent = 0
        self.received = 0
        self.failed = 0
        self.restarts = 0
        self.last_error = ""

        self._proc: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task | None = None
        self._stopping = False
        self._ready = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._seq = 0
        self._log_path = Path(self.cfg.log_file)
        if not self._log_path.is_absolute():
            self._log_path = ROOT / self._log_path

    # ------------------------------------------------------------ 状态
    @property
    def online(self) -> bool:
        return bool(self._proc and self._proc.returncode is None and self._ready.is_set())

    def status_text(self) -> str:
        if not self.cfg.enabled:
            return "未开启"
        if self._proc is None:
            return "未启动"
        if self._proc.returncode is not None:
            return f"已退出（code {self._proc.returncode}）"
        if not self._ready.is_set():
            return "启动中……"
        return f"已接入 {self.nick or self.self_id}"

    def voice_ok_for(self, target_id: Any) -> bool:
        return False

    def _bump(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if not self.cfg.enabled:
            log.info("微信接入：未开启")
            return
        script = Path(self.cfg.script)
        if not script.exists():
            log.error("微信接入：找不到桥接脚本 %s", script)
            self.last_error = f"找不到 {script}"
            return
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise(), name="wechat-supervisor")
        log.info("微信接入：启动桥接进程（%s）", script.name)

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor = None
        await self._kill_proc()

    async def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        reader, self._reader = self._reader, None
        self._ready.clear()
        if reader is not None and not reader.done():
            reader.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(WeChatOffline("桥接已停止"))
        self._pending.clear()
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def _supervise(self) -> None:
        """桥接崩了就拉起来，退避重连。"""
        backoff = 5.0
        while not self._stopping:
            try:
                await self._spawn()
                backoff = 5.0
                await self._wait_proc()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("微信桥接异常：%s", self.last_error)
            if self._stopping:
                break
            self.restarts += 1
            log.warning("微信桥接断开，%.0f 秒后重启（第 %d 次）", backoff, self.restarts)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 1.7, 60.0)

    async def _read_loop(self, proc) -> None:
        """把桥接的 stdout 一路读干，直到 EOF。

        这件事必须在等 ready 之前就开起来：桥接的握手是往 stdout 写一行
        JSON，如果没人读，行就躺在管道缓冲区里，永远等不到。
        """
        out = proc.stdout
        if out is None:
            return
        try:
            while True:
                line = await out.readline()
                if not line:
                    break
                self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("读桥接输出出错：%s", exc)

    async def _wait_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        await proc.wait()

    async def _spawn(self) -> None:
        self._ready.clear()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        err_file = open(self._log_path, "ab")
        argv = [self.cfg.python, "-u", self.cfg.script,
                "--interval", str(self.cfg.interval)]
        if self.cfg.echo_self:
            argv.append("--echo-self")
        if self.cfg.verify:
            argv.append("--verify")
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=err_file,
            env=_clean_env(),
            cwd=str(Path(self.cfg.script).parent),
        )
        log.info("微信桥接已拉起（pid %s），等它报 ready……", self._proc.pid)
        self._reader = asyncio.create_task(self._read_loop(self._proc), name="wechat-read")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.cfg.start_timeout)
        except asyncio.TimeoutError:
            log.error("微信桥接 %.0f 秒没就绪，日志见 %s", self.cfg.start_timeout, self._log_path)
            self.last_error = "桥接启动超时"
            await self._kill_proc()
            raise WeChatOffline("启动超时")
        log.info("微信桥接就绪：%s（%s）", self.nick or "?", self.self_id or "?")

    # ------------------------------------------------------------ 收
    def _handle_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            ev = json.loads(text)
        except Exception:
            log.debug("微信桥接输出无法解析：%s", text[:200])
            return
        kind = ev.get("event")
        if kind == "msg":
            self.received += 1
            self._dispatch(ev)
        elif kind in ("sent", "pong", "status"):
            fut = self._pending.pop(ev.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(ev)
        elif kind == "ready":
            self.self_id = str(ev.get("self_wxid") or "")
            self.nick = str(ev.get("nick") or "")
            self._ready.set()
        elif kind == "error":
            self.last_error = str(ev.get("error") or "")[:400]
            log.warning("微信桥接报错（%s）：%s", ev.get("where"), self.last_error[:200])
        elif kind == "bye":
            log.info("微信桥接正常退出")
        else:
            log.debug("微信桥接未知事件：%s", text[:200])

    def _dispatch(self, ev: dict) -> None:
        chat = str(ev.get("chat") or "")
        chat_name = str(ev.get("chat_name") or "")
        is_group = bool(ev.get("is_group"))
        sender = str(ev.get("sender") or "")
        sender_name = str(ev.get("sender_name") or "")
        content = str(ev.get("content") or "")
        ts = int(ev.get("ts") or time.time())
        if not chat or not content:
            return
        if ev.get("is_self"):
            # 绝不能把机器人自己的发言当成别人发来的，否则它会自问自答停不下来。
            # 桥接默认就不上报自己的消息（echo_self=false），这里再兜一道。
            return
        if ts < float(self.cfg.ignore_before or 0):
            return
        if not self._allowed(chat, chat_name, is_group, sender_name):
            log.debug("微信消息被白/黑名单挡下：%s / %s", chat_name or chat, sender_name)
            return

        onebot = {
            "post_type": "message",
            "message_type": "group" if is_group else "private",
            "self_id": self.self_id,
            "user_id": sender or chat,
            "time": ts,
            "message_id": ev.get("local_id"),
            "raw_message": content,
            "message": [{"type": "text", "data": {"text": content}}],
            "sender": {"nickname": sender_name or chat_name, "card": sender_name},
        }
        if is_group:
            onebot["group_id"] = chat
        asyncio.ensure_future(self._feed(onebot))

    async def _feed(self, event: dict) -> None:
        try:
            result = self.on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("微信消息处理出错")

    def _allowed(self, chat: str, chat_name: str, is_group: bool, sender_name: str) -> bool:
        if is_group and not self.cfg.group_enabled:
            return False
        if not is_group and not self.cfg.private_enabled:
            return False
        tokens = {chat, chat_name, sender_name} - {""}
        deny = set(self.cfg.deny_chats or [])
        if tokens & deny:
            return False
        allow = self.cfg.allow_chats or []
        if allow and not (tokens & set(allow)):
            return False
        return True

    # ------------------------------------------------------------ 发
    async def _command(self, payload: dict, timeout: float = 180.0) -> dict:
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise WeChatOffline("桥接进程不可用")
        rid = self._bump()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        blob = json.dumps({**payload, "id": rid}, ensure_ascii=False) + "\n"
        proc.stdin.write(blob.encode("utf-8"))
        await proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def send(self, kind: str, target_id: str, segments: list[dict]) -> dict:
        parts: list[str] = []
        for seg in segments or []:
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text") or ""))
            elif stype == "at":
                who = str(data.get("qq") or "")
                parts.append(f"@{who}")
            # 语音/图片/表情：微信侧暂不支持，直接丢掉
        # 微信是按回车发送的，换行会把一条消息切成好几条，压成空格
        text = " ".join("".join(parts).split())
        if not text:
            return {"status": "failed", "message": "没有可发送的文本"}
        try:
            res = await self._command({"cmd": "send", "to": str(target_id), "text": text})
        except (WeChatOffline, asyncio.TimeoutError) as exc:
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"status": "failed", "message": self.last_error}
        if res.get("ok"):
            self.sent += 1
            return {"status": "ok", "message": "", "message_id": res.get("id")}
        self.failed += 1
        self.last_error = str(res.get("error") or "发送失败")
        return {"status": "failed", "message": self.last_error}

    async def send_private(self, user_id: str, segments: list[dict]) -> dict:
        return await self.send("private", str(user_id), segments)

    async def send_group(self, group_id: str, segments: list[dict]) -> dict:
        return await self.send("group", str(group_id), segments)

    async def ping(self) -> bool:
        try:
            await self._command({"cmd": "ping"}, timeout=20)
            return True
        except Exception:
            return False

    def stats(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "online": self.online,
            "account": self.nick or self.self_id,
            "sent": self.sent,
            "received": self.received,
            "failed": self.failed,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }
