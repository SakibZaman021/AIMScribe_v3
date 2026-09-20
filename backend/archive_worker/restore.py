"""
Bring a recording back from the copy bucket (`SRS-STO-06`, `AT-74`).

The cloud copy is only worth keeping if it can be turned back into the archive,
so that path is a tool, not a plan. Given files downloaded from the copy bucket,
this decrypts them, decodes the FLAC back to WAV, and says whether the result
matches what the archive says it should be.

    set AIMS_COPY_KEY=<the UIU key>
    python restore.py restore  visit.v1.flac.enc  D:\\restored
    python restore.py restore  copies\\2026-09-13  D:\\restored      (a whole folder)
    python restore.py check    D:\\restored\\visit.wav  <sha256 from the catalogue>

The key is read from `AIMS_COPY_KEY`, the same value the worker uses. Without it
nothing can be restored, which is the point of encrypting before upload.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import cloudcopy
from cloudcopy import CopyError


def _key() -> bytes:
    key = cloudcopy.load_key(os.getenv("AIMS_COPY_KEY"))
    if key is None:
        raise SystemExit("AIMS_COPY_KEY is not set; the copies cannot be read without it")
    return key


def restore_one(sealed: Path, into: Path, key: bytes) -> Path:
    """One `.enc` object back to the file it was made from - WAV for audio."""
    into.mkdir(parents=True, exist_ok=True)
    name = sealed.name[:-len(".enc")] if sealed.name.endswith(".enc") else sealed.name
    plain = cloudcopy.decrypt_file(sealed, into / name, key)

    if plain.suffix.lower() != ".flac":
        print(f"  {sealed.name} -> {plain.name}")
        return plain

    wav = plain.with_suffix(".wav")
    soundfile = cloudcopy._soundfile()
    with soundfile.SoundFile(str(plain)) as source:
        with soundfile.SoundFile(str(wav), mode="w", samplerate=source.samplerate,
                                 channels=source.channels, format="WAV",
                                 subtype=source.subtype) as target:
            for block in source.blocks(blocksize=1 << 18, dtype="int32", always_2d=True):
                target.write(block)
    if not cloudcopy.flac_matches_wav(plain, wav):
        raise CopyError(f"{plain.name} did not decode cleanly to {wav.name}")
    plain.unlink()
    print(f"  {sealed.name} -> {wav.name}  ({wav.stat().st_size / 1024 ** 2:.1f} MB)")
    return wav


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    one = commands.add_parser("restore", help="decrypt and decode one file or a folder")
    one.add_argument("source", type=Path)
    one.add_argument("destination", type=Path)

    check = commands.add_parser("check", help="compare a restored file with its hash")
    check.add_argument("path", type=Path)
    check.add_argument("sha256")

    args = parser.parse_args(argv)

    if args.command == "check":
        actual = cloudcopy.sha256_file(args.path)
        same = actual.lower() == args.sha256.strip().lower()
        print(f"{args.path.name}: {'matches' if same else 'DOES NOT MATCH'}\n"
              f"  expected {args.sha256.strip().lower()}\n  found    {actual}")
        return 0 if same else 1

    key = _key()
    sources = sorted(args.source.rglob("*.enc")) if args.source.is_dir() else [args.source]
    if not sources:
        print(f"Nothing to restore in {args.source}")
        return 1

    print(f"Restoring {len(sources)} file(s) into {args.destination}")
    failed = 0
    for sealed in sources:
        try:
            restore_one(sealed, args.destination, key)
        except CopyError as exc:
            failed += 1
            print(f"  {sealed.name}: {exc}")
    if failed:
        print(f"{failed} file(s) could not be restored")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
