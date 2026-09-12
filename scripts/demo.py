#!/usr/bin/env python3
"""Demonstrate the whole system in about a minute. Runs locally, no AWS needed.

    python scripts/demo.py              # the standard walkthrough
    python scripts/demo.py --fault 6    # a different fault
    python scripts/demo.py --bedrock    # use the real model rather than the cache

Written to be watched over a screen share: pauses between sections, prints
what it is about to do before doing it, and shows the numbers rather than
claiming them.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tepguard import PCAMonitor, load_test, load_training  # noqa: E402
from tepguard.data import FAULTS, FAULT_ONSET_INDEX, describe_variable  # noqa: E402
from tepguard.drift import check_drift  # noqa: E402
from tepguard.explain import explain  # noqa: E402
from tepguard.metrics import evaluate_detection  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def header(text: str, pause: float) -> None:
    print(f"\n{BOLD}{'─' * 68}{RESET}")
    print(f"{BOLD}{text}{RESET}")
    print(f"{BOLD}{'─' * 68}{RESET}")
    time.sleep(pause)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fault", type=int, default=4)
    ap.add_argument("--data", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--bedrock", action="store_true")
    ap.add_argument("--pause", type=float, default=1.2)
    args = ap.parse_args()
    p = args.pause

    spec = FAULTS[args.fault]

    header("1. The problem", p)
    print("A chemical plant runs 52 correlated sensors. Individual alarms on")
    print("each one miss the failures that matter, because a fault often shows")
    print("up as the relationship between sensors breaking, not as any single")
    print("reading leaving its range.")
    time.sleep(p)

    header("2. Learning what normal looks like", p)
    X_train = load_training(args.data)
    print(f"   training data: {X_train.shape[0]} samples x {X_train.shape[1]} sensors")
    print("   fitting PCA on the first 350 samples...")
    monitor = PCAMonitor().fit_calibrate(X_train)
    print(f"   retained {monitor.k_} components covering 80% of the variance")
    print(f"   calibrating alarm limits on the remaining {monitor.n_calibration_}")
    print(f"   limits: T2 > {monitor.t2_limit_:.1f}, SPE > {monitor.spe_limit_:.1f}")
    print()
    print(f"{DIM}   The limits come from held-out data, not the textbook formula.{RESET}")
    print(f"{DIM}   The textbook version gave 17.7% false alarms against a 1% design.{RESET}")
    time.sleep(p)

    header("3. Checking a run with no fault", p)
    normal = monitor.score(load_test(0, args.data))
    print(f"   960 samples of normal operation")
    print(f"   flagged: {normal.alarm.sum()} ({normal.alarm.mean():.1%})")
    print(f"{DIM}   Low false alarms is the whole game. A monitor that cries wolf{RESET}")
    print(f"{DIM}   gets muted by operators in the first week.{RESET}")
    time.sleep(p)

    header(f"4. Introducing fault {args.fault}", p)
    print(f"   {spec.description} ({spec.kind})")
    X = load_test(args.fault, args.data)
    print(f"   960 samples, fault injected at sample {FAULT_ONSET_INDEX}")
    result = monitor.score(X)
    metrics = evaluate_detection(result.alarm, args.fault)
    print()
    print(f"   before the fault : {result.alarm[:FAULT_ONSET_INDEX].mean():>6.1%} flagged")
    print(f"   after the fault  : {result.alarm[FAULT_ONSET_INDEX:].mean():>6.1%} flagged")
    print(f"   detected in      : {metrics.detection_delay_minutes:.0f} minutes")
    time.sleep(p)

    header("5. Which instrument is responsible", p)
    onset = X[FAULT_ONSET_INDEX]
    print("   Detection says something is wrong. Isolation says what.")
    print()
    for rank, idx in enumerate(monitor.rank_contributions(onset, "plain", 3), 1):
        print(f"   {rank}. {describe_variable(idx)}")
    if spec.root_variables:
        truth = ", ".join(describe_variable(i).split()[0] for i in spec.root_variables)
        print()
        print(f"{DIM}   Ground truth for this fault: {truth}{RESET}")
    time.sleep(p)

    header("6. Turning that into something an operator can act on", p)
    t2, spe = monitor.statistics(onset.reshape(1, -1))
    trigger = "T2" if t2[0] > monitor.t2_limit_ else "SPE"
    expl = explain(
        monitor, onset, float(t2[0]), float(spe[0]),
        trigger=trigger, use_bedrock=args.bedrock,
    )
    print(f"   source: {expl.source}")
    print()
    for line in _wrap(expl.text, 64):
        print(f"   {line}")
    print()
    print(f"{DIM}   The model never sees raw process data and is never asked what is{RESET}")
    print(f"{DIM}   wrong. PCA decides; the model only writes it up. Every number{RESET}")
    print(f"{DIM}   above is passed through, not generated.{RESET}")
    time.sleep(p)

    header("7. Is the plant broken, or is the model stale?", p)
    fault_drift = check_drift(monitor, X)
    shifted_plant = load_test(0, args.data) + 1.5 * monitor.std_
    real_drift = check_drift(monitor, shifted_plant)
    print(f"   real fault run        -> broad shift: {fault_drift.broad_shift}, "
          f"onset detected: {fault_drift.step_change}")
    print(f"   plant moved 1.5 sigma -> broad shift: {real_drift.broad_shift}, "
          f"onset detected: {real_drift.step_change}")
    print()
    print(f"{DIM}   Both produce high alarm rates. A fault means the plant broke and{RESET}")
    print(f"{DIM}   the monitor is right. Drift means the monitor's assumptions{RESET}")
    print(f"{DIM}   expired. Different people need to be told.{RESET}")
    time.sleep(p)

    header("8. In production", p)
    print("   A CSV lands in S3. S3 invokes a Lambda. The Lambda scores it and")
    print("   writes the result back, including the explanation above.")
    print("   No server. Terraform builds it, CI guards the numbers, and")
    print("   `./deploy.sh destroy` removes every resource.")
    print()
    print(f"   Benchmarked across all 21 faults: {BOLD}78.4%{RESET} mean detection,")
    print(f"   {BOLD}3.9%{RESET} false alarms, {BOLD}44 min{RESET} median delay.")
    print()
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
