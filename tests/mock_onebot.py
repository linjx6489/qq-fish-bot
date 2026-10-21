"""测试用的假 QQ 端：实现 OneBot v11 正向 WebSocket 服务端。

- 接收机器人发来的 action 调用并记录（send_private_msg / send_group_msg ...）
- 允许测试脚本向机器人注入事件
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from mock_openai import free_port


class MockOneBot:
    def __init__(self, self_id: int = 3000000) -> None:
        self.port = free_port()
        self.self_id = self_id
        self.actions: list[dict] = []
        self.connections = 0
        self._conn: ServerConnection | None = None
        self._server = None
        self._message_id = 1000

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, conn: ServerConnection) -> None:
        self._conn = conn
        self.connections += 1
        try:
            async for raw in conn:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                self.actions.append(payload)
                echo = payload.get("echo")
                reply = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": self._next_id()},
                    "echo": echo,
                }
                await conn.send(json.dumps(reply))
        except ConnectionClosed:
            pass  # 测试结束时机器人被强杀，属正常收尾
        finally:
            if self._conn is conn:
                self._conn = None

    def _next_id(self) -> int:
        self._message_id += 1
        return self._message_id

    # ---------------------------------------------------------------- 测试接口
    async def wait_connection(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.connections > 0 and self._conn is not None:
                return True
            await asyncio.sleep(0.05)
        return False

    async def inject(self, event: dict) -> None:
        if self._conn is None:
            raise RuntimeError("机器人还没连上")
        event.setdefault("self_id", self.self_id)
        event.setdefault("post_type", "message")
        event.setdefault("time", int(time.time()))
        await self._conn.send(json.dumps(event, ensure_ascii=False))

    def mark(self) -> int:
        return len(self.actions)

    async def collect(self, mark: int, count: int = 1, timeout: float = 15.0) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.actions) - mark >= count:
                break
            await asyncio.sleep(0.05)
        return self.actions[mark:]

    async def quiet(self, mark: int, seconds: float = 1.2) -> list[dict]:
        """等待一段时间，返回期间机器人的所有动作（期望是空的）。"""
        await asyncio.sleep(seconds)
        return self.actions[mark:]

    async def wait_for(self, pred: Callable[[dict], bool], timeout: float = 15.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for action in self.actions:
                if pred(action):
                    return action
            await asyncio.sleep(0.05)
        return None


# ------------------------------------------------------------------ 事件构造
def private_event(text: str, user_id: int = 10001, nickname: str = "小鱼干", message_id: int = 1) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": user_id,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user_id, "nickname": nickname},
    }


def group_event(
    text: str,
    *,
    group_id: int = 88888,
    user_id: int = 10002,
    nickname: str = "路人甲",
    role: str = "member",
    at_bot: bool = False,
    self_id: int = 3000000,
    message_id: int = 2,
) -> dict:
    message: list[dict] = []
    if at_bot:
        message.append({"type": "at", "data": {"qq": str(self_id)}})
        message.append({"type": "text", "data": {"text": " "}})
    message.append({"type": "text", "data": {"text": text}})
    raw = ("[CQ:at,qq=%d] " % self_id if at_bot else "") + text
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": group_id,
        "user_id": user_id,
        "raw_message": raw,
        "message": message,
        "sender": {"user_id": user_id, "nickname": nickname, "card": "", "role": role},
    }


def action_text(action: dict[str, Any]) -> str:
    """把 send_*_msg 的 params 还原成纯文本。"""
    params = action.get("params") or {}
    message = params.get("message") or []
    if isinstance(message, str):
        return message
    parts: list[str] = []
    for seg in message:
        if isinstance(seg, dict) and seg.get("type") == "text":
            parts.append(str((seg.get("data") or {}).get("text", "")))
    return "".join(parts)
