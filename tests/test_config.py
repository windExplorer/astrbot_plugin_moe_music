"""配置解析测试。"""

from astrbot_plugin_moe_music.core.config import (
    COMMAND_SOURCE_ALIAS,
    PluginConfig,
    parse_str_list,
    resolve_command_source,
)
from astrbot_plugin_moe_music.main import FILE_COMMAND_ALIASES, SONG_COMMAND_ALIASES


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

    def test_file_download_defaults(self):
        """「点歌文件」指令的独立配置：默认 flac + 嵌入元数据。"""
        cfg = PluginConfig.from_astrbot_config({})
        assert cfg.file_quality == "flac"
        assert cfg.file_embed_metadata is True

        cfg2 = PluginConfig.from_astrbot_config(
            {"file_quality": "master(母带)", "file_embed_metadata": False}
        )
        assert cfg2.file_quality == "master"  # 面板选项后缀已剥离
        assert cfg2.file_embed_metadata is False

    def test_file_quality_independent_from_default_quality(self):
        """两者互不影响：改文件音质不会动普通点歌音质，反之亦然。"""
        cfg = PluginConfig.from_astrbot_config({"default_quality": "128k", "file_quality": "flac"})
        assert cfg.default_quality == "128k"
        assert cfg.file_quality == "flac"

    def test_key_masked(self):
        cfg = PluginConfig.from_astrbot_config({"api_key": "sk-abcdef1234567890"})
        masked = cfg.key_masked
        assert "abcdef1234567890" not in masked
        assert masked.startswith("sk-")

    def test_access_lists_tolerate_string_values(self):
        """名单被存成字符串时也要解析正确（历史配置/手工填写），否则等于名单失效。"""
        cfg = PluginConfig.from_astrbot_config({"whitelist_groups": "20001,20002"})
        assert cfg.whitelist_groups == ["20001", "20002"]


class TestParseStrList:
    """名单类配置解析：字符串必须按分隔符切分，不能逐字符遍历。"""

    def test_empty_values(self):
        assert parse_str_list(None) == []
        assert parse_str_list([]) == []
        assert parse_str_list("") == []
        assert parse_str_list("   ") == []

    def test_list_elements_trimmed(self):
        assert parse_str_list(["10001", " 10002 "]) == ["10001", "10002"]

    def test_plain_string_is_not_split_by_character(self):
        # 逐字符遍历的症状：["1", "0", "0", "0", "1"] —— 名单看着填了却匹配不上
        assert parse_str_list("10001") == ["10001"]

    def test_supported_separators(self):
        for raw in ("10001 10002", "10001,10002", "10001，10002", "10001、10002", "10001\n10002"):
            assert parse_str_list(raw) == ["10001", "10002"], raw

    def test_list_element_containing_multiple_ids(self):
        # AstrBot 配置页可能把整串塞进列表的单个元素
        assert parse_str_list(["10001,10002"]) == ["10001", "10002"]

    def test_wildcard_kept(self):
        assert parse_str_list(["200*", "*0001"]) == ["200*", "*0001"]


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


class TestResolveCommandSource:
    """命令名 → 平台码：文件指令只需剥掉「文件」后缀，与点歌共用同一张映射表。"""

    def test_plain_commands(self):
        assert resolve_command_source("点歌") == ""
        assert resolve_command_source("酷狗点歌") == "kg"
        assert resolve_command_source("网易") == "wy"

    def test_file_commands_resolve_like_song_commands(self):
        for cmd, expect in [
            ("点歌文件", ""),
            ("网易点歌文件", "wy"),
            ("网易文件", "wy"),
            ("酷狗点歌文件", "kg"),
            ("酷我点歌文件", "kw"),
            ("咪咕点歌文件", "mg"),
            ("腾讯点歌文件", "tx"),
            ("QQ点歌文件", "tx"),
            ("qq点歌文件", "tx"),
            ("Qq点歌文件", "tx"),
        ]:
            assert resolve_command_source(cmd, file_mode=True) == expect, cmd

    def test_file_suffix_only_stripped_in_file_mode(self):
        # 普通点歌模式下不认识带「文件」后缀的命令，避免误判为平台别名
        assert resolve_command_source("酷狗点歌文件") == ""


class TestFileCommandAliases:
    """「点歌文件」指令别名与点歌别名同构（点歌名 + 文件后缀）。"""

    def test_aliases_are_song_aliases_plus_suffix(self):
        assert FILE_COMMAND_ALIASES == {
            f"{name}文件" for name in SONG_COMMAND_ALIASES
        } | {"下载"}  # 「下载」：点歌文件的短别名（配合引用分享使用）

    def test_platform_variants_present(self):
        # 「点歌文件」是主命令名本身，不在 alias 集合里（与「点歌」同理）
        assert {
            "网易点歌文件",
            "QQ点歌文件",
            "qq点歌文件",
            "酷狗点歌文件",
            "酷我点歌文件",
            "咪咕点歌文件",
            "腾讯点歌文件",
        } <= FILE_COMMAND_ALIASES

    def test_every_alias_resolves_to_expected_source(self):
        expect = {
            "点歌文件": "",
            "网易点歌文件": "wy",
            "QQ点歌文件": "tx",
            "酷狗点歌文件": "kg",
            "酷我点歌文件": "kw",
            "咪咕点歌文件": "mg",
            "腾讯点歌文件": "tx",
        }
        for cmd, source in expect.items():
            assert resolve_command_source(cmd, file_mode=True) == source, cmd
