# 点歌插件 PRD（astrbot_plugin_moe_music）

> 版本：v0.3（草案）
> 日期：2026-09-11
> 状态：待评审
> 关联系统：`lx_music_api`（自有后端，开放 API `/api/v1`，API Key 鉴权）

---

## 1. 背景与目标

### 1.1 背景

我们自研了音乐解析后端 `lx_music_api`，它基于洛雪音源引擎，提供规范化开放 API（搜索、歌词、封面、播放链接、下载），并通过 `sk-` 前缀的 API Key 做第三方鉴权与限流。

当前需要一个 AstrBot 插件，让聊天机器人具备「点歌」能力，数据源为自有后端，而非任何第三方公共音乐接口。

### 1.2 目标

1. 打通「AstrBot 聊天 → 自有后端 API」的完整点歌链路。
2. 提供易用的命令与 AI（LLM Tool）两种点歌入口。
3. 点歌交互做到「搜索 → 展示候选 → 选歌 → 发送音频/歌词」的连续可视化流程。
4. 通过 API Key 合规调用后端，遵守其限流与最小暴露原则。
5. 实现健壮：超时、限流（429）、降级、异常兜底；**面向用户只输出温馨提示，技术细节全部进日志**。

### 1.3 平台策略

- **首版聚焦 QQ 平台**，优先提供最佳体验（含音乐卡片消息）。
- QQ 平台两种接入形态：
  - `aiocqhttp`（OneBot 协议，如 NapCat / Lagrange / go-cqhttp 等）——支持音乐卡片。
  - `qqofficial`（QQ 官方机器人，botpy）——不支持音乐卡片，降级。
- **其它平台（Telegram / 微信 / Discord 等）不针对性优化，但通过通用语音/文件/文本组件保持可用（兼容后退）**。

### 1.4 非目标（首版不做）

- 不引入任何第三方公共音乐接口（如 meting、网易 weapi、txqq.pro 等）。
- 不做多音源平台适配层（自有后端已屏蔽音源细节）。
- 不做用户独立歌单本地持久化。
- 不做热评（后端开放 API 无此接口）。
- 不做整曲「下载任务队列」式下载（首版聚焦播放；`/api/v1/download` 作为后续扩展预留）。

---

## 2. 术语

| 术语 | 说明 |
|---|---|
| AstrBot | 多平台聊天机器人框架，插件以 `Star` 类注册 |
| API Key | 后端开放 API 的鉴权凭证，`sk-` 前缀，绑定音质上限 / QPS / 平台白名单 |
| 临时链接 | 后端签发的本站反向代理地址 `/api/temp/<token>`，播放/下载直链的真实载体 |
| LRC | 歌词文本格式，逐行 `[mm:ss.xx]歌词` |
| 音源/平台码 | 后端对音乐平台的短码：`kw/kg/tx/wy/mg/xm/bd`，`all`=聚合搜索 |
| 音乐卡片 | OneBot `music` 消息段，在 QQ 聊天窗内嵌卡片直接播放 |
| LLM Tool | 暴露给 AstrBot 大模型调用的函数，使 AI 能自动点歌 |

---

## 3. 参考对象分析（取其精华，去其糟粕）

对标仓库 `astrbot_plugin_music`（社区成熟点歌插件）与 `AstrBot` 框架，结论如下。

### 3.1 保留的精华（借鉴）

1. **命令 + 别名设计**：主命令 `点歌`，辅以平台别名（网易点歌 / QQ点歌 / 酷狗点歌…）即时切换平台。
2. **连续交互链路**：搜索 → 候选列表（文本序号）→ 用户回序号 → 发送；用 `session_waiter` 做会话内等待与超时取消。
3. **单曲直发**：搜索唯一结果时自动发送，减少一步交互。
4. **一次到位语法**：`点歌 <歌名> <序号>` 跳过选歌步骤。
5. **多发送方式 + 优先级 + 失败自动降级**：音乐卡片 / 语音链接 / 本地语音 / 文件链接 / 本地文件 / 文本，按序尝试，失败或平台不支持时切换下一种。
6. **音乐卡片实现**：OneBot 平台直连 `music` 消息段（`custom` 类型），体验最佳。
7. **歌词渲染为图片**（PIL + 中文字体），避免歌词纯文本刷屏。
8. **LLM Tool 注册**（`play_song_by_name` / `query_lyrics_by_name`），让 AI 自然语言点歌。
9. **配置 Schema 驱动 UI**（`_conf_schema.json`），统一在 AstrBot 配置面板维护。
10. **超时 / 异常兜底 / `event.stop_event()`**，防止事件被重复处理。
11. **统一消息抽象**（`MessageChain` + `Image` / `Record` / `File` / `Plain` 组件），跨平台复用。

