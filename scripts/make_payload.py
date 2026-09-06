#!/usr/bin/env python3
"""Build a Lambda invoke payload from a TEP fault run."""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fault", type=int, default=4)
    ap.add_argument("--start", type=int, default=160, help="first sample (160 = fault onset)")
    ap.add_argument("--n", type=int, default=240)
    ap.add_argument("--data", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out", default="payload.json")
    args = ap.parse_args()

    X = np.loadtxt(Path(args.data) / f"d{args.fault:02d}_te.dat")
    window = X[args.start : args.start + args.n]
    Path(args.out).write_text(json.dumps({"samples": window.tolist()}))
    kb = Path(args.out).stat().st_size / 1024
    print(f"wrote {args.out}: {window.shape[0]} samples from fault {args.fault} ({kb:.0f} KB)")
    if kb > 250:
        print("  warning: Lambda's synchronous payload limit is 6 MB; use S3 for larger batches")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
