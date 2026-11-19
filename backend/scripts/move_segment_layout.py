"""
Put the pieces waiting in the segment store into the archive's layout
(20 Nov 2026). Safe to run again: a recording already in place is left alone.

On the server, inside the API container (it has every setting this needs):

    docker exec aims-api python /app/src/segment_layout.py            # list what it would move
    docker exec aims-api python /app/src/segment_layout.py --apply    # move it

From a checkout, with the same environment variables as the API:

    python backend/scripts/move_segment_layout.py [--apply]

What it does, and why it cannot lose a piece, is in backend/src/segment_layout.py.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.argv[0] = str(SRC / "segment_layout.py")
runpy.run_path(str(SRC / "segment_layout.py"), run_name="__main__")
