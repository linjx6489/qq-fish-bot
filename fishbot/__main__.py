"""入口：python -m fishbot [--config config.yaml] [--simulate] [--check]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .bot import FishBot
from .config import ROOT, AppConfig
from .console import ConsoleTransport
from .onebot import OneBotClient

BANNER = r"""
   ∧___∧      鲸鲸 · QQ 自动回复机器人
  (  ·ω· )    "本鲸只是路过，顺便偷一口 token。"
   \  ~  /     version {ver}
"""


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def health_check(cfg: AppConfig) -> int:
    bot = FishBot(cfg)
    await bot.mem.open()
    await bot.llm.start()
    ok, detail = await bot.llm.health()
    print(f"[模型服务] {'✔ 可用' if ok else '✘ 不可达'} - {detail}")

    if ok:
        messages = [
            {"role": "system", "content": bot.persona.system_prompt(scene="private", speaker="主人", max_chars=60)},
            {"role": "user", "content": "你好呀，你是谁？"},
        ]
        print("[试聊天] 发送：你好呀，你是谁？")
        result = await bot.llm.chat(messages)
        if result.ok:
            print(f"[试聊天] 回复：{result.text}")
            print(f"[试聊天] 用量：{result.usage.as_dict()}（{result.elapsed:.2f}s）")
        else:
            print(f"[试聊天] ✘ {result.error}")
    print(f"[人设] {bot.persona.cfg.name}｜别名：{'、'.join(bot.persona.cfg.aliases)}")
    print(f"[人设文件] {bot.persona.cfg.resolve_prompt_file()}")
    await bot.close()
    return 0 if ok else 1


async def run_console(bot: FishBot) -> None:
    print("本地控制台模式：直接输入消息和她聊天，输入 /quit 退出。\n")
    assert isinstance(bot.transport, ConsoleTransport)
    while True:
        event = await bot.transport.next_event()
        if event is None:
            break
        await bot.on_event(event)


async def amain(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config)
    if args.log_level:
        cfg.log_level = args.log_level
    setup_logging(cfg.log_level)

    if args.check:
        return await health_check(cfg)

    print(BANNER.format(ver=__version__))
    bot = FishBot(cfg)
    bot.cfg_source = str(args.config or (ROOT / "config.yaml"))

    simulate = args.simulate
    if simulate:
        bot.attach(ConsoleTransport(self_id=args.self_id))
    else:
        bot.attach(OneBotClient(cfg.onebot, bot.on_event))

    await bot.start()
    if not simulate:
        mode = "正向" if cfg.onebot.mode == "forward" else "反向"
        target = cfg.onebot.ws_url if cfg.onebot.mode == "forward" else f"{cfg.onebot.listen_host}:{cfg.onebot.listen_port}"
        print(f"已接入 {mode} WebSocket：{target}")
        print(f"模型：{cfg.llm.model} @ {cfg.llm.base_url}")
        print("Ctrl+C 退出\n")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(__import__("signal"), sig), stop.set)
        except (NotImplementedError, AttributeError):  # Windows 部分场景不支持
            pass

    try:
        if simulate:
            console_task = asyncio.create_task(run_console(bot))
            stop_task = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(
                {console_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        else:
            try:
                await stop.wait()
            except KeyboardInterrupt:
                pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n本鲸下班了。")
        await bot.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        # 保留控制台当前代码页，只把解码错误改成替换——
        # 否则 Windows 下按 GBK 解码 UTF-8 输入会生成孤立代理对，直接炸 sqlite。
        sys.stdin.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="fishbot", description="鲸鲸 · QQ 自动回复机器人"
    )
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("--simulate", "-s", action="store_true", help="本地控制台模式，不连接 QQ")
    parser.add_argument("--check", action="store_true", help="只做模型连通性自检后退出")
    parser.add_argument("--self-id", default="10000", help="控制台模式下的机器人 QQ 号")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("--version", action="version", version=f"fishbot {__version__}")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
