#!/usr/bin/env python3
"""峰会议题协同后端命令行入口。

示例：
  PYTHONPATH=src python3 run_cli.py demo
  PYTHONPATH=src python3 run_cli.py --role coordinator publish --idem pub-1
  PYTHONPATH=src python3 run_cli.py replay 1
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from summit_agenda.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
