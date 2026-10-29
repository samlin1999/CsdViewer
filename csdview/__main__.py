"""讓 `python -m csdview` 也能用（不想安裝時的替代寫法）。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
