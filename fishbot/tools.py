"""她真能干点活：截屏、看机器状况、把聊过的东西压成摘要。

设计取向：

* **每样活都是一次函数调用，不依赖外部命令**。截屏走 Pillow 的 ``ImageGrab``，
  看机器状况走 ``ctypes`` 直接问 Windows，不装 psutil、不拉 PowerShell 子进程
  （那玩意儿在 12 秒生成时间里太贵了）。
* **只读**。这个模块永远不改系统状态、不写别人的文件；唯一的写是自己截的图。
* **能关**。每样能力在 ``config.yaml`` 的 ``tools:`` 段都有独立开关，默认
  只开截屏和系统信息；读文件默认**关着**，因为它会把内容发到群里。
"""

from __future__ import annotations

import ctypes
import logging
import re
import shutil
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

log = logging.getLogger("fishbot.tools")


class ToolError(RuntimeError):
    """干活失败。消息是给用户看的中文，不含堆栈。"""


# ------------------------------------------------------------ 标记解析
TOOL_MARKER_RE = re.compile(r"\[\[\s*(?:工具|tool|干活)\s*[:：]?\s*([^\[\]]{0,12})\s*\]\]")

TOOL_ALIASES = {
    "截图": "screenshot",
    "截屏": "screenshot",
    "屏幕": "screenshot",
    "screenshot": "screenshot",
    "shot": "screenshot",
    "系统": "sysinfo",
    "系统信息": "sysinfo",
    "机器": "sysinfo",
    "机器状况": "sysinfo",
    "sysinfo": "sysinfo",
    "压缩": "compact",
    "压缩上下文": "compact",
    "compact": "compact",
}

_PARTIAL_TOOL_PREFIXES = (
    "[[",
    "[[工",
    "[[工具",
    "[[工具:",
    "[[工具：",
    "[[tool",
    "[[tool:",
    "[[干活",
    "[[干活:",
)


def parse_tool(text: str) -> tuple[str, str | None]:
    """从回复里摘出 ``[[工具:截图]]``，返回（干净正文, 工具名或 None）。

    只认第一个标记；没有标记时原样返回。
    """
    if "[[" not in text:
        return text, None
    m = TOOL_MARKER_RE.search(text)
    if not m:
        return text, None
    name = TOOL_ALIASES.get(m.group(1).strip().lower()) or TOOL_ALIASES.get(m.group(1).strip())
    clean = TOOL_MARKER_RE.sub("", text, count=1)
    clean = "\n".join(line.rstrip() for line in clean.splitlines())
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    return clean, name


def split_partial_tool(text: str) -> tuple[str, str]:
    """把没写完的 ``[[工具:截`` 切下来，别当正文发出去。"""
    if "[[" not in text:
        return text, ""
    pos = text.rfind("[[")
    if "]]" in text[pos:]:
        return text, ""
    tail = text[pos:]
    if tail in _PARTIAL_TOOL_PREFIXES or len(tail) <= 6:
        return text[:pos], tail
    return text, ""


def tools_hint(available: list[str]) -> str:
    """写给模型看的工具说明；没有可用工具就返回空串。"""
    if not available:
        return ""
    lines = ["【你能自己动手做的事】想在回复里动手，就在正文最后另起一行写标记，系统会替你执行并把结果发出去："]
    if "截图" in available:
        lines.append("- `[[工具:截图]]` —— 看一眼现在的屏幕，截张图发出去（有冷却，别连着用）")
    if "系统信息" in available:
        lines.append("- `[[工具:系统]]` —— 报一下这台机器的状况（开机多久、内存、磁盘）")
    if "压缩上下文" in available:
        lines.append("- `[[工具:压缩]]` —— 把之前聊过的东西压成一小段摘要，给自己省点地方")
    lines.append("标记要单独占一行，正文里不要解释它是干什么的。对方明确让你做的时候再用，平时别主动秀。")
    return "\n".join(lines)



# ------------------------------------------------------------------ 截屏
def grab_screen(out_dir: Path, *, max_edge: int = 1280, keep: int = 30) -> Path:
    """抓一张全屏图，缩到最长边 ``max_edge``，存成 PNG，返回文件路径。

    多屏会一起抓（``all_screens=True``），所以副屏上的东西也在。
    """
    try:
        from PIL import ImageGrab
    except ImportError as exc:  # pragma: no cover - 装依赖失败才会走到
        raise ToolError(f"没有 Pillow，装不上就截不了图（{exc}）") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        shot = ImageGrab.grab(all_screens=True)
    except Exception as exc:
        raise ToolError(f"截屏失败：{type(exc).__name__}: {exc}") from exc

    if shot.mode not in ("RGB", "RGBA"):
        shot = shot.convert("RGB")
    if shot.mode == "RGBA":
        shot = shot.convert("RGB")

    w, h = shot.size
    if max(w, h) > max_edge:
        scale = max_edge / float(max(w, h))
        shot = shot.resize((max(1, int(w * scale)), max(1, int(h * scale))), _resample())

    path = out_dir / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    shot.save(path, "PNG", optimize=True)
    _prune(out_dir, keep)
    return path


def _resample():
    from PIL import Image

    return getattr(Image, "Resampling", Image).LANCZOS


