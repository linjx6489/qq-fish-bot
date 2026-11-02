"""让她能操控自己的 QQ。

能做的事（都走 OneBot / NapCat 的接口）：

| 想干的事 | 调的接口 | 参数 |
| --- | --- | --- |
| 改昵称 / 签名 | `set_qq_profile` | `{nickname, personal_note}`（昵称必填，不填会把昵称写坏） |
| 只改签名 | `set_self_longnick` | `{longNick}` |
| 换头像 | `set_qq_avatar` | `{file}`（本地路径 / http(s) / base64） |
| 改在线状态 | `set_online_status` | `{status, ext_status, battery_status}` |
| 发空间说说 | `send_qzone_msg` | `{content, images?, ugc_right?, target_uins?}` → `{tid}` |
| 删说说 | `delete_qzone_msg` | `{tid}` |
| 给好友点赞 | `send_like` | `{user_id, times}` |

```
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

# OneBot 的在线状态编号（NapCat 文档里的那张表）
STATUS_CHOICES: dict[str, tuple[int, int]] = {
    "在线": (10, 0),
    "q我吧": (60, 0),
    "离开": (30, 0),
    "忙碌": (50, 0),
    "请勿打扰": (70, 0),
    "隐身": (40, 0),
    "听歌中": (10, 1028),
    "春日限定": (10, 2037),
    "一起元梦": (10, 2025),
}

# 空间说说的可见范围
UGC_CHOICES: dict[str, int] = {
    "所有人": 1,
    "好友": 4,
    "部分好友": 16,
    "仅自己": 64,
    "部分好友不可见": 128,
}
UGC_LABELS: dict[int, str] = {v: k for k, v in UGC_CHOICES.items()}


class QQControlError(RuntimeError):
    """QQ 那边没干成，带一句能直接读给主人听的原因。"""


class QQControl:
    def __init__(self, cfg: Any, root: Path):
        self.cfg = cfg
        self.root = Path(root)
        self.transport: Any = None
        self.self_id = ""
        self.counters: dict[str, int] = {"ok": 0, "failed": 0, "qzone": 0, "avatar": 0}
        self.last_error = ""
        self.last_action = ""
        self.last_tid = ""  # 最近一条说说的 tid（便于"撤了"）
        self._qzone_window: list[float] = []

    # ------------------------------------------------------------ 接线
    def attach(self, transport: Any) -> None:
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    @property
    def qzone_enabled(self) -> bool:
        return self.enabled and bool(getattr(self.cfg, "qzone_enabled", True))

    async def start(self) -> None:
        if not self.enabled:
            log.info("QQ 操控：已关闭")
            return
        self.self_id = str(getattr(self.transport, "self_id", "") or "")
        log.info("%s", self.status_text())

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------ 底座
    async def call(self, action: str, params: dict | None = None, *, timeout: float = 30.0) -> Any:
        """调一个 QQ 接口，出错就抛 QQControlError（外面好照着回话）。"""
        if not self.enabled:
            raise QQControlError("QQ 操控被关掉了（config.yaml 里 ctrl.enabled: false）")
        transport = self.transport
        if transport is None:
            raise QQControlError("她还没接到 QQ 上")
        caller = getattr(transport, "call", None)
        if caller is None:
            raise QQControlError("现在这个通道不会调 QQ 接口")
        self.last_action = action
        try:
            data = await caller(action, params or {}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self.counters["failed"] += 1
            self.last_error = f"{action} 失败：{type(exc).__name__}: {exc}"
            log.warning("QQ 操控：%s", self.last_error)
            raise QQControlError(self.last_error) from exc
        self.counters["ok"] += 1
        self.last_error = ""
        return data

    # ------------------------------------------------------------ 资料
    async def profile(self) -> dict:
        """看看她现在的昵称 / 签名 / 头像。"""
        info = await self.call("get_login_info", {})
        out: dict[str, Any] = {"user_id": str((info or {}).get("user_id", "") or self.self_id)}
        out["nickname"] = str((info or {}).get("nickname", ""))
        uid = str((info or {}).get("uid", "") or "")
        if uid:
            try:
                detail = await self.call("get_user_detail_info", {"uid": uid})
                if isinstance(detail, dict):
                    out["longNick"] = str(detail.get("longNick") or detail.get("long_nick") or "")
            except QQControlError:
                pass
        return out

    async def set_nickname(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise QQControlError("新昵称不能是空的")
        if len(name) > 24:
            name = name[:24]
        # 昵称是必填项：只改昵称时也把现在的签名带过去，免得被清空
        now = await self.profile()
        payload = {"nickname": name}
        if now.get("longNick"):
            payload["personal_note"] = now["longNick"]
        await self.call("set_qq_profile", payload)
        log.info("QQ 操控：昵称改成「%s」", name)
        return name

    async def set_signature(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise QQControlError("签名不能是空的（想清空就说 /签名 空格）")
        if len(text) > 60:
            text = text[:60]
        await self.call("set_self_longnick", {"longNick": text})
        log.info("QQ 操控：签名改成「%s」", text)
        return text

    async def set_avatar(self, src: str) -> str:
        src = (src or "").strip()
        if not src:
            raise QQControlError("得给我一张图（网址、本机路径，或者直接发图带命令）")
        file = await self.image_file(src)
        await self.call("set_qq_avatar", {"file": file}, timeout=60.0)
        self.counters["avatar"] += 1
        log.info("QQ 操控：头像换了（%s）", file[:60])
        return file

    async def set_status(self, name: str) -> str:
        key = (name or "").strip().lower().replace(" ", "")
        hit = None
        for label in STATUS_CHOICES:
            if label.lower() == key:
                hit = label
                break
        if hit is None:
            for label in STATUS_CHOICES:
                if key and (key in label.lower() or label.lower() in key):
                    hit = label
                    break
        if hit is None:
            raise QQControlError("认不出这个状态。能用：" + "、".join(STATUS_CHOICES))
        status, ext = STATUS_CHOICES[hit]
        await self.call(
            "set_online_status",
            {"status": status, "ext_status": ext, "battery_status": 100},
        )
        log.info("QQ 操控：在线状态改成「%s」（%s/%s）", hit, status, ext)
        return hit

    async def like(self, user_id: str, times: int = 10) -> str:
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            raise QQControlError("点赞要给 QQ 号")
        n = max(1, min(20, int(times or 10)))
        await self.call("send_like", {"user_id": uid, "times": n})
        log.info("QQ 操控：给 %s 点了 %d 个赞", uid, n)
        return f"{uid} × {n}"

    # ------------------------------------------------------------ 空间
    async def post_qzone(
        self,
        text: str,
        images: Sequence[str] | None = None,
        *,
        right: str | int = "",
        target_uins: Sequence[str] | None = None,
    ) -> str:
        """发一条说说，返回 tid（想删就用这个 tid）。"""
        if not self.qzone_enabled:
            raise QQControlError("发空间被关掉了（config.yaml 里 ctrl.qzone_enabled: false）")
        body = (text or "").strip()
        files: list[str] = []
        for src in images or []:
            got = await self.image_file(src)
            if got:
                files.append(got)
        if not body and not files:
            raise QQControlError("说说总得有点内容吧：给句话，或者带张图")

        ok, why = self._qzone_budget()
        if not ok:
            raise QQControlError(why)

        ugc = self._ugc_right(right)
        payload: dict[str, Any] = {"content": body, "ugc_right": ugc}
        if files:
            payload["images"] = files
        uins = [str(x) for x in (target_uins or []) if str(x).strip()]
        if uins and ugc in (16, 128):
            payload["target_uins"] = uins

        data = await self.call("send_qzone_msg", payload, timeout=90.0)
        tid = str((data or {}).get("tid") or "") if isinstance(data, dict) else ""
        if tid:
            self.last_tid = tid
        self.counters["qzone"] += 1
        self._qzone_window.append(time.time())
        log.info("QQ 操控：发了一条说说（%s，%d 图，tid=%s）", UGC_LABELS.get(ugc, ugc), len(files), tid or "?")
        return tid

    async def delete_qzone(self, tid: str) -> str:
        tid = str(tid or "").strip()
        if not tid:
            raise QQControlError("要删哪条？给我 tid（发说说时会回一个）")
        await self.call("delete_qzone_msg", {"tid": tid}, timeout=60.0)
        log.info("QQ 操控：删了说说 %s", tid)
        return tid

    # ------------------------------------------------------------ 小工具
    async def image_file(self, src: str) -> str:
        """把一条图源（网址 / 本机路径 / base64 / 刚刚那条消息里的图）变成接口认的 file 字段。

        返回空字符串表示“没找到这张图”，由调用方决定怎么说话。
        """
        s = (src or "").strip()
        if not s:
            return ""
        if s.startswith(("http://", "https://", "base64://", "file://")):
            return s
        path = Path(s)
        if path.exists():
            return str(path)
        # 既不是网址也不是本机路径：多半是刚发来的图，问 NapCat 要它手上的那份
        try:
            got = await self.call("get_image", {"file": s}, timeout=30.0)
        except QQControlError:
            return s
        if isinstance(got, dict):
            local = str(got.get("file") or "")
            if local:
                return local
            url = str(got.get("url") or "")
            if url:
                return url
        return s

    async def image_sources_from_message(self, parsed: Any, limit: int = 1) -> list[str]:
        """从一条消息里挑出图片（她/对方刚发的那张）。"""
        from .vision import image_refs  # 局部导入，免得两个模块互相拽

        out: list[str] = []
        for ref in image_refs(parsed)[: max(1, limit)]:
            src = await self.image_file(str(ref.get("url") or "") or str(ref.get("file") or ""))
            if src:
                out.append(src)
        return out

    def _ugc_right(self, right: str | int) -> int:
        if right in ("", None):
            return int(getattr(self.cfg, "default_ugc_right", 1) or 1)
        if isinstance(right, int) or str(right).isdigit():
            n = int(right)
        else:
            key = str(right).strip()
            n = UGC_CHOICES.get(key, 0)
            if not n:
                for label, val in UGC_CHOICES.items():
                    if key and (key in label or label in key):
                        n = val
                        break
        if n not in UGC_LABELS:
            raise QQControlError("权限认不出。能用：" + "、".join(UGC_CHOICES))
        return n

    def _qzone_budget(self) -> tuple[bool, str]:
        limit = int(getattr(self.cfg, "max_qzone_per_day", 0) or 0)
        if limit <= 0:
            return True, ""
        now = time.time()
        self._qzone_window = [t for t in self._qzone_window if now - t < 86400]
        if len(self._qzone_window) >= limit:
            return False, f"今天已经发了 {len(self._qzone_window)} 条说说，到上限了（{limit} 条/天）"
        return True, ""

    def last_qzone_tid(self) -> str:
        """最近发出去的那条说说（一句话"撤了"就能撤掉它）。"""
        return self.last_tid

    def status_text(self) -> str:
        if not self.enabled:
            return "QQ 操控：已关闭"
        bits = [f"QQ 操控：开启（{self.counters['ok']} 次成功／{self.counters['failed']} 次失败"]
        if self.counters["qzone"]:
            bits.append(f"，说过 {self.counters['qzone']} 条")
        return "".join(bits) + f"）{'，最近出错：' + self.last_error if self.last_error else ''}"

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "qzone_enabled": self.qzone_enabled,
            "counters": dict(self.counters),
            "last_action": self.last_action,
            "last_error": self.last_error,
        }