### 3.2 摒弃的糟粕（规避）

1. **依赖不稳定第三方公共 API**：`api.qijieya.cn`（meting）、`music.txqq.pro`、网易 `weapi` 加密参数（`enc_params`/`enc_sec_key`）——无鉴权、随时失效、有合规/稳定性风险。→ 统一替换为自有后端。
2. **多平台硬编码适配**：`ncm_nodejs`、`txqq`、`cz_card`（CZ 音乐签名卡）等冗余实现。→ 单一后端，仅需一个 HTTP 客户端。
3. **依赖 Node.js 网易云服务**（`nodejs_base_url`）。→ 完全不需要。
4. **对 QQ 官方的过度平台特判**：为按钮选择、markdown keyboard、intent 位运算、消息撤回等写大量 `isinstance` 分支。→ 首版只在「音乐卡片」这一必要点做 `aiocqhttp` 判断，其余一律走 AstrBot 抽象层。
5. **自研 `ConfigNode` 反射式 typed config**。→ 过度设计，用 `AstrBotConfig` + 简单 dataclass 即可。
6. **硬编码网易云野生密钥**。→ 安全隐患且失效，剔除。
7. **用户独立歌单、热评**。→ 后端无对应接口，首版不做。

---

## 4. 产品定位与范围

- **一句话**：一个「连自有后端、可 AI 自动点歌、QQ 优先带音乐卡片」的 AstrBot 点歌插件。
- **核心闭环**：搜索（后端聚合多音源）→ 选歌 → 拿临时播放链接 → 以音乐卡片 / 语音 / 文件形式发送 → 可选发送歌词图片。
- **鉴权模型**：插件持有 1 个 API Key，作为「第三方调用方」访问 `/api/v1/*`。

---

## 5. 用户画像与场景

| 场景 | 描述 |
|---|---|
| 群聊点歌 | 用户在群里发「点歌 晴天」，机器人回候选列表，用户回序号，机器人发音乐卡片/音频 |
| 精准点歌 | 「点歌 晴天 1」直接选第一首，少一步交互 |
| 指定平台 | 「网易点歌 晴天」「qq点歌 晴天」限定平台（大小写均可） |
| 查歌词 | 「查歌词 晴天」返回歌词图片 |
| AI 点歌 | 用户对 AI 说「放一首周杰伦的歌」，LLM Tool 自动调用 |

---

## 6. 总体架构

```
用户消息
   │
   ▼
AstrBot 事件 / LLM Tool
   │
   ▼
┌──────────────────────────────────────────────┐
│ Plugin (Star)                                │
│  ├─ commands.py   命令入口（点歌/查歌词/自检） │
│  ├─ llm_tools.py  LLM Tool 入口               │
│  ├─ sender.py     发送策略与降级              │
│  │    ├─ card  (仅 aiocqhttp 音乐卡片)        │
│  │    └─ record/file/text (通用组件)          │
│  └─ lyrics_render.py 歌词图片渲染             │
│        │                                     │
│        ▼                                     │
│  api_client.py   （HTTP，aiohttp）            │
│  ├─ 鉴权注入（Bearer sk- / X-Api-Key）        │
│  ├─ 429 退避重试 / QPS 节流                  │
│  ├─ 统一响应解析 {code,message,data}          │
│  └─ 错误→业务异常（带 code），不进用户侧      │
└──────────────────────────────────────────────┘
   │  HTTPS（/api/v1/*）
   ▼
lx_music_api（自有后端）
```

依赖：`aiohttp`、`Pillow`（歌词渲染）、`aiofiles`（本地文件写入）。字体：随插件分发 `fonts/simhei.ttf`。

---

## 7. 后端对接规范（lx_music_api `/api/v1`）

### 7.1 鉴权

