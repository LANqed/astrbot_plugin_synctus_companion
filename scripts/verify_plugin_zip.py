#!/usr/bin/env python3
"""校验打包好的 AstrBot 插件 ZIP 真的能装、能跑。

分两步，都是"装上之后才会发现"的问题：

1. **归档结构**。AstrBot 按顶层目录名识别插件，压成"目录里的文件"是最常见的
   失败；测试与字节码缓存也不该进用户的插件目录。
2. **可导入**。解开归档，用 AstrBot 的最小替身实例化插件并渲染一次状态文本。
   这能抓住漏打包的模块、相对导入写错、schema 与代码不同步这几类问题。

用法：
    python scripts/verify_astrbot_plugin_zip.py dist/astrbot_plugin_synctus_companion-v1.0.0.zip
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import types
import zipfile
from datetime import datetime
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_synctus_companion"

REQUIRED_IN_ARCHIVE = (
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


def check_structure(zip_path: Path, plugin_name: str) -> list[str]:
    names = zipfile.ZipFile(zip_path).namelist()
    if not names:
        raise SystemExit("归档是空的")

    prefix = f"{plugin_name}/"
    problems = []

    outside = [name for name in names if not name.startswith(prefix)]
    if outside:
        problems.append(
            f"顶层目录必须是 {prefix}，但发现: {', '.join(outside[:5])}"
        )

    # Windows 上用错 API 会写入反斜杠路径，Linux 上解不开。
    backslash = [name for name in names if "\\" in name]
    if backslash:
        problems.append(f"路径含反斜杠: {', '.join(backslash[:5])}")

    junk = [
        name
        for name in names
        if "__pycache__" in name
        or "/tests/" in name
        or name.endswith((".pyc", ".pyo"))
    ]
    if junk:
        problems.append(f"含无用文件: {', '.join(junk[:5])}")

    missing = [rel for rel in REQUIRED_IN_ARCHIVE if prefix + rel not in names]
    if missing:
        problems.append(f"缺少必需文件: {', '.join(missing)}")

    if not problems:
        print(f"归档结构校验通过：{len(names)} 个文件")
    return problems


def _install_astrbot_stub(data_root: Path) -> None:
    """注入 AstrBot 的最小替身，只覆盖插件实际用到的接口。"""

    def stub(name: str, **attrs) -> None:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    quiet = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )

    class Star:
        def __init__(self, context):
            self.context = context

    stub("astrbot")
    stub("astrbot.api", AstrBotConfig=dict, logger=quiet)
    stub(
        "astrbot.api.event",
        AstrMessageEvent=object,
        MessageChain=lambda chain: chain,
        filter=types.SimpleNamespace(command=lambda *a, **k: (lambda f: f)),
    )
    stub("astrbot.api.message_components", Plain=lambda text: ("plain", text))
    stub(
        "astrbot.api.star",
        Context=object,
        Star=Star,
        StarTools=types.SimpleNamespace(get_data_dir=lambda name: data_root / name),
    )
    stub("astrbot.core")
    stub("astrbot.core.utils")
    stub(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: str(data_root / "data"),
    )


def _defaults_from_schema(schema: dict) -> dict:
    """把 _conf_schema.json 的默认值摊成 AstrBotConfig 的形状。"""
    config = {}
    for key, spec in schema.items():
        if spec.get("type") == "object":
            config[key] = {
                inner_key: inner["default"]
                for inner_key, inner in spec.get("items", {}).items()
            }
        else:
            config[key] = spec["default"]
    return config


def check_importable(zip_path: Path, plugin_name: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        zipfile.ZipFile(zip_path).extractall(root)
        sys.path.insert(0, str(root))
        _install_astrbot_stub(root)
        try:
            import importlib

            plugin_module = importlib.import_module(f"{plugin_name}.main")
            schema = json.loads(
                (root / plugin_name / "_conf_schema.json").read_text(encoding="utf-8")
            )

            class Context:
                async def send_message(self, umo, chain):
                    return True

                def get_all_stars(self):
                    return []

            instance = plugin_module.SynctusCompanionPlugin(
                Context(), _defaults_from_schema(schema)
            )
            # 一次真实渲染：读日程、算电量、生成待办全都会走到。
            status = instance._render_status("", datetime(2026, 1, 1, 10, 0))
            schedule = instance._render_schedule(datetime(2026, 1, 1, 10, 0))
            tasks = instance._render_tasks(datetime(2026, 1, 1, 10, 0))
        except Exception as exc:  # noqa: BLE001 - 任何异常都意味着装上就崩
            return [f"ZIP 内的插件无法导入或运行: {type(exc).__name__}: {exc}"]
        finally:
            sys.path.remove(str(root))
            for name in list(sys.modules):
                if name.startswith(("astrbot", plugin_name)):
                    del sys.modules[name]

    problems = []
    if "%" not in status:
        problems.append(f"状态文本里没有电量: {status!r}")
    if "-" not in schedule:
        problems.append(f"日程文本看起来不对: {schedule!r}")
    if "/" not in tasks:
        problems.append(f"待办文本看起来不对: {tasks!r}")
    if not problems:
        first_line = status.splitlines()[0]
        print(f"ZIP 可导入，状态渲染正常：{first_line}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 AstrBot 插件 ZIP")
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--plugin", default=PLUGIN_NAME)
    args = parser.parse_args()

    if not args.zip_path.is_file():
        raise SystemExit(f"找不到归档: {args.zip_path}")

    problems = check_structure(args.zip_path, args.plugin)
    problems += check_importable(args.zip_path, args.plugin)

    if problems:
        for problem in problems:
            print(f"错误: {problem}", file=sys.stderr)
        return 1
    print("归档可以直接在 AstrBot「从本地上传」安装")
    return 0


if __name__ == "__main__":
    sys.exit(main())
