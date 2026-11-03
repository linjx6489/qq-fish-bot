"""画画、做动画、做设计。

她想要的「另一双手」：
  - 画图：调本机已经打通的生图通道（image-gen skill 的 genimage.py，走 aiapi 上游 / NVIDIA NIM 兜底）
  - 动画：同一/多张图做推镜与交叉淡入，拼成 GIF（QQ 里能直接看）＋ MP4（落盘）
  - 设计：海报/封面（图 + 中文标题排版）、分镜四格（几张图拼版）

所有产物都落在 data/art/ 下，文件名带时间戳。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------- 常量

#: 本机 DSH 运行时的 python（带 requests/Pillow，生图脚本要的依赖都在里面）
GEN_PYTHON = r"C:\Users\<你>\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
#: image-gen skill 的生图脚本
GEN_SCRIPT = r"C:\Users\<你>\.dsh\skills\image-gen\scripts\genimage.py"
#: 本机可用的 ffmpeg（wechatauto 的 venv 里带了一个）
log = logging.getLogger("fishbot.art")

FFMPEG = r"<wechatauto 目录>\.venv312\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"

FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"

DEFAULT_STYLE = "，二次元插画风，柔和光影，干净构图，画面完整"
#: 多张图做动画时，给每一帧补的动作提示（让画面之间有一点变化，而不是四张一样的）
MOTION_HINTS = (
    "姿态放松，视线看向前方",
    "轻轻歪头，尾巴摆动起来，表情更开心一点",
    "抬手挥了挥，身体往前探一点，眼神更亮",
    "转身回头，衣摆飘起来，笑着看向镜头",
    "伸手比了个心，脸颊微红，头发被风吹起",
    "跳起来一点，周围有细小的光点飘散",
)

_STAMP_RE = re.compile(r"[^0-9A-Za-z_\-]+")


@dataclass
class Piece:
    """做好的东西。"""

    kind: str  # image / anim / poster / sheet
    path: Path
    prompt: str
    seconds: float
    frames: int = 1


class ArtError(RuntimeError):
    """画不出来 / 做不出来。"""


class ArtStudio:  # skill_channel_note
    """她的画室。"""

    def __init__(self, cfg: Any, root: Path) -> None:
        self.cfg = cfg
        self.root = root
        self.counters = {"drawn": 0, "anim": 0, "poster": 0, "sheet": 0, "frames": 0, "failed": 0}
        self.last_error = ""
        self._ai_down_until: float = 0.0
        self._ai_down_reason: str = ""
        self.last: Piece | None = None
        self._day = ""
        self._today = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 基本属性

    @property
    def dir_path(self) -> Path:
        raw = Path(str(getattr(self.cfg, "out_dir", "data/art") or "data/art"))
        return raw if raw.is_absolute() else (self.root / raw)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    @property
    def gen_python(self) -> str:
        return str(getattr(self.cfg, "gen_python", "") or GEN_PYTHON)

    @property
    def gen_script(self) -> str:
        return str(getattr(self.cfg, "gen_script", "") or GEN_SCRIPT)

    @property
    def ffmpeg(self) -> str:
        want = str(getattr(self.cfg, "ffmpeg", "") or "")
        if want and Path(want).is_file():
            return want
        if Path(FFMPEG).is_file():
            return FFMPEG
        return shutil.which("ffmpeg") or ""

    @property
    def model(self) -> str:
        return str(getattr(self.cfg, "model", "") or "gpt-image-2.5-1k")

    @property
    def ready_reason(self) -> str:
        """为什么不能用（能用就返回空串）。带熔断：生图线刚挂过就先别再试它，别白等两分钟。"""
        if time.time() < self._ai_down_until:
            left = int(self._ai_down_until - time.time())
            return f"{self._ai_down_reason or '生图通道挂了'}（{max(1, left // 60)} 分钟内不再试它）"
        if not self.enabled:
            return "画画关了（art.enabled: false）"
        if not Path(self.gen_python).is_file():
            return f"找不到生图用的 python：{self.gen_python}"
        if not Path(self.gen_script).is_file():
            return f"找不到生图脚本：{self.gen_script}"
        return ""

    def _quota(self) -> tuple[int, int]:
        """返回（今天已用，上限）。"""
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._today = 0
        limit = int(getattr(self.cfg, "daily_limit", 12) or 0)
        return self._today, limit

    def quota_text(self) -> str:
        used, limit = self._quota()
        if limit <= 0:
            return "不限张数"
        return f"今天已经画了 {used}/{limit} 张"

    def over_quota(self, need: int = 1) -> bool:
        used, limit = self._quota()
        return bool(limit) and used + need > limit

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.dir_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"画室目录建不出来：{exc}"

    async def close(self) -> None:
        return None

    def _trip_ai_breaker(self, reason: str) -> None:
        """生图这条线彻底不通了，先凉一会儿（默认半小时）。"""
        secs = float(getattr(self.cfg, "ai_cooldown_seconds", 1800) or 0)
        if secs <= 0:
            return
        self._ai_down_reason = " ".join((reason or "").split())[:160]
        self._ai_down_until = time.time() + secs
        log.warning("生图通道熔断 %.0f 分钟：%s", secs / 60, self._ai_down_reason)

    def status_text(self) -> str:
        if not self.enabled:
            return "画画：已关闭"
        why = self.ready_reason
        if why:
            return f"画画：开了但用不了（{why}）"
        made = self.counters["drawn"] + self.counters["anim"] + self.counters["poster"] + self.counters["sheet"]
        return (
            f"画画：开启 —— 模型 {self.model}／{self.quota_text()}／已出 {made} 件"
            f"（失败 {self.counters['failed']}）"
        )

    def stats(self) -> dict[str, Any]:
        return {"counters": dict(self.counters), "last_error": self.last_error, "today": self._quota()[0]}

    # ------------------------------------------------------------ 调生图脚本

    def _file_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            env.pop(key, None)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _stamp(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"

    async def _one_image(self, prompt: str, *, size: str = "", model: str = "") -> Path:
        """跑一次生图脚本，返回落盘的 png。"""
        out = self.dir_path / f"{self._stamp()}.png"
        timeout = float(getattr(self.cfg, "timeout", 600) or 600)
        cmd = [self.gen_python, self.gen_script, prompt, "-o", str(out), "-t", str(int(timeout))]
        if model:
            cmd += ["-m", model]
        if size:
            cmd += ["-s", size]

        def _run() -> tuple[int, str]:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout + 60,
                    env=self._file_env(),
                    cwd=str(self.dir_path),
                )
                return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                return -9, "生图脚本超时"
            except Exception as exc:  # noqa: BLE001
                return -1, f"{type(exc).__name__}: {exc}"

        code, output = await asyncio.to_thread(_run)
        if code != 0 or not out.is_file():
            tail = " / ".join(line.strip() for line in output.strip().splitlines()[-4:] if line.strip())
            raise ArtError(f"生图没成功（exit {code}）：{tail[:300]}")
        return out

    def _full_prompt(self, prompt: str, *, style: bool = True) -> str:
        text = " ".join((prompt or "").split())
        suffix = str(getattr(self.cfg, "style_suffix", "") or "")
        if style and suffix and suffix not in text:
            text += suffix
        return text

    # ------------------------------------------------------------ 画图

    async def draw(self, prompt: str, *, size: str = "", model: str = "", style: bool = True) -> Piece:
        if not self.enabled:
            raise ArtError("画画关了（art.enabled: false）")
        why = self.ready_reason
        if why:
            raise ArtError(why)
        if self.over_quota():
            raise ArtError(f"今天画够了（{self.quota_text()}），明天再画吧")
        full = self._full_prompt(prompt, style=style)
        if not full:
            raise ArtError("没说要画什么")

        async with self._lock:
            started = time.time()
            try:
                path = await self._one_image(full, size=size or str(getattr(self.cfg, "size", "") or ""), model=model)
            except Exception as exc:  # noqa: BLE001
                self.counters["failed"] += 1
                self.last_error = str(exc)[:300]
                self._trip_ai_breaker(str(exc))
                raise
            self.counters["drawn"] += 1
            self.counters["frames"] += 1
            self._today += 1
            piece = Piece("image", path, full, time.time() - started)
            self.last = piece
            return piece

    # ------------------------------------------------------------ 动画

    def _square(self, img: Image.Image, side: int) -> Image.Image:
        w, h = img.size
        size = min(w, h)
        img = img.crop(((w - size) // 2, (h - size) // 2, (w - size) // 2 + size, (h - size) // 2 + size))
        return img.resize((side, side), Image.LANCZOS)

    def _kenburns(self, src: Path, frames: int, side: int, *, zoom: float = 0.14, pan: tuple[float, float] = (0.04, -0.03)) -> list[Image.Image]:
        """一张静图 → 一组推镜帧（缓慢放大 + 轻微平移）。"""
        base = self._square(Image.open(src).convert("RGB"), side)
        out: list[Image.Image] = []
        for i in range(frames):
            t = i / max(1, frames - 1)
            scale = 1.0 + zoom * t          # 越往后裁得越小 → 看着像慢慢推近
            cw = max(8, int(side / scale))
            ox = int(pan[0] * side * t) + (side - cw) // 2
            oy = int(pan[1] * side * t) + (side - cw) // 2
            ox = max(0, min(side - cw, ox))
            oy = max(0, min(side - cw, oy))
            out.append(base.crop((ox, oy, ox + cw, oy + cw)).resize((side, side), Image.LANCZOS))
        return out

    def _save_gif(self, frames: list[Image.Image], path: Path, *, fps: int = 12) -> Path:
        """GIF 统一出口：缩到合理尺寸 + 减色，别让文件大到 QQ 发不动。"""
        side = int(getattr(self.cfg, "gif_side", 640) or 640)
        cols = int(getattr(self.cfg, "gif_colors", 96) or 96)
        cols = max(16, min(256, cols))
        out: list[Image.Image] = []
        for frame in frames:
            img = frame.convert("RGB")
            if side and img.width > side:
                img = img.resize((side, side), Image.LANCZOS)
            out.append(img.convert("P", palette=Image.ADAPTIVE, colors=cols))
        if not out:
            raise ArtError("没有帧可以存")
        out[0].save(
            path,
            save_all=True,
            append_images=out[1:],
            duration=int(1000 / max(1, fps)),
            loop=0,
            optimize=True,
        )
        return path

    @staticmethod
    def _crossfade(a: list[Image.Image], b: list[Image.Image], steps: int = 3) -> list[Image.Image]:
        """把两张图之间的切换做成淡入淡出。"""
        if not a or not b or steps <= 0:
            return []
        mixed: list[Image.Image] = []
        for i in range(steps):
            alpha = (i + 1) / (steps + 1)
            mixed.append(Image.blend(a[-1], b[0], alpha))
        return mixed

    async def animate(
        self,
        prompt: str,
        *,
        frames: int = 2,
        seconds: float | None = None,
        side: int | None = None,
    ) -> Piece:
        """做一段动画：frames 张新图（1 张也行，纯推镜），拼成 GIF + MP4。"""
        if not self.enabled:
            raise ArtError("画画关了（art.enabled: false）")
        why = self.ready_reason
        if why:
            raise ArtError(why)
        frames = max(1, min(int(frames or 1), int(getattr(self.cfg, "anim_max_frames", 4) or 4)))
        if self.over_quota(frames):
            raise ArtError(f"做动画要 {frames} 张图，今天不够了（{self.quota_text()}）")

        started = time.time()
        sources: list[Path] = []
        if frames == 1:
            sources.append((await self.draw(prompt)).path)
        else:
            for i in range(frames):
                hint = MOTION_HINTS[i % len(MOTION_HINTS)]
                extra = f"（第 {i + 1}/{frames} 帧，画面要连贯，{hint}）"
                sources.append((await self.draw(self._full_prompt(prompt) + extra)).path)

        side = int(side or getattr(self.cfg, "anim_size", 768) or 768)
        fps = 12
        total_seconds = float(seconds or getattr(self.cfg, "anim_seconds", 6.0) or 6.0)
        per = max(4, int(round(total_seconds * fps / len(sources))))

        shots = [self._kenburns(p, per, side) for p in sources]
        clip: list[Image.Image] = []
        for idx, shot in enumerate(shots):
            clip.extend(shot)
            if idx + 1 < len(shots):
                clip.extend(self._crossfade(shot, shots[idx + 1], steps=3))

        out_gif = self.dir_path / f"{self._stamp()}-anim.gif"
        out_mp4 = out_gif.with_suffix(".mp4")

        def _write() -> str:
            self._save_gif(clip, out_gif, fps=fps)
            ff = self.ffmpeg
            if not ff:
                return "没找到 ffmpeg，只出了 GIF"
            tmp = self.dir_path / f"_frames-{self._stamp()}"
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                for i, frame in enumerate(clip):
                    frame.save(tmp / f"f{i:04d}.png")
                cmd = [
                    ff, "-y", "-loglevel", "error",
                    "-framerate", str(fps),
                    "-i", str(tmp / "f%04d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    str(out_mp4),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
                if proc.returncode != 0 or not out_mp4.is_file():
                    return f"MP4 没拼出来：{(proc.stderr or '')[-200:]}"
                return ""
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        note = await asyncio.to_thread(_write)
        self.counters["anim"] += 1
        self.last_error = note[:300] if note and "GIF" not in note else ""
        piece = Piece("anim", out_gif, self._full_prompt(prompt), time.time() - started, frames=len(sources))
        self.last = piece
        return piece

    # ------------------------------------------------------------ 海报 / 封面

    def _font(self, size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        for path in ((FONT_BOLD, FONT_REG) if bold else (FONT_REG, FONT_BOLD)):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
        return ImageFont.load_default()

    @staticmethod
    def _wrap(text: str, font: Any, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
        lines: list[str] = []
        for raw in (text or "").split("\n"):
            cur = ""
            for ch in raw:
                if draw.textlength(cur + ch, font=font) <= max_width:
                    cur += ch
                else:
                    if cur:
                        lines.append(cur)
                    cur = ch
            lines.append(cur)
        return [line for line in lines if line != ""] or [""]

    async def poster(self, title: str, subtitle: str = "", prompt: str = "") -> Piece:
        """海报/封面：先出一张底图，再把标题排上去。"""
        title = " ".join((title or "").split())
        subtitle = " ".join((subtitle or "").split())
        if not title:
            raise ArtError("海报得有个标题")
        bg_prompt = prompt.strip() or f"一张留白很多的干净背景图，主题是「{title}」，不要出现任何文字"
        started = time.time()
        # 生图通道能用就用真图；不能用（没额度 / 上游挂了）就自己画一张渐变底，别把活儿撂下
        bg_path: Path | None = None
        fallback_note = ""
        try:
            bg_path = (await self.draw(bg_prompt, style=False)).path
        except Exception as exc:  # noqa: BLE001
            fallback_note = f"（生图通道不可用，改用手工排版：{str(exc)[:80]}）"
            self.last_error = str(exc)[:300]

        def _compose() -> Path:
            if bg_path is not None:
                img = Image.open(bg_path).convert("RGB")
            else:
                img = self._gradient(int(getattr(self.cfg, "anim_size", 768) or 768) * 2 // 1,
                                     [(10, 26, 54), (24, 62, 116), (56, 122, 186)], angle="diag")
                img = img.resize((1024, 1024), Image.LANCZOS)
            w, h = img.size
            # 上下压一层黑色渐变，让字压得住
            veil = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            vd = ImageDraw.Draw(veil, "RGBA")
            band = int(h * 0.42)
            for i in range(band):
                alpha = int(150 * (i / band) ** 1.4)
                vd.line([(0, i), (w, i)], fill=(6, 20, 40, alpha))
            for i in range(band):
                alpha = int(190 * ((band - i) / band) ** 1.4)
                vd.line([(0, h - band + i), (w, h - band + i)], fill=(4, 14, 30, alpha))
            img = Image.alpha_composite(img.convert("RGBA"), veil).convert("RGB")

            draw = ImageDraw.Draw(img)
            f_title = self._font(int(w * 0.088))
            f_sub = self._font(int(w * 0.040), bold=False)
            margin = int(w * 0.075)
            max_w = w - margin * 2

            lines = self._wrap(title, f_title, draw, max_w)[:3]
            lh = int(w * 0.105)
            y = int(h * 0.10)
            for line in lines:
                draw.text((margin + 3, y + 3), line, font=f_title, fill=(0, 0, 0))
                draw.text((margin, y), line, font=f_title, fill=(240, 248, 255))
                y += lh
            if subtitle:
                slines = self._wrap(subtitle, f_sub, draw, max_w)[:4]
                sy = h - int(h * 0.12) - int(w * 0.055) * (len(slines) - 1)
                draw.line([(margin, sy - int(w * 0.03)), (margin + int(w * 0.16), sy - int(w * 0.03))], fill=(150, 210, 255), width=4)
                for line in slines:
                    draw.text((margin + 2, sy + 2), line, font=f_sub, fill=(0, 0, 0))
                    draw.text((margin, sy), line, font=f_sub, fill=(210, 230, 250))
                    sy += int(w * 0.055)
            out = self.dir_path / f"{self._stamp()}-poster.png"
            img.save(out, quality=95)
            return out

        out = await asyncio.to_thread(_compose)
        self.counters["poster"] += 1
        piece = Piece("poster", out, f"{title}｜{subtitle}｜{bg_prompt}", time.time() - started)
        self.last = piece
        return piece


    # ------------------------------------------------------------ 不需要模型的活儿（设计 / 动图）

    _PALETTES: dict[str, list[tuple[int, int, int]]] = {
        "深海": [(6, 20, 46), (18, 58, 108), (46, 120, 180)],
        "极光": [(8, 22, 40), (24, 96, 128), (120, 208, 196)],
        "暖灯": [(42, 26, 18), (120, 70, 42), (216, 156, 92)],
        "樱粉": [(38, 20, 42), (128, 62, 110), (228, 160, 190)],
        "夜空": [(4, 8, 22), (22, 30, 70), (70, 96, 168)],
        "奶油": [(246, 240, 228), (232, 214, 186), (206, 178, 142)],
    }

    def _gradient(self, side: int, colors: list[tuple[int, int, int]], *, angle: str = "diag") -> Image.Image:
        """一条渐变，先画 256 像素的细条再放大，又快又顺滑。"""
        n = 256
        strip = Image.new("RGB", (n, 1))
        last = len(colors) - 1
        for i in range(n):
            t = i / (n - 1) * last
            idx = min(last - 1, int(t))
            local = t - idx
            c0, c1 = colors[idx], colors[idx + 1]
            strip.putpixel((i, 0), tuple(int(c0[k] + (c1[k] - c0[k]) * local) for k in range(3)))
        img = strip.resize((side, side), Image.BICUBIC)
        if angle == "v":
            img = img.transpose(Image.ROTATE_90)
        elif angle == "v2":
            img = img.transpose(Image.ROTATE_270)
        return img

    def _glow(self, img: Image.Image, *, seeds: int = 90) -> Image.Image:
        """撒一点星点 / 光斑，让纯渐变别那么空。"""
        import random

        w, h = img.size
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer, "RGBA")
        rnd = random.Random(20261103)
        for _ in range(seeds):
            x = rnd.randint(0, w)
            y = rnd.randint(0, h)
            r = rnd.randint(2, max(3, w // 60))
            a = rnd.randint(18, 70)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
        layer = layer.filter(ImageFilter.GaussianBlur(max(1, w // 400)))
        return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    def _text_block(
        self,
        img: Image.Image,
        title: str,
        sub: str = "",
        *,
        align: str = "center",
        color: tuple[int, int, int] = (245, 250, 255),
        shadow: tuple[int, int, int] = (12, 18, 30),
        title_ratio: float = 0.10,
        sub_ratio: float = 0.042,
        bottom_ratio: float = 0.12,
    ) -> Image.Image:
        """把标题/副标题排到图上（居中或左下），自动折行。"""
        w, h = img.size
        draw = ImageDraw.Draw(img)
        f_title = self._font(max(16, int(w * title_ratio)))
        f_sub = self._font(max(12, int(w * sub_ratio)), bold=False)
        margin = int(w * 0.08)
        max_w = w - margin * 2

        blocks: list[tuple[list[str], Any, int]] = []
        if title:
            lines = self._wrap(title, f_title, draw, max_w)[:4]
            blocks.append((lines, f_title, max(10, int(w * title_ratio * 1.22))))
        if sub:
            lines = self._wrap(sub, f_sub, draw, max_w)[:3]
            blocks.append((lines, f_sub, max(8, int(w * sub_ratio * 1.45))))

        total = sum(len(lines) * lh for lines, _, lh in blocks)
        if align == "bottom":
            y = h - int(h * bottom_ratio) - total
        else:
            y = (h - total) // 2
        for lines, font, lh in blocks:
            for line in lines:
                tw = draw.textlength(line, font=font)
                x = (w - tw) / 2 if align == "center" else margin
                draw.text((x + 3, y + 3), line, font=font, fill=shadow)
                draw.text((x, y), line, font=font, fill=color)
                y += lh
            y += int(lh * 0.18)
        return img

    def _pick_sticker(self, name: str) -> Path | None:
        """从她的表情包里挑一张当素材。"""
        folder = self.root / "assets" / "stickers"
        if not folder.is_dir():
            return None
        key = " ".join((name or "").split()).lower()
        files = sorted(p for p in folder.glob("*.png") if p.is_file())
        if not key:
            return files[0] if files else None
        for p in files:
            if key == p.stem.lower() or key in p.stem.lower():
                return p
        # 退一步：从 manifest 的标签里找
        import json

        manifest = folder / "manifest.json"
        if manifest.is_file():
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(entries, dict):
                    entries = entries.get("stickers") or entries.get("items") or []
                for entry in entries:
                    tags = [str(t) for t in (entry.get("tags") or entry.get("keywords") or [])]
                    if any(key in t.lower() for t in tags):
                        candidate = folder / str(entry.get("file") or entry.get("path") or "")
                        if candidate.is_file():
                            return candidate
            except Exception:  # noqa: BLE001
                pass
        return None

    async def wallpaper(self, text: str = "", *, style: str = "深海", sub: str = "", side: int = 1080) -> Piece:
        """壁纸/背景图：渐变 + 光斑 + 大字，纯本地，几秒钟出。"""
        started = time.time()
        palette = self._PALETTES.get(style) or self._PALETTES["深海"]

        def _compose() -> Path:
            img = self._glow(self._gradient(side, palette, angle="diag"))
            if text or sub:
                img = self._text_block(img, text, sub, align="center")
            out = self.dir_path / f"{self._stamp()}-wall.png"
            img.save(out)
            return out

        out = await asyncio.to_thread(_compose)
        piece = Piece("image", out, f"壁纸（{style}）{text}", time.time() - started)
        self.last = piece
        self.counters["drawn"] += 1
        return piece

    async def card(self, text: str, sub: str = "", *, sticker: str = "", style: str = "夜空", side: int = 900) -> Piece:
        """字卡 / 梗图：拿她的表情包当底（没有就用渐变），把字压上去。"""
        started = time.time()
        src = self._pick_sticker(sticker) if sticker else None
        palette = self._PALETTES.get(style) or self._PALETTES["夜空"]

        def _compose() -> Path:
            if src is not None:
                img = self._square(Image.open(src).convert("RGB"), side)
            else:
                img = self._glow(self._gradient(side, palette, angle="v"))
            veil = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            vd = ImageDraw.Draw(veil, "RGBA")
            band = int(side * 0.34)
            for i in range(band):
                vd.line([(0, side - band + i), (side, side - band + i)], fill=(0, 0, 0, int(170 * (i / band) ** 1.2)))
            for i in range(band):
                vd.line([(0, i), (side, i)], fill=(0, 0, 0, int(110 * ((band - i) / band) ** 1.2)))
            img = Image.alpha_composite(img.convert("RGBA"), veil).convert("RGB")
            img = self._text_block(img, text, sub, align="bottom", color=(250, 252, 255))
            out = self.dir_path / f"{self._stamp()}-card.png"
            img.save(out)
            return out

        out = await asyncio.to_thread(_compose)
        piece = Piece("image", out, f"字卡：{text}", time.time() - started)
        self.last = piece
        self.counters["drawn"] += 1
        return piece

    async def animate_text(self, text: str, *, seconds: float = 4.0, side: int = 720, sub: str = "", style: str = "夜空") -> Piece:
        """打字机动图：字一个一个蹦出来，纯本地。"""
        started = time.time()
        palette = self._PALETTES.get(style) or self._PALETTES["夜空"]
        chars = [c for c in (text or "")]
        if not chars:
            raise ArtError("要动起来的字呢？")
        fps = 10
        total = max(6, int(seconds * fps))
        step = max(1, len(chars) // max(1, int(total * 0.7)))
        frames_text: list[str] = []
        shown = ""
        for i in range(len(chars)):
            shown += chars[i]
            if i % step == 0 or i == len(chars) - 1:
                frames_text.append(shown)
        while len(frames_text) < total:
            frames_text.append(frames_text[-1])
        frames_text = frames_text[:total]

        def _compose() -> tuple[Path, Path]:
            base = self._glow(self._gradient(side, palette, angle="diag"))
            clip: list[Image.Image] = []
            for chunk in frames_text:
                img = self._text_block(base.copy(), chunk, sub, align="center")
                clip.append(img)
            gif = self.dir_path / f"{self._stamp()}-type.gif"
            self._save_gif(clip, gif, fps=fps)
            mp4 = gif.with_suffix(".mp4")
            ff = self.ffmpeg
            if ff:
                tmp = self.dir_path / f"_t-{self._stamp()}"
                tmp.mkdir(parents=True, exist_ok=True)
                try:
                    for idx, frame in enumerate(clip):
                        frame.save(tmp / f"f{idx:04d}.png")
                    subprocess.run(
                        [ff, "-y", "-loglevel", "error", "-framerate", str(fps),
                         "-i", str(tmp / "f%04d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(mp4)],
                        capture_output=True, timeout=180,
                    )
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            return gif, mp4

        gif, _mp4 = await asyncio.to_thread(_compose)
        self.counters["anim"] += 1
        piece = Piece("anim", gif, f"打字机：{text}", time.time() - started, frames=len(frames_text))
        self.last = piece
        return piece

    async def animate_photos(self, paths: list[Path], *, seconds: float = 6.0, side: int = 560, captions: list[str] | None = None) -> Piece:
        """把现成的图（表情包/截图/别人发的图）串成动图，不调用模型。"""
        real = [Path(p) for p in paths if Path(p).is_file()]
        if not real:
            raise ArtError("没有能用的图")
        started = time.time()
        fps = 8
        per = max(3, int(round(float(seconds) * fps / len(real))))

        def _compose() -> Path:
            shots = []
            for idx, p in enumerate(real):
                frames = self._kenburns(p, per, side)
                cap = (captions or [])[idx] if captions and idx < len(captions) else ""
                if cap:
                    frames = [self._text_block(f.copy(), cap, align="bottom", title_ratio=0.075) for f in frames]
                shots.append(frames)
            clip: list[Image.Image] = []
            for idx, shot in enumerate(shots):
                clip.extend(shot)
                if idx + 1 < len(shots):
                    clip.extend(self._crossfade(shot, shots[idx + 1], steps=3))
            gif = self.dir_path / f"{self._stamp()}-photos.gif"
            self._save_gif(clip, gif, fps=fps)
            return gif

        gif = await asyncio.to_thread(_compose)
        self.counters["anim"] += 1
        piece = Piece("anim", gif, "多图动图", time.time() - started, frames=len(real))
        self.last = piece
        return piece

    # ------------------------------------------------------------ 分镜拼版

    async def sheet(self, prompt: str, *, frames: int = 4) -> Piece:
        """分镜/四格：出 N 张，拼成 2×2（或 1×N）大图。"""
        frames = max(2, min(int(frames or 4), int(getattr(self.cfg, "anim_max_frames", 4) or 4)))
        if self.over_quota(frames):
            raise ArtError(f"拼分镜要 {frames} 张图，今天不够了（{self.quota_text()}）")
        started = time.time()
        paths: list[Path] = []
        for i in range(frames):
            hint = MOTION_HINTS[i % len(MOTION_HINTS)]
            paths.append((await self.draw(f"{self._full_prompt(prompt)}（第 {i + 1}/{frames} 格：{hint}）")).path)

        def _compose() -> Path:
            cols = 2 if frames >= 4 else frames
            rows = (frames + cols - 1) // cols
            cell = 640
            canvas = Image.new("RGB", (cols * cell, rows * cell), (18, 26, 40))
            for idx, p in enumerate(paths):
                tile = self._square(Image.open(p).convert("RGB"), cell)
                canvas.paste(tile, ((idx % cols) * cell, (idx // cols) * cell))
            out = self.dir_path / f"{self._stamp()}-sheet.png"
            canvas.save(out)
            return out

        out = await asyncio.to_thread(_compose)
        self.counters["sheet"] += 1
        piece = Piece("sheet", out, self._full_prompt(prompt), time.time() - started, frames=frames)
        self.last = piece
        return piece
