# 设计：歌曲分享自动识别（v0.11.5）

> 状态：已实现（2026-09-18）。本文档记录需求、方案取舍与实现落点，便于后续维护。

## 一、需求

用户希望：**有人在群里分享 QQ音乐 / 网易云音乐 / 酷狗音乐 / 酷我音乐 的歌曲时，插件自动识别并把这首歌发出来**（默认发语音）；并且**引用那条分享再发指令，可以下载音乐文件**。

需求要点：

- 自动识别，不需要任何人打字（@机器人 / 发指令）；
- 分享形式既可能是 QQ 的「分享卡片」，也可能是直接把分享链接贴进聊天；
- 覆盖四家平台：QQ音乐、网易云、酷狗、酷我；
- 引用分享 + 指令 → 下载文件（音质/嵌入开关沿用「点歌文件」那套）。

## 二、已确认的方案决策

| 决策点 | 结论 | 理由 |
|---|---|---|
| 自动识别开关 | **默认关闭**（`share_auto_play=false`） | 机器人主动在群里发消息需要使用者显式同意；引用分享 + 指令是用户自己的操作，不受开关影响 |
| 默认发送方式 | **语音**（`record_link → text`） | 用户明确要「发送语音」；语音会被 QQ 转码成 24kHz 单声道 silk，想无损可把配置改成 `file_local` |
| 曲目定位 | **原生 id 优先，关键词搜索兜底** | 卡片链接里带平台原生 id 时能精确命中「那一首」；聚合搜索下取第 1 条容易选到翻唱/合集 |
| 认不出平台的卡片 | **不响应** | 小程序 / 图文 / 视频分享同样带 `title`，只看「有歌名」就动手等于给每个分享都搜一遍歌 |
| 解析失败时 | 一句提示，**不弹候选列表** | 分享是「自动说话」，弹一排候选项比直接说没找到更打扰人 |
| 事件是否终止 | 识别成功才终止 | 已经发过语音，不该再让 AI 复读一句；认不出时保持原样，不影响命令与 AI |
| 权限 | 沿用白/黑名单与管理员豁免 | 分享也是「点歌」，规则必须一致，否则等于绕过名单 |
| 指令 | 复用现有点歌指令族（不带歌名 + 引用） | 不新增命令词，用户不用记新东西 |

## 三、识别链路

### 3.1 消息形态

| 形态 | OneBot 消息段 | 关键字段 |
|---|---|---|
| QQ音乐卡片 | `json`（`app=com.tencent.music.lua`） | `meta`（**字符串套 JSON**）：`musicUrl` / `jumpUrl` / `title` / `desc`(歌手) |
| 网易云 / 酷狗 / 酷我 卡片 | `json`（`app=com.tencent.structmsg`，`view=music`） | `meta.music`（有的是 `meta.detail_1`）：`musicUrl` / `title` / `desc` / `tag` |
| 粘贴的分享链接 | `text` | 形如 `https://i.y.qq.com/v8/playsong.html?songmid=...`、`https://music.163.com/song?id=...` |
| 少数客户端的分享 | `music`（兜底） | `_type`(qq/163) / `id` / `title` / `content` |

`core/share.py::parse_share_payload` 会递归遍历卡片 JSON（遇到 JSON 字符串就地二次解析），收集所有链接与标题/歌手，因此上面几种结构差异不影响识别。

### 3.2 平台与曲目 id

| 平台 | 域名 | 链接里的 id | 后端能否直接定位 |
|---|---|---|---|
| QQ音乐 | `*.y.qq.com` / `music.qq.com` | `songmid`（查询参数或 `/songDetail/<mid>`） | ✅ `GET /music/tx:<songmid>/info` |
| 网易云 | `music.163.com` / `163cn.tv` | 数字歌曲 id（含 `#/song?id=` 路由） | ✅ `GET /music/wy:<id>/info` |
| 酷狗 | `*.kugou.com` | 文件 hash（`/mixsong/<hash>.html` 或 `hash=`） | ✅ `GET /music/kg:<hash>/info` |
| 酷我 | `*.kuwo.cn` | `rid`（`/play_detail/<rid>`） | ❌ 后端无 kw 内置详情能力，只能搜索 |

酷我链接不提取 id（避免一次必然 404 的请求），直接进入搜索分支；其余平台取 id → 详情失败（4040）时同样退化为搜索。

### 3.3 搜索兜底与选曲

关键词 = `歌名 歌手`，`source` 固定为分享所在平台，`limit = max(3, min(10, song_limit))`。命中多首时用 `core/commands.py::pick_best_track` 打分（歌名完全一致 +100 / 包含 +60，歌手一致 +40 / 包含 +20，同平台 +10），全部分数为 0 才退回第 1 条（搜索关键词本身就含歌名歌手，第 1 条即搜索引擎的相关度判断）。

