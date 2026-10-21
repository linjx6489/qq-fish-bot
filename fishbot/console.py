"""本地控制台调试通道：不接 QQ 也能和鱼聊天。"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .textutil import at_segment, clean_text, text_segment


class ConsoleTransport:
    """把出站消息打印到终端，把 stdin 的输入当作私聊消息。"""

    mode = "console"

    def __init__(self, self_id: str = "10000", user_id: str = "10001", nickname: str = "主人"):
        self.self_id = self_id
        self.user_id = user_id
        self.nickname = nickname
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_stdin())

    async def stop(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None

    @property
    def online(self) -> bool:
        return True

    def status_text(self) -> str:
        return "本地控制台模式"

    # ------------------------------------------------------------ 收发
    async def send(self, kind: str, target_id: str, segments: list[dict]) -> dict:
        sys.stdout.write("\n\033[36m肥鱼>\033[0m " + _render(segments) + "\n")
        sys.stdout.flush()
        return {"message_id": 0}

    async def _read_stdin(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                await asyncio.sleep(0.2)
                continue
            line = clean_text(line.strip())
            if line:
                await self._queue.put(line)

    async def next_event(self) -> dict[str, Any] | None:
        """阻塞等待用户输入，组装成一条 OneBot 私聊事件。"""
        text = await self._queue.get()
        if text in ("/quit", "/exit", "/退出"):
            return None
        return {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 1,
            "user_id": int(self.user_id),
            "self_id": int(self.self_id),
            "raw_message": text,
            "message": [text_segment(text)],
            "sender": {"user_id": int(self.user_id), "nickname": self.nickname},
        }


def _render(segments: list[dict]) -> str:
    out: list[str] = []
    for seg in segments:
        seg_type = seg.get("type")
        if seg_type == "text":
            out.append(str((seg.get("data") or {}).get("text", "")))
        elif seg_type == "at":
            out.append(f"@{(seg.get('data') or {}).get('qq')}")
        elif seg_type == "reply":
            out.append("")
    return "".join(out).strip()


__all__ = ["ConsoleTransport", "at_segment"]
