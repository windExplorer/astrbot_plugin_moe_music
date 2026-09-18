"""歌曲信息卡片渲染测试：动态高度、缺失信息不画块、超长文本不崩。"""

import io
from pathlib import Path

from astrbot_plugin_moe_music.core.model import Track
from astrbot_plugin_moe_music.core.song_card_render import CardInfo, SongCardRenderer
from PIL import Image

FONT = Path(__file__).resolve().parent.parent / "fonts" / "simhei.ttf"


def make_renderer() -> SongCardRenderer:
    return SongCardRenderer(FONT)


def make_track(**kwargs) -> Track:
    data = {
        "id": "tx:002VCTrS3LalJ5",
        "name": "奥黛塔，快陪我去堆个雪人吧",
        "singer": "HOYO-MiX",
        "album": "原神-风与牧歌之城",
        "duration": 236,
        "source": "tx",
        "qualitys": ["128k", "320k", "flac"],
        "coverUrl": "http://h/c.jpg",
    }
    data.update(kwargs)
    return Track.from_api(data)


def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (60, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def open_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


class TestRenderBasic:
    def test_renders_valid_jpeg(self):
        data = make_renderer().render(
            make_track(), quality="flac", requester="测试用户", timestamp="2026-09-18 17:20"
        )
        img = open_image(data)
        assert img.format == "JPEG"
        assert img.width == 760
        assert img.height > 400

    def test_works_without_cover(self):
        """无封面 → 占位图，不抛异常。"""
        data = make_renderer().render(make_track())
        assert open_image(data).width == 760

    def test_broken_cover_falls_back(self):
        data = make_renderer().render(make_track(), cover=b"not-an-image")
        assert open_image(data).width == 760

    def test_cover_used(self):
        renderer = make_renderer()
        data = renderer.render(make_track(), cover=jpeg_bytes())
        assert open_image(data).width == 760


class TestRenderDynamicHeight:
    def test_extra_blocks_increase_height(self):
        renderer = make_renderer()
        track = make_track()
        base = open_image(renderer.render(track)).height
        with_bio = open_image(
            renderer.render(track, info=CardInfo(artist_bio="HOYO-MiX 是米哈游旗下音乐团队。"))
        ).height
        both = open_image(
            renderer.render(
                track,
                info=CardInfo(
                    year=2020,
                    artist_bio="HOYO-MiX 是米哈游旗下音乐团队。",
                    hot_comment="这首歌陪我熬过了整个冬天。",
                    hot_comment_user="某位网友",
                    hot_comment_likes=23456,
                ),
            )
        ).height
        assert with_bio > base
        assert both > with_bio

    def test_missing_blocks_not_drawn(self):
        """空 CardInfo 与不传 info 渲染高度一致（没有内容就不留白）。"""
        renderer = make_renderer()
        track = make_track()
        assert renderer.render(track) == renderer.render(track, info=CardInfo())

    def test_card_info_empty_flag(self):
        assert CardInfo().empty
        assert not CardInfo(year=2020).empty
        assert not CardInfo(hot_comment="x").empty
        assert not CardInfo(artist_bio="x").empty


class TestRenderRobustness:
    def test_long_text_truncated_not_crashing(self):
        """简介/热评超长时按行数上限截断（末行加省略号），高度不失控。"""
        renderer = make_renderer()
        track = make_track()
        info = CardInfo(
            artist_bio="很长很长的简介。" * 60,
            hot_comment="很长的评论内容，" * 60,
            hot_comment_user="昵称" * 40,
        )
        img = open_image(renderer.render(track, info=info))
        assert img.width == 760
        assert img.height < 2000  # 有上限，不会被超长文本撑成一张巨图

    def test_long_song_name_and_multiline_text(self):
        renderer = make_renderer()
        track = make_track(name="标题" * 80, singer="歌手" * 40, album="")
        info = CardInfo(hot_comment="第一行\n第二行\r\n第三行")
        img = open_image(renderer.render(track, info=info, cover=jpeg_bytes()))
        assert img.width == 760

    def test_wrap_respects_max_lines(self):
        renderer = make_renderer()
        from PIL import ImageDraw

        painter = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        lines = renderer._wrap(painter, "字" * 400, renderer.body_font, 300, 3)
        assert len(lines) == 3
        assert lines[-1].endswith("…")