## 四、配置（新增 3 项）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `share_auto_play` | bool | `false` | 是否**自动**识别并发送分享（默认关闭；只影响自动发送，引用 + 指令不受它控制） |
| `share_send_modes` | list | `record_link, text` | 分享识别的发送方式与优先级（独立于 `send_modes`） |
| `share_quality` | string | `320k` | 分享识别取链音质（超 Key 上限自动收敛） |

WebUI 配置页单独成组「分享识别（QQ音乐 / 网易云 / 酷狗 / 酷我）」。

## 五、触发方式

| 场景 | 行为 |
|---|---|
| 消息里含分享（无指令） | 自动识别 → 按 `share_send_modes` 发送（默认语音）；**需 `share_auto_play=true`**，默认关闭 |
| 引用分享 + `点歌文件` / `下载`（不带歌名） | 下载文件本体（`file_quality` + `file_embed_metadata`），**不受开关影响** |
| 引用分享 + `歌词`（不带歌名） | 查那首歌的歌词（图片渲染，失败回退文本），**不受开关影响** |
| 引用分享 + `点歌`（不带歌名） | 按分享策略发送（默认语音），**不受开关影响** |
| 带歌名的指令 | 完全走原流程，歌名优先，引用被忽略 |
| 分享 + 其他文本 | 只处理分享，识别成功即终止本条消息（不再交给 AI） |

实现上：`main.py` 注册一个 `event_message_type(ALL)` 的钩子（`priority=10`，晚于框架的会话控制、早于默认优先级的命令与 AI），只在识别出分享时才 `stop_event()`。引用场景在 `song_command` / `song_file_command` / `lyrics_command` 的「无歌名」分支里插入 `extract_quoted_share(event.get_messages())`——aiocqhttp 适配器会把被引用消息的内容解析进 `Reply.chain`，所以引用卡片信息天然可得。`下载` 是「点歌文件」的短别名、`歌词` 是「查歌词」的短别名（v0.11.8）。

## 六、实现落点

| 文件 | 改动 |
|---|---|
| `core/share.py`（新增） | `ShareInfo`、`extract_share` / `extract_quoted_share` / `extract_share_from_text`、卡片 JSON 与 URL 解析、`music` 段兜底、跨模块安全的 `is_component` |
| `core/api_client.py` | `music_info()`（`GET /music/:id/info`，4040 → None） |
| `core/config.py` | 3 个新字段；抽出 `parse_send_modes()` 供普通点歌与分享共用 |
| `core/commands.py` | `handle_share_request()`、`_resolve_share_track()`、`_track_by_native_id()`、`_share_delivery()`、`pick_best_track()`；`_search_with_record(send_hint=)` |
| `main.py` | `share_message` 钩子、`_handle_quoted_share()`、两个指令的空歌名分支 |
| `_conf_schema.json` / `webui_api.py` / `SettingsView.vue` | 3 个配置项 + 可编辑白名单 + 配置页分组（含前端产物重建） |
| `tests/test_share.py`（新增） | 四家卡片、字符串套 JSON、链接形态、引用提取、原生 id 优先、搜索兜底与选曲、权限、记录、发送策略、钩子行为 |

## 七、验收

- 开启 `share_auto_play` 后在群里分享一首 QQ音乐/网易云/酷狗/酷我的歌 → 自动收到语音；日志可见「识别到分享：<平台>《歌名 - 歌手》（id=... 来源=card 发送=['record_link','text']）」；开关关闭时同样的分享不作任何响应（一个后端请求都不发）；
- 分享链接贴进聊天（非卡片）同样生效；
- 引用分享发 `点歌文件` / `下载` → 收到带封面与歌词的文件；发 `歌词` → 收到那首歌的歌词；`点歌文件 别的歌名` 仍按歌名搜索；
- 小程序 / 图文分享、普通聊天 → 无任何反应，AI 照常回复；
- 白名单外群分享 → 提示无权限且不请求后端；
- 控制台统计里 `trigger_type` / `selection_type` 显示「分享识别」。

## 八、已知边界

- **酷我分享**：只能按「歌名 歌手」搜索，同名翻唱/现场版可能选错（后端没有 kw 内置详情接口，拿不到 rid → 详情）；
- **短链分享**：`t1.kugou.com/xxxx`、`163cn.tv/xxx`、`c6.y.qq.com/...?__=xxx` 这类短链不含曲目 id，只能靠卡片里的歌名歌手搜索；纯短链文本（无卡片）不响应；
- **语音音质**：QQ 会把语音强制转成 24kHz 单声道 silk，音乐发闷是协议限制；想要无损请把「分享发送方式」改成 `file_local`；
- **等待选歌期间**：会话被 session_waiter 占用时（用户正在回复序号选歌），同一人的分享消息会先被选号会话接走，分享识别不会触发；
- **`music` 消息段**：QQ 的 `music` 段里 `id` 是 songid（不是后端用的 songmid），只能退化搜索；网易云 `163` 可直接定位。
