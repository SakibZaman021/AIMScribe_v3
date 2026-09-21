"""
Write the icon the build compiles into AIMScribe_Agent.exe.

    python scripts/make_icon.py            writes assets/aimscribe.ico
    python scripts/make_icon.py --preview  also writes a PNG of each state

BUILD.bat runs this before PyInstaller, so the icon in the executable is
always the mark in `ui/brand.py` and never a stale file somebody exported
once and forgot.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ui import brand  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true",
                        help="write a PNG of each state, to look at")
    args = parser.parse_args(argv)

    assets = HERE.parent / "assets"
    assets.mkdir(exist_ok=True)

    icon = assets / "aimscribe.ico"
    brand.write_ico(icon)
    print(f"  {icon}  ({', '.join(str(s) for s in brand.ICO_SIZES)} px)")

    if args.preview:
        for state in (brand.READY, brand.RECORDING, brand.PAUSED,
                      brand.OFFLINE, brand.BLOCKED):
            out = assets / f"preview_{state}.png"
            brand.mark(256, state, pulse=1.0 if state == brand.RECORDING else 0.0,
                       badge=3 if state == brand.OFFLINE else None).save(out)
            print(f"  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
