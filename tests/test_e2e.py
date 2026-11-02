#!/usr/bin/env python
"""端到端测试：用假模型 + 假 QQ 端把整条链路真跑一遍。

    python tests/test_e2e.py

覆盖：连接、私聊回复、记忆累积、token 统计、重置、群聊 @ 触发、
未 @ 不插话、长回复分句、模型故障兜底、管理员静音开关。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK，会炸在 ①②③ 上
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from mock_openai import MockOpenAI  # noqa: E402
from mock_onebot import MockOneBot, action_text, group_event, private_event  # noqa: E402

ADMIN_ID = 999999
USER_ID = 10001
GROUP_ID = 88888
SELF_ID = 3000000

CONFIG_TEMPLATE = """\
llm:
  base_url: {base_url}
  api_key: test-key
  model: deepseek-v4-flash-free
  temperature: 0.9
  top_p: 0.95
  max_tokens: 400
  timeout: 30
  stream: true
  retries: 0

persona:
  name: 鲸鲸
  aliases: [肥鱼, 大肥鱼, 鲸鲸, 鱼鱼]
  prompt_file: {prompt_file}
  max_chars: 90
  hard_max_chars: 400
  allow_emoji: true

onebot:
  mode: forward
  ws_url: {ws_url}
  access_token: ""
  reconnect_max_seconds: 5

behavior:
  private_enabled: true
  group_enabled: true
  group_require_at: true
  group_reply_probability: 0.0
  group_min_chars: 4
  group_ambient_lines: 12
  ignore_bots: true
  reply_to_self: false
  per_session_cooldown: 0
  global_concurrency: 4
  rate_limit_per_minute: 0
  quote_reply: false
  reply_prefix_at: false
  typing_delay:
    enabled: false
    per_char_ms: 0
    min_ms: 0
    max_ms: 0
  split_reply: true
  split_max_chars: 60

memory:
  db_path: {db_path}
  max_messages: 16
  max_context_chars: 6000
  ttl_hours: 168

admin:
  users: [{admin_id}]
  command_prefix: "/"

