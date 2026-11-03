"""电脑操控：开/关软件、看进程、切窗口、调音量。

只走 Windows 自带的东西（tasklist / taskkill / winreg / ctypes），不依赖任何第三方库，
所以项目再轻也不会被它拖累。

安全护栏（都在这里，而不是散在 bot.py 里）：
* 关软件有黑名单：系统关键进程、她自己的进程链（python/node）、主人正在用的这台机器上的
  DeepSeek Harness，一律不碰；
* 开关/按键分开关（allow_launch / allow_close / allow_keys / allow_focus）；
* 每分钟动作数有上限，超了就等下一分钟；
* 每次动作都追加一行到 data/pc_log.jsonl，方便主人回头查她动了什么。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logging

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 绝对不能碰的进程（小写、带 .exe）
BLOCKED = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsaiso.exe", "dwm.exe", "fontdrvhost.exe",
    "svchost.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe", "wudfhost.exe",
    "audiodg.exe", "spoolsv.exe", "searchindexer.exe", "securityhealthservice.exe",
    "msmpeng.exe", "nissrv.exe", "wmiprvse.exe", "dllhost.exe", "runtimebroker.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "explorer.exe",  # 关了桌面就没了，别碰
    "applicationframehost.exe",  # UWP 应用的壳，关了会把记事本/计算器那类一起弄没
    "runtimebroker.exe",
    "sihost.exe",
    "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
    "wt.exe", "openconsole.exe",
    # 她自己的链路
    "python.exe", "pythonw.exe", "node.exe",
}

UWP_HOSTS = {"applicationframehost.exe", "runtimebroker.exe", "sihost.exe"}

# 中文别名 → 进程名或可执行文件名（找不到就交给 Start Menu / App Paths 去解析）
DEFAULT_ALIASES = {
    "微信": "Weixin.exe",
    "wechat": "Weixin.exe",
    "weixin": "Weixin.exe",
    "企业微信": "WXWork.exe",
    "qq": "QQ.exe",
    "腾讯qq": "QQ.exe",
    "浏览器": "msedge.exe",
    "edge": "msedge.exe",
    "微软浏览器": "msedge.exe",
    "chrome": "chrome.exe",
    "谷歌浏览器": "chrome.exe",
    "记事本": "notepad.exe",
    "notepad": "notepad.exe",
    "计算器": "calc.exe",
    "calc": "calc.exe",
    "画图": "mspaint.exe",
    "任务管理器": "taskmgr.exe",
    "资源管理器": "explorer.exe",
    "文件资源管理器": "explorer.exe",
    "终端": "wt.exe",
    "powershell": "powershell.exe",
    "命令行": "cmd.exe",
    "vscode": "Code.exe",
    "code": "Code.exe",
    "网易云音乐": "cloudmusic.exe",
    "网易云": "cloudmusic.exe",
    "steam": "steam.exe",
    "钉钉": "DingTalk.exe",
    "抖音": "douyin.exe",
    "potplayer": "PotPlayerMini64.exe",
    "播放器": "PotPlayerMini64.exe",
    "qq音乐": "QQMusic.exe",
    "b站": "bilibili.exe",
    "哔哩哔哩": "bilibili.exe",
    "微信开发者工具": "wechatdevtools.exe",
}

# 虚拟键码
VK = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
    "play": 0xB3,
    "next": 0xB0,
    "prev": 0xB1,
    "stop": 0xB2,
}

VOLUME_NAMES = {
    "大": "volume_up", "调大": "volume_up", "大声": "volume_up", "up": "volume_up", "大声点": "volume_up",
    "小": "volume_down", "调小": "volume_down", "小声": "volume_down", "down": "volume_down", "小声点": "volume_down",
    "静音": "mute", "闭嘴": "mute", "别吵": "mute", "mute": "mute",
    "暂停": "play", "播放": "play", "继续": "play", "play": "play",
    "下一首": "next", "next": "next", "换一首": "next", "切歌": "next",
    "上一首": "prev", "prev": "prev",
}

# 找窗口 / 找进程时忽略的标题（她自己说话用的窗口也算）
_SKIP_TITLE = re.compile(r"^(Program Manager|Windows 输入体验|Microsoft Text Input Application)$")


class PCError(RuntimeError):
    pass


@dataclass
class App:
    name: str          # 人看的名字（开始菜单里的）
    target: str        # 实际要启动的东西（.exe / .lnk / 命令名）
    kind: str = "exe"  # exe / lnk / cmd
    source: str = ""   # 从哪儿找到的，排查用


def _run(cmd: list[str], timeout: float = 12.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="gbk", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW, env=env,
    )


def _norm(text: str) -> str:
    return re.sub(r"[\s　]+", "", (text or "").strip().lower())


class PC:
    def __init__(self, cfg: Any, root: Path) -> None:
        self.cfg = cfg
        self.root = root
        self.data_dir = root / "data"
        self.cache_file = self.data_dir / "apps.json"
        self.audit_file = self.data_dir / "pc_log.jsonl"
        self._apps: list[App] = []
        self._loaded = False
        self._stamps: list[float] = []
        self.counters = {"launched": 0, "closed": 0, "keys": 0, "focus": 0, "listed": 0, "failed": 0}
        self.last_error = ""

    # ---------------------------------------------------------------- 基本属性
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False))

    @property
    def aliases(self) -> dict:
        table = dict(DEFAULT_ALIASES)
        table.update({str(k): str(v) for k, v in (getattr(self.cfg, "aliases", {}) or {}).items()})
        return table

    def _allows(self, what: str) -> bool:
        if not self.enabled:
            return False
        return bool(getattr(self.cfg, f"allow_{what}", True))

    def _rate_ok(self) -> bool:
        limit = int(getattr(self.cfg, "max_actions_per_minute", 10) or 0)
        if limit <= 0:
            return True
        now = time.time()
        self._stamps = [t for t in self._stamps if now - t < 60]
        if len(self._stamps) >= limit:
            return False
        self._stamps.append(now)
        return True

    def _audit(self, action: str, target: str, ok: bool, note: str = "") -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": int(time.time()), "action": action, "target": target, "ok": ok, "note": note},
                    ensure_ascii=False,
                ) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- 软件清单
    def apps(self, *, refresh: bool = False) -> list[App]:
        if self._loaded and not refresh:
            return self._apps
        if not refresh and self.cache_file.exists():
            try:
                raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
                self._apps = [App(**item) for item in raw.get("apps", [])]
                self._loaded = True
                if self._apps:
                    return self._apps
            except (OSError, ValueError, TypeError):
                pass
        self._apps = self._scan_apps()
        self._loaded = True
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps({"at": int(time.time()), "apps": [a.__dict__ for a in self._apps]},
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass
        return self._apps

    def _scan_apps(self) -> list[App]:
        found: dict[str, App] = {}

        # 1) 开始菜单快捷方式（用户 + 所有用户）
        roots = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        for base in roots:
            if not base.exists():
                continue
            try:
                for lnk in base.rglob("*.lnk"):
                    name = lnk.stem
                    if not name or name.startswith("Uninstall") or name.startswith("卸载"):
                        continue
                    key = _norm(name)
                    if key and key not in found:
                        found[key] = App(name=name, target=str(lnk), kind="lnk", source="开始菜单")
            except OSError:
                continue

        # 2) 常见别名对应的可执行文件
        for alias, exe in DEFAULT_ALIASES.items():
            key = _norm(alias)
            if key not in found:
                found[key] = App(name=alias, target=exe, kind="exe", source="别名表")

        apps = sorted(found.values(), key=lambda a: a.name)
        log.info("电脑操控：扫到 %d 个可启动的软件", len(apps))
        return apps

    # ---------------------------------------------------------------- 解析名字
    def resolve(self, name: str) -> App | None:
        raw = (name or "").strip().strip("的了个一下吧呗呀啊 ")
        if not raw:
            return None
        key = _norm(raw)
        table = self.aliases

        # 1) 别名表（精确 → 包含）
        if key in {_norm(k) for k in table}:
            for k, v in table.items():
                if _norm(k) == key:
                    return self._exe_app(k, v)
        for k, v in table.items():
            nk = _norm(k)
            if nk and (nk in key or key in nk):
                return self._exe_app(k, v)

        # 2) 软件清单（精确 → 包含 → 首字母）
        apps = self.apps()
        normed = {_norm(a.name): a for a in apps}
        if key in normed:
            return normed[key]
        for nk, app in normed.items():
            if key and (key in nk or nk in key):
                return app

        # 3) App Paths 注册表
        hit = self._from_app_paths(raw)
        if hit:
            return hit

        # 4) PATH 里的可执行文件
        exe = raw if raw.lower().endswith(".exe") else raw + ".exe"
        where = shutil.which(raw) or shutil.which(exe)
        if where:
            return App(name=raw, target=where, kind="exe", source="PATH")
        return None

    @staticmethod
    def _exe_app(alias: str, exe: str) -> App:
        return App(name=alias, target=exe, kind="exe", source="别名表")

    def _from_app_paths(self, name: str) -> App | None:
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        keys = [exe]
        for k, v in self.aliases.items():
            if _norm(k) and _norm(k) in _norm(name):
                keys.append(v if v.lower().endswith(".exe") else v + ".exe")
        seen: set[str] = set()
        for key in keys:
            if key.lower() in seen:
                continue
            seen.add(key.lower())
            for hive, sub in (
                (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{key}"),
                (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{key}"),
            ):
                try:
                    with winreg.OpenKey(hive, sub) as handle:
                        value, _ = winreg.QueryValueEx(handle, "")
                    path = str(value).strip().strip('"')
                    if path and Path(path).exists():
                        return App(name=key, target=path, kind="exe", source="App Paths")
                except OSError:
                    continue
        return None

    # ---------------------------------------------------------------- 看进程 / 窗口
    def running(self, limit: int = 12) -> list[dict]:
        out: list[dict] = []
        try:
            res = _run(["tasklist", "/fo", "csv", "/nh"])
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return out
        agg: dict[str, dict] = {}
        for line in (res.stdout or "").splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) < 5:
                continue
            name, pid, mem = parts[0].strip('"'), parts[1], parts[4]
            num = int(re.sub(r"[^\d]", "", mem) or 0)
            item = agg.setdefault(name.lower(), {"name": name, "pids": 0, "mem_mb": 0.0})
            item["pids"] += 1
            item["mem_mb"] += num / 1024
        for item in agg.values():
            item["mem_mb"] = round(item["mem_mb"], 1)
        out = sorted(agg.values(), key=lambda d: d["mem_mb"], reverse=True)
        self.counters["listed"] += 1
        return out[: max(1, limit)]

    def windows(self) -> list[tuple[int, str]]:
        u32 = ctypes.windll.user32
        out: list[tuple[int, str]] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):  # noqa: ANN001
            try:
                if not u32.IsWindowVisible(hwnd):
                    return True
                length = u32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                title = (buf.value or "").strip()
                if title and not _SKIP_TITLE.match(title):
                    out.append((int(hwnd), title))
            except Exception:  # noqa: BLE001
                pass
            return True

        try:
            u32.EnumWindows(proc(cb), 0)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
        return out

    def _window_pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, pid: int) -> str:
        try:
            res = _run(["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"])
        except (OSError, subprocess.SubprocessError):
            return ""
        line = (res.stdout or "").strip().splitlines()
        if not line:
            return ""
        parts = [p.strip('"') for p in line[0].split('","')]
        return parts[0].strip('"') if parts else ""

    # ---------------------------------------------------------------- 动作
    def launch(self, name: str) -> str:
        if not self._allows("launch"):
            return "管理员没让本鲸开软件（pc.allow_launch: false）。"
        if not self._rate_ok():
            return "本鲸手速太快了，歇一分钟再开。"
        app = self.resolve(name)
        if app is None:
            return f"本鲸翻遍了开始菜单和注册表，没找到「{name}」这个软件。用 /软件 看看有哪些能开。"
        try:
            if app.kind == "lnk":
                os.startfile(app.target)  # type: ignore[attr-defined]
            else:
                target = app.target
                if not Path(target).exists():
                    found = shutil.which(target)
                    if not found:
                        raise PCError(f"找不到 {target}")
                    target = found
                subprocess.Popen(
                    [target], cwd=str(Path(target).parent),
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
            self.counters["launched"] += 1
            self._audit("launch", f"{name} -> {app.target}", True)
            log.info("电脑操控：打开了 %s（%s）", app.name, app.target)
            return f"{app.name} 给你打开了。"
        except Exception as exc:  # noqa: BLE001
            self.counters["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._audit("launch", name, False, self.last_error)
            return f"「{app.name}」本鲸没打开（{type(exc).__name__}: {exc}）。"

    def close(self, name: str) -> str:
        if not self._allows("close"):
            return "管理员没让本鲸关软件（pc.allow_close: false）。"
        if not self._rate_ok():
            return "本鲸手速太快了，歇一分钟再关。"
        raw = (name or "").strip()
        if not raw:
            return "关哪个？"
        targets: list[str] = []

        # 1) 从窗口标题反查进程（「把计算器关了」这种）
        key = _norm(raw)
        for hwnd, title in self.windows():
            if key and (key in _norm(title) or _norm(title) in key):
                exe = self._process_name(self._window_pid(hwnd))
                if exe and exe.lower() in UWP_HOSTS:
                    # 窗口属于 UWP 壳进程，动它会把别的应用一起关掉 —— 改用别名/进程名去找真身
                    continue
                if exe:
                    targets.append(exe)
                break
        # 2) 从名字/别名猜进程名
        if not targets:
            if raw.lower().endswith(".exe"):
                targets.append(raw)
            else:
                for k, v in self.aliases.items():
                    if _norm(k) and (_norm(k) in key or key in _norm(k)):
                        targets.append(v)
                        break
        # 3) 已经在跑的进程名里模糊找
        if not targets:
            for item in self.running(limit=400):
                if key and (key in _norm(item["name"]) or _norm(item["name"]).startswith(key)):
                    targets.append(item["name"])
                    break
        if not targets:
            return f"「{raw}」本鲸既没找到窗口、也没找到进程，可能已经关着了。"

        exe = targets[0]
        low = exe.lower()
        if low in BLOCKED:
            return f"「{exe}」本鲸不敢关 —— 关了要么系统出事，要么本鲸自己就没了。"
        # 主人正在用的这台机器上的工具（DSH）别碰
        try:
            res = _run(["wmic", "process", "where", f"name='{exe}'", "get", "ExecutablePath"])
            paths = [ln.strip() for ln in (res.stdout or "").splitlines()[1:] if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            paths = []
        if any(("deepseek harness" in p.lower() or "dsh-desktop" in p.lower()) for p in paths):
            return "那个关了就没人陪你玩了，本鲸不碰它（你要真关，自己动手吧）。"

        try:
            stem = Path(exe).stem.lower()
            cands: list[str] = []
            for c in targets + [it["name"] for it in self.running(limit=400)
                                if stem[:5] and Path(it["name"]).stem.lower().startswith(stem[:5])]:
                if c and c.lower() not in [x.lower() for x in cands] and c.lower() not in BLOCKED:
                    cands.append(c)
            res = None
            ok = False
            for c in cands[:4]:
                res = _run(["taskkill", "/f", "/im", c])
                if res.returncode == 0:
                    ok, exe = True, c
                    break
            if not ok:
                # taskkill 的返回码对 UWP 应用不可靠，再用进程列表复核一次
                rest = [it["name"] for it in self.running(limit=400)
                        if stem[:5] and Path(it["name"]).stem.lower().startswith(stem[:5])]
                if not rest:
                    ok = True
        except (OSError, subprocess.SubprocessError) as exc:
            self.counters["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._audit("close", exe, False, self.last_error)
            return f"关「{exe}」的时候出错了（{type(exc).__name__}: {exc}）。"
        if ok:
            self.counters["closed"] += 1
            self._audit("close", exe, True)
            log.info("电脑操控：关掉了 %s", exe)
            asked = (raw or exe).strip()
            extra = "" if _norm(asked) == _norm(exe) else f"（进程 {exe}）"
            return f"{asked} 已经关掉了{extra}。"
        self.counters["failed"] += 1
        note = ((res.stdout or res.stderr or "") if res is not None else "").strip()
        self.last_error = note
        self._audit("close", exe, False, note)
        return f"「{exe}」没关掉（{note or '没有权限'}）。"

    def focus(self, name: str, *, minimize: bool = False) -> str:
        what = "minimize" if minimize else "focus"
        if not self._allows("focus"):
            return "管理员没让本鲸摆弄窗口（pc.allow_focus: false）。"
        if not self._rate_ok():
            return "本鲸手速太快了，歇一分钟。"
        key = _norm(name)
        best: tuple[int, str] | None = None
        for hwnd, title in self.windows():
            if key and key in _norm(title):
                best = (hwnd, title)
                break
        if best is None:
            for hwnd, title in self.windows():
                if key and (_norm(title).startswith(key) or any(part and part in _norm(title) for part in (key,))):
                    best = (hwnd, title)
                    break
        if best is None:
            return f"没找到「{name}」的窗口，可能没开着。"
        hwnd, title = best
        u32 = ctypes.windll.user32
        try:
            if minimize:
                u32.ShowWindow(wintypes.HWND(hwnd), 6)  # SW_MINIMIZE
                self._audit("minimize", title, True)
                return f"把「{title}」缩下去了。"
            u32.ShowWindow(wintypes.HWND(hwnd), 9)      # SW_RESTORE
            u32.SetForegroundWindow(wintypes.HWND(hwnd))
            self.counters["focus"] += 1
            self._audit("focus", title, True)
            return f"「{title}」给你调到前面了。"
        except Exception as exc:  # noqa: BLE001
            self.counters["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return f"窗口没动成（{type(exc).__name__}: {exc}）。"

    def key(self, which: str) -> str:
        if not self._allows("keys"):
            return "管理员没让本鲸碰按键（pc.allow_keys: false）。"
        if not self._rate_ok():
            return "本鲸手速太快了，歇一分钟。"
        action = VOLUME_NAMES.get((which or "").strip().lower()) or VOLUME_NAMES.get((which or "").strip())
        if not action:
            return "这个键本鲸按不了（只会：大点 / 小点 / 静音 / 暂停 / 下一首 / 上一首）。"
        vk = VK[action]
        times = 4 if action in ("volume_up", "volume_down") else 1
        try:
            for _ in range(times):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
                time.sleep(0.06)
            self.counters["keys"] += 1
            self._audit("key", action, True)
            word = {"volume_up": "音量调大了点", "volume_down": "音量调小了点", "mute": "静音了",
                    "play": "播放/暂停了", "next": "下一首", "prev": "上一首"}[action]
            return f"{word}。"
        except Exception as exc:  # noqa: BLE001
            self.counters["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return f"按键没按成（{type(exc).__name__}: {exc}）。"

    # ---------------------------------------------------------------- 上报
    def running_text(self, limit: int = 12) -> str:
        rows = self.running(limit=limit)
        if not rows:
            return "本鲸看不到进程列表（可能是权限问题）。"
        lines = []
        for item in rows:
            lines.append(f"{item['name']}（{item['pids']} 个，{item['mem_mb']:.0f}MB）")
        wins = [t for _h, t in self.windows()][:6]
        text = "现在开着（按内存排）：\n" + "、".join(lines)
        if wins:
            text += "\n有窗口的：" + "、".join(wins)
        return text

    def apps_text(self, limit: int = 40) -> str:
        apps = self.apps()
        names = "、".join(a.name for a in apps[:limit])
        more = "" if len(apps) <= limit else f"\n（一共 {len(apps)} 个，先列这些）"
        return f"本鲸能开的（{len(apps)} 个）：{names}{more}"

    def status_text(self) -> str:
        if not self.enabled:
            return "电脑操控：关着"
        flags = []
        if getattr(self.cfg, "allow_launch", True):
            flags.append("开软件")
        if getattr(self.cfg, "allow_close", True):
            flags.append("关软件")
        if getattr(self.cfg, "allow_focus", True):
            flags.append("切窗口")
        if getattr(self.cfg, "allow_keys", True):
            flags.append("按键")
        c = self.counters
        return (f"电脑操控：开启（{'/'.join(flags) or '只读'}）｜开 {c['launched']} 关 {c['closed']} "
                f"按键 {c['keys']} 切窗口 {c['focus']} 失败 {c['failed']}")

    def stats(self) -> dict:
        return dict(self.counters, last_error=self.last_error, enabled=self.enabled)
