#!/usr/bin/env python3
"""Measure the Lambda memory/duration/cost tradeoff on the deployed function.

Lambda bills GB-seconds, and CPU scales with the memory setting. So halving
memory does not halve cost: it roughly doubles duration, and the product can
go either way. The only honest way to pick a setting is to measure it.

Reconfigures the deployed function across several memory sizes, invokes it a
few times at each, reads the billed duration out of the REPORT log line, and
restores the original setting at the end.

    python scripts/tune_memory.py --function tep-guard --sizes 256,512,1024
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time

# us-east-1 x86 on-demand pricing, USD per GB-second.
PRICE_PER_GB_SECOND = 0.0000166667
PRICE_PER_REQUEST = 0.0000002

REPORT = re.compile(
    r"Billed Duration: (\d+) ms.*?Memory Size: (\d+) MB.*?Max Memory Used: (\d+) MB"
)


def aws(*args: str) -> str:
    return subprocess.run(
        ["aws", *args], capture_output=True, text=True, check=True
    ).stdout


def set_memory(fn: str, mb: int, region: str) -> None:
    aws("lambda", "update-function-configuration", "--function-name", fn,
        "--memory-size", str(mb), "--region", region)
    # The update is asynchronous; invoking mid-update fails.
    for _ in range(30):
        state = json.loads(
            aws("lambda", "get-function-configuration", "--function-name", fn,
                "--region", region)
        )
        if state["LastUpdateStatus"] == "Successful":
            return
        time.sleep(1)
    raise RuntimeError(f"function did not settle at {mb} MB")


def invoke(fn: str, payload: str, region: str) -> tuple[int, int]:
    """Invoke once, return (billed_ms, max_memory_used_mb) from the log tail."""
    out = aws("lambda", "invoke", "--function-name", fn, "--region", region,
              "--cli-binary-format", "raw-in-base64-out",
              "--payload", f"file://{payload}",
              "--log-type", "Tail", "--query", "LogResult", "--output", "text",
              "/dev/null")
    import base64

    log = base64.b64decode(out.strip()).decode()
    m = REPORT.search(log.replace("\n", " "))
    if not m:
        raise RuntimeError(f"no REPORT line found in log:\n{log[-500:]}")
    return int(m.group(1)), int(m.group(3))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--function", default="tep-guard")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--payload", default="/tmp/payload.json")
    ap.add_argument("--sizes", default="128,256,512,1024")
    ap.add_argument("--repeats", type=int, default=4)
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]
    original = json.loads(
        aws("lambda", "get-function-configuration", "--function-name",
            args.function, "--region", args.region)
    )["MemorySize"]
    print(f"current setting: {original} MB\n")

    rows = []
    try:
        for mb in sizes:
            set_memory(args.function, mb, args.region)
            invoke(args.function, args.payload, args.region)  # discard cold start
            samples = [invoke(args.function, args.payload, args.region)
                       for _ in range(args.repeats)]
            billed = sorted(s[0] for s in samples)[len(samples) // 2]
            used = max(s[1] for s in samples)
            cost = (mb / 1024) * (billed / 1000) * PRICE_PER_GB_SECOND + PRICE_PER_REQUEST
            rows.append((mb, billed, used, cost))
            print(f"  {mb:>5} MB  {billed:>5} ms billed  {used:>4} MB used  "
                  f"${cost * 1_000_000:.2f} per million invocations")
    finally:
        print(f"\nrestoring {original} MB")
        set_memory(args.function, original, args.region)

    best = min(rows, key=lambda r: r[3])
    print(f"\ncheapest: {best[0]} MB at ${best[3] * 1_000_000:.2f} per million")
    fastest = min(rows, key=lambda r: r[1])
    print(f"fastest:  {fastest[0]} MB at {fastest[1]} ms")
    if best[0] != fastest[0]:
        print("\nThey differ, which is the point: more memory buys CPU, so the")
        print("fastest setting is not always the cheapest, and vice versa.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