- 请求头二选一：
  - `Authorization: Bearer sk-xxxx`
  - `X-Api-Key: sk-xxxx`
- 插件统一用 `Authorization: Bearer <api_key>` 注入。

### 7.2 统一响应

```jsonc
// 成功
{ "code": 0, "message": "ok", "data": { } }
// 失败
{ "code": 4220, "message": "...", "data": null }
```

业务错误码（`code` 字段）：

| code | 含义 |
|---|---|
| 0 | 成功 |
| 4000 | 参数错误 |
| 4010 | 未认证 / Key 无效 |
| 4011 | 无权限 / Key 被禁用 |
| 4040 | 资源不存在 |
| 4090 | 冲突 |
| 4220 | 校验失败（如音质超上限） |
| 4280 | 系统未初始化 |
| 4290 | 触发限流（响应头带 `Retry-After`） |
| 5000 | 服务器内部错误 |

### 7.3 接口清单

| 方法 | 路径 | 说明 | 关键入参 | 关键出参（`data`） |
|---|---|---|---|---|
| GET | `/api/v1/me` | 自检，回显本 Key 限额 | — | `authenticatedAs`、`apiKey{name,keyPrefix,status,qpsLimit,defaultQuality,maxQuality,allowedSources,defaultSource}` |
| GET | `/api/v1/search` | 搜索 | `keyword`、`limit`、`quality`、`source`、`dedupMode`、`mode` | `{ total, list: Track[] }` |
| GET | `/api/v1/music/:id/info` | 歌曲详情 | — | `Track` |
| GET | `/api/v1/music/:id/lyric` | 歌词 | — | `{ lyric, tlyric, rlyric, lxlyric }`（LRC 文本） |
| GET | `/api/v1/music/:id/pic` | 封面 | — | `{ url }` |
| GET | `/api/v1/music/:id/url` | 播放链接 | `quality` | `{ url, quality, expiresAt }`（`url` 为本站临时链接，默认 2h 有效） |
| POST | `/api/v1/download` | 创建下载任务（预留） | `musicId`/`musicInfo`、`quality` | `{ taskId, status:'pending', reused }` |
| GET | `/api/v1/download/:taskId` | 任务状态（预留） | — | `{ status, progress, error }` |
| GET | `/api/v1/download/:taskId/result` | 任务产物（预留） | — | `{ status, tempUrl, filename, size, contentType, expiresAt }` |

### 7.4 曲目 DTO（`Track`，即后端 `PublicTrack`）

```jsonc
{
  "id": "wy:123456",       // 全局唯一，可直接用于 /music/:id/* 的 :id
  "name": "晴天",
  "singer": "周杰伦",
  "album": "叶惠美",
  "duration": 269,         // 秒
  "source": "wy",          // kw/kg/tx/wy/mg/xm/bd
  "qualitys": ["128k","320k","flac"], // 可用音质
  "coverUrl": "https://..." // 可能为 null
}
```

### 7.5 平台码与音质

- 平台码：`kw`=酷我、`kg`=酷狗、`tx`=QQ、`wy`=网易云、`mg`=咪咕、`xm`=虾米、`bd`=百度；`all`=聚合搜索。
- 音质档位（等级递增）：`128k < 192k < 320k < flac < flac24bit < wav/ape < atmos < atmos_plus < dolby < master`。
- 播放链接 `quality` 缺省时按 Key 的 `default_quality`（再缺省 320k）；显式指定且超 `max_quality` 时后端返回 422（不静默降级）。
- **插件默认音质**：可配置 `default_quality`，点歌取链接时作为 `quality` 传入；若超过后端 Key 上限（422），插件自动收敛到 Key 允许上限重取。

### 7.6 限流与重试约定

- 后端对每个 Key 做 QPS 限流，超限返回 `429 + Retry-After: N`。
- 插件策略：
  1. 单次请求超时（默认 10s，可配）。
  2. 命中 429：读取 `Retry-After`（缺省 1s），等待后**重试一次**；再次 429 则失败并提示「后端繁忙」。
  3. 连续请求（如点歌后紧接取歌词）之间按 Key 的 `qpsLimit` 推导最小间隔做节流排队，避免自触发限流。

---

## 8. 功能需求明细

