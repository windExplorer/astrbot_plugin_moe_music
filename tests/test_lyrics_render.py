"""歌词渲染测试。"""

import io

import pytest
from astrbot_plugin_moe_music.core.lyrics_render import LyricsRenderer
from PIL import Image


@pytest.fixture(scope="module")
def renderer():
    from pathlib import Path

    font = Path(__file__).resolve().parent.parent / "fonts" / "simhei.ttf"
    return LyricsRenderer(font)


LRC = """[ti:晴天]
[ar:周杰伦]
[00:00.00]晴天 - 周杰伦
[00:12.50]故事的小黄花
[00:18.20]从出生那年就飘着
[00:24.80]
[00:25.10]童年的荡秋千
[00:31.40]随记忆一直晃到现在
"""


class TestCleanLrc:
    def test_strips_timeline_and_meta(self):
        lines = LyricsRenderer.clean_lrc(LRC)
        assert all("[ti:" not in line and "[00:" not in line for line in lines)
        assert "故事的小黄花" in lines
        assert any("晴天 - 周杰伦" in line for line in lines)

    def test_compacts_blank_lines(self):
        lines = LyricsRenderer.clean_lrc("[00:01.00]a\n\n\n\n[00:02.00]b")
        assert lines == ["a", "", "b"]

    def test_truncates_long_lyrics(self):
        long_lrc = "\n".join(f"[00:{i:02d}.00]第{i}句" for i in range(200))
        lines = LyricsRenderer.clean_lrc(long_lrc)
        assert len(lines) <= 122  # 120 行 + 截断提示
        assert any("截断" in line for line in lines)


class TestRender:
    def test_render_returns_valid_jpeg(self, renderer):
        data = renderer.render(LRC, title="晴天", subtitle="晴天 - 周杰伦")
        img = Image.open(io.BytesIO(data))
        assert img.format == "JPEG"
        assert img.size[0] >= 1000

    def test_render_multiline_width_expansion(self, renderer):
        long_line = "超" * 60
        data = renderer.render(f"[00:01.00]{long_line}")
        img = Image.open(io.BytesIO(data))
        # 图片宽度应扩展以容纳长行
        assert img.size[0] > 1000

    def test_render_async(self, renderer):
        import asyncio

        data = asyncio.run(renderer.render_async(LRC, title="晴天"))
        assert data[:2] == b"\xff\xd8"

    def test_empty_lyrics_raises(self, renderer):
        with pytest.raises(ValueError):
            renderer.render("")
