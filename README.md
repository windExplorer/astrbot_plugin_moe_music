# astrbot_plugin_moe_music

萌音点歌 —— 一个连接自建 [lx_music_api](../lx_music_api) 音源后端的 AstrBot 点歌插件。

数据源为自有后端（基于洛雪音源引擎的规范化开放 API），通过 `sk-` 前缀 API Key 鉴权，不依赖任何第三方公共音乐接口。

## 功能

- **点歌**：`点歌 <歌名> [序号]`，聚合搜索自建后端的多音源曲库，回复序号选歌，超时可取消；
- **平台别名**：`网易点歌` / `网易` / `QQ点歌` / `腾讯点歌` / `酷狗点歌` / `酷我点歌` / `咪咕点歌 <歌名>`，大小写不敏感；
- **一次到位**：`点歌 晴天 1` 直接发送第一首；搜索唯一结果自动直发；
- **多模式发送与自动降级**：音乐卡片（QQ/OneBot）→ 语音链接 → 本地语音 → 文件链接 → 本地文件 → 文本链接，失败或平台不支持时自动切换下一种；
- **查歌词**：`查歌词 <歌名>`，歌词渲染为图片发送，渲染失败回退纯文本；
- **AI 点歌**：注册 `play_song_by_name` / `query_lyrics_by_name` 两个 LLM Tool，对 AI 说「放一首周杰伦的歌」即可自动点歌；
- **连接自检**：`点歌自检` 回显 API Key 名称、状态、最高音质、QPS 限额、允许音源；
- **健壮性**：429 限流按 `Retry-After` 退避重试一次、按 Key QPS 节流排队、音质超 Key 上限自动收敛、超时与异常兜底；用户侧只见温馨提示，技术细节全部进 AstrBot 日志（API Key 脱敏）。

## 配置

安装后在 AstrBot 插件配置中填写：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `api_base_url` | lx_music_api 服务端地址（不含 `/api/v1`），需保证聊天客户端可访问 | `http://127.0.0.1:3000` |
| `api_key` | 后端签发的 `sk-` 开头 API Key | 空 |
| `default_source` | 默认音源，`all` 为聚合搜索 | `all` |
| `default_quality` | 默认音质（期望最高档，超 Key 上限自动收敛） | `320k` |
| `song_limit` | 候选列表数量（1-20） | `5` |
| `selection_display` | 候选列表显示方式：`text` 文本列表 / `image` 图片菜单（带封面，渲染失败自动回退文本） | `text` |
| `send_modes` | 发送方式与优先级 | `card, record_link, file_local, text` |
| `timeout` | 选歌等待超时（秒） | `15` |
| `request_timeout` | 单次 HTTP 超时（秒） | `10` |
| `enable_lyrics` | 点歌成功后追加歌词图片 | `false` |
| `embed_metadata` | 本地文件模式嵌入封面/歌词/标题等元数据（mp3/flac/m4a） | `true` |
| `proxy` | 网络代理（可选） | 空 |
| `enable_self_test` | 启用「点歌自检」命令 | `true` |
| `whitelist_groups` | 点歌白名单 · 群号（支持 `*` 通配） | 空 |
| `whitelist_users` | 点歌白名单 · 个人 QQ | 空 |
| `blacklist_groups` | 点歌黑名单 · 群号（仅白名单为空时生效） | 空 |
| `blacklist_users` | 点歌黑名单 · 个人 QQ（仅白名单为空时生效） | 空 |

**访问控制规则**：白名单与黑名单只能生效一个，白名单优先——白名单（群号或个人）任一非空即启用白名单模式并整体忽略黑名单；群号判断优先于个人（群不在白名单时，个人在白名单也无法在该群点歌）；AstrBot 管理员不受任何限制；两个名单都为空时所有人所有群都可以点。

**使用记录**：每次搜索与成功点歌都会写入 AstrBot 数据目录 `data/moe_music/records.db`（SQLite），字段含 QQ 号、昵称、群号、群名、平台、关键词、曲目、音质、发送模式、选歌方式、耗时等，为后续统计功能做数据积累。

## 使用示例

```text
点歌 晴天
点歌 晴天 1
网易点歌 晴天
QQ点歌 晴天 2
查歌词 晴天
点歌自检
（对 AI）放一首周杰伦的歌
```

## 平台支持

- **QQ（aiocqhttp / OneBot：NapCat、Lagrange 等）**：完整体验，含音乐卡片；
- **QQ 官方机器人 / Telegram / 微信 / Discord 等**：自动降级为语音 / 文件 / 文本发送。

### 关于音质（重要）

各发送方式的音质由 QQ 协议决定：

| 方式 | 音质 |
|---|---|
| `card` 音乐卡片 | ★★★★★ 客户端直接在线播放临时链接，不转码，flac 链接即无损听感 |
| `file_link` / `file_local` 文件 | ★★★★★ 文件原样传输，下载后本地播放无损 |
| `record_link` / `record_local` 语音 | ★☆☆☆☆ QQ 协议强制将音频转为 **24kHz 单声道 SILK 语音编码**（NapCat 用 ffmpeg 转码），音乐会明显模糊发闷，仅适合人声/语音场景 |
| `text` 文本链接 | 取决于用户用什么播放器打开 |

如果你点了 flac 却觉得「模糊」，大概率是降级到了语音模式——建议把 `send_modes` 调整为 `card → file_local → text`（去掉 record），或检查音乐卡片为何发送失败（看 AstrBot 日志）。

## 安装

1. 在 AstrBot 插件市场或 Dashboard「插件管理 → 从本地上传」安装本插件 zip 包；
2. 在插件配置中填写 `api_base_url` 与 `api_key`；
3. 发送 `点歌自检` 验证连通性。

依赖（安装时由 AstrBot 自动安装，见 `requirements.txt`）：`aiohttp`、`aiofiles`、`Pillow`、`mutagen`。

## 开发

- 打包：`python scripts/pack.py`，产物输出至 `dist/astrbot_plugin_moe_music_v<version>.zip`（跨平台，标准库实现）；
- 本地测试：`uv run pytest`（详见 `tests/`）。

## License

AGPL-3.0
