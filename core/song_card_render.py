"""歌曲信息卡片渲染（图片，点歌成功后先于音频发送）。

竖版卡片（宽 760px），自上而下：

    封面（320px 圆角） → 歌名（最多 2 行） → 歌手
    → 信息行（专辑 · 时长 · 年份 · 平台 · 音质）
    → 歌手简介（可选，标注 AI 生成）
    → 热评（可选，标注来源与点赞数）
    → 页脚（点歌人 · 时间 · 插件名）

设计要点：
- **基础信息永不缺席**（封面/歌名/歌手/时长/专辑/平台/音质都在曲目里）；
- 增强信息（年份/简介/热评）由调用方从缓存读出来传入，**缺失就整块不画**，
  因此卡片高度是动态算的，不会留白；
- 渲染跑线程池（``render_async``），不阻塞事件循环。
"""

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont

from .lyrics_render import resolve_font_path
from .model import SOURCE_NAMES, Track

_W = 760
_PAD = 40
_COVER = 320
# 页脚块高度：分隔线 + 点歌人行 + 「引用指令」提示行 + 底部留白
_FOOTER_BLOCK = 92


@dataclass(slots=True)
class CardInfo:
    """卡片上的增强信息（全部来自缓存，取不到就留空）。"""

    year: int = 0
    artist_bio: str = ""
    hot_comment: str = ""
    hot_comment_user: str = ""
    hot_comment_likes: int = 0
    hot_comment_source: str = ""

    @property
    def empty(self) -> bool:
        return not (self.year or self.artist_bio or self.hot_comment)

    @property
    def comment_source_name(self) -> str:
        """热评来源展示名（未知平台一律标「网易云」，历史缓存没有来源字段）。"""
        return SOURCE_NAMES.get(self.hot_comment_source, "网易云")


def _likes_text(likes: int) -> str:
    """点赞数展示：过万折成「x.x万」。"""
    if likes >= 10000:
        return f"{likes / 10000:.1f}万"
    return str(int(likes or 0))


def _flat(text: str) -> str:
    """去掉换行、压缩空白（画布上自己排版，换行符只会添乱）。"""
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


