#!/usr/bin/env python3
"""打包 AstrBot 插件为可上传的 ZIP。

AstrBot 的「从本地上传」需要归档里有一个与 metadata.yaml 的 name 同名的顶层
目录；压缩成"目录里的文件"是最常见的失败原因，所以这里由脚本保证结构。

用 Python 而不是 shell/PowerShell：CI 与用户机器上都一定有 Python（AstrBot
本身就是 Python 应用），一份实现不会两处走样。

用法：
    python scripts/pack_astrbot_plugin.py
    python scripts/pack_astrbot_plugin.py --out-dir dist --plugin astrbot_plugin_synctus_companion
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

DEFAULT_PLUGIN = "astrbot_plugin_synctus_companion"

# 缺了这些文件 AstrBot 装上也跑不起来，宁可在打包时失败。
REQUIRED_FILES = (
    "metadata.yaml",
    "_conf_schema.json",
    "__init__.py",
    "main.py",
    "companion_bridge.py",
    "battery.py",
    "tasks.py",
    "presence.py",
    "synctus/__init__.py",
    "synctus/client.py",
    "synctus/crypto.py",
    "synctus/model.py",
    "synctus/proto.py",
)

# 测试与缓存不属于运行时；带进用户的插件目录只会添乱。
EXCLUDED_DIRS = {"tests", "__pycache__", ".ruff_cache", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def read_version(metadata_path: Path) -> str:
    match = re.search(
        r"^version:\s*(.+)$", metadata_path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not match:
        raise SystemExit(f"{metadata_path} 里读不到 version")
    return match.group(1).strip().strip("\"'")


def read_plugin_name(metadata_path: Path) -> str:
    match = re.search(
        r"^name:\s*(.+)$", metadata_path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not match:
        raise SystemExit(f"{metadata_path} 里读不到 name")
    return match.group(1).strip().strip("\"'")


def collect_files(plugin_dir: Path) -> list[Path]:
    files = []
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(plugin_dir)
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 AstrBot 插件为 ZIP")
    parser.add_argument("--plugin", default=DEFAULT_PLUGIN, help="插件目录名")
    parser.add_argument("--out-dir", default="", help="输出目录，默认 dist/")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    plugin_dir = repo_root / args.plugin
    if not plugin_dir.is_dir():
        raise SystemExit(f"找不到插件目录: {plugin_dir}")

    metadata_path = plugin_dir / "metadata.yaml"
    if not metadata_path.is_file():
        raise SystemExit(f"缺少 {metadata_path}")

    declared_name = read_plugin_name(metadata_path)
    if declared_name != plugin_dir.name:
        # AstrBot 按目录名识别插件，两者不一致会装成一个"另一个插件"。
        raise SystemExit(
            f"metadata.yaml 的 name（{declared_name}）与目录名（{plugin_dir.name}）不一致"
        )

    missing = [
        rel for rel in REQUIRED_FILES if not (plugin_dir / rel).is_file()
    ]
    if missing:
        raise SystemExit("插件缺少必需文件: " + ", ".join(missing))

    version = read_version(metadata_path)
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{plugin_dir.name}-{version}.zip"

    files = collect_files(plugin_dir)
    if not files:
        raise SystemExit("没有可打包的文件")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            arcname = Path(plugin_dir.name) / path.relative_to(plugin_dir)
            # 归档内统一用正斜杠，Windows 上打的包在 Linux 上也能正常解开。
            archive.write(path, arcname.as_posix())

    size_kb = zip_path.stat().st_size / 1024
    print(f"已生成 {zip_path} （{len(files)} 个文件，{size_kb:.1f} KB）")
    print("在 AstrBot WebUI 的插件管理里选「从本地上传」，选这个文件即可。")
    print("别忘了在 AstrBot 环境执行: pip install argon2-cffi pynacl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
