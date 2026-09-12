"""Turn a statistical detection into something an operator can act on.

The contribution analysis outputs this:

    XMEAS(21) Reactor cooling water outlet temperature   15.746
    XMEAS(14) Product separator underflow (stream 10)    13.542
    XMEAS(9)  Reactor temperature                         9.371

which is correct and nearly useless at 3am. This module converts it into a
short written summary of what moved, in which direction, and what to check.

The important design constraint: **the model does not diagnose anything.**

PCA decides whether there is a fault and which variables are responsible.
Those are deterministic, benchmarked, and reproducible. The language model
receives that finished answer and only renders it as prose. It never sees raw
process data, is never asked "what is wrong", and cannot promote a variable
into the suspect list or invent a root cause.

That split matters because the failure modes are so different. A wrong number
from PCA is a bug you can find with a test. A confident fabrication from an
LLM is indistinguishable from a correct answer, and on a plant it would be
acted on. So the LLM is given no authority over any claim that matters, and
every number in the output is passed through from the statistics.

Falls back to a deterministic template when Bedrock is unavailable, so the
demo and the test suite never depend on a network call.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .data import VARIABLE_NAMES, VARIABLE_TAGS, describe_variable

DEFAULT_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0")
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
CACHE_DIR = Path(os.environ.get("EXPLANATION_CACHE", "artifacts/explanations"))

MAX_TOKENS = 300
TEMPERATURE = 0.0  # same alarm should produce the same words

SYSTEM_PROMPT = """\
You are writing a short alarm summary for a control room operator on a \
chemical plant.

You are given the output of a statistical process monitor that has already \
determined a fault exists and which instruments are responsible. Your only \
job is to write that up clearly.