### 8.1 配置项（`_conf_schema.json`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `api_base_url` | string | `http://127.0.0.1:3000` | 后端地址，不含 `/api/v1` |
| `api_key` | string(invisible) | 空 | `sk-` 开头的 API Key |
| `default_source` | string | `all` | **默认点歌音源**，`all`=聚合搜索，也可选 `kw/kg/tx/wy/mg/xm/bd` |
| `default_quality` | string | `320k` | **默认音质（期望最高音质）**，取链接时传给后端 `quality`，受后端 Key 上限约束 |
| `song_limit` | int(slider 1-20) | 5 | 候选列表数量 |
| `send_modes` | list | `[card, record_link, file_local, text]` | 发送方式优先级，失败/不支持降级 |
| `timeout` | int(slider 5-60) | 15 | 选歌等待超时（秒） |
| `request_timeout` | int(slider 5-30) | 10 | 单次 HTTP 超时（秒） |
| `enable_lyrics` | bool | false | 点歌成功后是否追加歌词图片 |
| `proxy` | string | 空 | 可选网络代理 |
| `enable_self_test` | bool | true | 是否启用「点歌自检」命令 |

`default_quality` 可选档位：`128k / 192k / 320k / flac / flac24bit / wav / ape / atmos / dolby / master`。

### 8.2 点歌命令

**命令与别名**（匹配大小写不敏感，`QQ点歌` 与 `qq点歌` 等效）：

| 命令 | 语义 |
|---|---|
| `点歌 <歌名> [序号]` | 默认音源（`default_source`）点歌 |
| `<平台名>点歌 <歌名> [序号]` | 指定平台，别名映射见下 |

平台别名 → 平台码映射：

| 别名 | 平台码 |
|---|---|
| 网易点歌 / 网易 | `wy` |
| QQ点歌 / qq点歌 / 腾讯点歌 | `tx` |
| 酷狗点歌 | `kg` |
| 酷我点歌 | `kw` |
| 咪咕点歌 | `mg` |

**流程**：

1. 解析命令：`cmd`（主命令或平台别名，`lower()` 归一后匹配）、`arg`（歌名 + 可选序号）。
2. 无歌名 → 提示用法；有序号且合法 → 直接选中对应候选（见步骤 5）。
3. 调 `GET /search?keyword=&limit=&source=&quality=`（`source` 为平台码或省略=聚合；`quality` 传 `default_quality`）。
4. 结果处理：
   - 空 → 提示「搜索无结果」。
   - 仅 1 条 → 直接发送。
   - 多条 → 发文本候选列表：`1. 歌名 - 歌手`，并进入 `session_waiter` 等待序号。
5. 用户回序号 → 发送对应歌曲；非法/超范围 → 提示并结束；超时 → 提示「点歌超时」。

### 8.3 发送策略（`sender`）

发送一首歌的通用链路：

1. 调 `GET /music/:id/url?quality=<default_quality>` 拿 `{url, expiresAt}`（临时链接）；422 时降级音质重取。
2. 若需要卡片（或本地文件），按需取 `GET /music/:id/pic` 封面。
3. 按 `send_modes` 顺序尝试：

| 模式 | 实现 | 平台支持 | 说明 |
|---|---|---|---|
| `card` | OneBot `music` 段 `custom` 类型（`url/audio/title/image/singer`），经 `event.bot.api.call_action` 发送 | 仅 `aiocqhttp` | 音乐卡片，体验最佳；其它平台自动跳过 |
| `record_link` | `Record.fromURL(url)` | 通用 | 语音链接 |
| `record_local` | 下载 `url` → `Record.fromFileSystem(path)` | 通用 | 本地语音 |
| `file_link` | `File(name="歌名-歌手.mp3", url=url)` | 通用 | 文件链接 |
| `file_local` | 下载 `url` 到临时目录 → `File.fromFileSystem(path)` | 通用 | 本地文件，最稳，速度慢 |
| `text` | 发纯文本临时链接 | 通用 | 兜底 |

4. 任一成功即止；全部失败 → 提示「歌曲发送失败」。
5. 可选：`enable_lyrics` 时，发送成功后调 `GET /music/:id/lyric` 渲染图片并追加发送。

**音乐卡片补充约定**：

