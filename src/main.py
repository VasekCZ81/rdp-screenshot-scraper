"""Vstupní bod aplikace RDP Screenshot Scraper.

DPI awareness se zapíná dřív, než vznikne jakékoli okno Tk – jinak by se
souřadnice výběru rozcházely se skutečnými pixely obrazovky.
"""

from __future__ import annotations

import os
import sys

# Umožní spuštění jako `python src\main.py` i jako modul zabalený PyInstallerem.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from window_manager import enable_dpi_awareness  # noqa: E402

DPI_MODE = enable_dpi_awareness()

import gui  # noqa: E402


def main() -> int:
    if not sys.platform.startswith("win"):
        print("Aplikace je určena pouze pro Windows.", file=sys.stderr)
        return 2
    gui.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
