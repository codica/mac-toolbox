"""mac-toolbox 统一命令行入口。"""

import argparse
import sys

from mac_toolbox.tools import awake, monitor


TOOLS = {
    "awake": awake,
    "monitor": monitor,
}


def main():
    parser = argparse.ArgumentParser(
        prog="mt",
        description="Mac 个人工具箱",
    )
    sub = parser.add_subparsers(dest="tool", help="可用工具")

    for name, module in TOOLS.items():
        module.register(sub)

    args = parser.parse_args()

    if args.tool is None:
        parser.print_help()
        sys.exit(0)

    # 每个工具模块通过 set_defaults(func=...) 注册执行函数
    args.func(args)