- 仅当 `event.get_platform_name() == "aiocqhttp"` 时尝试 `card`，其余平台跳过到下一模式。
- 卡片字段：`type="custom"`、`url` 与 `audio` 均填临时链接、`title`=歌名、`singer`=歌手、`image`=封面（无封面时留空）。
- 卡片发送失败（如客户端不支持）时记录日志并降级到下一模式。

### 8.4 查歌词命令

- 命令：`查歌词 <歌名>`，别名 `查看歌词`。
- 流程：`/search?limit=1` 取第一首 → `/music/:id/lyric` → 渲染图片发送。
- 渲染失败回退：发纯文本（去掉 `[mm:ss]` 时间轴）。

### 8.5 LLM Tool

| Tool | 入参 | 行为 |
|---|---|---|
| `play_song_by_name` | `song_name`、`source`（可选） | 搜索取第一首并发送 |
| `query_lyrics_by_name` | `song_name` | 搜索取第一首，发送歌词图片 |

### 8.6 连接自检命令

- 命令：`点歌自检`（`enable_self_test=true` 时启用）。
- 流程：调 `GET /me`，回显 Key 名称、状态、音质上限（`maxQuality`）、QPS 上限、平台白名单/默认平台；失败则回显**温馨提示**（如「API Key 无效，请检查配置」），技术细节记日志。

### 8.7 日志与用户提示规范（重要）

原则：**技术日志只进 AstrBot，绝不返回给用户；用户只看到友好的温馨提示。**

| 分级 | 内容 | 去向 |
|---|---|---|
| `logger.debug` | 请求 URL、参数、响应摘要、发送模式尝试过程 | AstrBot 日志 |
| `logger.info` | 点歌成功、命令触发、自检结果 | AstrBot 日志 |
| `logger.warning` | 限流、降级、可恢复异常 | AstrBot 日志 |
| `logger.error` | 未捕获异常（附 `traceback`）、后端错误码 | AstrBot 日志 |

**脱敏要求**：

- API Key 永不写入日志（仅记前缀 `sk-xxxx…` 或脱敏）。
- 后端地址、临时链接、上游 URL 等敏感信息不进用户侧；日志中按需记录。
- 用户侧文案固定为「温馨提示」风格，不含 `traceback`、错误码、URL、内部字段名。

**用户侧提示文案示例**：

| 场景 | 用户看到 |
|---|---|
| 搜索无结果 | 「没有找到相关歌曲，换个关键词试试吧～」 |
| 选歌超时 | 「点歌超时啦，请重新点歌～」 |
| 后端限流 | 「音乐服务有点忙，请稍后再试～」 |
| Key 无效 | 「音乐服务配置有误，请联系管理员～」 |
| 发送失败 | 「这首歌暂时发不出来，换一首试试吧～」 |

---

## 9. 交互流程（状态机）

```
[点歌/平台点歌] ──搜索──> 候选列表 ──等待序号──> 选中 ──取链接──> 发送（卡片→语音→文件→文本降级）
    │                        │                │
    └──无结果/失败          └──超时/非法      └──发送失败→逐级降级→兜底文本
```

关键交互约定：

- 选歌等待使用 `session_waiter(timeout=self.cfg.timeout)`，会话维度隔离（按 `unified_msg_origin + sender_id` 建键）。
- 处理完成后调用 `event.stop_event()`，避免事件继续被其它监听器处理。
- 单曲直发、`点歌 歌名 序号` 一次到位，均跳过等待态。

---

## 10. 数据模型

```python
@dataclass(slots=True)
class Track:
    id: str              # 后端全局 id
    name: str
    singer: str
    album: str
    duration: int        # 秒
    source: str          # 平台码
    qualitys: list[str]
    cover_url: str | None
```

`api_client` 负责将后端 `PublicTrack` JSON 反序列化为 `Track`，并封装 `search / info / lyric / pic / url / me` 方法；后端非 0 `code` 统一抛 `ApiError(code, message)` 业务异常，由上层转成用户提示或日志。

---

## 11. 非功能需求

