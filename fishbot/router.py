"""传输层路由：把 QQ（OneBot）和微信两个通道合成一个交接口。

给 ``FishBot`` 看到的东西和单个 transport 完全一样（``start/stop/online/
status_text/send/send_private/send_group``），内部按目标形状分派：

* **纯数字** 目标 → QQ（QQ 号和群号都是数字）
* **其他** 目标 → 微信（wxid、``xxx@chatroom``、``filehelper`` 都带非数字字符）

哪边缺席就整体退到另一边，所以只开 QQ 时行为和以前一模一样。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("fishbot.router")


class TransportRouter:
    def __init__(self, qq: Any = None, wechat: Any = None) -> None:
        self.qq = qq
        self.wechat = wechat

    # ------------------------------------------------------------ 状态
    @property
    def self_id(self) -> str:
        return str(getattr(self.qq, "self_id", "") or getattr(self.wechat, "self_id", "") or "")

    @property
    def online(self) -> bool:
        return bool(self.any_online())

    def any_online(self) -> bool:
        for t in (self.qq, self.wechat):
            if t is not None and getattr(t, "online", False):
                return True
        return False

    def each(self) -> list[Any]:
        return [t for t in (self.qq, self.wechat) if t is not None]

    def status_text(self) -> str:
        bits: list[str] = []
        if self.qq is not None:
            bits.append(f"QQ：{self._text_of(self.qq)}")
        if self.wechat is not None:
            bits.append(f"微信：{self._text_of(self.wechat)}")
        return " ｜ ".join(bits) if bits else "未接入"

    @staticmethod
    def _text_of(t: Any) -> str:
        try:
            fn = getattr(t, "status_text", None)
            if callable(fn):
                return str(fn())
        except Exception as exc:
            return f"状态未知（{type(exc).__name__}）"
        return "已接入" if getattr(t, "online", False) else "未接入"

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        for t in self.each():
            try:
                await t.start()
            except Exception:
                log.exception("传输层启动失败：%r", type(t).__name__)

    async def stop(self) -> None:
        for t in self.each():
            try:
                await t.stop()
            except Exception:
                log.exception("传输层停止失败：%r", type(t).__name__)

    # ------------------------------------------------------------ 分派
    def is_qq_target(self, target_id: Any) -> bool:
        return str(target_id or "").strip().isdigit()

    def pick(self, target_id: Any) -> Any:
        qq, wx = self.qq, self.wechat
        if self.is_qq_target(target_id):
            return qq or wx
        return wx or qq

    def voice_ok_for(self, target_id: Any) -> bool:
        t = self.pick(target_id)
        fn = getattr(t, "voice_ok_for", None)
        if callable(fn):
            try:
                return bool(fn(target_id))
            except Exception:
                return True
        return bool(getattr(t, "supports_voice", True))

    # ------------------------------------------------------------ 发送
    async def send(self, kind: str, target_id: str, segments: list[dict]) -> dict:
        t = self.pick(target_id)
        if t is None:
            return {"status": "failed", "message": "没有可用的传输层"}
        try:
            return await t.send(kind, target_id, segments)
        except Exception as exc:
            log.warning("发送失败（%s -> %s）：%s", kind, target_id, exc)
            return {"status": "failed", "message": f"{type(exc).__name__}: {exc}"}

    async def send_private(self, user_id: str, segments: list[dict]) -> dict:
        return await self.send("private", user_id, segments)

    async def send_group(self, group_id: str, segments: list[dict]) -> dict:
        return await self.send("group", group_id, segments)

    # ------------------------------------------------------------ 其他
    async def call(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        """转调 OneBot 专有接口（改签名、取群成员、传文件等）。

        这些接口只有 QQ 有，微信桥接是自研协议，所以一律发给 QQ 通道；
        没接 QQ 就直接报错，让上层用 try/except 兜住。
        """
        t = self.qq
        if t is None:
            raise RuntimeError("没接 QQ 通道，这个接口用不了")
        fn = getattr(t, "call", None)
        if not callable(fn):
            raise RuntimeError(f"{type(t).__name__} 不支持 call()")
        return await fn(action, params=params, timeout=timeout)

    def supports(self, action: str) -> bool:
        """问一句：这个 OneBot 接口现在能不能用。"""
        t = self.qq
        return t is not None and callable(getattr(t, "call", None)) and getattr(t, "online", False)

    def stats(self) -> dict:
        out: dict = {"online": self.online, "status": self.status_text()}
        for name, t in (("qq", self.qq), ("wechat", self.wechat)):
            fn = getattr(t, "stats", None)
            if callable(fn):
                try:
                    out[name] = fn()
                except Exception:
                    out[name] = None
        return out
