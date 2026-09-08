"""命令行入口：python -m tgforward。"""

import argparse

from tgforward import __version__


def main():
    parser = argparse.ArgumentParser(description="TGForward 消息提取机器人")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    from tgforward.app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