| 维度 | 要求 |
|---|---|
| 性能 | 全部网络请求走 `aiohttp` 异步；封面/文件流式下载，不整体载入内存 |
| 限流 | 429 按 `Retry-After` 退避重试一次；连续调用做最小间隔节流 |
| 超时 | 请求与选歌等待均可配，超时给出友好提示 |
| 降级 | 发送模式按序降级（卡片仅 aiocqhttp）；歌词图片失败回退文本；音质超限自动收敛 |
| 安全 | API Key 走 `invisible` 配置项，日志脱敏，不出现在用户侧 |
| 日志 | 完整分级日志只进 AstrBot；用户仅见温馨提示 |
| 健壮 | 所有 handler 捕获异常并记录 `traceback`，不抛出到框架层 |
| 跨平台 | 除「音乐卡片」必要的 `aiocqhttp` 判断外，其余使用 AstrBot 统一消息组件 |
| 可维护 | 模块分层清晰；遵循 Google-style docstring；中文日志 |

---

## 12. 错误处理与边界

| 场景 | 用户看到（温馨提示） | 日志记录 |
|---|---|---|
| 后端未初始化（4280） | 「音乐服务尚未就绪，请稍后再试～」 | error：4280 |
| Key 无效（4010） | 「音乐服务配置有误，请联系管理员～」 | error：4010 + 脱敏 key |
| Key 被禁用（4011） | 「音乐服务配置有误，请联系管理员～」 | error：4011 |
| 触发限流（4290） | 「音乐服务有点忙，请稍后再试～」 | warning：429 + Retry-After |
| 搜索无结果 | 「没有找到相关歌曲，换个关键词试试吧～」 | info：关键词 |
| 歌词为空 | 「这首歌暂无歌词～」 | info |
| 音质超上限（4220） | （自动降级重取，无感） | warning：4220 + 降级音质 |
| 网络异常/超时 | 「网络开小差了，请稍后重试～」 | error：traceback |
| 发送失败 | 「这首歌暂时发不出来，换一首试试吧～」 | error：模式 + 异常 |

---

## 13. 里程碑与验收标准

| 阶段 | 交付 | 验收标准 |
|---|---|---|
| M1 骨架 | 插件注册、配置 Schema、`api_client`（鉴权/响应解析/429 重试/业务异常） | 插件可在 AstrBot 加载；`点歌自检` 正确回显 `/me`；日志分级且脱敏 |
| M2 点歌闭环 | 搜索、候选列表、选歌等待、发送（含音乐卡片 + 多模式降级） | `点歌 晴天` 在 QQ(OneBot) 出卡片、在其它平台降级为语音/文件/文本；超时/无结果/降级均正常 |
| M3 歌词与 AI | `查歌词`、歌词图片渲染、LLM Tool | 歌词图片可发；AI 能自然语言点歌 |
| M4 打磨 | 异常兜底、日志脱敏、文档 | 无未捕获异常；用户侧仅见温馨提示；`ruff` 通过；README 齐备 |

**整体验收**：在 AstrBot 中配置自有后端 API Key 后，QQ(OneBot) 群聊内 `点歌 <歌名>` 返回音乐卡片（可播放）、按序号点歌、`qq点歌`/`QQ点歌` 均可用、可切换默认音源与默认音质；其它平台降级为语音/文件/文本；`查歌词` 返回歌词图片；AI 自动点歌可用；后端限流/超时时用户仅见温馨提示，技术细节全部落日志且无敏感信息泄露。

---

## 14. 版本号规范

- **格式**：语义化版本 `v<major>.<minor>.<patch>`（如 `v0.1.0`），写入 `metadata.yaml` 的 `version` 字段（AstrBot 据此做更新检测与比较）。
- **预发布**：`v0.1.0-alpha` / `-beta` / `-rc`（AstrBot 更新检测会识别并在「非预发布」模式跳过）。
- **递增规则**：

| 变更类型 | 递增位 |
|---|---|
| 破坏性 / 不兼容改动 | major |
| 新增功能（向后兼容） | minor |
| 缺陷修复 / 文档 / 小优化 | patch |

- **同步要求**：`metadata.yaml` 的 `version`、`CHANGELOG.md` 最新条目、Git tag（可选，`vX.Y.Z`）三者保持一致。
- **metadata.yaml 字段**：
  - 必需：`name`、`desc`、`version`、`author`（AstrBot 安装校验必需字段）。
  - 可选：`display_name`（展示名）、`help`（帮助文本）、`repo`（仓库地址）、`astrbot_version`（声明最低 AstrBot 版本）。

