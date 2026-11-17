"""
Put what is already archived into its visit's folder (migrations 044 and
clinical/006). Safe to run again; nothing is changed or deleted, only added.

On the server, inside the API container (it has every setting this needs):

    docker exec aims-api python /app/src/visit_layout.py            # say what it would do
    docker exec aims-api python /app/src/visit_layout.py --apply    # do it

From a checkout, with the same environment variables as the API:

    python backend/scripts/backfill_visit_layout.py [--apply]

What it does is described in backend/src/visit_layout.py.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.argv[0] = str(SRC / "visit_layout.py")
runpy.run_path(str(SRC / "visit_layout.py"), run_name="__main__")