class SongCardRenderer:
    """把 Track（+ 缓存里的增强信息）渲染成一张歌曲信息卡片。"""

    def __init__(self, font_path: Path):
        self.font_path = resolve_font_path(font_path)
        self.title_font = ImageFont.truetype(str(self.font_path), 40)
        self.artist_font = ImageFont.truetype(str(self.font_path), 28)
        self.meta_font = ImageFont.truetype(str(self.font_path), 22)
        self.section_font = ImageFont.truetype(str(self.font_path), 22)
        self.body_font = ImageFont.truetype(str(self.font_path), 22)
        self.footer_font = ImageFont.truetype(str(self.font_path), 20)
        self.hint_font = ImageFont.truetype(str(self.font_path), 18)

        # 与候选列表图同一套配色，控制台/聊天里风格统一
        self.bg_top = (246, 248, 252)
        self.bg_bottom = (240, 250, 245)
        self.title_color = (40, 44, 58)
        self.artist_color = (86, 96, 116)
        self.meta_color = (130, 138, 150)
        self.section_color = (108, 140, 224)
        self.body_color = (72, 78, 92)
        self.divider_color = (222, 227, 236)
        self.footer_color = (150, 158, 170)

        self._cover_mask = self._round_mask(_COVER, 20)
        self._placeholder = self._make_placeholder()

    # ============ 素材 ============

    @staticmethod
    def _round_mask(size: int, radius: int) -> Image.Image:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
        return mask

    def _make_placeholder(self) -> Image.Image:
        """无封面时的占位图（渐变块 + 音符）。"""
        img = Image.new("RGB", (_COVER, _COVER), (228, 232, 240))
        painter = ImageDraw.Draw(img)
        for y in range(_COVER):
            ratio = y / _COVER
            color = tuple(int(238 * (1 - ratio) + 216 * ratio) for _ in range(3))
            painter.line([(0, y), (_COVER, y)], fill=color)
        cx, cy = _COVER // 2, _COVER // 2
        painter.ellipse([cx - 60, cy + 10, cx + 6, cy + 76], fill=(186, 194, 208))
        painter.line([(cx + 2, cy - 74), (cx + 2, cy + 46)], fill=(186, 194, 208), width=12)
        painter.line([(cx + 2, cy - 74), (cx + 52, cy - 56)], fill=(186, 194, 208), width=10)
        return img

    def _cover_image(self, cover: bytes | None) -> Image.Image:
        if not cover:
            return self._placeholder.copy()
        try:
            from PIL import ImageOps

            return ImageOps.fit(Image.open(io.BytesIO(cover)).convert("RGB"), (_COVER, _COVER))
        except Exception:
            logger.debug("[萌音点歌] 卡片封面解码失败，使用占位图")
            return self._placeholder.copy()

    # ============ 排版工具 ============

    @staticmethod
    def _wrap(painter, text: str, font, max_width: int, max_lines: int) -> list[str]:
        """按像素宽度逐字换行（中文友好），超出 max_lines 时末行加省略号。"""
        lines: list[str] = []
        current = ""
        for ch in text:
            if painter.textlength(current + ch, font=font) <= max_width:
                current += ch
                continue
            cut = current.rfind(" ")  # 英文/混排优先在空格处断开
            if 0 < cut < len(current) - 1:
                lines.append(current[:cut])
                current = current[cut + 1 :] + ch
            else:
                lines.append(current)
                current = ch
            if len(lines) >= max_lines:
                break
        if len(lines) < max_lines and current:
            lines.append(current)
            current = ""
        if current and lines:
            lines[-1] = lines[-1][:-1] + "…"
        return lines

    @staticmethod
    def _centered(painter, y: int, text: str, font, fill) -> None:
        width = painter.textlength(text, font=font)
        painter.text(((_W - width) / 2, y), text, font=font, fill=fill)

    # ============ 渲染 ============

    def render(
        self,
        track: Track,
        *,
        info: CardInfo | None = None,
        cover: bytes | None = None,
        quality: str = "",
        requester: str = "",
        timestamp: str = "",
    ) -> bytes:
        """渲染卡片，返回 JPEG 字节。

        Args:
            track: 曲目。
            info: 缓存里的增强信息（None = 只有基础信息）。
            cover: 封面字节（None = 画占位图）。
            quality: 本次下发音质（展示用）。
            requester: 点歌人昵称。
            timestamp: 时间文本（如 ``2026-09-18 17:20``）。
        """
        info = info or CardInfo()
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        inner = _W - _PAD * 2

        # ---- 先算高度与各块内容 ----
        title_lines = self._wrap(probe, _flat(track.name) or "未知歌曲", self.title_font, inner, 2)
        artist_text = _flat(track.singer) or "未知歌手"

        meta_parts = [
            _flat(track.album),
            track.duration_text(),
            f"{info.year} 年" if info.year else "",
            track.source_name,
            quality,
        ]
        meta_lines = self._wrap(
            probe, "  ·  ".join(p for p in meta_parts if p), self.meta_font, inner, 2
        )

        bio_lines = (
            self._wrap(probe, _flat(info.artist_bio), self.body_font, inner, 3)
            if info.artist_bio
            else []
        )
        comment_lines = (
            self._wrap(probe, f"「{_flat(info.hot_comment)}」", self.body_font, inner, 3)
            if info.hot_comment
            else []
        )

        title_lh, artist_lh, meta_lh = 50, 38, 32
        body_lh, section_lh = 32, 34
        height = _PAD + _COVER + 26
        height += title_lh * len(title_lines) + 8 + artist_lh
        height += 14 + meta_lh * len(meta_lines)
        if bio_lines:
            height += 26 + section_lh + body_lh * len(bio_lines)
        if comment_lines:
            height += 26 + section_lh + body_lh * len(comment_lines)
            if info.hot_comment_user:
                height += body_lh
        height += _FOOTER_BLOCK

        img = Image.new("RGB", (_W, height))
        painter = ImageDraw.Draw(img)
        for y in range(height):  # 竖向渐变背景
            ratio = y / height
            color = tuple(
                int(t * (1 - ratio) + b * ratio) for t, b in zip(self.bg_top, self.bg_bottom)
            )
            painter.line([(0, y), (_W, y)], fill=color)

        # ---- 封面（居中，圆角） ----
        cover_img = self._cover_image(cover)
        img.paste(cover_img, ((_W - _COVER) // 2, _PAD), self._cover_mask)

        y = _PAD + _COVER + 26
        for line in title_lines:
            self._centered(painter, y, line, self.title_font, self.title_color)
            y += title_lh
        y += 8
        self._centered(painter, y, artist_text, self.artist_font, self.artist_color)
        y += artist_lh + 14
        for line in meta_lines:
            self._centered(painter, y, line, self.meta_font, self.meta_color)
            y += meta_lh

        # ---- 歌手简介 ----
        if bio_lines:
            y += 26
            painter.text((_PAD, y), "歌手简介 · AI 生成", font=self.section_font, fill=self.section_color)
            y += section_lh
            for line in bio_lines:
                painter.text((_PAD, y), line, font=self.body_font, fill=self.body_color)
                y += body_lh

        # ---- 热评 ----
        if comment_lines:
            y += 26
            label = f"热评 · {info.comment_source_name}"
            if info.hot_comment_likes:
                label += f" · {_likes_text(info.hot_comment_likes)}赞"
            painter.text((_PAD, y), label, font=self.section_font, fill=self.section_color)
            y += section_lh
            for line in comment_lines:
                painter.text((_PAD, y), line, font=self.body_font, fill=self.body_color)
                y += body_lh
            if info.hot_comment_user:
                painter.text(
                    (_PAD, y), f"—— {_flat(info.hot_comment_user)}", font=self.meta_font, fill=self.meta_color
                )
                y += body_lh

        # ---- 页脚：点歌人 / 时间 + 引用指令提示 ----
        divider_y = height - 70
        painter.line([(_PAD, divider_y), (_W - _PAD, divider_y)], fill=self.divider_color)
        parts = [f"点歌人：{requester}" if requester else "", timestamp, "萌音点歌"]
        footer_y = divider_y + 12
        self._centered(
            painter, footer_y, "  ·  ".join(p for p in parts if p), self.footer_font, self.footer_color
        )
        self._centered(
            painter,
            footer_y + 26,
            "提示：引用歌曲分享可发 下载 / 歌词 / 点歌",
            self.hint_font,
            self.footer_color,
        )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        logger.debug(f"[萌音点歌] 信息卡片渲染完成：{_W}x{height}《{track.display}》")
        return buf.getvalue()

    async def render_async(self, track: Track, **kwargs) -> bytes:
        """在线程池中渲染，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.render, track, **kwargs)