Rules:
- Do not diagnose a root cause that is not supported by the listed variables.
- Do not introduce any variable, tag, unit or number that is not given to you.
- Do not speculate about equipment that is not mentioned.
- If the evidence is thin, say so plainly rather than inventing a story.
- Write 3 to 5 sentences of plain prose. No headings, no bullet points, no \
markdown.
- Assume the reader knows the plant and does not know statistics. Never \
mention T-squared, SPE, statistics, thresholds, standard deviations or \
monitors. Say "well outside its normal range", not "3.6 times its alarm \
threshold".
- Name the instruments in EXACTLY the order given. The order is the \
diagnosis, not a detail. Do not re-sort by how far anything moved.
- The first instrument named must be the first one in the list you are given.
- End with one concrete thing to check first.
"""


@dataclass
class Deviation:
    """One suspect variable and how far it has moved from normal."""

    index: int
    tag: str
    name: str
    contribution: float
    z_score: float

    @property
    def direction(self) -> str:
        if self.z_score > 0.5:
            return "above"
        if self.z_score < -0.5:
            return "below"
        return "near"

    def describe(self) -> str:
        return (
            f"{self.tag} {self.name}: {abs(self.z_score):.1f} standard "
            f"deviations {self.direction} its normal operating value"
        )


@dataclass
class Explanation:
    text: str
    source: str  # "bedrock" | "template" | "cache"
    deviations: list[Deviation] = field(default_factory=list)
    model_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "model_id": self.model_id,
            "deviations": [
                {
                    "variable": describe_variable(d.index),
                    "z_score": round(d.z_score, 2),
                    "direction": d.direction,
                }
                for d in self.deviations
            ],
        }


def compute_deviations(
    monitor, sample: np.ndarray, top_k: int = 3
) -> list[Deviation]:
    """Rank suspects by contribution and measure how far each has moved.

    Contribution says which variable is responsible. It does not say whether
    the value went up or down, because it is a squared quantity. An operator
    needs the direction, so it is recovered from the standardised value.
    """
    sample = np.ravel(np.asarray(sample, dtype=np.float64))
    contrib = np.ravel(np.atleast_2d(monitor.contributions(sample)))
    z = (sample - monitor.mean_) / monitor.std_

    order = np.argsort(contrib)[::-1][:top_k]
    return [
        Deviation(
            index=int(i),
            tag=VARIABLE_TAGS[i],
            name=VARIABLE_NAMES[i],
            contribution=float(contrib[i]),
            z_score=float(z[i]),
        )
        for i in order
    ]


def build_prompt(
    deviations: list[Deviation],
    t2: float,
    spe: float,
    t2_limit: float,
    spe_limit: float,
    trigger: str,
) -> str:
    """Assemble the user message. Every number here comes from the monitor."""
    severity = max(t2 / t2_limit, spe / spe_limit)
    lines = [f"- {d.describe()}" for d in deviations]

    # The list is ordered by contribution, which is the *unexplained* part of
    # each deviation, not its size. A variable can move 11 sigma and rank last
    # because the monitor predicted that move from the others -- it is a knock-on
    # symptom, not an independent anomaly. Without saying so, the model sees a
    # list whose order contradicts its own numbers and will "helpfully" reorder
    # it by magnitude, which is exactly the wrong answer for the operator.
    largest = max(deviations, key=lambda d: abs(d.z_score)) if deviations else None
    note = ""
    if largest and deviations and largest.index != deviations[0].index:
        note = (
            f"\n\nNote: {largest.tag} has moved the furthest in absolute terms, "
            f"but the monitor attributes that to the variables above it rather "
            f"than to an independent problem with {largest.tag} itself. "
            f"Mention it as a consequence, not as the cause."
        )

    return (
        "A process monitor has flagged an abnormal condition.\n\n"
        f"Severity: {'severe' if severity > 3 else 'moderate' if severity > 1.5 else 'mild'}.\n\n"
        "The instruments responsible, ordered by how much of their deviation "
        "the monitor could NOT explain from the rest of the plant:\n"
        + "\n".join(lines)
        + note
        + "\n\nWrite the operator summary."
    )


def _cache_key(prompt: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\n{prompt}".encode()).hexdigest()[:16]


def _template(deviations: list[Deviation], trigger: str) -> str:
    """Deterministic fallback. Not elegant, but it never fabricates."""
    if not deviations:
        return "An abnormal condition was detected but no single instrument stands out."
    lead = deviations[0]
    rest = deviations[1:]
    text = (
        f"An abnormal condition was detected on the {trigger} statistic. "
        f"{lead.tag} ({lead.name}) has moved {abs(lead.z_score):.1f} standard "
        f"deviations {lead.direction} its normal value and is the largest "
        f"contributor."
    )
    if rest:
        others = ", ".join(f"{d.tag} ({d.name})" for d in rest)
        text += f" {others} also moved outside normal range."
    text += f" Check {lead.name.lower()} first."
    return text


def _invoke_bedrock(prompt: str, model_id: str, region: str) -> str:
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    response = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
    )
    return response["output"]["message"]["content"][0]["text"].strip()


def explain(
    monitor,
    sample: np.ndarray,
    t2: float,
    spe: float,
    trigger: str = "T2",
    top_k: int = 3,
    model_id: str = DEFAULT_MODEL_ID,
    region: str = DEFAULT_REGION,
    use_bedrock: bool = True,
    cache_dir: Path | None = None,
) -> Explanation:
    """Produce an operator-facing summary of a flagged sample.

    Cached by prompt hash. Two identical alarms cost one model call, and the
    committed cache lets anyone reproduce the demo with no AWS account.
    """
    deviations = compute_deviations(monitor, sample, top_k)
    prompt = build_prompt(
        deviations, t2, spe, monitor.t2_limit_, monitor.spe_limit_, trigger
    )

    if not use_bedrock:
        return Explanation(_template(deviations, trigger), "template", deviations)

    cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    cache_file = cache_dir / f"{_cache_key(prompt, model_id)}.json"

    if cache_file.exists():
        payload = json.loads(cache_file.read_text())
        return Explanation(payload["text"], "cache", deviations, model_id)

    try:
        text = _invoke_bedrock(prompt, model_id, region)
    except Exception as exc:  # noqa: BLE001
        # A monitoring system must not stop reporting faults because a
        # language model is unavailable. Degrade to the template.
        import logging

        logging.getLogger(__name__).warning(
            "bedrock unavailable (%s), using template", type(exc).__name__
        )
        return Explanation(_template(deviations, trigger), "template", deviations)

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"prompt": prompt, "text": text}, indent=2))
    except OSError:
        pass  # read-only filesystem in Lambda is fine, just skip caching

    return Explanation(text, "bedrock", deviations, model_id)