log_level: INFO
"""


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append((name, bool(ok), detail))
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name}" + (f"   ({detail})" if detail else ""), flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.items if not ok]


def sends(actions: list[dict], action: str) -> list[dict]:
    return [a for a in actions if a.get("action") == action]


def texts(actions: list[dict], action: str) -> list[str]:
    return [action_text(a) for a in sends(actions, action)]


def flat(actions: list[dict], action: str) -> str:
    return " | ".join(texts(actions, action))


def write_config(tmp: Path, llm: MockOpenAI, ob: MockOneBot) -> Path:
    path = tmp / "config.yaml"
    path.write_text(
        CONFIG_TEMPLATE.format(
            base_url=llm.base_url,
            ws_url=ob.url,
            prompt_file=str(ROOT / "personas" / "jingjing.md"),
            db_path=str(tmp / "fishbot.db"),
            admin_id=ADMIN_ID,
        ),
        encoding="utf-8",
    )
    return path


async def launch_bot(cfg_path: Path):
    log_path = cfg_path.parent / "bot.log"
    handle = open(log_path, "wb")
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fishbot",
        "--config",
        str(cfg_path),
        cwd=str(ROOT),
        stdout=handle,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    return proc, log_path, handle


def tail(path: Path, limit: int = 2500) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<无日志>"
    return data[-limit:]


async def main() -> int:
    report = Report()
    print("=" * 62)
    print(" 鲸鲸 · 端到端测试")
    print(f" python: {sys.executable}")
    print(f" 项目根: {ROOT}")
    print("=" * 62)

    llm = MockOpenAI()
    llm.start()
    ob = MockOneBot(self_id=SELF_ID)
    await ob.start()

    tmp = Path(tempfile.mkdtemp(prefix="fishbot-e2e-"))
    cfg_path = write_config(tmp, llm, ob)
    proc, log_path, handle = await launch_bot(cfg_path)

    try:
        # ① 连接
        connected = await ob.wait_connection(timeout=25)
        report.check("① 机器人已连接 QQ 端（正向 WebSocket）", connected, f"mock_onebot:{ob.port}")
        if not connected:
            print(tail(log_path))
            return 1

        # ② 私聊回复
        mark = ob.mark()
        await ob.inject(private_event("你好呀，你叫什么名字", user_id=USER_ID, message_id=101))
        got = await ob.collect(mark, 1, timeout=20)
        reply1 = flat(got, "send_private_msg")
        report.check("② 私聊消息能收到模型回复", bool(reply1.strip()), reply1[:60])

        req1 = llm.last_request or {}
        msgs1 = req1.get("messages", [])
        report.check(
            "②b 请求体包含 system 人设 + 用户消息",
            len(msgs1) == 2 and msgs1[0]["role"] == "system" and "肥鱼" in msgs1[0]["content"],
            f"{len(msgs1)} 条",
        )
        report.check("②c 模型名按配置透传", req1.get("model") == "deepseek-v4-flash-free", str(req1.get("model")))

        # ③ 记忆
        mark = ob.mark()
        await ob.inject(private_event("我刚才说的第一句话是什么？", user_id=USER_ID, message_id=102))
        await ob.collect(mark, 1, timeout=20)
        msgs2 = (llm.last_request or {}).get("messages", [])
        report.check(
            "③ 上下文被记住（第二轮请求带上历史）",
            len(msgs2) >= 4 and [m["role"] for m in msgs2[:3]] == ["system", "user", "assistant"],
            f"{len(msgs1)} → {len(msgs2)} 条",
        )

        # ④ token 统计
        mark = ob.mark()
        await ob.inject(private_event("/token", user_id=USER_ID, message_id=103))
        got = await ob.collect(mark, 1, timeout=20)
        tok = flat(got, "send_private_msg")
        nums = re.findall(r"\d+", tok)
        report.check("④ /token 返回真实 token 数字", "token" in tok and any(int(n) > 0 for n in nums or ["0"]), tok[:70])

        # ⑤ 重置
        mark = ob.mark()
        await ob.inject(private_event("/重置", user_id=USER_ID, message_id=104))
        got = await ob.collect(mark, 1, timeout=20)
        reset_txt = flat(got, "send_private_msg")
        report.check("⑤ /重置 清空该会话记忆", "吃掉" in reset_txt, reset_txt[:60])

        mark = ob.mark()
        await ob.inject(private_event("重置之后的第一句话", user_id=USER_ID, message_id=105))
        await ob.collect(mark, 1, timeout=20)
        msgs3 = (llm.last_request or {}).get("messages", [])
        report.check("⑤b 重置后请求只剩 system + user", len(msgs3) == 2, f"{len(msgs3)} 条")

        # ⑥ 群聊 @ 触发
        mark = ob.mark()
        await ob.inject(group_event("在吗", at_bot=True, group_id=GROUP_ID, self_id=SELF_ID, message_id=201))
        got = await ob.collect(mark, 1, timeout=20)
        group_reply = flat(got, "send_group_msg")
        sent = sends(got, "send_group_msg")
        report.check("⑥ 群聊被 @ 时回复", bool(group_reply.strip()) and sent[0]["params"]["group_id"] in (GROUP_ID, str(GROUP_ID)), group_reply[:60])
        report.check(
            "⑥b 群聊消息正文带【昵称】前缀",
            any("【" in str(m.get("content", "")) for m in (llm.last_request or {}).get("messages", []) if m["role"] == "user"),
            "昵称已注入上下文",
        )

        # ⑦ 群聊未 @ 不插话
        mark = ob.mark()
        await ob.inject(group_event("今天天气真不错啊", at_bot=False, group_id=GROUP_ID, self_id=SELF_ID, message_id=202))
        quiet = await ob.quiet(mark, seconds=1.5)
        report.check("⑦ 群聊没被 @ 时不插话", not sends(quiet, "send_group_msg"), f"期间动作 {len(quiet)} 个")

        # ⑧ 长回复分句
        mark = ob.mark()
        await ob.inject(group_event("讲一段长文给我听", at_bot=True, group_id=GROUP_ID, self_id=SELF_ID, message_id=203))
        await ob.collect(mark, 2, timeout=25)
        got = await ob.quiet(mark, seconds=1.0)  # 等剩余的句子都发完
        pieces = [t for t in texts(got, "send_group_msg") if t.strip()]
        report.check("⑧ 长回复被拆成多条发出", len(pieces) >= 2, f"{len(pieces)} 条：{pieces[:2]}")

        # ⑨ 模型故障兜底
        llm.fail_next = 1
        mark = ob.mark()
        await ob.inject(private_event("模型挂掉的时候你会说什么", user_id=USER_ID, message_id=106))
        got = await ob.collect(mark, 1, timeout=20)
        err_txt = flat(got, "send_private_msg")
        report.check("⑨ 上游 500 时发卖萌兜底句而不是崩溃", "本鲸" in err_txt, err_txt[:60])

        # ⑩ 静音权限
        mark = ob.mark()
        await ob.inject(group_event("/闭嘴", at_bot=False, group_id=GROUP_ID, user_id=10002, role="member", self_id=SELF_ID, message_id=204))
        got = await ob.collect(mark, 1, timeout=20)
        report.check("⑩ 普通成员 /闭嘴 被拒绝", "群管理" in flat(got, "send_group_msg"), flat(got, "send_group_msg")[:50])

        mark = ob.mark()
        await ob.inject(group_event("/闭嘴", at_bot=False, group_id=GROUP_ID, user_id=ADMIN_ID, role="admin", self_id=SELF_ID, message_id=205))
        got = await ob.collect(mark, 1, timeout=20)
        report.check("⑩b 管理员 /闭嘴 生效", "安静" in flat(got, "send_group_msg"), flat(got, "send_group_msg")[:50])

        mark = ob.mark()
        await ob.inject(group_event("喂，理我一下", at_bot=True, group_id=GROUP_ID, self_id=SELF_ID, message_id=206))
        quiet = await ob.quiet(mark, seconds=1.5)
        report.check("⑩c 静音后即使被 @ 也不回复", not sends(quiet, "send_group_msg"), f"期间动作 {len(quiet)} 个")

        mark = ob.mark()
        await ob.inject(group_event("/起床", at_bot=False, group_id=GROUP_ID, user_id=ADMIN_ID, role="admin", self_id=SELF_ID, message_id=207))
        got = await ob.collect(mark, 1, timeout=20)
        report.check("⑩d 管理员 /起床 恢复回复", "回来" in flat(got, "send_group_msg"), flat(got, "send_group_msg")[:50])

        mark = ob.mark()
        await ob.inject(group_event("终于理我了", at_bot=True, group_id=GROUP_ID, self_id=SELF_ID, message_id=208))
        got = await ob.collect(mark, 1, timeout=20)
        report.check("⑩e 解除静音后能正常接话", bool(flat(got, "send_group_msg").strip()), flat(got, "send_group_msg")[:50])

        # ⑪ 进程存活
        alive = proc.returncode is None
        report.check("⑪ 机器人进程全程存活", alive, f"returncode={proc.returncode}")
        report.check("⑪b 假模型服务收到多次调用", llm.call_count >= 6, f"{llm.call_count} 次")

    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        handle.close()
        await ob.stop()
        llm.stop()

    print("-" * 62)
    failed = report.failed
    if failed:
        print(f" 结果：{len(report.items) - len(failed)}/{len(report.items)} 通过，失败 {len(failed)} 项：")
        for name in failed:
            print(f"   - {name}")
        print("-" * 62)
        print(" 机器人日志尾部：")
        print(tail(log_path))
        return 1
    print(f" 结果：全部 {len(report.items)} 项通过 ✔")
    print(f" 临时目录：{tmp}")
    return 0


if __name__ == "__main__":
    started = time.time()
    code = asyncio.run(main())
    print(f" 用时 {time.time() - started:.1f}s")
    sys.exit(code)
