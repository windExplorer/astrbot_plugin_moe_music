# 设计：「点歌文件」指令（v0.11.0）

> 状态：已实现（2026-09-12）。本文档记录需求、方案取舍与实现落点，便于后续维护。

## 一、需求

用户希望有一个与点歌平行的指令族，**专门输出音乐文件本体**（带封面、歌词），并且文件下载的音质、是否嵌入元数据都**单独配置**，与普通点歌以及普通点歌里 `file_local` 发送方式所用的音质区分开。

流程要求与点歌一致：搜索 → 候选列表 → 回复序号 → 下载文件。

用户的原话要点：

- 指令形如 `/点歌文件`、`/酷狗点歌文件` 等等；
- 后台可以配置文件最高音质；
- 这个和普通点歌区分开，和点歌指令的（本地文件）发送方式的音质也区分开；
- 这个是单独的下载音乐文件，音质单独配置的，嵌入封面和歌词也可以是可配置项。

## 二、已确认的方案决策

| 决策点 | 结论 | 理由 |
|---|---|---|
| 默认文件音质 | `flac` | 既然是专门下载文件，音质优先；超 Key 上限会自动收敛 |
| `file_local` 失败时的降级 | **不降级**（只用 `file_local`） | 「文件」语义最纯粹，失败就明确提示，不用链接糊弄 |
| 文件模式是否额外发歌词图 | 不发 | 歌词已经写进文件标签，再发一张图多余 |
| 指令别名范围 | 与点歌别名同构（点歌名 + 「文件」） | 无需维护第二张映射表 |

## 三、指令

主命令 `点歌文件`，别名 = 现有点歌别名逐项加「文件」后缀（含 QQ 点歌的四种大小写组合）：

```
点歌文件              → 聚合搜索
网易点歌文件 / 网易文件 → wy
QQ点歌文件 / qq点歌文件 / Qq点歌文件 / qQ点歌文件 → tx
腾讯点歌文件           → tx
酷狗点歌文件           → kg
酷我点歌文件           → kw
咪咕点歌文件           → mg
```

- 支持一次到位语法：`点歌文件 晴天 2`；
- 候选列表、等待选号、超时、取消、候选列表自动撤回、白/黑名单全部复用点歌逻辑；
- 点歌记录（`play_records`）照常写入，`command` 字段记录实际命令名（如 `酷狗点歌文件`），统计可区分。

平台识别在 `core/config.py::resolve_command_source(cmd, file_mode=True)`：剥掉尾部「文件」后缀后查现成的 `COMMAND_SOURCE_ALIAS`。

## 四、配置（新增 2 项，与普通点歌完全独立）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file_quality` | string | `flac` | 文件下载期望音质；独立于 `default_quality` |
| `file_embed_metadata` | bool | `true` | 是否把标题/歌手/专辑/封面/歌词写入文件标签；独立于 `embed_metadata` |

WebUI 配置页单独成组「文件下载（点歌文件 指令）」，与「点歌行为」组区分开。

## 五、发送链路

普通点歌与文件指令共用同一套搜索 / 候选 / 等待选号 / 记录 / 撤回逻辑，差异集中在发送阶段，通过 `core/sender.py::DeliveryOptions` 注入：

| | 普通点歌 | 点歌文件 |
|---|---|---|
| 音质 | `cfg.default_quality` | `cfg.file_quality` |
| 发送方式 | `cfg.send_modes`（card/record/file/text 依次降级） | 固定 `file_local` |
| 嵌入元数据 | `cfg.embed_metadata` | `cfg.file_embed_metadata` |
| 失败提示 | 「这首歌暂时发不出来…」 | 「文件下载失败了…」 |
| 附加歌词图 | 跟随 `enable_lyrics` | 始终不发 |

`DeliveryOptions` 为 `None` 时 sender 使用配置默认策略，**普通点歌行为与改造前完全一致**（有回归测试覆盖）。

`file_local` 的实际动作：解析播放链接 → 下载音频到插件临时目录 → 用 mutagen 把标题/歌手/专辑/封面/歌词写入文件标签 → 作为文件消息发出。

## 六、实现落点

| 文件 | 改动 |
|---|---|
| `main.py` | 注册 `点歌文件` 命令与 `FILE_COMMAND_ALIASES`；`FILE_USAGE_HINT` |
| `core/config.py` | `file_quality` / `file_embed_metadata` 字段与解析；`resolve_command_source()` |
| `core/sender.py` | 新增 `DeliveryOptions`；`send_track(options=)` / `resolve_play_url(wanted=)` 参数化；`FILE_ONLY_MODES` |
| `core/commands.py` | `handle_song_request(file_mode=)`、`_file_delivery()`、`DeliveryOptions` 逐层传递；候选列表文案区分；文件模式跳过歌词图 |
| `_conf_schema.json` | 新增 2 个配置项 |
| `webui_api.py` | 2 个键加入 `_EDITABLE_KEYS` |
| `webui/src/views/SettingsView.vue` | 新增「文件下载」分组 |
| `tests/` | 指令解析、别名集合、策略隔离、嵌入开关、不降级、跳过歌词图等用例 |

## 七、验收

- `点歌文件 红豆` → 候选列表提示「回复序号下载文件」→ 回复序号 → 收到音频文件，播放器可见封面与歌词；
- 音质以 `file_quality` 请求（日志 / 记录里 `quality_requested` 可验）；
- 关闭 `file_embed_metadata` 后文件无标签；
- 文件下载失败时只提示失败，不发链接；
- 普通点歌行为不变（音质、发送方式、附加歌词图均不受影响）。

## 八、已知边界

- 文件体积：`flac` 单曲普遍 20–40 MB，QQ 对单文件上传有大小限制，超大文件可能发送失败（此时提示下载失败）。
- 部分协议端对 `file_local`（本地文件上传）支持不一，失败时按上述策略提示。
