"""候选列表图片渲染测试。"""

import io
from pathlib import Path

import pytest
from astrbot_plugin_moe_music.core.model import Track
from astrbot_plugin_moe_music.core.songlist_render import SonglistRenderer
from PIL import Image


@pytest.fixture(scope="module")
def renderer():
    font = Path(__file__).resolve().parent.parent / "fonts" / "simhei.ttf"
    return SonglistRenderer(font)


def make_tracks(n=3, with_cover=False):
    return [
        Track.from_api(
            {
                "id": f"wy:{i}",
                "name": f"测试歌曲{i}" + ("一个特别特别特别长的歌名测试截断逻辑" if i == 2 else ""),
                "singer": f"歌手{i}",
                "album": f"专辑{i}",
                "duration": 234,
                "source": "wy",
                "qualitys": ["320k"],
                "coverUrl": "http://h/cover.jpg" if with_cover else None,
            }
        )
        for i in range(1, n + 1)
    ]


def tiny_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (90, 140, 220)).save(buf, format="JPEG")
    return buf.getvalue()


class TestSonglistRender:
    def test_render_valid_jpeg(self, renderer):
        data = renderer.render("晴天", make_tracks(3), timeout=15)
        img = Image.open(io.BytesIO(data))
        assert img.format == "JPEG"
        assert img.size[0] == 760
        assert img.size[1] == 96 + 84 * 3 + 64

    def test_render_with_covers(self, renderer):
        data = renderer.render("晴天", make_tracks(2), covers={"wy:1": tiny_jpeg()}, timeout=15)
        assert data[:2] == b"\xff\xd8"  # JPEG magic

    def test_render_with_bad_cover_uses_placeholder(self, renderer):
        # 无效图片字节不应抛异常，回退占位图
        data = renderer.render("晴天", make_tracks(1), covers={"wy:1": b"not-an-image"}, timeout=15)
        assert data[:2] == b"\xff\xd8"

    def test_render_long_name_truncated(self, renderer):
        data = renderer.render("晴天", make_tracks(5), timeout=30)
        assert Image.open(io.BytesIO(data)).size[1] == 96 + 84 * 5 + 64

    def test_render_async(self, renderer):
        import asyncio

        data = asyncio.run(renderer.render_async("晴天", make_tracks(2)))
        assert len(data) > 1000
