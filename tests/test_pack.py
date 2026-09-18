"""打包脚本的排除规则测试（scripts/pack.py）。

回归点：`cm_tmp.txt`（`git commit -F` 用的提交信息临时文件，gitignore 里也有）曾因为
「先写提交信息、再打包」的顺序被打进安装包——分发包里混入无意义的临时文件。
"""

import importlib.util
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_pack_module():
    spec = importlib.util.spec_from_file_location(
        "moe_music_pack", PLUGIN_ROOT / "scripts" / "pack.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PACK = _load_pack_module()


class TestPackExcludes:
    def test_commit_message_temp_file_excluded(self):
        """提交信息临时文件（cm_tmp.txt）不能进安装包——真的建出来再验一遍。"""
        tmp_file = PLUGIN_ROOT / "cm_tmp.txt"
        try:
            tmp_file.write_text("commit message", encoding="utf-8")
            assert PACK.should_exclude(tmp_file) is True
            assert tmp_file not in PACK.collect_files()
        finally:
            tmp_file.unlink(missing_ok=True)

    def test_tmp_files_excluded(self):
        tmp_file = PLUGIN_ROOT / "core" / "x.tmp"
        try:
            tmp_file.write_text("partial", encoding="utf-8")
            assert PACK.should_exclude(tmp_file) is True
            assert tmp_file not in PACK.collect_files()
        finally:
            tmp_file.unlink(missing_ok=True)

    def test_cache_and_dev_dirs_excluded(self):
        for rel in ("__pycache__/x.pyc", "docs/x.md", "tests/test_x.py", "webui/vite.config.ts",
                    "dist/pack.zip", ".venv/lib/a.py", "scripts/pack.py"):
            assert PACK.should_exclude(PLUGIN_ROOT / rel) is True, rel

    def test_runtime_files_kept(self):
        for rel in (
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
            "core/share.py",
            "pages/moe-console/index.html",
            "fonts/simhei.ttf",
        ):
            assert PACK.should_exclude(PLUGIN_ROOT / rel) is False, rel

    def test_collected_files_free_of_temp_and_ignored(self):
        """真正会进包的文件清单里不应出现临时文件 / 开发目录。"""
        for path in PACK.collect_files():
            rel = path.relative_to(PLUGIN_ROOT).as_posix()
            assert not rel.endswith(".tmp"), rel
            assert not rel.startswith(("docs/", "tests/", "webui/", "scripts/", "dist/")), rel
