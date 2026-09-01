#!/usr/bin/env python3
"""Fetch the Tennessee Eastman benchmark data.

The .dat files are simulation output from Downs & Vogel (1993), redistributed
by the Braatz group at UIUC. Public benchmark data -- nothing proprietary and
no plant data of any kind is used anywhere in this project.

Every file is validated by loading it and checking its shape, not just by
checking that a file exists. A download interrupted halfway leaves a
valid-looking file on disk that is silently truncated, and a truncated fault
run produces a plausible but wrong detection rate. Existence is not integrity.

    python scripts/download_data.py            # fetch anything missing or bad
    python scripts/download_data.py --verify   # check only, download nothing
    python scripts/download_data.py --force    # re-fetch everything
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

BASE = "https://raw.githubusercontent.com/camaramm/tennessee-eastman-profBraatz/master"

N_VARS = 52
MAX_ATTEMPTS = 4
TIMEOUT = 60


def is_valid(path: Path, name: str | None = None) -> tuple[bool, str]:
    """Load the file and check it actually has the shape it should.

    `name` is the file's intended name, which is not always path.name: while
    downloading we validate a temp file called something like tmpab12cd.part.
    Inferring the expected shape from the temp name asks a 960-row test run
    for 480 rows and rejects a perfectly good download.
    """
    name = name or path.name
    if not path.exists():
        return False, "missing"
    if path.stat().st_size < 1024:
        return False, f"too small ({path.stat().st_size} bytes)"
    try:
        arr = np.loadtxt(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable ({type(exc).__name__})"

    # d00.dat is the odd one out: it ships transposed as (52, 500) in most
    # mirrors but (500, 52) in some. The loader in tepguard.data normalises
    # it, so accept either orientation here.
    if name == "d00.dat":
        if arr.shape in {(N_VARS, 500), (500, N_VARS)}:
            return True, "ok"
        return False, f"bad shape {arr.shape}"

    want = (960, N_VARS) if name.endswith("_te.dat") else (480, N_VARS)
    if arr.shape != want:
        return False, f"bad shape {arr.shape}, expected {want}"
    if not np.isfinite(arr).all():
        return False, "contains NaN or inf"
    return True, "ok"


def download(name: str, dest: Path) -> tuple[bool, str]:
    """Download to a temp file, validate, then move into place.

    Writing to a temp file first means an interrupted transfer can never
    leave a half-written file where the loader will find it.
    """
    url = f"{BASE}/{name}"
    last = "unknown error"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, dir=dest.parent, suffix=".part"
            ) as fh:
                tmp = Path(fh.name)
                with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                    shutil.copyfileobj(resp, fh)

            ok, why = is_valid(tmp, name)
            if not ok:
                raise ValueError(why)

            tmp.replace(dest)
            return True, "ok" if attempt == 1 else f"ok (attempt {attempt})"

        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last = str(exc) or type(exc).__name__
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s

    return False, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    ap.add_argument("--verify", action="store_true", help="check only")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    files = [f"d{i:02d}.dat" for i in range(22)] + [
        f"d{i:02d}_te.dat" for i in range(22)
    ]

    if args.verify:
        bad = [(n, w) for n in files for ok, w in [is_valid(out / n)] if not ok]
        for name, why in bad:
            print(f"  BAD  {name}: {why}")
        if bad:
            print(f"\n{len(bad)}/{len(files)} files bad. Run without --verify to fix.")
            return 1
        print(f"All {len(files)} files present and valid.")
        return 0

    failed: list[str] = []
    fetched = repaired = 0

    for i, name in enumerate(files, 1):
        dest = out / name
        ok, why = is_valid(dest)

        if ok and not args.force:
            continue
        if dest.exists() and not ok:
            print(f"[{i:2d}/{len(files)}] {name}: {why}, re-downloading")
            repaired += 1

        got, detail = download(name, dest)
        if got:
            fetched += 1
            print(f"[{i:2d}/{len(files)}] {name} {detail}")
        else:
            failed.append(name)
            print(f"[{i:2d}/{len(files)}] {name} FAILED: {detail}", file=sys.stderr)

    valid = sum(is_valid(out / n)[0] for n in files)
    print(f"\n{valid}/{len(files)} files present and valid in {out}")
    if fetched:
        extra = f", {repaired} were corrupt" if repaired else ""
        print(f"  downloaded {fetched}{extra}")

    if failed:
        print(
            f"\nStill failing after {MAX_ATTEMPTS} attempts each: "
            f"{', '.join(failed)}\n"
            "This is normally a flaky network rather than a bad mirror.\n"
            "Run the command again -- valid files are skipped, so it only\n"
            "retries what is still missing.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
