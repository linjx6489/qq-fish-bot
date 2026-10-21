#!/usr/bin/env python
"""吃白饭的肥鱼 · 看门狗

每 12 小时被计划任务唤醒一次，检查三条线：

  1. QQ 通道  —— NapCat 的 OneBot WebSocket（127.0.0.1:3001 有没有在听）
  2. 微信通道 —— Weixin.exe 进程在不在
  3. 机器人   —— data/heartbeat 有没有在更新（进程在 ≠ 没卡死）

哪条断了就按对应姿势拉起来，把结果写进 data/watchdog.log；
修完如果 QQ 通着，就自动给主人发条私聊报备一声。

用法：
    python watchdog.py              # 检查一次就退出（计划任务用这个）
    python watchdog.py --loop 180   # 常驻循环，每 180 秒一次（调试用）
    python watchdog.py --dry-run    # 只报告不动手
    python watchdog.py --no-notify  # 不要给我发 QQ 消息
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG_FILE = DATA / "watchdog.log"
STATE_FILE = DATA / "watchdog_state.json"
HEARTBEAT = DATA / "heartbeat"

QQ_EXE = Path(r"C:\Program Files\Tencent\QQNT\QQ.exe")
WEIXIN_EXE = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")
BOOT_QQ_BAT = ROOT / "boot_qq.bat"
RUN_BOT_BAT = ROOT / "run_bot.bat"
BOT_PY = ROOT / ".venv" / "Scripts" / "python.exe"

ONEBOT_PORT = 3001
OWNER_QQ = "987654321"  # 主人，报备用
HEARTBEAT_STALE = 300  # 心跳超过 5 分钟没更新就算机器人卡了
MAX_REPAIRS_PER_HOUR = 6
TASK_NAME = "FishBotWatchdog"  # 计划任务名：stop_bot.bat 的 --stop 会停用它

log = logging.getLogger("watchdog")


# ---------------------------------------------------------------- 日志
def setup_logging() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.addHandler(fh)
    log.addHandler(sh)


# ---------------------------------------------------------------- 探测
def listening(port: int) -> bool:
    """端口有没有在 LISTENING（用 netstat，避免去戳 NapCat 的 WS）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("netstat 失败：%s", exc)
        return False
    needle = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "TCP" and needle in parts[1] and parts[3].upper() == "LISTENING":
            return True
    return False


