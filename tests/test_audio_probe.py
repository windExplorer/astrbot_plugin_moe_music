"""音频容器嗅探测试（core/audio_probe.py）。

用例对应线上报错：下载的 `.flac` 文件其实不是 FLAC，mutagen 按扩展名分派写入器
直接报 `FLACNoHeaderError: is not a valid FLAC file`。
"""

from astrbot_plugin_moe_music.core.audio_probe import id3_tag_size, sniff_audio_ext


def id3_prefix(tag_body: bytes) -> bytes:
    """构造「ID3v2 头 + 标签体」（长度字段为 synchsafe 编码，不含头自身）。"""
    size = len(tag_body)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + synchsafe + tag_body


class TestSniffByMagic:
    def test_flac(self):
        assert sniff_audio_ext(b"fLaC" + b"\x00" * 40) == ".flac"

    def test_mp3_frame_sync(self):
        assert sniff_audio_ext(b"\xff\xfb\x90\x00" + b"\x00" * 40) == ".mp3"
        assert sniff_audio_ext(b"\xff\xf3\x80\x00") == ".mp3"

    def test_mp3_with_id3_prefix(self):
        assert sniff_audio_ext(id3_prefix(b"\xff\xfb\x90\x00" + b"\x00" * 40)) == ".mp3"

    def test_flac_with_id3_prefix(self):
        """ID3 标签 + FLAC 是真实存在的畸形组合：标签体之后才是 fLaC 魔数。"""
        data = id3_prefix(b"\x00" * 20) + b"fLaC" + b"\x00" * 40
        assert sniff_audio_ext(data) == ".flac"

    def test_flac_behind_long_id3_tag_not_detected(self):
        """标签体超出读取窗口时看不到 fLaC，只能按 mp3 处理（窗口有限，属已知边界）。"""
        data = (id3_prefix(b"\x00" * 200) + b"fLaC" + b"\x00" * 40)[:64]
        assert sniff_audio_ext(data) == ".mp3"

    def test_m4a(self):
        assert sniff_audio_ext(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 40) == ".m4a"

    def test_ogg_wav_ape_wma_amr(self):
        assert sniff_audio_ext(b"OggS\x00\x02" + b"\x00" * 40) == ".ogg"
        assert sniff_audio_ext(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 40) == ".wav"
        assert sniff_audio_ext(b"MAC \x00\x00" + b"\x00" * 40) == ".ape"
        assert sniff_audio_ext(b"\x30\x26\xb2\x75" + b"\x00" * 40) == ".wma"
        assert sniff_audio_ext(b"#!AMR\x0a" + b"\x00" * 40) == ".amr"

    def test_riff_without_wave_is_not_audio(self):
        assert sniff_audio_ext(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 40) is None


class TestSniffFallback:
    def test_content_type_used_when_magic_unknown(self):
        assert sniff_audio_ext(b"garbage-bytes", "audio/mpeg") == ".mp3"
        assert sniff_audio_ext(b"garbage-bytes", "audio/flac; charset=utf-8") == ".flac"
        assert sniff_audio_ext(b"garbage-bytes", "audio/mp4") == ".m4a"

    def test_unknown_returns_none(self):
        """错误页 / 加密文件：认不出就返回 None（调用方保留原扩展名并告警）。"""
        assert sniff_audio_ext(b"<!DOCTYPE html><html>", "text/html") is None
        assert sniff_audio_ext(b'{"code":4040}', "application/json") is None
        assert sniff_audio_ext(b"") is None

    def test_magic_wins_over_wrong_content_type(self):
        """Content-Type 是上游写的，可能是错的；魔数优先。"""
        assert sniff_audio_ext(b"fLaC" + b"\x00" * 40, "audio/mpeg") == ".flac"


class TestId3Size:
    def test_synchsafe_size(self):
        assert id3_tag_size(id3_prefix(b"x" * 10)) == 20  # 10 字节头 + 10 字节体

    def test_non_id3(self):
        assert id3_tag_size(b"fLaC\x00\x00\x00\x00\x00\x00") is None

    def test_invalid_synchsafe_rejected(self):
        """最高位为 1 说明长度不可信（不是合法 synchsafe），宁可放弃跳过标签。"""
        assert id3_tag_size(b"ID3\x03\x00\x00\x80\x00\x00\x00") is None

    def test_zero_size_rejected(self):
        assert id3_tag_size(b"ID3\x03\x00\x00\x00\x00\x00\x00") is None
