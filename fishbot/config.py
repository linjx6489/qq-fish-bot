"""配置加载：config.yaml + 环境变量覆盖。"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _section(cls: type, data: Any):
    """只取 dataclass 已声明的字段，忽略 yaml 里的多余键。"""
    if not isinstance(data, dict):
        return cls()
    allowed = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class LLMConfig:
    base_url: str = "http://127.0.0.1:8792/v1"
    api_key: str = "sk-local"
    model: str = "deepseek-v4-flash-free"
    # 主模型要是有病（只思考不吐正文、被限流、连不上），就按顺序往后换着问
    fallback_models: list[str] = field(default_factory=list)
    temperature: float = 0.9
    top_p: float = 0.95
    max_tokens: int = 400
    timeout: float = 60.0
    stream: bool = True
    retries: int = 2
    extra_body: dict = field(default_factory=dict)
    # 模型把思考塞进 content 时，剥不剥由 textutil.strip_thinking 管；
    # 这里的开关只决定「正文为空时要不要拿 reasoning_content 顶替」——默认不要。
    allow_reasoning_fallback: bool = False
    # 模型输出这个标记时，机器人保持沉默（群聊插话场景很有用）
    silent_marker: str = "[沉默]"

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def models_url(self) -> str:
        return self.base_url.rstrip("/") + "/models"


@dataclass
class PersonaConfig:
    name: str = "鲸鲸"
    aliases: list = field(
        default_factory=lambda: ["鲸鲸", "小鲸", "鲸鲸鱼", "鲸宝", "鲸鱼"]
    )
    prompt_file: str = "personas/jingjing.md"
    max_chars: int = 90
    hard_max_chars: int = 400
    allow_emoji: bool = True

    def resolve_prompt_file(self) -> Path:
        path = Path(self.prompt_file)
        return path if path.is_absolute() else (ROOT / path)


@dataclass
class OneBotConfig:
    mode: str = "forward"  # forward | reverse
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    listen_path: str = "/onebot/v11/ws"
    reconnect_max_seconds: float = 30.0


@dataclass
class TypingDelayConfig:
    enabled: bool = True
    per_char_ms: int = 45
    min_ms: int = 500
    max_ms: int = 3000


@dataclass
class BehaviorConfig:
    private_enabled: bool = True
    private_allow: list = field(default_factory=list)
    private_deny: list = field(default_factory=list)
    group_enabled: bool = True
    group_allow: list = field(default_factory=list)
    group_deny: list = field(default_factory=list)
    group_require_at: bool = True
    group_reply_probability: float = 0.02
    group_min_chars: int = 4
    group_directed_probability: float = 0.85  # 没@她、但话像是冲她说的，接话概率
    group_ignore_at_others: bool = True  # 消息里@的是别人（没@她）就不接
    group_ambient_lines: int = 12
    ignore_bots: bool = True
    reply_to_self: bool = False
    per_session_cooldown: float = 1.5
    global_concurrency: int = 4
    rate_limit_per_minute: int = 20
    quote_reply: bool = False
    reply_prefix_at: bool = False
    typing_delay: TypingDelayConfig = field(default_factory=TypingDelayConfig)
    split_reply: bool = True
    split_max_chars: int = 120


@dataclass
class MemoryConfig:
    db_path: str = "data/fishbot.db"
    max_messages: int = 16
    max_context_chars: int = 6000
    ttl_hours: int = 168

    def resolve_db_path(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else (ROOT / path)


@dataclass
class AdminConfig:
    users: list = field(default_factory=list)
    command_prefix: str = "/"


@dataclass
class AutonomyConfig:
    """自主活动：让肥鱼自己折腾 QQ，而不是只等着别人说话。"""

    enabled: bool = False
    interval_minutes: float = 45.0  # 两次活动之间的基础间隔
    jitter: float = 0.4  # 间隔抖动比例（0~1），避免像定时任务
    max_actions_per_hour: int = 3  # 每小时最多折腾几次
    quiet_hours: str = "00:00-08:00"  # 这段时间不活动，留空=不限制
    actions: list = field(default_factory=lambda: ["signature", "diary", "chat"])
    proactive_private_allow: list = field(default_factory=list)  # 允许主动私聊的 QQ，空=不主动找人
    proactive_group_allow: list = field(default_factory=list)  # 允许主动冒泡的群，空=不主动冒泡
    allow_profile_edit: bool = True  # 允许改个性签名
    allow_nickname_change: bool = False  # 允许改昵称（比较显眼，默认关）
    min_idle_minutes: float = 20.0  # 最近这么久内有人聊过，就安分一点
    follow_up_after_hours: float = 3.0  # 自己上句没人接，隔这么久才允许回访
    max_tokens: int = 200  # 自主活动内部生成的 token 上限
    signature_interval_days: float = 15.0  # 签名限流：这么多天内只准自己动一次
    greet_interval_hours: float = 12.0  # 每隔这么久主动跟主人打个招呼，0=关
    greet_targets: list = field(default_factory=list)  # 招呼发给谁，空=发管理员

    def parsed_quiet_hours(self) -> tuple[int, int] | None:
        """返回 (起始小时, 结束小时)，支持跨天；解析不出来就是 None。"""
        raw = (self.quiet_hours or "").strip()
        if not raw or "-" not in raw:
            return None
        try:
            left, right = raw.split("-", 1)
            start = int(left.strip().split(":")[0])
            end = int(right.strip().split(":")[0])
        except (ValueError, IndexError):
            return None
        if not (0 <= start <= 23 and 0 <= end <= 24):
            return None
        return start, end


@dataclass
class NetConfig:
    """联网：搜索 + 看网页。"""

    enabled: bool = True
    max_results: int = 5  # 一次搜索取几条结果
    timeout: float = 20.0
    max_page_chars: int = 3000  # 单个网页最多塞给模型多少字
    cache_seconds: float = 600.0  # 同一个词多久内不重复搜
    auto: bool = True  # 允许她自己决定要不要查（[[查]] 协议）
    auto_max_per_turn: int = 1  # 一轮对话最多查几次
    show_sources: bool = True  # 需要时把来源附在回复末尾


@dataclass
class StickersConfig:
    enabled: bool = True
    dir: str = "assets/stickers"  # 表情包素材目录
    manifest: str = "manifest.json"  # 素材说明文件（没有就扫目录）
    probability: float = 0.22  # 没被点名时，自己贴一张的概率
    cooldown_seconds: float = 90.0  # 两张表情包之间至少隔这么久
    max_per_hour: int = 12  # 每小时最多贴几张


@dataclass
class ToolsConfig:
    enabled: bool = True  # 总开关
    screenshot: bool = True  # 允许截屏
    sysinfo: bool = True  # 允许报机器状况
    compact: bool = True  # 允许压缩上下文
    file_read: bool = False  # 允许读文件（默认关：会把文件内容发到群里）
    auto: bool = True  # 允许她自己决定动手（不写标记就只能等命令）
    auto_max_per_turn: int = 1  # 一轮最多动一次手
    max_shot_edge: int = 1280  # 截图最长边，缩到这个尺寸再发
    shot_dir: str = "data/shots"  # 截图存哪
    keep_shots: int = 30  # 只留最近几张
    shot_cooldown_seconds: float = 30.0  # 两张截图之间至少隔这么久
    shot_max_per_hour: int = 6  # 每小时最多截几张
    keep_recent: int = 6  # 压缩上下文时，最近这么多条原样留着
    auto_compact_rows: int = 60  # 攒够这么多条新消息就自己压一次（0=不自动压）


@dataclass
class SkillsConfig:
    """借来的本事：把 .dsh/skills 里的 skill 按需塞给她当参考。"""

    enabled: bool = True
    dir: str = r"C:\Users\<你>\.dsh\skills"  # skill 根目录
    allow: list = field(default_factory=lambda: ["goutoujunshi", "nuwa-skill"])
    auto: bool = True  # 聊天内容命中领域就自动把正文递给她
    max_chars: int = 4000  # 一套本事最多塞多少字进上下文
    per_skill: dict = field(default_factory=dict)  # 单独放宽/收紧：{目录名: 字数}
    index_preview: int = 60  # 索引里每套本事的说明显示多少字
    triggers: dict = field(default_factory=dict)  # 覆盖内置触发词：{目录名: [词, ...]}


@dataclass
class VisionConfig:
    """识图：把对方发来的图看一眼，取回一句客观描述。"""

    enabled: bool = True
    model: str = ""  # 留空就用 vision.py 里的 DEFAULT_MODEL
    fallback_models: list = field(default_factory=list)  # 前面那个不吐正文时按顺序换
    auto: bool = True  # 收到图就自动看一眼（关掉就只能用 /看图）
    max_images: int = 3  # 一条消息最多看几张
    max_bytes: int = 6000000  # 单张图最大多少字节
    timeout: float = 60.0
    max_tokens: int = 300
    cache_seconds: float = 3600.0  # 同一张图（按内容哈希）多久内不重复看
    keep_files: bool = True  # 把图存到 data/vision 便于回看
    dir: str = "data/vision"
    keep_recent: int = 50  # 只留最近这么多张
    prompt: str = ""  # 留空用内置提示词


@dataclass
class CtrlConfig:
    """让她能操控自己的 QQ：资料、在线状态、空间说说、点赞。"""

    enabled: bool = True  # 总开关
    nickname: bool = True  # 允许改昵称
    signature: bool = True  # 允许改个性签名
    avatar: bool = True  # 允许换头像
    status: bool = True  # 允许改在线状态
    like: bool = True  # 允许给好友点赞
    qzone_enabled: bool = True  # 允许发 QQ 空间说说
    qzone_delete: bool = True  # 允许删自己刚发的说说
    default_ugc_right: int = 1  # 说说默认可见范围：1 所有人 4 好友 16 部分好友 64 仅自己 128 部分好友不可见
    max_qzone_per_day: int = 5  # 每天最多发几条说说（0=不限）
    only_owner: bool = True  # 只有主人能下令（别人说了不算）


@dataclass
class CtrlConfig:
    """QQ 操控：改昵称/签名/头像/在线状态，发空间说说，点赞。"""

    enabled: bool = True  # 总开关
    qzone_enabled: bool = True  # 单独关掉「发空间」
    default_ugc_right: int = 1  # 说说默认可见：1 所有人 4 好友 16 部分好友 64 仅自己 128 部分好友不可见
    max_qzone_per_day: int = 5  # 一天最多发几条说说（0 = 不限）
    owner_only: bool = True  # 只有主人能用这些命令（改的是她自己的账号）


@dataclass
class CtrlConfig:
    """操控 QQ：改昵称/签名/头像/在线状态，发空间说说，点赞。"""

    enabled: bool = True  # 总开关
    qzone_enabled: bool = True  # 是否允许发 QQ 空间
    max_qzone_per_day: int = 5  # 一天最多发几条说说（0=不限）
    default_ugc_right: int = 1  # 说说的默认可见范围：1所有人 4好友 16部分好友 64仅自己 128部分不可见
    max_chars: int = 600  # 一条说说最多多少字
    announce: bool = True  # 干完活回报一句结果


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    net: NetConfig = field(default_factory=NetConfig)
    stickers: StickersConfig = field(default_factory=StickersConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    ctrl: CtrlConfig = field(default_factory=CtrlConfig)
    ctrl: CtrlConfig = field(default_factory=CtrlConfig)
    ctrl: CtrlConfig = field(default_factory=CtrlConfig)
    ctrl: CtrlConfig = field(default_factory=CtrlConfig)
    log_level: str = "INFO"

    # ---------- 加载 ----------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        cfg_path = Path(path or _env("FISHBOT_CONFIG") or (ROOT / "config.yaml"))
        raw: dict = {}
        if cfg_path.exists():
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        cfg = cls(
            llm=_section(LLMConfig, raw.get("llm")),
            persona=_section(PersonaConfig, raw.get("persona")),
            onebot=_section(OneBotConfig, raw.get("onebot")),
            behavior=_build_behavior(raw.get("behavior")),
            memory=_section(MemoryConfig, raw.get("memory")),
            admin=_section(AdminConfig, raw.get("admin")),
            autonomy=_section(AutonomyConfig, raw.get("autonomy")),
            net=_section(NetConfig, raw.get("net")),
            stickers=_section(StickersConfig, raw.get("stickers")),
            tools=_section(ToolsConfig, raw.get("tools")),
            skills=_section(SkillsConfig, raw.get("skills")),
            vision=_section(VisionConfig, raw.get("vision")),
            ctrl=_section(CtrlConfig, raw.get("ctrl")),
            log_level=str(raw.get("log_level", "INFO")),
        )
        cfg.apply_env()
        cfg._normalize()
        return cfg

    def apply_env(self) -> None:
        if v := _env("FISHBOT_LLM_BASE_URL"):
            self.llm.base_url = v
        if v := _env("FISHBOT_LLM_API_KEY"):
            self.llm.api_key = v
        if v := _env("FISHBOT_LLM_MODEL"):
            self.llm.model = v
        if v := _env("FISHBOT_WS_URL"):
            self.onebot.ws_url = v
        if v := _env("FISHBOT_ACCESS_TOKEN"):
            self.onebot.access_token = v
        if v := _env("FISHBOT_LOG_LEVEL"):
            self.log_level = v

    def _normalize(self) -> None:
        p = self.persona
        p.aliases = [str(a) for a in (p.aliases or []) if str(a).strip()]
        if p.name not in p.aliases:
            p.aliases.insert(0, p.name)
        for key in ("private_allow", "private_deny", "group_allow", "group_deny"):
            setattr(self.behavior, key, [str(x) for x in (getattr(self.behavior, key) or [])])
        self.admin.users = [str(x) for x in (self.admin.users or [])]
        self.llm.fallback_models = [
            str(m).strip()
            for m in (self.llm.fallback_models or [])
            if str(m).strip() and str(m).strip() != self.llm.model
        ]
        self.behavior.group_reply_probability = max(0.0, min(1.0, self.behavior.group_reply_probability))
        for key in ("proactive_private_allow", "proactive_group_allow"):
            setattr(self.autonomy, key, [str(x) for x in (getattr(self.autonomy, key) or [])])
        self.autonomy.actions = [str(a).strip() for a in (self.autonomy.actions or []) if str(a).strip()]
        self.autonomy.interval_minutes = max(1.0, float(self.autonomy.interval_minutes or 45.0))
        self.autonomy.jitter = max(0.0, min(1.0, float(self.autonomy.jitter or 0.0)))
        self.autonomy.max_actions_per_hour = max(0, int(self.autonomy.max_actions_per_hour or 0))
        self.autonomy.signature_interval_days = max(0.0, float(self.autonomy.signature_interval_days or 0))
        self.autonomy.greet_interval_hours = max(0.0, float(self.autonomy.greet_interval_hours or 0))
        self.onebot.listen_path = self.onebot.listen_path or ""
        self.net.max_results = max(1, min(10, int(self.net.max_results or 5)))
        self.net.timeout = max(5.0, float(self.net.timeout or 20.0))
        self.net.max_page_chars = max(300, int(self.net.max_page_chars or 3000))
        self.net.cache_seconds = max(30.0, float(self.net.cache_seconds or 600.0))
        self.net.auto_max_per_turn = max(0, min(3, int(self.net.auto_max_per_turn or 0)))
        self.stickers.probability = max(0.0, min(1.0, float(self.stickers.probability or 0.0)))
        self.stickers.cooldown_seconds = max(0.0, float(self.stickers.cooldown_seconds or 0.0))
        self.stickers.max_per_hour = max(0, int(self.stickers.max_per_hour or 0))
        self.tools.max_shot_edge = max(240, min(3840, int(self.tools.max_shot_edge or 1280)))
        self.tools.keep_shots = max(0, int(self.tools.keep_shots or 0))
        self.tools.shot_cooldown_seconds = max(0.0, float(self.tools.shot_cooldown_seconds or 0.0))
        self.tools.shot_max_per_hour = max(0, int(self.tools.shot_max_per_hour or 0))
        self.tools.auto_max_per_turn = max(0, min(3, int(self.tools.auto_max_per_turn or 0)))
        self.tools.keep_recent = max(1, int(self.tools.keep_recent or 6))
        self.tools.auto_compact_rows = max(0, int(self.tools.auto_compact_rows or 0))
        self.skills.allow = [str(x).strip() for x in (self.skills.allow or []) if str(x).strip()]
        self.skills.max_chars = max(600, min(20000, int(self.skills.max_chars or 4000)))
        self.skills.index_preview = max(0, min(200, int(self.skills.index_preview or 60)))
        if not isinstance(self.skills.per_skill, dict):
            self.skills.per_skill = {}
        if not isinstance(self.skills.triggers, dict):
            self.skills.triggers = {}
        self.behavior.group_directed_probability = max(
            0.0, min(1.0, float(self.behavior.group_directed_probability or 0.0))
        )
        self.vision.max_images = max(1, min(6, int(self.vision.max_images or 3)))
        self.vision.max_bytes = max(100000, int(self.vision.max_bytes or 6000000))
        self.vision.timeout = max(5.0, float(self.vision.timeout or 60.0))
        self.vision.max_tokens = max(60, min(1200, int(self.vision.max_tokens or 300)))
        self.vision.cache_seconds = max(0.0, float(self.vision.cache_seconds or 0.0))
        self.vision.keep_recent = max(0, int(self.vision.keep_recent or 0))
        self.vision.fallback_models = [
            str(m).strip()
            for m in (self.vision.fallback_models or [])
            if str(m).strip()
        ]
        self.ctrl.max_qzone_per_day = max(0, int(self.ctrl.max_qzone_per_day or 0))
        if self.ctrl.default_ugc_right not in (1, 4, 16, 64, 128):
            self.ctrl.default_ugc_right = 1
        if self.onebot.mode not in ("forward", "reverse"):
            raise ValueError(f"onebot.mode 只能是 forward 或 reverse，收到：{self.onebot.mode}")
        self.ctrl.max_qzone_per_day = max(0, min(50, int(self.ctrl.max_qzone_per_day or 0)))
        self.ctrl.default_ugc_right = int(self.ctrl.default_ugc_right or 1)
        if self.ctrl.default_ugc_right not in (1, 4, 16, 64, 128):
            self.ctrl.default_ugc_right = 1


def _build_behavior(data: Any) -> BehaviorConfig:
    cfg = _section(BehaviorConfig, data)
    if isinstance(data, dict) and isinstance(data.get("typing_delay"), dict):
        cfg.typing_delay = _section(TypingDelayConfig, data["typing_delay"])
    return cfg