def _prune(out_dir: Path, keep: int) -> None:
    """只留最近 ``keep`` 张截图，别把磁盘吃满。"""
    if keep <= 0:
        return
    try:
        files = sorted(out_dir.glob("shot-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except Exception:
        log.debug("清理旧截图失败", exc_info=True)


def shot_count(out_dir: Path) -> int:
    try:
        return len(list(out_dir.glob("shot-*.png")))
    except Exception:
        return 0


# -------------------------------------------------------------- 机器状况
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _gb(n: int) -> float:
    return n / (1024.0 ** 3)


def _memory() -> tuple[float, float, int] | None:
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return _gb(st.ullTotalPhys), _gb(st.ullAvailPhys), int(st.dwMemoryLoad)
    except Exception:
        return None


def _self_rss() -> float | None:
    """本进程占了多少内存（MB）——她关心自己胖不胖。"""
    try:
        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        info = _PMC()
        info.cb = ctypes.sizeof(_PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(info), info.cb):
            return None
        return info.WorkingSetSize / (1024.0 * 1024.0)
    except Exception:
        return None


def _uptime_text() -> str:
    try:
        ms = ctypes.windll.kernel32.GetTickCount64()
    except Exception:
        return "未知"
    secs = int(ms // 1000)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分"


def sysinfo() -> str:
    """一段自述式的机器状况，够她用自己的话讲出来。"""
    bits: list[str] = []
    bits.append(f"开机 {_uptime_text()}")

    mem = _memory()
    if mem:
        total, avail, pct = mem
        bits.append(f"内存 {total - avail:.1f}/{total:.1f} GB（用了 {pct}%）")

    rss = _self_rss()
    if rss is not None:
        bits.append(f"我自己占 {rss:.0f} MB")

    try:
        bits.append(f"CPU {__import__('os').cpu_count()} 核")
    except Exception:
        pass

    for drive in ("C:\\", "D:\\"):
        try:
            u = shutil.disk_usage(drive)
            bits.append(f"{drive[0]} 盘剩 {u.free / (1024 ** 3):.0f} GB / {u.total / (1024 ** 3):.0f} GB")
        except Exception:
            continue

    return "；".join(bits) + "。"


# ---------------------------------------------------------------- 工具台账
class ToolBox:
    """管住她能干的活：开关、限流、计数。"""

    def __init__(self, cfg: Any, root: Path) -> None:
        self.cfg = cfg
        self.root = root
        self._shots: list[float] = []
        self.counters = {"shot": 0, "sysinfo": 0, "compact": 0, "denied": 0, "failed": 0}
        self.last_error = ""

    # --------------------------------------------------------------- 路径
    @property
    def shot_dir(self) -> Path:
        raw = str(getattr(self.cfg, "shot_dir", "data/shots") or "data/shots")
        p = Path(raw)
        return p if p.is_absolute() else self.root / p

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    def allows(self, name: str) -> bool:
        if not self.enabled:
            return False
        return bool(getattr(self.cfg, name, False))

    # --------------------------------------------------------------- 限流
    def _shot_ok(self) -> tuple[bool, str]:
        now = time.time()
        cooldown = float(getattr(self.cfg, "shot_cooldown_seconds", 30.0) or 0)
        per_hour = int(getattr(self.cfg, "shot_max_per_hour", 6) or 0)
        self._shots = [t for t in self._shots if now - t < 3600]
        if cooldown > 0 and self._shots and now - self._shots[-1] < cooldown:
            left = int(cooldown - (now - self._shots[-1])) + 1
            return False, f"刚截过，{left} 秒后再来"
        if per_hour > 0 and len(self._shots) >= per_hour:
            return False, f"这个小时截够了（{per_hour} 张）"
        return True, ""

    # --------------------------------------------------------------- 动作
    def screenshot(self) -> Path:
        ok, why = self._shot_ok()
        if not ok:
            self.counters["denied"] += 1
            raise ToolError(why)
        try:
            path = grab_screen(
                self.shot_dir,
                max_edge=int(getattr(self.cfg, "max_shot_edge", 1280) or 1280),
                keep=int(getattr(self.cfg, "keep_shots", 30) or 30),
            )
        except ToolError:
            self.counters["failed"] += 1
            raise
        except Exception as exc:
            self.counters["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ToolError(f"截屏失败：{self.last_error}") from exc
        self._shots.append(time.time())
        self.counters["shot"] += 1
        return path

    def machine(self) -> str:
        try:
            text = sysinfo()
        except Exception as exc:
            self.counters["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ToolError(f"看不了机器状况：{self.last_error}") from exc
        self.counters["sysinfo"] += 1
        return text

    # --------------------------------------------------------------- 状态
    def names(self) -> list[str]:
        """现在开了哪些活。"""
        out = []
        if self.allows("screenshot"):
            out.append("截图")
        if self.allows("sysinfo"):
            out.append("系统信息")
        if self.allows("compact"):
            out.append("压缩上下文")
        if self.allows("file_read"):
            out.append("读文件")
        return out

    def status_text(self) -> str:
        if not self.enabled:
            return "干活：关"
        names = self.names()
        if not names:
            return "干活：没开任何项"
        return "干活：{}（截图 {} 张）".format("、".join(names), self.counters["shot"])

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "names": self.names(),
            "shots_on_disk": shot_count(self.shot_dir),
            **self.counters,
            "last_error": self.last_error,
        }
