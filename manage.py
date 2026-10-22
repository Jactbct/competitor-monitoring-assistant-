#!/usr/bin/env python
"""Django 命令行入口。"""
import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    from core.config import ConfigError

    try:
        execute_from_command_line(sys.argv)
    except ConfigError as exc:
        # 配置类错误是「你得改文件」，不是「程序有 bug」：一行说清楚就够，不必甩 traceback
        sys.stderr.write(f"\n配置错误：{exc}\n请检查项目根目录的 .env（可对照 .env.example）。\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
