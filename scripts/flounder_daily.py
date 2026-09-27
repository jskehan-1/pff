"""Superseded by fantasy_daily.py (all leagues). Kept as a shim:
    python scripts\\flounder_daily.py [args]  ==  fantasy_daily.py --league flounder [args]
"""
import runpy
import sys
from pathlib import Path

sys.argv = [sys.argv[0], "--league", "flounder"] + sys.argv[1:]
runpy.run_path(str(Path(__file__).resolve().parent / "fantasy_daily.py"), run_name="__main__")