## 15. 更新日志（CHANGELOG）规范

- 文件：根目录 `CHANGELOG.md`，**最新版本在最上方**。
- 每个版本标题**必须包含版本号与日期**，格式如下：

```markdown
## v0.1.1 - 2026-09-20

### 修复
- 修复选歌超时后会话状态未清理的问题。

## v0.1.0 - 2026-09-11

### 新增
- 首个版本发布：点歌、查歌词、连接自检、LLM Tool。
```

- 分类标签（无对应内容则省略该分类）：`### 新增`、`### 改进`、`### 修复`、`### 破坏性变更`。
- 每条一行、一句话，面向用户/维护者可读；日期格式统一 `YYYY-MM-DD`。
- 首个版本（`v0.1.0`）标注为「初始发布」。

## 16. 打包与分发规范（跨平台）

### 16.1 打包工具

- 使用 **Python 标准库脚本** `scripts/pack.py`（`zipfile` + `pathlib`）打包，**跨平台**（Windows / Linux / macOS 均可运行），不依赖 PowerShell / 外部命令。
- 运行方式：`python scripts/pack.py`，自动读取 `metadata.yaml` 的 `version` 生成产物名。

### 16.2 产物

- 输出到 `dist/`，命名：`<name>_v<version>.zip`，如 `astrbot_plugin_moe_music_v0.1.0.zip`。
- 该 zip 即 AstrBot 插件安装包（插件市场上传 / Dashboard 本地上传均可，AstrBot 会自动校验并解压）。

### 16.3 zip 结构

- **平铺结构**：`metadata.yaml` 位于 zip 根目录（AstrBot 同时兼容「带单层顶层目录」的结构，会自动剥层）。
- 打包内容（运行必需）：

```
metadata.yaml
main.py
requirements.txt       # AstrBot 安装时自动 pip 安装依赖
_conf_schema.json
core/
fonts/
logo.png
LICENSE
README.md
```

- **排除**：`__pycache__/`、`*.pyc`、`.git/`、`docs/`、`dist/`、`scripts/`、测试文件、`.env` 等。

### 16.4 跨平台要求

- zip 内路径统一使用 `/` 分隔，不包含绝对路径或平台特定路径。
- 依赖一律写入 `requirements.txt`，由 AstrBot 按目标系统自动安装（如 `Pillow` 二进制由 pip 按平台下载），**不打包任何平台特定二进制 / wheel**。
- 代码路径处理使用 `pathlib` 与 AstrBot 路径工具（`get_astrbot_plugin_path` / `get_astrbot_temp_path`），不硬编码 Windows 路径或 `\` 分隔符。
- 字体等纯资源随包分发（不依赖目标系统字体）。
- 源码与脚本统一 UTF-8 编码，避免中文路径 / 注释乱码。

## 17. 附录

### 17.1 后端临时链接说明

- 播放链接接口返回的 `url` 形如 `http(s)://<host>/api/temp/<token>`，由后端反向代理上游音频，**同源、无跨域问题、可直接被下载/播放客户端访问**，默认 2 小时有效。
- 插件应把 `api_base_url` 配置为后端对外可达地址，确保临时链接对用户客户端同样可达。

### 17.2 依赖清单

```text
aiohttp
aiofiles
Pillow
astrbot（框架依赖，运行时提供）
```

### 17.3 文件结构（规划）

```
astrbot_plugin_moe_music/
├─ main.py               # Star 入口
├─ metadata.yaml         # 插件元信息（name/desc/version/author...）
├─ requirements.txt      # 依赖声明（AstrBot 自动安装）
├─ _conf_schema.json     # 配置 Schema
├─ CHANGELOG.md          # 更新日志
├─ core/
│  ├─ config.py          # 配置封装
│  ├─ api_client.py      # 后端 HTTP 客户端（含 ApiError）
│  ├─ model.py           # Track 等数据模型
│  ├─ sender.py          # 发送策略与降级（card/record/file/text）
│  ├─ lyrics_render.py   # 歌词图片渲染
│  └─ commands.py        # 命令 / LLM Tool
├─ fonts/
│  └─ simhei.ttf
├─ scripts/
│  └─ pack.py            # 跨平台打包脚本
├─ docs/
│  └─ PRD.md
└─ README.md
```
