"""
Bring a recording back from the copy bucket (`SRS-STO-06`, `AT-74`).

The cloud copy is only worth keeping if it can be turned back into the archive,
so that path is a tool, not a plan. Given files downloaded from the copy bucket,
this decrypts them and says whether the result matches what the archive says it
should be. Audio copies are the raw WAV, so a decrypted copy is the archived
file itself. Copies made earlier as FLAC are decoded back to WAV as well.

    set AIMS_COPY_KEY=<the UIU key>
    python restore.py restore  visit.v1.wav.enc  D:\\restored
    python restore.py restore  copies\\2026-09-13  D:\\restored      (a whole folder)
    python restore.py check    D:\\restored\\visit.wav  <sha256 from the catalogue>

A recording that held several consultations is archived, and copied, as one
file per patient. To get the whole recording back - to check it against the
chain, or to cut it differently - join the parts in order:

    python restore.py join  D:\\restored\\whole.wav  part1.wav part2.wav part3.wav
                            --sha256 <the recording's sha256 from the catalogue>

The key is read from `AIMS_COPY_KEY`, the same value the worker uses. Without it
nothing can be restored, which is the point of encrypting before upload.

Each copy made since format 2 names the recording it belongs to. A copy whose
file name, or kind (audio / JSON), is not the one it was made as - an object
renamed or copied onto another recording's key - is refused rather than
restored as the wrong patient. `--session <id>` also requires the session.
Restored files are readable by their owner only.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import archive
import cloudcopy
from cloudcopy import CopyError


def _key() -> bytes:
    key = cloudcopy.load_key(os.getenv("AIMS_COPY_KEY"))
    if key is None:
        raise SystemExit("AIMS_COPY_KEY is not set; the copies cannot be read without it")
    return key


def _stem_of(name: str) -> str:
    """`<stem>.v3.wav.enc` / `<stem>.wav.enc` -> `<stem>`: the archive's file name."""
    base = name[:-len(".enc")] if name.endswith(".enc") else name
    base = base.rsplit(".", 1)[0]                        # .wav / .json / .flac
    head, _, version = base.rpartition(".")
    if head and version.startswith("v") and version[1:].isdigit():
        base = head
    return base


def expected_binding(sealed: Path, session_id: Optional[str] = None) -> Dict[str, Any]:
    """What a copy with this file name must be bound to."""
    name = sealed.name
    expect: Dict[str, Any] = {"stem_sha256": cloudcopy.stem_fingerprint(_stem_of(name))}
    if name.endswith(".json.enc"):
        expect["kind"] = "json"
    elif name.endswith(".wav.enc") or name.endswith(".flac.enc"):
        expect["kind"] = "audio"
    if session_id:
        expect["session_id"] = session_id
    return expect


def restore_one(sealed: Path, into: Path, key: bytes,
                session_id: Optional[str] = None) -> Path:
    """
    One `.enc` object back to the file it was made from. A `.wav.enc` is the
    WAV already; only an older `.flac.enc` needs decoding. A copy bound to a
    different recording than its name says is refused.
    """
    archive.make_dirs(into)
    name = sealed.name[:-len(".enc")] if sealed.name.endswith(".enc") else sealed.name
    plain = cloudcopy.decrypt_file(sealed, into / name, key,
                                   expect=expected_binding(sealed, session_id))
    if "stem_sha256" not in (cloudcopy.read_binding(sealed) or {}):
        print(f"  {sealed.name}: not bound to a recording (made before copies were); "
              f"check its sha256 against the catalogue")

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
    archive.make_private(wav)
    plain.unlink()
    print(f"  {sealed.name} -> {wav.name}  ({wav.stat().st_size / 1024 ** 2:.1f} MB)")
    return wav


def join_parts(parts, destination: Path):
    """
    Consultation files back into the recording they were cut from. The parts
    are contiguous, so this is the joined WAV byte for byte, and its sha256 is
    the one the catalogue and the chain record for the whole recording.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    return archive.join_wav(list(parts), destination)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    one = commands.add_parser("restore", help="decrypt and decode one file or a folder")
    one.add_argument("source", type=Path)
    one.add_argument("destination", type=Path)
    one.add_argument("--session", default=None,
                     help="refuse any copy not bound to this session")

    check = commands.add_parser("check", help="compare a restored file with its hash")
    check.add_argument("path", type=Path)
    check.add_argument("sha256")

    join = commands.add_parser("join", help="join a recording's parts back into one WAV")
    join.add_argument("destination", type=Path)
    join.add_argument("parts", type=Path, nargs="+")
    join.add_argument("--sha256", default="")

    args = parser.parse_args(argv)

    if args.command == "join":
        result = join_parts(args.parts, args.destination)
        expected = args.sha256.strip().lower()
        same = not expected or result.sha256.hex() == expected
        print(f"{args.destination.name}: {len(args.parts)} part(s), "
              f"{result.duration_seconds:.1f}s, sha256 {result.sha256.hex()}"
              + ("" if not expected else
                 ("  matches" if same else f"  DOES NOT MATCH {expected}")))
        return 0 if same else 1

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
            restore_one(sealed, args.destination, key, session_id=args.session)
        except CopyError as exc:
            failed += 1
            print(f"  {sealed.name}: {exc}")
    if failed:
        print(f"{failed} file(s) could not be restored")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
