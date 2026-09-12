#!/usr/bin/env python3
"""Add the Bedrock explanation layer to lambda/handler.py. Safe to re-run."""
import pathlib, sys

p = pathlib.Path("lambda/handler.py")
if not p.exists():
    sys.exit("lambda/handler.py not found -- run this from the repo root")
s = p.read_text()

if "ENABLE_EXPLANATION" in s:
    sys.exit("already patched, nothing to do")

edits = [
(
 "from tepguard.data import N_VARIABLES, describe_variable  # noqa: E402",
 "from tepguard.data import N_VARIABLES, describe_variable  # noqa: E402\n"
 "from tepguard.explain import explain  # noqa: E402",
),
(
 'MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "5000"))',
 'MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "5000"))\n\n'
 "# Explanations are opt-in. Bedrock is pay-per-token with no free tier, and a\n"
 "# batch of 240 flagged samples does not need 240 model calls -- consecutive\n"
 "# samples in one alarm describe the same event. So at most one call per batch,\n"
 "# on the first flagged sample, and only when explicitly enabled.\n"
 'ENABLE_EXPLANATION = os.environ.get("ENABLE_EXPLANATION", "false").lower() == "true"\n'
 'BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0")',
),
(
 '    return {\n        "n_samples"',
 '    payload = {\n        "n_samples"',
),
(
 '        "flagged_samples": samples,\n    }\n',
 '        "flagged_samples": samples,\n    }\n\n'
 "    # One explanation per batch, describing the first flagged sample. If\n"
 "    # Bedrock is unavailable, explain() falls back to a deterministic\n"
 "    # template -- a monitoring system must not stop reporting faults\n"
 "    # because a language model is down.\n"
 "    if ENABLE_EXPLANATION and flagged.size:\n"
 "        first = int(flagged[0])\n"
 "        result_expl = explain(\n"
 "            monitor,\n"
 "            X[first],\n"
 "            float(result.t2[first]),\n"
 "            float(result.spe[first]),\n"
 '            trigger=samples[0]["trigger"],\n'
 "            top_k=top_k,\n"
 "            model_id=BEDROCK_MODEL_ID,\n"
 "        )\n"
 '        payload["explanation"] = result_expl.as_dict()\n\n'
 "    return payload\n",
),
]

for old, new in edits:
    if s.count(old) != 1:
        sys.exit(f"anchor not found exactly once, aborting:\n  {old[:60]}...")
    s = s.replace(old, new)

p.write_text(s)
compile(s, str(p), "exec")
print("patched lambda/handler.py")
print("  ENABLE_EXPLANATION occurrences:", s.count("ENABLE_EXPLANATION"))
