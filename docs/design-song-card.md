# 设计：歌曲信息卡片（v0.11.7）

> 状态：已实现（2026-09-18）。本文档记录需求、来源调研、方案取舍与实现落点。

## 一、需求

点完歌之后，顺带发一张**歌曲信息卡片**（图片形式）：先发卡片、后发语音/文件。可开关，默认开。

- 必有：封面、歌名、歌手、时长；
- 能拿到就更好：歌手资料、热门评论（第一条）、发行年份等；
- **要存起来复用**；歌手资料按歌手维度存储（可重复用）；
- 获取手段不限：联网搜索、LLM 均可。

已确认的三个决策：

| 决策点 | 结论 |
|---|---|
| 增强信息范围 | 三样都要（年份 + 热评 + LLM 歌手简介），其中**热评默认关**（外部接口可能被限流） |
| 首次抓取 | **不等**：卡片即时发（只读缓存），增强信息后台补齐、只写缓存，下次就是完整卡片 |
| 防刷屏 | 同一会话同一首歌 **5 分钟内不重发**卡片（`song_card_repeat_sec`，0 = 每次都发） |

## 二、数据来源调研

后端开放 API 的曲目 DTO 只下发最小字段（`toPublicTrack`：id/name/singer/album/duration/source/qualitys/coverUrl），**没有年份、歌手资料、评论**。各增强项的来源与取舍：

| 信息 | 来源 | 结论 |
|---|---|---|
| 封面/歌名/歌手/专辑/时长/平台/音质 | 后端已有 | 必有 |
| 发行年份 | 网易云旧版公开接口 `GET /api/song/detail/?ids=[id]` → `songs[0].album.publishTime`（毫秒，无需加密） | 采用；秒级时间戳要求 ≥1980，否则视为非时间戳字段（宁可不显示也不画错年份） |
| 热门评论 | 按曲目平台直取：网易云旧版 `GET /api/v1/resource/comments/R_SO_4_<id>?limit=1`；酷我 `GET ncomment.kuwo.cn/com.s?type=get_rec_comment&sid=<id>`（免签名）；酷狗 `GET m.comment.service.kugou.com/r/v1/rank/topliked`（参数 md5 签名，盐 `OIlwieks28dk2k092lksi2UIkp`，与洛雪桌面端一致） | wy/kw/kg 直取本平台热评并在卡片标来源；QQ音乐需额外 songId 映射与签名，不移植——回退「网易云同曲映射」；映射也失败则不显示 |
| 歌手简介 | AstrBot 插件侧没有联网搜索能力；用 `context.llm_generate(...)` / `provider.text_chat(...)` 生成一句简介 | 采用；标注「AI 生成」，模型答「暂无资料」视为无简介 |
| 非 wy 曲目的映射 | 让自己后端按「歌名 歌手」搜 wy，**歌名归一化后完全一致 + 歌手互相包含**才采用 | 映射错会挂错年份/评论，必须严格 |

外部请求统一走 `core/enrich.py`（独立 aiohttp 会话，复用 `proxy` 配置，3 秒超时），域名挂模块常量 `WY_API_BASE`，测试替换为本地假服务。

## 三、缓存设计（`core/info_cache.py`）

与点歌记录同库（`data/moe_music/records.db`）、独立两张表：

```sql
song_meta(track_id PK, wy_id, year, hot_comment, hot_comment_user,
          hot_comment_likes, hot_comment_source, mapped_at, info_at, updated_at)
artist_meta(singer PK, bio, source, attempt_at, updated_at)
```

要点：

- **列级 UPSERT**：只更新传入的列，后写的 `year` 不会冲掉先前的 `hot_comment`；
- **负缓存**：`mapped_at` / `info_at` / `attempt_at` 记录「尝试」时间（含失败）。取值规则在 `fresh(value, attempted_at)`：有值 → 新鲜；值为空且 7 天内试过 → 不再重试。否则每点一次这首歌都会重打一遍永远抓不到的接口（外部限流 + LLM 白烧 token）；
- 写失败只记日志，绝不影响点歌；连接被插件重载关闭后自动重开。

