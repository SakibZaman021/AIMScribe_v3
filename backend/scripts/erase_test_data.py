"""
Erase the recordings made while testing (23 Nov 2026).

On the server, inside the API container (it has every setting this needs):

    docker exec aims-api python /app/src/erase_test_data.py --clinic TEST
        lists what it would erase; deletes nothing
    docker exec aims-api python /app/src/erase_test_data.py --clinic TEST \\
        --erase --expect <the number listed> --by "<your name>"
        erases exactly that list, through the server's admin erase

What it removes, and its safeguards, are in backend/src/erase_test_data.py.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.argv[0] = str(SRC / "erase_test_data.py")
runpy.run_path(str(SRC / "erase_test_data.py"), run_name="__main__")
