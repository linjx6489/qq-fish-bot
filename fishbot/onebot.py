"""OneBot v11 协议客户端（NapCat / Lagrange / LLOneBot 通用）。

支持两种接入方式：
  * forward —— 机器人主动连接 QQ 端的 WebSocket 服务端（默认，最省事）
  * reverse —— 机器人起 WS 服务端，让 QQ 端反向连过来
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import InvalidStatus

from .config import OneBotConfig
from .textutil import at_segment, reply_segment, text_segment

log = logging.getLogger("fishbot.onebot")

EventHandler = Callable[[dict], Awaitable[None]]


class OneBotOffline(RuntimeError):
    """还没连上 QQ 端。"""


class OneBotClient:
    def __init__(self, cfg: OneBotConfig, on_event: EventHandler):
        self.cfg = cfg
        self._on_event = on_event
        self.self_id: str | None = None
        self._conn: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self.connected_at: float | None = None
        self.events_received = 0
        self.actions_sent = 0

    # ---------------------------------------------------------------- 状态
    @property
    def online(self) -> bool:
        return self._conn is not None

    def status_text(self) -> str:
        if not self.online:
            return "离线"
        secs = int(time.time() - (self.connected_at or time.time()))
        return f"在线（self_id={self.self_id or '未知'}，已连接 {secs}s）"

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._stopping = False
        if self.cfg.mode == "reverse":
            self._spawn(self._run_reverse())
        else:
            self._spawn(self._run_forward())

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- forward
    async def _run_forward(self) -> None:
        heads = {}
        if self.cfg.access_token:
            heads["Authorization"] = f"Bearer {self.cfg.access_token}"
        attempt = 0
        while not self._stopping:
            try:
                log.info("连接 QQ 端：%s", self.cfg.ws_url)
                async with connect(
                    self.cfg.ws_url,
                    additional_headers=heads or None,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=15,
                    max_size=16 * 1024 * 1024,
                    proxy=None,
                ) as conn:
                    self._conn = conn
                    self.connected_at = time.time()
                    attempt = 0
                    log.info("已连接 QQ 端 ✔")
                    await self._read_loop(conn)
            except InvalidStatus as exc:
                log.error("握手被拒绝（%s）—— 请检查 access_token 是否与 QQ 端一致", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("连接断开：%s: %s", type(exc).__name__, exc)
            finally:
                self._conn = None
                self.connected_at = None
            if self._stopping:
                break
            attempt += 1
            delay = min(self.cfg.reconnect_max_seconds, 1.5 ** min(attempt, 6))
            log.info("%.1fs 后重连……", delay)
            await asyncio.sleep(delay)

    # ---------------------------------------------------------------- reverse
    async def _run_reverse(self) -> None:
        expected = self.cfg.access_token

        def check(conn: ServerConnection, request) -> Any:
            path = getattr(request, "path", "/") or "/"
            if self.cfg.listen_path and not path.split("?")[0].endswith(self.cfg.listen_path):
                return conn.respond(404, "Not Found\n")
            if expected:
                headers = getattr(request, "headers", {}) or {}
                auth = headers.get("authorization") or headers.get("Authorization") or ""
                query = path.split("?", 1)[1] if "?" in path else ""
                token_ok = auth == f"Bearer {expected}" or f"access_token={expected}" in query
                if not token_ok:
                    log.warning("反向连接 token 校验失败：%s", path)
                    return conn.respond(401, "Unauthorized\n")
            return None

        log.info("反向 WS 监听 %s:%s%s", self.cfg.listen_host, self.cfg.listen_port, self.cfg.listen_path)

        async def handler(conn: ServerConnection) -> None:
            self._conn = conn
            self.connected_at = time.time()
            log.info("QQ 端已反向接入 ✔")
            try:
                await self._read_loop(conn)
            finally:
                if self._conn is conn:
                    self._conn = None
                    self.connected_at = None
                log.info("反向连接结束")

        async with serve(handler, self.cfg.listen_host, self.cfg.listen_port, process_request=check):
            await asyncio.Future()  # 一直服务

    # ---------------------------------------------------------------- 收包
    async def _read_loop(self, conn) -> None:
        async for raw in conn:
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                log.debug("无法解析的报文：%r", str(raw)[:200])
                continue
            if not isinstance(payload, dict):
                continue
            echo = payload.get("echo")
            if echo and echo in self._pending:
                fut = self._pending.pop(echo)
                if not fut.done():
                    fut.set_result(payload)
                continue
            if payload.get("post_type") in ("message", "notice", "request", "meta_event"):
                self.events_received += 1
                if payload.get("self_id"):
                    self.self_id = str(payload["self_id"])
                if payload.get("post_type") == "meta_event":
                    if payload.get("meta_event_type") == "lifecycle":
                        log.info("生命周期事件：%s", payload.get("sub_type"))
                    continue
                task = asyncio.create_task(self._safe_event(payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                log.debug("忽略报文：%s", str(payload)[:200])

    async def _safe_event(self, payload: dict) -> None:
        try:
            await self._on_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("处理事件失败：%s", payload.get("post_type"))

    # ---------------------------------------------------------------- 发包
    async def call(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        if self._conn is None:
            raise OneBotOffline("尚未连接 QQ 端")
        echo = uuid.uuid4().hex
        payload = {"action": action, "params": params or {}, "echo": echo}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            async with self._write_lock:
                await self._conn.send(json.dumps(payload, ensure_ascii=False))
            self.actions_sent += 1
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise OneBotOffline(f"{action} 超时（{timeout}s）") from None
        finally:
            self._pending.pop(echo, None)
        if str(result.get("status")) not in ("ok", "async") and result.get("retcode") not in (0, 1, None):
            raise OneBotOffline(f"{action} 失败：{result.get('message') or result}")
        return result.get("data") or {}

    # ---------------------------------------------------------------- 便捷发送
    async def send_private(self, user_id: str, segments: list[dict]) -> dict:
        return await self.call("send_private_msg", {"user_id": int(user_id), "message": segments})

    async def send_group(self, group_id: str, segments: list[dict]) -> dict:
        return await self.call("send_group_msg", {"group_id": int(group_id), "message": segments})

    async def send(self, kind: str, target_id: str, segments: list[dict]) -> dict:
        if kind == "group":
            return await self.send_group(target_id, segments)
        return await self.send_private(target_id, segments)

    async def get_login_info(self) -> dict:
        return await self.call("get_login_info")

    async def get_group_info(self, group_id: str) -> dict:
        return await self.call("get_group_info", {"group_id": int(group_id), "no_cache": False})


def record_segment(path: str) -> dict:
    """把本地音频文件包成 OneBot 的 record 消息段（NapCat 会自己转 silk）。"""
    raw = str(path)
    if raw.startswith(("http://", "https://", "file://", "base64://")):
        uri = raw
    else:
        uri = "file:///" + raw.replace("\\", "/")
    return {"type": "record", "data": {"file": uri}}


def image_segment(path: str) -> dict:
    """把本地图片包成 OneBot 的 image 消息段（表情包走这条路）。"""
    raw = str(path)
    if raw.startswith(("http://", "https://", "file://", "base64://")):
        uri = raw
    else:
        uri = "file:///" + raw.replace("\\", "/")
    return {"type": "image", "data": {"file": uri}}


def build_reply_segments(
    text: str,
    *,
    at_user: str | None = None,
    reply_to: str | None = None,
) -> list[dict]:
    """组装出站消息段。"""
    segments: list[dict] = []
    if reply_to:
        segments.append(reply_segment(reply_to))
    if at_user:
        segments.append(at_segment(at_user))
        segments.append(text_segment(" "))
    segments.append(text_segment(text))
    return segments
