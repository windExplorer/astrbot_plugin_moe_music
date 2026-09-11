"""歌词渲染为图片（PIL），避免歌词纯文本刷屏。

- 渐变背景逐行绘制（每行一条 1px 水平线），避免逐像素 putpixel 的性能问题；
- LRC 时间轴与元信息标签统一剔除；
- 行数超限自动截断；渲染跑在线程池中，不阻塞事件循环。
"""

import asyncio
import io
import re
from pathlib import Path

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont

# LRC 时间轴 [mm:ss] / [mm:ss.xx]
_RE_TIMELINE = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
# LRC 元信息标签 [ti:...] / [ar:...] 等
_RE_META = re.compile(r"^\[(ti|ar|al|by|offset|hash|total|kana|encoding):.*?\]\s*", re.IGNORECASE)
# 行首残留的多个连续时间轴（部分 LRC 同行多时间轴）
_MAX_LINES = 120

# 常见系统中文字体兜底路径（随包字体缺失时尝试）
_FALLBACK_FONTS = [
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def resolve_font_path(font_path: Path) -> Path:
    """优先使用随包字体，缺失时尝试系统字体；都不存在时原样返回（让 ImageFont 抛明确异常）。"""
    if font_path.exists():
        return font_path
    for candidate in _FALLBACK_FONTS:
        p = Path(candidate)
        if p.exists():
            logger.warning(f"[萌音点歌] 随包字体不存在，使用系统字体：{p}")
            return p
    return font_path  # 让 ImageFont 抛出明确异常，由上层回退文本


class LyricsRenderer:
    """把 LRC 文本渲染成一张歌词图片。"""

    def __init__(self, font_path: Path, width: int = 1000, font_size: int = 30):
        self.width = width
        self.font_size = font_size
        self.line_spacing = 14
        self.h_padding = 60
        self.v_padding = 50
        self.top_color = (245, 247, 252)
        self.bottom_color = (240, 250, 245)
        self.title_color = (85, 105, 160)
        self.text_color = (65, 65, 70)

        self.font_path = self._resolve_font(font_path)
        self._font = ImageFont.truetype(str(self.font_path), self.font_size)
        self._title_font = ImageFont.truetype(str(self.font_path), self.font_size + 8)

    def _resolve_font(self, font_path: Path) -> Path:
        """优先使用随包字体，缺失时尝试系统字体。"""
        return resolve_font_path(font_path)

    @staticmethod
    def clean_lrc(lyrics: str) -> list[str]:
        """剔除时间轴 / 元信息标签 / 多余空行。"""
        lines: list[str] = []
        for raw in lyrics.splitlines():
            line = _RE_META.sub("", raw.strip())
            line = _RE_TIMELINE.sub("", line).strip()
            if not line:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            lines.append(line)
        while lines and lines[-1] == "":
            lines.pop()
        # 压缩多余空行后再截断
        compact: list[str] = []
        for line in lines:
            if line == "" and compact and compact[-1] == "":
                continue
            compact.append(line)
        if len(compact) > _MAX_LINES:
            compact = compact[:_MAX_LINES] + ["", "……（歌词过长已截断）"]
        return compact

    def render(self, lyrics: str, title: str = "", subtitle: str = "") -> bytes:
        """渲染歌词为 JPEG 图片字节流。

        Args:
            lyrics: LRC 文本。
            title: 标题（歌名）。
            subtitle: 副标题（歌手 / 专辑）。
        """
        body_lines = self.clean_lrc(lyrics)
        if not body_lines and not title:
            raise ValueError("歌词内容为空")

        # 预测量行高与宽度
        dummy = Image.new("RGB", (10, 10))
        draw = ImageDraw.Draw(dummy)

        def _measure(text: str, font) -> tuple[int, int]:
            bbox = draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]

        entries: list[tuple[str, object, int, int]] = []  # (text, font, height, width)
        if title:
            w, h = _measure(title, self._title_font)
            entries.append((title, self._title_font, h, w))
        if subtitle:
            w, h = _measure(subtitle, self._font)
            entries.append((subtitle, self._font, h, w))
            entries.append(("", self._font, 10, 0))  # 标题区与正文间隔
        for text in body_lines:
            text = text if text else "　"
            w, h = _measure(text, self._font)
            entries.append((text, self._font, h, w))

        inner_width = max(w for _, _, _, w in entries)
        img_width = max(self.width, inner_width + self.h_padding * 2)
        line_gap = self.line_spacing
        content_height = sum(h for _, _, h, _ in entries) + line_gap * (len(entries) - 1)
        img_height = int(content_height + self.v_padding * 2)

        img = Image.new("RGB", (img_width, img_height))
        painter = ImageDraw.Draw(img)
        # 渐变背景：逐行画 1px 水平线
        for y in range(img_height):
            ratio = y / img_height
            color = tuple(int(t * (1 - ratio) + b * ratio) for t, b in zip(self.top_color, self.bottom_color))
            painter.line([(0, y), (img_width, y)], fill=color)

        y = self.v_padding
        for text, font, h, w in entries:
            if text:
                painter.text(((img_width - w) / 2, y), text, font=font, fill=self.text_color)
            y += h + line_gap

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        logger.debug(f"[萌音点歌] 歌词图片渲染完成：{img_width}x{img_height}")
        return buf.getvalue()

    async def render_async(self, lyrics: str, title: str = "", subtitle: str = "") -> bytes:
        """在线程池中渲染，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.render, lyrics, title, subtitle)
