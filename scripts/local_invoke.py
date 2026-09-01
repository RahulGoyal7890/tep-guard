#!/usr/bin/env python3
"""Run the Lambda handler locally against a TEP fault run. No AWS required."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "lambda")]
import handler  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fault", type=int, default=4)
    ap.add_argument("--data", default=str(ROOT / "data" / "raw"))
    args = ap.parse_args()

    X = np.loadtxt(Path(args.data) / f"d{args.fault:02d}_te.dat")
    for label, window in [("normal (pre-fault)", X[:160]), ("faulty", X[160:])]:
        body = handler.handler({"samples": window.tolist()})["body"]
        print(f"\n{label}: {body['n_flagged']}/{body['n_samples']} flagged "
              f"({body['alarm_rate']:.1%}) in {body['duration_ms']:.0f} ms")
        for s in body["flagged_samples"][:1]:
            print(f"  sample {s['index']}: T2={s['t2']:.1f} SPE={s['spe']:.1f} "
                  f"via {s['trigger']}")
            for c in s["top_contributors"]:
                print(f"    {c['variable']:<58} {c['contribution']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
