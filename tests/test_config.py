"""配置解析测试。"""

from astrbot_plugin_moe_music.core.config import COMMAND_SOURCE_ALIAS, PluginConfig
from astrbot_plugin_moe_music.main import SONG_COMMAND_ALIASES


class TestPluginConfig:
    def test_defaults(self):
        cfg = PluginConfig.from_astrbot_config({})
        assert cfg.api_base_url == ""
        assert cfg.api_key == ""
        assert cfg.default_source == "all"
        assert cfg.default_quality == "320k"
        assert cfg.song_limit == 5
        assert cfg.timeout == 15
        assert cfg.request_timeout == 10
        assert cfg.enable_self_test is True
        assert "card" in cfg.send_modes

    def test_panel_option_labels_stripped(self):
        cfg = PluginConfig.from_astrbot_config(
            {
                "default_source": "all(聚合)",
                "default_quality": "320k",
                "send_modes": ["card(音乐卡片)", "record_link(语音链接)", "text(文本链接)", "bad_mode"],
            }
        )
        assert cfg.default_source == "all"
        assert cfg.send_modes == ["card", "record_link", "text"]

    def test_send_modes_dedup_and_fallback(self):
        cfg = PluginConfig.from_astrbot_config({"send_modes": ["card", "card", "text"]})
        assert cfg.send_modes == ["card", "text"]
        cfg_empty = PluginConfig.from_astrbot_config({"send_modes": []})
        assert cfg_empty.send_modes  # 空配置回退默认

    def test_bounds_clamped(self):
        cfg = PluginConfig.from_astrbot_config({"song_limit": 99, "timeout": 1, "request_timeout": 999})
        assert cfg.song_limit == 20
        assert cfg.timeout == 5
        assert cfg.request_timeout == 30

    def test_base_url_trailing_slash(self):
        cfg = PluginConfig.from_astrbot_config({"api_base_url": "http://127.0.0.1:3000/"})
        assert cfg.api_base_url == "http://127.0.0.1:3000"

    def test_invalid_source_fallback(self):
        cfg = PluginConfig.from_astrbot_config({"default_source": "spotify"})
        assert cfg.default_source == "all"

    def test_selection_display(self):
        assert PluginConfig.from_astrbot_config({}).selection_display == "text"
        assert (
            PluginConfig.from_astrbot_config({"selection_display": "image(图片菜单)"}).selection_display
            == "image"
        )
        assert PluginConfig.from_astrbot_config({"selection_display": "card"}).selection_display == "text"

    def test_key_masked(self):
        cfg = PluginConfig.from_astrbot_config({"api_key": "sk-abcdef1234567890"})
        masked = cfg.key_masked
        assert "abcdef1234567890" not in masked
        assert masked.startswith("sk-")


class TestCommandAlias:
    def test_alias_lowercase_keys(self):
        for key in COMMAND_SOURCE_ALIAS:
            assert key == key.lower()

    def test_qq_alias_maps_tx(self):
        assert COMMAND_SOURCE_ALIAS.get("qq点歌") == "tx"
        assert COMMAND_SOURCE_ALIAS.get("腾讯点歌") == "tx"
        assert COMMAND_SOURCE_ALIAS.get("网易点歌") == "wy"
        assert COMMAND_SOURCE_ALIAS.get("酷狗点歌") == "kg"

    def test_song_command_aliases_cover_case_variants(self):
        # CommandFilter 大小写敏感，QQ 点歌的四种大小写组合都必须注册
        assert {"QQ点歌", "qq点歌", "Qq点歌", "qQ点歌"} <= SONG_COMMAND_ALIASES
