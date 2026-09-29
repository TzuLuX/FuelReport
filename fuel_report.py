#!/usr/bin/env python3
"""Entry point del tool: genera il report HTML sui prezzi dei carburanti (open data MIMIT).

Uso rapido:
    python3 fuel_report.py --quarters 4
    python3 fuel_report.py --from 2020Q1 --to 2026Q2 --no-stations

L'output predefinito è ``report_carburanti.html`` nella cartella corrente.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mimit_fuel.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
