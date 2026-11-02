"""文字转语音：edge-tts（神经网络音色）优先，Windows SAPI 兜底。

NapCat 自带 ffmpeg 转码插件，会把 mp3/wav 转成 QQ 要的 silk 再发出去，
所以这里只要产出一个音频文件，交给 record 消息段就行。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("fishbot.tts")

_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
_AT_RE = re.compile(r"^\s*@\S+\s*")
_MD_RE = re.compile(r"[*_`#>]+")
_SPACE_RE = re.compile(r"[\s\u3000]+")
_RATE_RE = re.compile(r"^\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*%\s*$")
_PITCH_RE = re.compile(r"^\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*Hz\s*$", re.I)
_PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")

# 音色白名单校验。
# 踩过的坑：配置里写 zh-CN-XiaobeiNeural（这个名字根本不存在），
# Edge 服务端会把它模糊匹配成 zh-CN-liaoning-XiaobeiNeural —— 东北方言音色，
# 于是整段语音一股地方口音。所以合成前先拿真实音色列表对一遍，对不上就换普通话音色。
_FALLBACK_VOICE = "zh-CN-XiaoxiaoNeural"
_DIALECT_RE = re.compile(r"liaoning|shaanxi|sichuan|guangxi|hunan|henan|wuu|yue|dialect", re.I)
_VOICE_CACHE: set[str] | None = None


def is_dialect_voice(name: str) -> bool:
    """是不是方言音色（东北话/陕西话之类）。"""
    return bool(_DIALECT_RE.search(name or ""))


async def known_voices() -> set[str] | None:
    """拿一次真实音色列表并缓存；取不到就返回 None（此时不拦）。"""
    global _VOICE_CACHE
    if _VOICE_CACHE is not None:
        return _VOICE_CACHE
    try:
        import edge_tts

        voices = await edge_tts.list_voices()
        _VOICE_CACHE = {str(v.get("ShortName", "")) for v in voices}
    except Exception as exc:  # noqa: BLE001
        log.debug("取不到音色列表：%s: %s", type(exc).__name__, exc)
        return None
    return _VOICE_CACHE


def shift_rate(value: str, delta: float) -> str:
    """在 '+12%' 这种写法上加减一个数；解析不了就原样返回。"""
    m = _RATE_RE.match(value or "")
    if not m:
        return value
    base = float(m.group(2)) * (-1 if m.group(1) == "-" else 1)
    return f"{base + delta:+.0f}%"


def shift_pitch(value: str, delta: float) -> str:
    """在 '+4Hz' 这种写法上加减一个数；解析不了就原样返回。

    注意 edge-tts 只收整数 Hz，小数会被它顶回来（Invalid pitch '+9.3Hz'）。
    """
    m = _PITCH_RE.match(value or "")
    if not m:
        return value
    base = float(m.group(2)) * (-1 if m.group(1) == "-" else 1)
    return f"{int(round(base + delta)):+d}Hz"


def _free_phys_mb() -> float:
    """可用物理内存（MB）。读不到就返回 -1，当作「不知道」，不挡路。"""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return -1.0
        return float(st.ullAvailPhys) / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001
        return -1.0


class TTSError(RuntimeError):
    """语音合成失败。"""


# 念出来要清掉的东西。不发音的符号留在文本里，edge 会照着念或者在那儿瞎停顿，
# 听着就是「一顿一顿的、不像人说话」。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\u2B00-\u2BFF"
    "\u2190-\u21FF\u2300-\u23FF\uFE0F\u200D\u20E3]+"
)
# 括号旁白 / 颜文字：(￣▽￣)（小声）[doge]【趴】
_ASIDE_RE = re.compile(r"[（(\[【〔][^（()）\[\]【】〔〕]{0,24}[)）\]】〕]")
# 没带括号的颜文字符号
_KAOMOJI_RE = re.compile(r"[￣▽╯╰ω･_・∀≧≦°ﾟ﹏´｀＾⌒∇⊙皿﹃〃]")
_QUOTE_CHARS_RE = re.compile(r"[\"'“”‘’「」『』《》]")
_ASCII_JUNK_RE = re.compile(r"[*_`#>@^&%+=<>\\/|{}]+")
# 省略号、破折号、波浪线：每一个都会被读成一段沉默，是「不连贯」的头号元凶
_ELLIPSIS_RE = re.compile(r"(…+|\.{3,}|—+|–+|~+|～+)")
_DUP_PUNCT_RE = re.compile(r"([，。！？、；：,.!?])\1+")
_CJK_GAP_RE = re.compile(r"(?<=[，。！？、；：\u4e00-\u9fff])\s+(?=[，。！？、；：\u4e00-\u9fff])")
_SPEAK_END = "。！？…"


def clean_for_speech(text: str) -> str:
    """把聊天文本收拾成「念出来连贯」的一整段。

    聊天里那些不发音的东西必须清掉：颜文字、括号旁白、emoji、连串标点、
    省略号、破折号。留着的话 edge 会照着念，或者在那些位置停顿很久，
    听起来就是机器在一句一句地念稿子。
    """
    s = _CQ_RE.sub(" ", text or "")
    s = _AT_RE.sub("", s)
    s = _MD_RE.sub("", s)
    s = _EMOJI_RE.sub("", s)
    s = _ASIDE_RE.sub("，", s)
    s = _KAOMOJI_RE.sub("", s)
    s = _QUOTE_CHARS_RE.sub("", s)
    s = _ASCII_JUNK_RE.sub("", s)
    s = _ELLIPSIS_RE.sub("，", s)
    s = _DUP_PUNCT_RE.sub(r"\1", s)
    s = _SPACE_RE.sub(" ", s)
    s = _CJK_GAP_RE.sub("", s)  # 中文之间的空格会造成断句感
    s = s.strip(" ，、；：,;:")
    if not s:
        return ""
    if s[-1] not in _SPEAK_END:
        s += "。"
    return s


def split_speech(text: str, limit: int) -> list[str]:
    """按句号切成不超过 limit 字的几段；单段太长就硬切。"""
    if len(text) <= limit:
        return [text]
    pieces = [p for p in re.split(r"(?<=[。！？])", text) if p.strip()]
    out: list[str] = []
    buf = ""
    for piece in pieces:
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            out.append(buf)
            buf = ""
        while len(piece) > limit:
            out.append(piece[:limit])
            piece = piece[limit:]
        buf = piece
    if buf:
        out.append(buf)
    return [p for p in out if p.strip()]


def find_ffmpeg(explicit: str = "") -> str:
    """找一个能用的 ffmpeg（长句要拼成一条完整语音）。找不到就算了。"""
    if explicit:
        path = Path(explicit)
        if path.exists():
            return str(path)
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    for guess in (
        Path(r"<wechatauto 目录>\.venv312\Lib\site-packages\imageio_ffmpeg\binaries"),
        Path.home() / "AppData/Local/Programs/ffmpeg/bin",
    ):
        try:
            for exe in sorted(guess.glob("ffmpeg*.exe")):
                return str(exe)
        except OSError:
            continue
    try:
        import imageio_ffmpeg  # type: ignore

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001
        return ""


# 语气词表：决定这句该念得快活还是念得敷衍
_EXCITED_WORDS = (
    "哈", "嘿", "哇", "咦", "嘻", "好耶", "太棒", "冲", "加油", "笑死",
    "爽", "棒", "厉害", "牛", "漂亮", "可以啊", "有意思", "来", "走起",
)
_FLAT_WORDS = (
    "无语", "行吧", "算了", "懒得", "随便", "呵呵", "敷衍", "麻木",
    "没劲", "闭嘴", "哦", "嗯", "知道了", "不爱", "不想", "烦",
)

# 情绪对语速/音高的加减（百分比 / Hz）。
# 幅度要比随机抖动大一圈，否则情绪的差别会被抖动盖掉。
MOOD_DELTA = {
    "excited": (10.0, 12.0),
    "normal": (0.0, 0.0),
    "flat": (-12.0, -9.0),
}


def detect_mood(text: str) -> str:
    """粗判这句话该用什么调子念。宁可判成 normal，也别乱判。"""
    s = text or ""
    if not s:
        return "normal"
    excited = sum(s.count(w) for w in _EXCITED_WORDS) + s.count("！") + s.count("!")
    flat = sum(s.count(w) for w in _FLAT_WORDS)
    if excited >= 2 and excited > flat:
        return "excited"
    if flat >= 2 and flat > excited:
        return "flat"
    return "normal"


class TTS:
    """只负责「文本 -> 音频文件」，发不发、怎么发交给 bot 决定。"""

    def __init__(self, cfg: Any, root: Path):
        self.cfg = cfg
        self.root = Path(root)
        raw_dir = Path(str(cfg.dir or "data/voice"))
        self.dir = raw_dir if raw_dir.is_absolute() else (self.root / raw_dir)
        self.counters = {"ok": 0, "failed": 0, "too_long": 0}
        self.last_error = ""
        self.last_voice = ""
        self.ffmpeg = find_ffmpeg(str(getattr(cfg, "ffmpeg", "") or ""))
        self._lock = asyncio.Lock()
        self._rng = random.Random()

    # ------------------------------------------------------------ 状态
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _effective_rate(self, mood: str = "normal") -> str:
        """基础语速 + 情绪加减 + 随机浮动。她得活泼，不能每次都一个调子。"""
        if not getattr(self.cfg, "lively", True):
            return self.cfg.rate or "+0%"
        boost = MOOD_DELTA.get(mood, (0.0, 0.0))[0]
        jitter = max(0.0, float(getattr(self.cfg, "rate_jitter", 0) or 0))
        delta = boost + (self._rng.uniform(-jitter, jitter) if jitter else 0.0)
        return shift_rate(self.cfg.rate or "+0%", delta)

    def _effective_pitch(self, mood: str = "normal") -> str:
        """基础音高 + 情绪加减 + 随机浮动。音色是照录音挑的，这里只做微调。"""
        if not getattr(self.cfg, "lively", True):
            return self.cfg.pitch or "+0Hz"
        boost = MOOD_DELTA.get(mood, (0.0, 0.0))[1]
        jitter = max(0.0, float(getattr(self.cfg, "pitch_jitter", 0) or 0))
        delta = boost + (self._rng.uniform(-jitter, jitter) if jitter else 0.0)
        return shift_pitch(self.cfg.pitch or "+0Hz", delta)

    async def _resolve_voice(self) -> str:
        """核一遍配置的音色是否真存在、是否普通话，不合格就退回标准女声。"""
        want = str(self.cfg.voice or "").strip()
        if not want:
            return _FALLBACK_VOICE
        if is_dialect_voice(want):
            log.warning("音色 %s 是方言音色，改用 %s", want, _FALLBACK_VOICE)
            return _FALLBACK_VOICE
        known = await known_voices()
        if known is not None and want not in known:
            log.warning(
                "音色 %s 不在服务端列表里（会被模糊匹配成方言），改用 %s",
                want,
                _FALLBACK_VOICE,
            )
            return _FALLBACK_VOICE
        return want

    def status_text(self) -> str:
        if not self.cfg.enabled:
            return "语音：已关闭（config.yaml 里 tts.enabled: false，她只打字）"
        engine = (self.cfg.engine or "edge").lower()
        tone = "活泼" if getattr(self.cfg, "lively", True) else "平铺"
        return (
            f"语音引擎 {engine}／音色 {self.last_voice or self.cfg.voice}／{tone} "
            f"{self.cfg.rate}±{getattr(self.cfg, 'rate_jitter', 0)}% "
            f"{self.cfg.pitch}±{getattr(self.cfg, 'pitch_jitter', 0)}Hz／"
            f"成功 {self.counters['ok']} 失败 {self.counters['failed']}"
        )

    # ------------------------------------------------------------ 合成
    async def synth(self, text: str) -> Path:
        speech = clean_for_speech(text)
        if not speech:
            raise TTSError("没有可朗读的内容")
        limit = max(20, int(self.cfg.max_chars))
        # 从原始文本判语气（clean_for_speech 会把 ～ 换成逗号）
        mood = detect_mood(text)

        async with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TTSError(f"建不了音频目录：{exc}") from exc

            engine = (self.cfg.engine or "edge").lower()
            if engine in ("auto", ""):
                order = ["edge", "sapi"]
            elif engine == "clone":
                # 克隆优先；服务没起或超时就退回 edge，不能整条哑掉
                order = ["clone", "edge"] if getattr(self.cfg, "clone_fallback", True) else ["clone"]
            elif engine in ("rvc", "convert"):
                # 音色转换：先用 edge 合成干净连贯的中文，再交给 RVC 把音色换成她的
                order = ["rvc", "edge"] if getattr(self.cfg, "rvc_fallback", True) else ["rvc"]
            else:
                order = [engine]
            errors: list[str] = []
            for name in order:
                try:
                    if name == "edge":
                        path = await self._synth_edge_long(speech, mood=mood, limit=limit)
                    elif name == "sapi":
                        path = await self._synth_sapi(speech)
                    elif name == "clone":
                        cap = int(getattr(self.cfg, "clone_max_chars", 60) or 0)
                        if cap and len(speech) > cap:
                            raise TTSError(f"这句话 {len(speech)} 字，超过克隆上限 {cap} 字")
                        path = await self._synth_clone(speech)
                    elif name == "rvc":
                        cap = int(getattr(self.cfg, "rvc_max_chars", 80) or 0)
                        if cap and len(speech) > cap:
                            raise TTSError(f"这句话 {len(speech)} 字，超过音色转换上限 {cap} 字")
                        path = await self._synth_rvc(speech, mood=mood, limit=limit)
                    else:
                        raise TTSError(f"不认识的引擎 {name}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {exc}")
                    log.warning("TTS(%s) 失败：%s: %s", name, type(exc).__name__, exc)
                    continue
                self.counters["ok"] += 1
                self.counters.setdefault(f"mood_{mood}", 0)
                self.counters[f"mood_{mood}"] += 1
                self.last_error = ""
                self._cleanup()
                return path

            self.counters["failed"] += 1
            self.last_error = "；".join(errors) or "没有可用的语音引擎"
            raise TTSError(self.last_error)

    async def _synth_clone(self, speech: str) -> Path:
        """走本机 XTTS 克隆服务：中文文本 + 参考音 → 那条参考音的音色。"""
        url = str(getattr(self.cfg, "clone_url", "") or "").strip().rstrip("/")
        if not url:
            raise TTSError("clone 引擎没配 clone_url")
        payload = json.dumps(
            {"text": speech, "speed": float(getattr(self.cfg, "clone_speed", 1.0) or 1.0)},
            ensure_ascii=False,
        ).encode("utf-8")
        timeout = float(getattr(self.cfg, "clone_timeout", 180.0) or 180.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                f"{url}/say",
                content=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
        if resp.status_code != 200 or not data.get("ok"):
            raise TTSError(f"克隆服务返回 {resp.status_code}：{data.get('error') or resp.text[:120]}")
        src = Path(str(data.get("wav") or ""))
        if not src or not src.exists():
            raise TTSError(f"克隆服务没给出音频：{data}")
        dest = self._new_path(".wav")
        try:
            shutil.copyfile(src, dest)
        except OSError:
            return src          # 复制不动就直接用原路径，别浪费这一次合成
        log.debug("克隆合成：%s → %s（%.1fs）", src.name, dest.name, float(data.get("synth_seconds") or 0))
        return dest

    async def _synth_rvc(self, speech: str, *, mood: str = "normal", limit: int = 220) -> Path:
        """先让 edge 说一条干净连贯的中文，再送去 RVC 换成她的音色（内容与语气不变）。

        为什么不直接用克隆式 TTS：那种做法自带的声码器电子味重、断句也硬。
        转换式只换音色、保留源语音的节奏，听起来自然得多。

        这台机器只有 5.85GB 内存，引擎常驻会把内存吃光（2026-10-22 13:51 那次
        全系统崩机就是它，Resource-Exhaustion-Detector 记到 python 占了 6.6GB 虚拟内存），
        所以改成「按需启动 + 用完自动关」：
          · 可用内存低于 rvc_min_free_mb 就不用引擎，这一条直接退回 edge；
          · 引擎没在跑（连不上）就自己把 start_voice_engine.bat 拉起来，等它就绪再转；
          · 转完记时间，闲 rvc_idle_seconds 秒就 POST /shutdown，把内存还回去。
        """
        url = str(getattr(self.cfg, "rvc_url", "") or "").strip().rstrip("/")
        if not url:
            raise TTSError("rvc 引擎没配 rvc_url")
        floor = float(getattr(self.cfg, "rvc_min_free_mb", 500) or 0)
        free = _free_phys_mb()
        if floor and 0 <= free < floor:
            raise TTSError(f"可用内存只剩 {free:.0f}MB，这一条先用 edge 说")
        base = await self._synth_edge_long(speech, mood=mood, limit=limit)
        data = await self._rvc_convert(url, base)
        if data is None:  # 引擎没在跑：拉起来再试一次
            if not await self._rvc_boot(url):
                raise TTSError(f"音色引擎起不来（{url}），这一条先用 edge 说")
            data = await self._rvc_convert(url, base)
        if data is None:
            raise TTSError("音色引擎刚起来又没响应")
        out = Path(str(data.get("out") or ""))
        if not out.is_file():
            raise TTSError(f"转换后的文件不见了：{out}")
        self._rvc_last = time.time()
        self._rvc_arm_idle(url)
        return out

    async def _rvc_convert(self, url: str, base: Path) -> dict | None:
        """POST /convert；引擎没在跑（连不上）返回 None，其它错误抛 TTSError。"""
        timeout = float(getattr(self.cfg, "rvc_timeout", 300.0) or 300.0)

        def _num(key: str, default: float) -> float:
            try:
                return float(getattr(self.cfg, key, default) or default)
            except (TypeError, ValueError):
                return default

        payload: dict[str, Any] = {
            "audio": str(base),
            "pitch": int(_num("rvc_pitch", 0.0)),
            "protect": _num("rvc_protect", 0.5),
            "filter_radius": int(_num("rvc_filter_radius", 5)),
            "rms_mix": _num("rvc_rms_mix", 0.15),
        }
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.post(f"{url}/convert", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return None
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"音色转换服务没连上（{url}／{type(exc).__name__}）") from exc
        if resp.status_code != 200:
            raise TTSError(f"音色转换失败（HTTP {resp.status_code}）")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise TTSError("音色转换服务返回的不是 JSON") from exc
        if not data.get("ok"):
            raise TTSError(str(data.get("error") or "音色转换失败"))
        return data

    async def _rvc_ready(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.get(f"{url}/health")
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _spawn_engine(self, flags: int) -> bool:
        """直接用引擎自己的解释器把 rvc_server 拉起来。

        不走 bat 里的 `start`：DETACHED_PROCESS 下 cmd 没有控制台，`start` 会静默失败
        （2026-10-22 实测等 240 秒一个进程都没起来，而手动跑同一个 bat 就正常）。
        """
        eng = Path(str(getattr(self.cfg, "rvc_engine_dir", "") or ""))
        if not eng.is_dir():
            return False
        py = eng / ".venv" / "Scripts" / "python.exe"
        srv = eng / "rvc_server.py"
        if not py.is_file() or not srv.is_file():
            log.debug("引擎目录不全：%s", eng)
            return False
        threads = int(getattr(self.cfg, "rvc_threads", 2) or 2)
        try:
            port = int(str(getattr(self.cfg, "rvc_url", "")).rstrip("/").rsplit(":", 1)[-1])
        except Exception:  # noqa: BLE001
            port = 8799
        (eng / "_tmp").mkdir(parents=True, exist_ok=True)
        (eng / "logs").mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            env.pop(key, None)
        env.update({
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "TORCH_HOME": str(eng / "hf" / "torch"), "HF_HOME": str(eng / "hf"),
            "OMP_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads),
            "OMP_WAIT_POLICY": "PASSIVE",
            "TMP": str(eng / "_tmp"), "TEMP": str(eng / "_tmp"),
        })
        args = [str(py), "-W", "ignore", "-u", str(srv), "--port", str(port), "--threads", str(threads)]
        model = str(getattr(self.cfg, "rvc_model", "") or "")
        if model:
            args += ["--model", model]
        pitch = float(getattr(self.cfg, "rvc_pitch", 0.0) or 0.0)
        if pitch:
            args += ["--pitch", str(pitch)]
        try:
            handle = open(eng / "logs" / "rvc_server.log", "ab")
            try:
                subprocess.Popen(
                    args, cwd=str(eng), env=env, creationflags=flags,
                    stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                )
            finally:
                handle.close()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("直接拉引擎失败：%s", exc)
            return False

    async def _rvc_boot(self, url: str) -> bool:
        """引擎没在跑就拉起来；脱离父进程，别跟着机器人一起被关掉。"""
        wait = float(getattr(self.cfg, "rvc_boot_timeout", 150.0) or 150.0)
        root = Path(getattr(self, "root", None) or Path(__file__).resolve().parent.parent)
        bat = root / "start_voice_engine.bat"
        if not bat.is_file():
            log.warning("没有 start_voice_engine.bat，起不了音色引擎")
            return False
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            flags |= int(getattr(subprocess, name, 0) or 0)
        started = self._spawn_engine(flags)
        if not started:
            # 退路：老办法（boot_all.bat / watchdog 那条路也还在用它）
            try:
                subprocess.Popen(
                    ["cmd", "/c", str(bat)],
                    cwd=str(root),
                    creationflags=flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                started = True
            except Exception as exc:  # noqa: BLE001
                log.warning("拉不起音色引擎：%s", exc)
                return False
        log.info("音色引擎没在跑，已经拉起来了（要加载模型 + 预热，冷启动大约一分钟）……")
        deadline = time.time() + max(30.0, wait)
        while time.time() < deadline:
            if await self._rvc_ready(url):
                log.info("音色引擎就绪（等了 %.0f 秒）", max(0.0, wait - (deadline - time.time())))
                return True
            await asyncio.sleep(4.0)
        log.warning("等了 %.0f 秒音色引擎还没就绪", wait)
        return False

    def _rvc_arm_idle(self, url: str) -> None:
        task = getattr(self, "_rvc_idle", None)
        if task is None or task.done():
            self._rvc_idle = asyncio.create_task(self._rvc_idle_watch(url))

    async def _rvc_idle_watch(self, url: str) -> None:
        """闲着就把引擎关掉 —— 它一个进程能吃掉 1.4GB，这机器经不起常驻。"""
        idle = float(getattr(self.cfg, "rvc_idle_seconds", 180.0) or 0)
        if idle <= 0:
            return
        step = 20.0 if idle > 60 else max(5.0, idle / 2)
        while True:
            await asyncio.sleep(step)
            last = float(getattr(self, "_rvc_last", 0.0) or 0.0)
            if time.time() - last < idle:
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                    await client.post(f"{url}/shutdown")
                log.info("音色引擎闲了 %.0f 秒，先关掉把内存还回去", idle)
            except Exception as exc:  # noqa: BLE001
                log.debug("关引擎没成功：%s", exc)
            return

    async def _synth_edge_long(self, speech: str, *, mood: str = "normal", limit: int = 220) -> Path:
        """长句也尽量只做一条语音：分段合成，再用 ffmpeg 拼回去。

        以前超过 max_chars 就直接退回文字，结果她一会儿说一会儿打字；
        现在拼成一条，听着才是一口气说完。
        """
        chunks = split_speech(speech, limit)
        if len(chunks) <= 1:
            return await self._synth_edge(speech, mood=mood)
        if not self.ffmpeg:
            self.counters["too_long"] += 1
            raise TTSError(f"文本太长（{len(speech)} 字 > {limit} 字），而且没找到 ffmpeg 拼不起来")
        parts: list[Path] = []
        # 语速与音高整条只算一次：分段各算一次的话，每段音高都不一样，听着像几个人在接
        rate = self._effective_rate(mood)
        pitch = self._effective_pitch(mood)
        try:
            for chunk in chunks:
                parts.append(await self._synth_edge(chunk, mood=mood, rate=rate, pitch=pitch))
            return self._concat_audio(parts)
        finally:
            for part in parts:
                part.unlink(missing_ok=True)

    def _concat_audio(self, parts: list[Path]) -> Path:
        """把几段 mp3 按顺序拼成一条。"""
        out = self._new_path("mp3")
        listing = self.dir / f"concat-{int(time.time() * 1000)}.txt"
        try:
            listing.write_text(
                "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts) + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [self.ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy", str(out)],
                capture_output=True, timeout=120,
            )
        finally:
            listing.unlink(missing_ok=True)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            out.unlink(missing_ok=True)
            raise TTSError(detail or "拼接语音失败")
        return out

    async def _synth_edge(
        self,
        speech: str,
        mood: str = "normal",
        *,
        rate: str | None = None,
        pitch: str | None = None,
    ) -> Path:
        import edge_tts  # 延迟导入：没装也不影响机器人启动

        path = self._new_path("mp3")
        rate = rate or self._effective_rate(mood)
        pitch = pitch or self._effective_pitch(mood)
        voice = await self._resolve_voice()
        # 本机代理环境变量是坏的（NO_PROXY/no_proxy 冲突、端口畸形），aiohttp 会读它
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.lower() in _PROXY_KEYS}
        try:
            comm = edge_tts.Communicate(
                speech,
                voice,
                rate=rate,
                volume=self.cfg.volume or "+0%",
                pitch=pitch,
            )
            await asyncio.wait_for(comm.save(str(path)), timeout=float(self.cfg.timeout))
        finally:
            os.environ.update(saved)
        if not path.exists() or path.stat().st_size == 0:
            raise TTSError("edge-tts 没有产出音频")
        self.last_voice = voice
        log.debug("语音合成：%s / 语气 %s / %s / %s", voice, mood, rate, pitch)
        return path

    async def _synth_sapi(self, speech: str) -> Path:
        path = self._new_path("wav")
        voice = str(self.cfg.sapi_voice or "").replace("'", "''")
        literal = speech.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            + (f"$s.SelectVoice('{voice}');" if voice else "")
            + f"$s.SetOutputToWaveFile('{path}');"
            + f"$s.Speak('{literal}');"
            "$s.Dispose()"
        )
        # 用 -EncodedCommand（UTF-16LE + base64）绕开 PowerShell 的代码页问题
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=float(self.cfg.timeout))
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise TTSError("SAPI 合成超时") from exc
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:200]
            raise TTSError(detail or f"SAPI 退出码 {proc.returncode}")
        if not path.exists() or path.stat().st_size == 0:
            raise TTSError("SAPI 没有产出音频")
        return path

    # ------------------------------------------------------------ 杂项
    def _new_path(self, suffix: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = hashlib.md5(str(time.time()).encode()).hexdigest()[:6]
        return self.dir / f"voice-{stamp}-{tag}.{suffix}"

    def _cleanup(self) -> None:
        keep = max(0, int(self.cfg.keep_files))
        try:
            files = sorted(self.dir.glob("voice-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass


__all__ = ["TTS", "TTSError", "clean_for_speech", "detect_mood", "shift_pitch", "shift_rate"]
