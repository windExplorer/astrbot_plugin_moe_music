"""Track 数据模型与配置封装测试。"""

from astrbot_plugin_moe_music.core.model import SOURCE_NAMES, Track, quality_rank


class TestTrack:
    def test_from_api_full(self):
        data = {
            "id": "wy:123456",
            "name": "晴天",
            "singer": "周杰伦",
            "album": "叶惠美",
            "duration": 269,
            "source": "wy",
            "qualitys": ["128k", "320k", "flac"],
            "coverUrl": "https://example.com/cover.jpg",
        }
        track = Track.from_api(data)
        assert track.id == "wy:123456"
        assert track.name == "晴天"
        assert track.singer == "周杰伦"
        assert track.duration == 269
        assert track.source_name == "网易云"
        assert track.display == "晴天 - 周杰伦"
        assert track.duration_text() == "04:29"
        assert track.cover_url == "https://example.com/cover.jpg"

    def test_from_api_missing_fields(self):
        track = Track.from_api({})
        assert track.id == ""
        assert track.name == "未知歌曲"
        assert track.singer == ""
        assert track.cover_url is None
        assert track.duration_text() == ""

    def test_duration_string_interval(self):
        # 后端已统一秒数，但缺省容错
        track = Track.from_api({"duration": None})
        assert track.duration == 0

    def test_source_names_complete(self):
        for code in ("kw", "kg", "tx", "wy", "mg", "xm", "bd"):
            assert code in SOURCE_NAMES


class TestQualityRank:
    def test_order(self):
        assert quality_rank("128k") < quality_rank("320k") < quality_rank("flac")
        assert quality_rank("flac") < quality_rank("flac24bit")
        assert quality_rank("master") == max(quality_rank(q) for q in ("128k", "320k", "flac", "master"))

    def test_unknown(self):
        assert quality_rank("not-a-quality") == -1
