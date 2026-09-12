"""AWS Lambda entry point for TEP-Guard.

Accepts a batch of process samples and returns, per sample, whether the
monitor flagged it and which instruments the contribution analysis blames.

Three event shapes are supported:

    inline      {"samples": [[52 floats], ...]}
    s3 pointer  {"bucket": "...", "key": "batches/run.csv"}
    s3 trigger  the standard {"Records": [{"s3": {...}}]} notification

The fitted model is baked into the container image at /opt/monitor.json
rather than fetched from S3 at runtime. A 30 KB file is not worth a network
round trip on every cold start, and it keeps the function working when S3
is having a bad day.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sys
import time
from typing import Any

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from tepguard import PCAMonitor  # noqa: E402
from tepguard.data import N_VARIABLES, describe_variable  # noqa: E402
from tepguard.explain import explain  # noqa: E402

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

MODEL_PATH = os.environ.get("MODEL_PATH", "/opt/monitor.json")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET")
TOP_K = int(os.environ.get("TOP_K", "3"))
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "5000"))

# Explanations are opt-in. Bedrock is pay-per-token with no free tier, and a
# batch of 240 flagged samples does not need 240 model calls -- consecutive
# samples in one alarm describe the same event. So at most one call per batch,
# on the first flagged sample, and only when explicitly enabled.
ENABLE_EXPLANATION = os.environ.get("ENABLE_EXPLANATION", "false").lower() == "true"
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0")

# Loaded once per container, reused across warm invocations.
_MONITOR: PCAMonitor | None = None
_S3 = None


def _monitor() -> PCAMonitor:
    global _MONITOR
    if _MONITOR is None:
        start = time.perf_counter()
        _MONITOR = PCAMonitor.load(MODEL_PATH)
        logger.info(
            "loaded model k=%d in %.1f ms",
            _MONITOR.k_,
            (time.perf_counter() - start) * 1000,
        )
    return _MONITOR


def _s3():
    global _S3
    if _S3 is None:
        import boto3

        _S3 = boto3.client("s3")
    return _S3


class BadRequest(Exception):
    """Client error. Surfaced as a 400 rather than a stack trace."""


# ------------------------------------------------------------- parsing --


def _parse_csv(text: str) -> np.ndarray:
    rows = [r for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
    if not rows:
        raise BadRequest("CSV contained no data rows")
    # Skip a header row if the first cell is not a number.
    try:
        float(rows[0][0])
    except ValueError:
        rows = rows[1:]
    try:
        return np.array([[float(c) for c in r] for r in rows], dtype=np.float64)
    except ValueError as exc:
        raise BadRequest(f"Non-numeric value in CSV: {exc}") from exc


def _extract_samples(event: dict[str, Any]) -> tuple[np.ndarray, str]:
    if "Records" in event:
        record = event["Records"][0]["s3"]
        bucket, key = record["bucket"]["name"], record["object"]["key"]
    elif "bucket" in event and "key" in event:
        bucket, key = event["bucket"], event["key"]
    elif "samples" in event:
        raw = event["samples"]
        if not isinstance(raw, list) or not raw:
            raise BadRequest("'samples' must be a non-empty list of rows")
        try:
            return np.atleast_2d(np.asarray(raw, dtype=np.float64)), "inline"
        except (ValueError, TypeError) as exc:
            raise BadRequest(f"Could not parse 'samples': {exc}") from exc
    else:
        raise BadRequest(
            "Event must contain 'samples', or 'bucket' and 'key', "
            "or be an S3 notification"
        )

    body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    return _parse_csv(body), f"s3://{bucket}/{key}"


def _validate(X: np.ndarray) -> np.ndarray:
    if X.ndim != 2:
        raise BadRequest(f"Expected a 2-D batch, got shape {X.shape}")
    if X.shape[1] != N_VARIABLES:
        raise BadRequest(
            f"Expected {N_VARIABLES} variables per sample, got {X.shape[1]}"
        )
    if X.shape[0] > MAX_SAMPLES:
        raise BadRequest(f"Batch of {X.shape[0]} exceeds MAX_SAMPLES={MAX_SAMPLES}")
    if not np.isfinite(X).all():
        bad = int((~np.isfinite(X)).sum())
        raise BadRequest(f"Batch contains {bad} NaN or infinite values")
    return X


# -------------------------------------------------------------- scoring --


def score_batch(X: np.ndarray, top_k: int = TOP_K) -> dict[str, Any]:
    monitor = _monitor()
    result = monitor.score(X)
    contributions = monitor.contributions(X)
    contributions = np.atleast_2d(contributions)

    flagged = np.flatnonzero(result.alarm)
    order = np.argsort(-contributions[flagged], axis=1)[:, :top_k] if flagged.size else []

    samples = []
    for row, idx in enumerate(flagged):
        blame = [
            {
                "variable": describe_variable(int(c)),
                "contribution": round(float(contributions[idx, c]), 4),
            }
            for c in order[row]
        ]
        samples.append(
            {
                "index": int(idx),
                "t2": round(float(result.t2[idx]), 3),
                "spe": round(float(result.spe[idx]), 3),
                "trigger": "T2" if result.t2_alarm[idx] else "SPE",
                "top_contributors": blame,
            }
        )

    payload = {
        "n_samples": int(X.shape[0]),
        "n_flagged": int(flagged.size),
        "alarm_rate": round(float(result.alarm.mean()), 4),
        "limits": {
            "t2": round(float(monitor.t2_limit_), 3),
            "spe": round(float(monitor.spe_limit_), 3),
        },
        "components": int(monitor.k_),
        "flagged_samples": samples,
    }

    # One explanation per batch, describing the first flagged sample. If
    # Bedrock is unavailable, explain() falls back to a deterministic
    # template -- a monitoring system must not stop reporting faults
    # because a language model is down.
    if ENABLE_EXPLANATION and flagged.size:
        first = int(flagged[0])
        result_expl = explain(
            monitor,
            X[first],
            float(result.t2[first]),
            float(result.spe[first]),
            trigger=samples[0]["trigger"],
            top_k=top_k,
            model_id=BEDROCK_MODEL_ID,
        )
        payload["explanation"] = result_expl.as_dict()

    return payload


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        X, source = _extract_samples(event or {})
        X = _validate(X)
        payload = score_batch(X)
        payload["source"] = source
        payload["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)

        logger.info(
            "scored %d samples from %s: %d flagged (%.1f%%) in %.1f ms",
            payload["n_samples"],
            source,
            payload["n_flagged"],
            payload["alarm_rate"] * 100,
            payload["duration_ms"],
        )

        if OUTPUT_BUCKET:
            key = f"results/{int(time.time() * 1000)}.json"
            _s3().put_object(
                Bucket=OUTPUT_BUCKET,
                Key=key,
                Body=json.dumps(payload).encode(),
                ContentType="application/json",
            )
            payload["result_key"] = key

        return {"statusCode": 200, "body": payload}

    except BadRequest as exc:
        logger.warning("bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}
    except Exception as exc:  # noqa: BLE001
        logger.exception("unhandled error")
        return {"statusCode": 500, "body": {"error": f"{type(exc).__name__}: {exc}"}}
