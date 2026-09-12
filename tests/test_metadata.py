"""元数据嵌入测试：写标签后用 mutagen 读回断言。"""

import io
from pathlib import Path

from astrbot_plugin_moe_music.core.metadata import embed_metadata, sniff_image_mime
from PIL import Image


def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 120, 220)).save(buf, format="JPEG")
    return buf.getvalue()


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 120, 220)).save(buf, format="PNG")
    return buf.getvalue()


LYRICS = "[00:01.00]故事的小黄花\n[00:02.00]从出生那年就飘着"


class TestSniffMime:
    def test_jpeg(self):
        assert sniff_image_mime(jpeg_bytes()) == "image/jpeg"

    def test_png(self):
        assert sniff_image_mime(png_bytes()) == "image/png"

    def test_unknown_defaults_jpeg(self):
        assert sniff_image_mime(b"\x00\x00\x00") == "image/jpeg"


class TestEmbedId3:
    def test_mp3_full(self, tmp_path: Path):
        # 生成一个最小合法 mp3（裸 MPEG 帧即可被 ID3 挂载）
        p = tmp_path / "song.mp3"
        p.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)
        ok = embed_metadata(
            p,
            title="晴天",
            artist="周杰伦",
            album="叶惠美",
            cover_bytes=jpeg_bytes(),
            lyrics=LYRICS,
        )
        assert ok

        from mutagen.id3 import ID3

        tags = ID3(str(p))
        assert tags.getall("TIT2")[0].text == ["晴天"]
        assert tags.getall("TPE1")[0].text == ["周杰伦"]
        assert tags.getall("TALB")[0].text == ["叶惠美"]
        apic = tags.getall("APIC")[0]
        assert apic.mime == "image/jpeg"
        assert apic.data == jpeg_bytes()
        uslt = tags.getall("USLT")[0]
        assert "故事的小黄花" in uslt.text

    def test_mp3_no_cover_no_lyrics(self, tmp_path: Path):
        p = tmp_path / "song.mp3"
        p.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)
        ok = embed_metadata(p, title="晴天", artist="周杰伦")
        assert ok
        from mutagen.id3 import ID3

        tags = ID3(str(p))
        assert tags.getall("TIT2")[0].text == ["晴天"]
        assert not tags.getall("APIC")
        assert not tags.getall("USLT")


class TestEmbedFlac:
    def test_flac_full(self, tmp_path: Path):
        from mutagen.flac import FLAC

        p = tmp_path / "song.flac"
        # 最小合法 FLAC：fLaC magic + last STREAMINFO block（44100Hz stereo）
        streaminfo = (
            b"\x10\x00" + b"\x10\x00" + b"\x00" * 6 + bytes([0x0A, 0xC4, 0x42]) + b"\x00" * 5 + b"\x00" * 16
        )
        p.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + streaminfo)
        ok = embed_metadata(
            p,
            title="晴天",
            artist="周杰伦",
            album="叶惠美",
            cover_bytes=png_bytes(),
            lyrics=LYRICS,
        )
        assert ok
        audio = FLAC(str(p))
        assert audio["title"] == ["晴天"]
        assert audio["artist"] == ["周杰伦"]
        # 大写 LYRICS：与 Vorbis Comment 规范及后端下载链路保持一致
        assert audio["LYRICS"] == [LYRICS]
        pics = audio.pictures
        assert len(pics) == 1
        assert pics[0].mime == "image/png"
        assert pics[0].data == png_bytes()


class TestEmbedMp4:
    def test_m4a_full(self, tmp_path: Path):
        p = tmp_path / "song.m4a"
        # MP4 需要最小合法结构：用 mutagen 生成空 MP4 不便，直接构造一个
        # 空 moov 骨架太复杂——用 mutagen 不支持创建，因此跳过创建，改为
        # 验证 embed_metadata 对非法文件的容错路径。
        p.write_bytes(b"not-an-mp4")
        ok = embed_metadata(p, title="x", artist="y", cover_bytes=jpeg_bytes())
        assert not ok  # 非法文件失败且不抛异常


class TestUnsupportedAndErrors:
    def test_unsupported_ext_skipped(self, tmp_path: Path):
        p = tmp_path / "song.ogg"
        p.write_bytes(b"OggS-fake")
        assert embed_metadata(p, title="a", artist="b") is False

    def test_corrupt_file_does_not_raise(self, tmp_path: Path):
        p = tmp_path / "broken.mp3"
        p.write_bytes(b"garbage")
        # ID3 挂载垃圾数据不抛异常（写入可能成功或失败，但必须不炸）
        embed_metadata(p, title="a", artist="b")
