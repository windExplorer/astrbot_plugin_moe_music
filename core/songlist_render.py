"""候选列表图片渲染：带封面的点歌菜单，防歌词式纯文本刷屏。

布局（宽 760px）：
- 顶部：标题（关键词 + 命中数量）；
- 每行：序号徽标 + 64px 圆角封面 + 歌名 / 歌手·专辑 两行文本 + 右侧平台名与时长；
- 底部：操作提示（回复序号 / 取消 / 有效期）。

封面由调用方异步下载后以 ``{track_id: bytes}`` 传入，下载失败画占位块。
渲染跑在线程池（render_async），不阻塞事件循环。
"""

import asyncio
import io
from pathlib import Path

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .lyrics_render import resolve_font_path
from .model import Track

_COVER_SIZE = 64
_ROW_HEIGHT = 84
_WIDTH = 760
_H_PADDING = 32


def _round_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=12, fill=255)
    return mask


class SonglistRenderer:
    """把 Track 候选列表渲染成一张图片菜单。"""

    def __init__(self, font_path: Path):
        self.font_path = resolve_font_path(font_path)
        self.title_font = ImageFont.truetype(str(self.font_path), 34)
        self.name_font = ImageFont.truetype(str(self.font_path), 28)
        self.sub_font = ImageFont.truetype(str(self.font_path), 22)
        self.badge_font = ImageFont.truetype(str(self.font_path), 26)
        self.tip_font = ImageFont.truetype(str(self.font_path), 22)

        self.bg_top = (246, 248, 252)
        self.bg_bottom = (240, 250, 245)
        self.row_bg = (255, 255, 255)
        self.title_color = (55, 71, 110)
        self.name_color = (45, 45, 52)
        self.sub_color = (130, 138, 150)
        self.badge_bg = (108, 140, 224)
        self.badge_fg = (255, 255, 255)
        self.tip_color = (150, 158, 170)

        self._cover_mask = _round_mask(_COVER_SIZE)
        self._placeholder = self._make_placeholder()

    def _make_placeholder(self) -> Image.Image:
        img = Image.new("RGB", (_COVER_SIZE, _COVER_SIZE), (225, 229, 238))
        draw = ImageDraw.Draw(img)
        # 简易音符占位：圆点 + 竖线
        draw.ellipse([16, 36, 42, 62], fill=(180, 188, 202))
        draw.line([(40, 14), (40, 48)], fill=(180, 188, 202), width=6)
        draw.line([(40, 14), (54, 20)], fill=(180, 188, 202), width=5)
        return img

    def render(
        self, keyword: str, tracks: list[Track], covers: dict[str, bytes] | None = None, timeout: int = 15
    ) -> bytes:
        """渲染候选列表图片。

        Args:
            keyword: 搜索关键词（展示在标题）。
            tracks: 候选曲目。
            covers: ``{track_id: 封面字节}``，缺失或对应失败时画占位。
            timeout: 选歌等待秒数（展示在底部提示）。
        """
        covers = covers or {}
        header_h = 96
        footer_h = 64
        img_height = header_h + _ROW_HEIGHT * len(tracks) + footer_h

        img = Image.new("RGB", (_WIDTH, img_height))
        painter = ImageDraw.Draw(img)
        for y in range(img_height):
            ratio = y / img_height
            color = tuple(int(t * (1 - ratio) + b * ratio) for t, b in zip(self.bg_top, self.bg_bottom))
            painter.line([(0, y), (_WIDTH, y)], fill=color)

        # 标题
        title = f"为「{keyword[:20]}」找到 {len(tracks)} 首歌曲"
        painter.text((_H_PADDING, 30), title, font=self.title_font, fill=self.title_color)

        # 候选行
        for i, track in enumerate(tracks):
            top = header_h + i * _ROW_HEIGHT
            row_rect = [_H_PADDING - 8, top + 8, _WIDTH - _H_PADDING + 8, top + _ROW_HEIGHT - 8]
            painter.rounded_rectangle(row_rect, radius=14, fill=self.row_bg)

            # 序号徽标
            badge_r = 18
            cx = _H_PADDING + 18
            cy = top + _ROW_HEIGHT // 2
            painter.ellipse([cx - badge_r, cy - badge_r, cx + badge_r, cy + badge_r], fill=self.badge_bg)
            text = str(i + 1)
            bbox = painter.textbbox((0, 0), text, font=self.badge_font)
            painter.text(
                (cx - (bbox[2] - bbox[0]) / 2 - bbox[0], cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
                text,
                font=self.badge_font,
                fill=self.badge_fg,
            )

            # 封面（64px 圆角）
            cover_img = self._placeholder.copy()
            raw = covers.get(track.id)
            if raw:
                try:
                    cover_img = ImageOps.fit(
                        Image.open(io.BytesIO(raw)).convert("RGB"), (_COVER_SIZE, _COVER_SIZE)
                    )
                except Exception:
                    logger.debug(f"[萌音点歌] 封面解码失败，使用占位：{track.id}")
            x0 = cx + badge_r + 18
            y0 = top + (_ROW_HEIGHT - _COVER_SIZE) // 2
            img.paste(cover_img, (x0, y0), self._cover_mask)

            # 文本区：歌名 + 歌手·专辑
            text_x = x0 + _COVER_SIZE + 16
            name = track.name if len(track.name) <= 22 else track.name[:21] + "…"
            sub = track.singer or track.album or "未知"
            if track.album and track.singer:
                sub = f"{track.singer} · {track.album}"
                if len(sub) > 26:
                    sub = sub[:25] + "…"
            painter.text((text_x, top + 14), name, font=self.name_font, fill=self.name_color)
            painter.text((text_x, top + 48), sub, font=self.sub_font, fill=self.sub_color)

            # 右侧：平台 + 时长
            right = f"{track.source_name}  {track.duration_text()}".strip()
            bbox = painter.textbbox((0, 0), right, font=self.sub_font)
            painter.text(
                (_WIDTH - _H_PADDING - (bbox[2] - bbox[0]), top + 32),
                right,
                font=self.sub_font,
                fill=self.sub_color,
            )

        # 底部提示
        tip = f"回复序号点歌（如 1），回复「取消」退出 · {timeout} 秒内有效"
        bbox = painter.textbbox((0, 0), tip, font=self.tip_font)
        painter.text(
            ((_WIDTH - (bbox[2] - bbox[0])) / 2, img_height - footer_h + 18),
            tip,
            font=self.tip_font,
            fill=self.tip_color,
        )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        logger.debug(f"[萌音点歌] 候选列表图片渲染完成：{_WIDTH}x{img_height}")
        return buf.getvalue()

    async def render_async(
        self, keyword: str, tracks: list[Track], covers: dict[str, bytes] | None = None, timeout: int = 15
    ) -> bytes:
        """在线程池中渲染，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.render, keyword, tracks, covers, timeout)
