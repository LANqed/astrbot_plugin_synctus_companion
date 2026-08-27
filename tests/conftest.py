"""让测试以包的方式导入插件模块。

插件内部用相对导入（`from .synctus import model`），所以测试必须把仓库根目录
放进 sys.path 并以 `astrbot_plugin_synctus_companion.xxx` 导入。包的 __init__
不引入 main.py，因此这些测试不需要 AstrBot 运行时。
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
