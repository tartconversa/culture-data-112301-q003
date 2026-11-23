#!/usr/bin/env python3
"""releaseqa 命令行入口。

以脚本位置定位项目根目录，因此可配合 ``python3 -I`` 运行，
避免从不可信数据目录加载模块（参见 README 的安全说明）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from releaseqa.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
