"""端到端联调脚本：插件 API 客户端直连真实 lx_music_api 后端。

用法：uv run python scripts/e2e_check.py [base_url] [api_key]
验证：自检 /me → 聚合搜索 → 详情/歌词/封面/播放链接 → 音质收敛链路。
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))  # 插件父目录，使包可按正式名导入

# 手动加载 tests/conftest.py 安装 astrbot stub（tests 不是包）
_spec = importlib.util.spec_from_file_location("_e2e_conftest", PLUGIN_ROOT / "tests" / "conftest.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from astrbot_plugin_moe_music.core.api_client import ApiError, MusicApiClient  # noqa: E402


async def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000"
    api_key = sys.argv[2] if len(sys.argv) > 2 else "sk-moe-music-e2e-2026"

    client = MusicApiClient(base_url, api_key, request_timeout=15)
    failures = []

    # 1. 自检
    try:
        me = await client.me()
        key_info = me.get("apiKey") or {}
        print(
            f"[1] 自检 ✓  name={key_info.get('name')} status={key_info.get('status')} "
            f"maxQuality={client.key_max_quality} qps={client.key_qps_limit}"
        )
    except ApiError as e:
        print(f"[1] 自检 ✗ code={e.code} {e.message}")
        return 1

    # 2. 聚合搜索
    try:
        tracks = await client.search("晴天", limit=5)
        print(f"[2] 搜索 ✓  共 {len(tracks)} 条")
        for t in tracks:
            print(f"    {t.id:<14} {t.display}（{t.source_name}）{t.duration_text()} qualitys={t.qualitys}")
    except ApiError as e:
        print(f"[2] 搜索 ✗ code={e.code} {e.message}")
        return 1
    if not tracks:
        print("    （搜索无结果，后续接口跳过）")
        return 0

    track = tracks[0]

    # 3. 详情（后端对部分音源可能不支持 info，非关键链路）
    try:
        info = await client._request("GET", f"/music/{track.id}/info")
        print(f"[3] 详情 ✓  {info.get('name')} - {info.get('singer')}")
    except ApiError as e:
        print(f"[3] 详情 △ code={e.code}（后端对 {track.source} 源可能不支持 info，非关键）")

    # 4. 歌词（无歌词时后端 404 → 插件转 None）
    try:
        lyric = await client.lyric(track.id)
        text = (lyric or {}).get("lyric") or ""
        first = text[:40].replace("\n", " ")
        print(f"[4] 歌词 ✓  len={len(text)} 预览：{first}")
    except ApiError as e:
        failures.append(f"歌词: {e.code}")
        print(f"[4] 歌词 ✗ code={e.code}（非关键）")

    # 5. 封面
    try:
        cover = await client.pic(track.id)
        print(f"[5] 封面 ✓  {cover[:60] if cover else None}")
    except ApiError as e:
        failures.append(f"封面: {e.code}")
        print(f"[5] 封面 ✗ code={e.code}（非关键）")

    # 6. 播放链接 + 临时链接可达性
    try:
        audio = await client.play_url(track.id, "320k")
        print(f"[6] 播放链接 ✓  quality={audio.get('quality')} expiresAt={audio.get('expiresAt')}")
        print(f"    url={audio.get('url')}")
        tmp_path = await client.download(audio["url"], Path("data/tmp_e2e_download"))
        size = tmp_path.stat().st_size
        tmp_path.unlink()
        tmp_path.parent.rmdir()
        print(f"[7] 临时链接下载 ✓  {size} 字节（已清理）")
    except ApiError as e:
        failures.append(f"播放链接: {e.code} {e.message}")
        print(f"[6] 播放链接 ✗ code={e.code} {e.message}")

    # 7. 无效 Key 的错误码映射
    bad = MusicApiClient(base_url, "sk-invalid-key-xxxx", request_timeout=10)
    try:
        await bad.me()
        print("[8] 无效 Key 校验 ✗（居然成功了？）")
        failures.append("无效 Key 未被拒绝")
    except ApiError as e:
        print(f"[8] 无效 Key 拒绝 ✓  code={e.code} hint=「{e.user_hint}」")
    finally:
        await bad.close()

    await client.close()
    if failures:
        print(f"\n有 {len(failures)} 项失败：{failures}")
        return 1
    print("\n全部链路通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
