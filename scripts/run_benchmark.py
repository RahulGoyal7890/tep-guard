#!/usr/bin/env python3
"""Benchmark the PCA monitor across all 21 TEP faults.

Writes:
    artifacts/monitor.json        fitted model, consumed by the Lambda
    reports/benchmark.csv         per-fault metrics
    reports/benchmark.md          markdown tables for the README

Usage:
    python scripts/run_benchmark.py [--data data/raw] [--out .]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tepguard import PCAMonitor  # noqa: E402
from tepguard.data import (  # noqa: E402
    DETECTABLE_FAULTS,
    FAULTS,
    FAULT_ONSET_INDEX,
    ISOLATION_SCORED_FAULTS,
    UNDETECTABLE_FAULTS,
    load_test,
    load_training,
)
from tepguard.metrics import (  # noqa: E402
    evaluate_detection,
    median_of,
    summarize,
    topk_hit_rate,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/raw")
    ap.add_argument("--out", default=".")
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()

    out = Path(args.out)
    (out / "artifacts").mkdir(parents=True, exist_ok=True)
    (out / "reports").mkdir(parents=True, exist_ok=True)

    print("Fitting on d00.dat (fault-free) with split calibration...")
    monitor = PCAMonitor().fit_calibrate(load_training(args.data))
    print(
        f"  k={monitor.k_} components, {monitor.n_train_} fit / "
        f"{monitor.n_calibration_} calibration samples"
    )
    print(f"  limits: T2={monitor.t2_limit_:.2f}  SPE={monitor.spe_limit_:.2f}")

    model_path = monitor.save(out / "artifacts" / "monitor.json")
    print(f"  saved -> {model_path} ({model_path.stat().st_size / 1024:.0f} KB)")

    normal = monitor.score(load_test(0, args.data))
    print(f"\nFalse alarm rate on the held-out normal run: {normal.alarm.mean():.3f}")

    rows: list[dict] = []
    for fault in range(1, 22):
        X = load_test(fault, args.data)
        result = monitor.score(X)
        det = evaluate_detection(result.alarm, fault).as_dict()

        spec = FAULTS[fault]
        post = X[FAULT_ONSET_INDEX:]
        if spec.root_variables:
            plain = topk_hit_rate(
                monitor.contributions(post), spec.root_variables, args.top_k
            )
            rbc = topk_hit_rate(
                monitor.reconstruction_contributions(post),
                spec.root_variables,
                args.top_k,
            )
        else:
            plain = rbc = float("nan")

        det.update(
            description=spec.description,
            kind=spec.kind,
            t2_rate=float(result.t2_alarm[FAULT_ONSET_INDEX:].mean()),
            spe_rate=float(result.spe_alarm[FAULT_ONSET_INDEX:].mean()),
            isolation_plain=plain,
            isolation_rbc=rbc,
        )
        rows.append(det)
        flag = "  (known undetectable)" if fault in UNDETECTABLE_FAULTS else ""
        delay = det["detection_delay_minutes"]
        delay_s = "never" if delay is None else f"{delay:.0f} min"
        print(
            f"  fault {fault:2d}: DR={det['detection_rate']:.3f} "
            f"delay={delay_s:>9}  iso(plain)={plain:.3f} iso(rbc)={rbc:.3f}{flag}"
        )

    detectable = [r for r in rows if r["fault"] in DETECTABLE_FAULTS]
    isolation = [r for r in rows if r["fault"] in ISOLATION_SCORED_FAULTS]

    summary = {
        "components": monitor.k_,
        "far_normal_run": float(normal.alarm.mean()),
        "mean_detection_rate_detectable": summarize(detectable, "detection_rate"),
        "median_delay_minutes_detectable": median_of(
            detectable, "detection_delay_minutes"
        ),
        "mean_detection_rate_undetectable": summarize(
            [r for r in rows if r["fault"] in UNDETECTABLE_FAULTS], "detection_rate"
        ),
        "isolation_plain": summarize(isolation, "isolation_plain"),
        "isolation_rbc": summarize(isolation, "isolation_rbc"),
        "n_isolation_faults": len(isolation),
    }

    print("\n" + "=" * 62)
    print(f"Components retained                 : {summary['components']}")
    print(f"False alarm rate (normal run)       : {summary['far_normal_run']:.3f}")
    print(
        f"Mean detection rate ({len(detectable)} detectable) : "
        f"{summary['mean_detection_rate_detectable']:.3f}"
    )
    print(
        f"Median detection delay              : "
        f"{summary['median_delay_minutes_detectable']:.0f} min"
    )
    print(
        f"Mean DR on faults 3, 9, 15          : "
        f"{summary['mean_detection_rate_undetectable']:.3f}"
    )
    print(
        f"Top-{args.top_k} isolation, plain contributions : "
        f"{summary['isolation_plain']:.3f}  (n={summary['n_isolation_faults']})"
    )
    print(
        f"Top-{args.top_k} isolation, RBC                 : "
        f"{summary['isolation_rbc']:.3f}"
    )
    verdict = (
        "RBC WINS"
        if summary["isolation_rbc"] > summary["isolation_plain"]
        else "PLAIN CONTRIBUTIONS WIN"
    )
    print(f"  -> {verdict}")
    print("=" * 62)

    csv_path = out / "reports" / "benchmark.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    _write_markdown(out / "reports" / "benchmark.md", rows, summary, args.top_k)
    print(f"\nWrote {csv_path} and reports/benchmark.md")
    return 0


def _write_markdown(path: Path, rows: list[dict], summary: dict, k: int) -> None:
    lines = [
        "# Benchmark results",
        "",
        "Generated by `scripts/run_benchmark.py`. Every number here is",
        "reproducible from the public TEP data with one command.",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Components retained | {summary['components']} of 52 |",
        f"| False alarm rate, held-out normal run | {summary['far_normal_run']:.1%} |",
        f"| Mean detection rate, 18 detectable faults | {summary['mean_detection_rate_detectable']:.1%} |",
        f"| Median detection delay | {summary['median_delay_minutes_detectable']:.0f} min |",
        f"| Mean detection rate, faults 3/9/15 | {summary['mean_detection_rate_undetectable']:.1%} |",
        f"| Top-{k} isolation, plain contributions | {summary['isolation_plain']:.1%} |",
        f"| Top-{k} isolation, RBC | {summary['isolation_rbc']:.1%} |",
        "",
        "## Per-fault",
        "",
        "| Fault | Description | Type | DR | FAR | Delay | Iso plain | Iso RBC |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        delay = r["detection_delay_minutes"]
        delay_s = "never" if delay is None else f"{delay:.0f} min"
        iso_p = "n/a" if np.isnan(r["isolation_plain"]) else f"{r['isolation_plain']:.1%}"
        iso_r = "n/a" if np.isnan(r["isolation_rbc"]) else f"{r['isolation_rbc']:.1%}"
        lines.append(
            f"| {r['fault']} | {r['description']} | {r['kind']} | "
            f"{r['detection_rate']:.1%} | {r['false_alarm_rate']:.1%} | "
            f"{delay_s} | {iso_p} | {iso_r} |"
        )
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