## 四、发送时序

```
点歌指令 / 分享识别 / AI 点歌
  → 搜索 / 选曲
  → resolve_play_url（取链成功）
  → 发送信息卡片（图片）        ← 只读缓存渲染，<0.5s；失败只记日志
  → 按发送方式发语音/文件/音乐卡片
  → （成功后）后台任务补齐年份/热评/歌手简介 → 只写缓存
```

- 卡片在**取链成功之后**发送：取链失败就不会出现「有卡片没歌」；
- 卡片渲染挂在 `MoeMusicService._send_track`（所有点歌路径的统一入口），经
  `SongSender.send_track(song_card=...)` 传入，`options=None` 的语义不变（不影响附加歌词判断）；
- 后台任务收集在 `service._bg_tasks`，插件 `terminate` 时统一取消。

## 五、实现落点

| 文件 | 内容 |
|---|---|
| `core/song_card_render.py` | 竖版卡片渲染（宽 760，封面 320 圆角，动态高度）；`CardInfo` 承载增强信息；逐字换行 + 超行省略号 |
| `core/info_cache.py` | 两张缓存表 + 负缓存判定 `fresh()` |
| `core/enrich.py` | 网易云年份/热评、同曲映射、LLM 歌手简介（全部 best-effort） |
| `core/commands.py` | `_prepare_song_card`（读缓存渲染）、`_card_allowed`（去重）、`_schedule_enrich` / `_enrich_song` / `_enrich_artist`（后台补齐） |
| `core/sender.py` | `send_track(song_card=)`：取链成功后先发卡片，失败不阻断 |
| `main.py` | 组装 `InfoCache` / `Enricher`（provider_getter 接 `context.get_using_provider_async`）/ `SongCardRenderer`；terminate 清理 |
| 配置 | `song_card_enable`（开）/ `song_card_year`（开）/ `song_card_comment`（关）/ `song_card_artist_bio`（开）/ `song_card_repeat_sec`（300） |
| WebUI | `_EDITABLE_KEYS` + 「歌曲信息卡片」分组；`点歌自检` 增加缓存条数 |

## 六、测试

- `tests/test_song_card_render.py`：基础渲染、动态高度、无封面/坏封面占位、超长文本截断；
- `tests/test_info_cache.py`：列级 UPSERT 不互相覆盖、失败只记尝试时间、`fresh()` 语义；
- `tests/test_enrich.py`（本地假网易云服务）：年份/热评解析与容错、同曲映射的严格匹配、LLM 简介清洗与跳过；
- `tests/test_song_card_flow.py`：先卡片后音频、开关、去重窗口、跨会话不去重、后台补齐写缓存、负缓存不重试、缓存不可用降级；
- `tests/test_sender.py::TestSongCard`：卡片发送失败不阻断发歌。

测试环境约束：conftest 提供了 autouse 的 `_offline_external_calls`，把 `WY_API_BASE` 指向本机关闭端口——**测试绝不打真实外部接口**；需要假网易云服务的用例在自己的 fixture 里覆盖它。

## 七、已知边界

- 首次点某首歌时卡片没有年份/热评/简介（异步补齐的设计取舍）；同一首歌第二次起完整；
- 网易云旧版接口若失效/被限流，年份与热评会静默缺失（热评默认已关）；可考虑后续在后端补一个统一的「歌曲元数据」接口；
- 热评的平台覆盖：wy / kw / kg 直取本平台；QQ音乐评论需要额外的 songId 映射与签名，暂不直取（走网易云同曲映射）；酷狗直取以歌曲 **hash** 为键——分享链接里的 kg 曲目带 hash 能精确命中，搜索来的曲目 id 是 AudioID，多半查不到并回退映射；
- AI 简介可能有误（已标注「AI 生成」）；小众歌手/组合会被模型拒绝并记为「7 天内不再尝试」；
- 非 wy 曲目同曲映射失败（搜不到完全同名的 wy 曲目）时没有年份/热评；
- 卡片图片约 100KB（JPEG q90），与歌词图片同量级。
