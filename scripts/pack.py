"""跨平台打包脚本：把插件按 AstrBot 安装包规范打成 zip。

用法：python scripts/pack.py
- 自动读取 metadata.yaml 的 name / version 生成产物名；
- 输出到 dist/<name>_v<version>.zip；
- zip 内为平铺结构（metadata.yaml 位于根目录），路径统一使用 "/" 分隔。

仅依赖 Python 标准库（zipfile + pathlib + re），Windows / Linux / macOS 均可运行。
"""

import re
import sys
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# 打包排除：目录名 / 文件名 / 扩展名模式
EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "docs",
    "dist",
    "scripts",
    "tests",
    "node_modules",
    "webui",  # 前端源码（运行时只需构建产物 pages/moe-console/）
}
EXCLUDED_FILES = {"pyproject.toml", "uv.lock", ".python-version"}
EXCLUDED_FILE_PATTERNS = (re.compile(r".*\.pyc$"), re.compile(r"^\.env"), re.compile(r"^\.gitignore$"))
REQUIRED_FILES = ("metadata.yaml", "main.py", "requirements.txt", "_conf_schema.json")


def read_metadata() -> tuple[str, str]:
    """从 metadata.yaml 读取 name / version（标准库实现，仅解析顶层 key: value）。"""
    metadata_path = PLUGIN_ROOT / "metadata.yaml"
    if not metadata_path.exists():
        raise SystemExit(f"未找到 {metadata_path}")
    name = version = ""
    for line in metadata_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(name|version):\s*(.+?)\s*$", line)
        if match:
            if match.group(1) == "name":
                name = match.group(2)
            else:
                version = match.group(2)
        elif line and not line.startswith((" ", "#")) and ":" in line:
            continue
    if not name or not version:
        raise SystemExit("metadata.yaml 缺少 name 或 version 字段")
    return name, version


def should_exclude(path: Path) -> bool:
    rel_parts = path.relative_to(PLUGIN_ROOT).parts
    if any(part in EXCLUDED_DIRS for part in rel_parts):
        return True
    if path.is_file():
        if path.name in EXCLUDED_FILES:
            return True
        for pattern in EXCLUDED_FILE_PATTERNS:
            if pattern.match(path.name):
                return True
    return False


def collect_files() -> list[Path]:
    files: list[Path] = []
    for path in PLUGIN_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if should_exclude(path):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(PLUGIN_ROOT).as_posix())


def main() -> int:
    name, version = read_metadata()

    missing = [f for f in REQUIRED_FILES if not (PLUGIN_ROOT / f).exists()]
    if missing:
        raise SystemExit(f"缺少运行必需文件：{missing}")

    dist_dir = PLUGIN_ROOT / "dist"
    dist_dir.mkdir(exist_ok=True)

    # 历史版本包一律保留：只允许覆盖「当前版本号」的包（重新打包同一版本），绝不删除其他版本
    out_path = dist_dir / f"{name}_v{version.lstrip('v')}.zip"

    files = collect_files()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            arcname = file.relative_to(PLUGIN_ROOT).as_posix()
            zf.write(file, arcname)

    size_kb = out_path.stat().st_size / 1024
    print(f"打包完成：{out_path}（{len(files)} 个文件，{size_kb:.1f} KB）")
    print("内容清单：")
    with zipfile.ZipFile(out_path) as zf:
        for info in zf.infolist():
            print(f"  {info.filename}")

    others = sorted(p for p in dist_dir.glob("*.zip") if p != out_path)
    if others:
        print(f"dist 目录中保留的历史版本包（{len(others)} 个，未改动）：")
        for p in others:
            print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