def procs_named(name: str) -> int:
    """按镜像名数进程（不区分大小写）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/fi", f"imagename eq {name}", "/fo", "csv", "/nh"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("tasklist 失败：%s", exc)
        return 0
    return sum(1 for line in out.splitlines() if name.lower() in line.lower())


def heartbeat_age() -> float | None:
    """心跳文件多少秒没更新了；没有文件返回 None。"""
    try:
        return max(0.0, time.time() - HEARTBEAT.stat().st_mtime)
    except OSError:
        return None


# ---------------------------------------------------------------- 修复预算
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"repairs": []}


def save_state(state: dict) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("状态写盘失败：%s", exc)


def budget_left(state: dict) -> int:
    now = time.time()
    recent = [t for t in state.get("repairs", []) if now - t < 3600]
    state["repairs"] = recent
    return MAX_REPAIRS_PER_HOUR - len(recent)


def note_repair(state: dict) -> None:
    state.setdefault("repairs", []).append(time.time())
    save_state(state)


# ---------------------------------------------------------------- 修复动作
def _wmi_create(command_line: str, cwd: Path | None = None) -> bool:
    """用 WMI 派生一个进程，父进程是 WmiPrvSE。

    必须走这条路，不能用 subprocess.Popen：计划任务跑完会把 job object 里
    的整棵进程树带走，被任务直接拉起的机器人活不过几分钟。
    """
    work = str(cwd or ROOT).replace("'", "''")
    cmd = command_line.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{cmd}';CurrentDirectory='{work}'}}; "
        "exit $r.ReturnValue"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("WMI 派生失败：%s", exc)
        return False
    if proc.returncode != 0:
        log.error("WMI 派生被拒（返回 %s）：%s", proc.returncode, (proc.stdout or "").strip())
        return False
    return True


def fix_qq() -> bool:
    """杀掉旧 QQ，重新用 NapCat 拉起小号，等 3001 回来。"""
    log.info("修复 QQ 通道：跑 boot_qq.bat（WMI 派生）")
    if not _wmi_create(f'cmd.exe /c "{BOOT_QQ_BAT}"', ROOT):
        log.error("boot_qq.bat 没派出去")
    for _ in range(30):
        if listening(ONEBOT_PORT):
            return True
        time.sleep(6)
    return listening(ONEBOT_PORT)


def fix_wechat() -> bool:
    """把微信拉起来（登录态一般还在；要是停在登录页得人工点一下）。"""
    if not WEIXIN_EXE.exists():
        log.error("找不到微信：%s", WEIXIN_EXE)
        return False
    log.info("修复微信通道：启动 %s", WEIXIN_EXE)
    _wmi_create(f'"{WEIXIN_EXE}"')
    for _ in range(15):
        time.sleep(4)
        if procs_named("Weixin.exe"):
            return True
    return procs_named("Weixin.exe") > 0


def fix_bot() -> bool:
    """重启机器人本体（同样走 WMI，否则会被计划任务连坐）。"""
    if not BOT_PY.exists():
        log.error("找不到项目 venv：%s", BOT_PY)
        return False
    if not RUN_BOT_BAT.exists():
        log.error("找不到 run_bot.bat：%s", RUN_BOT_BAT)
        return False
    log.info("修复机器人：WMI 派生 run_bot.bat")
    return _wmi_create(f'cmd.exe /c "{RUN_BOT_BAT}"', ROOT)


# ---------------------------------------------------------------- 报备
def notify(text: str) -> None:
    """通过 OneBot 给主人发一条私聊；失败就只记日志。"""
    try:
        import asyncio

        from websockets.asyncio.client import connect
    except Exception:  # noqa: BLE001
        log.info("没有 websockets，跳过报备")
        return

    async def _send() -> None:
        async with connect(f"ws://127.0.0.1:{ONEBOT_PORT}", proxy=None, open_timeout=8) as ws:
            await ws.send(json.dumps({
                "action": "send_private_msg",
                "params": {
                    "user_id": int(OWNER_QQ),
                    "message": [{"type": "text", "data": {"text": text}}],
                },
                "echo": "watchdog",
            }, ensure_ascii=False))
            deadline = time.time() + 10
            while time.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("echo") == "watchdog":
                    log.info("已给主人报备（%s）", msg.get("status"))
                    return

    try:
        asyncio.run(_send())
    except Exception as exc:  # noqa: BLE001
        log.warning("报备失败：%s", exc)


# ---------------------------------------------------------------- 主流程
def wechat_enabled() -> bool:
    """配置里 wechat.enabled 关掉时，看门狗就不该去管微信死活。"""
    cfg = ROOT / "config.yaml"
    try:
        import yaml

        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return True  # 读不出来就按老规矩管着
    section = raw.get("wechat")
    if not isinstance(section, dict):
        return True
    return bool(section.get("enabled", True))


def check_once(dry_run: bool = False, do_notify: bool = True) -> dict:
    state = load_state()
    report: dict[str, object] = {}
    actions: list[str] = []

    # 1) QQ 通道
    qq_ok = listening(ONEBOT_PORT)
    report["qq"] = qq_ok

    # 2) 微信通道（关掉了就跳过，不算掉线）
    wx_on = wechat_enabled()
    if wx_on:
        wx_procs = procs_named("Weixin.exe")
        wx_ok = wx_procs > 0
    else:
        wx_procs, wx_ok = 0, True
    report["wechat"] = wx_ok
    report["wechat_procs"] = wx_procs
    report["wechat_enabled"] = wx_on

    # 3) 机器人（心跳）
    age = heartbeat_age()
    bot_ok = age is not None and age < HEARTBEAT_STALE
    report["bot"] = bot_ok
    report["heartbeat_age"] = None if age is None else round(age)

    log.info(
        "体检 QQ=%s 微信=%s(%d 进程) 机器人=%s(心跳 %s)",
        "ok" if qq_ok else "★断",
        "关" if not wx_on else ("ok" if wx_ok else "★断"), wx_procs,
        "ok" if bot_ok else "★断", "无" if age is None else f"{age:.0f}s",
    )
    if qq_ok and wx_ok and bot_ok:
        save_state(state)
        report["actions"] = actions
        return report

    if dry_run:
        log.info("--dry-run：只看不动")
        report["actions"] = ["(dry-run)"]
        return report

    left = budget_left(state)
    if left <= 0:
        log.error("一小时内的修复次数已用完（%d 次），这次只报告不动手", MAX_REPAIRS_PER_HOUR)
        report["actions"] = ["预算用尽"]
        if do_notify and qq_ok:
            notify("【肥鱼看门狗】她又掉线了，而且我一小时内已经修了 "
                   f"{MAX_REPAIRS_PER_HOUR} 次，先停手。你回来看一眼？")
        return report

    # 顺序很重要：先修 QQ（机器人要连它），再修微信，最后修机器人
    if not qq_ok:
        ok = fix_qq()
        actions.append(f"重启 QQ/NapCat：{'成功' if ok else '失败'}")
        note_repair(state)
        report["qq"] = ok

    if wx_on and not wx_ok:
        ok = fix_wechat()
        actions.append(f"启动微信：{'成功' if ok else '失败'}")
        note_repair(state)
        report["wechat"] = ok

    if not bot_ok:
        ok = fix_bot()
        actions.append(f"重启机器人：{'已拉起' if ok else '失败'}")
        note_repair(state)
        report["bot"] = ok

    report["actions"] = actions
    if actions:
        log.info("处理结果：%s", "；".join(actions))
        if do_notify and listening(ONEBOT_PORT):
            time.sleep(20)  # 给机器人一点时间连上 QQ
            notify("【肥鱼看门狗】" + "；".join(actions) + "。现在应该好了，你试试？")
    save_state(state)
    return report


def stop_everything(keep_task: bool = False) -> int:
    """暂停按钮：停掉机器人本体与微信桥接，并（默认）停掉看门狗计划任务。

    注意：NapCat 与 QQ 不动 —— 那是你自己在用的 QQ。
    """
    if not keep_task:
        subprocess.run(["schtasks", "/change", "/tn", TASK_NAME, "/disable"],
                       capture_output=True, text=True, errors="replace")
        log.info("已停用看门狗计划任务 %s", TASK_NAME)

    killed = 0
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'fishbot|wx_bridge' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout
        for token in out.split():
            if token.isdigit():
                subprocess.run(["taskkill", "/f", "/pid", token],
                               capture_output=True, text=True, errors="replace")
                killed += 1
    except Exception as exc:  # noqa: BLE001
        log.error("停进程失败：%s", exc)

    log.info("已停掉 %d 个机器人进程（NapCat 和 QQ 保持不动）", killed)
    print(f"已停掉 {killed} 个机器人进程。")
    print("NapCat 和 QQ 还在跑（那是你自己的 QQ）。")
    if not keep_task:
        print(f"看门狗已暂停；想恢复：schtasks /change /tn {TASK_NAME} /enable")
    print("想重新开：双击 boot_all.bat")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="吃白饭的肥鱼 · 看门狗")
    ap.add_argument("--loop", type=float, default=0, help="常驻循环，间隔秒数")
    ap.add_argument("--dry-run", action="store_true", help="只看不动手")
    ap.add_argument("--no-notify", action="store_true", help="不要发 QQ 报备")
    ap.add_argument("--quiet", action="store_true", help="健康时不打日志（计划任务用）")
    ap.add_argument("--stop", action="store_true", help="暂停：停机器人 + 停看门狗任务")
    ap.add_argument("--keep-task", action="store_true", help="配合 --stop：只停进程，留着看门狗")
    args = ap.parse_args(argv)

    setup_logging()
    if args.quiet:
        log.handlers[1].setLevel(logging.WARNING)

    if args.stop:
        return stop_everything(keep_task=args.keep_task)

    if args.loop > 0:
        log.info("看门狗常驻模式，每 %.0f 秒一次", args.loop)
        while True:
            try:
                check_once(dry_run=args.dry_run, do_notify=not args.no_notify)
            except Exception as exc:  # noqa: BLE001
                log.exception("本轮异常：%s", exc)
            time.sleep(args.loop)

    report = check_once(dry_run=args.dry_run, do_notify=not args.no_notify)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
