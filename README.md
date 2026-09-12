![CI](https://github.com/RahulGoyal7890/tep-guard/actions/workflows/ci.yml/badge.svg)

# TEP-Guard

Serverless multivariate process monitoring for a chemical plant. Detects that
something has gone wrong, names the instruments responsible, and writes a
summary an operator can act on.

Benchmarked on the Tennessee Eastman Process. Deployed on AWS Lambda with
Terraform, tested in CI, and costs about a dollar a month to run.

```bash
make venv && source .venv/bin/activate
make data && make bench && make test
python scripts/demo.py
```

## Why this exists

Chemical plants run hundreds of correlated sensors. Univariate alarms on each
one miss the failures that matter, because a fault often shows up as the
*relationship* between variables breaking rather than any single tag leaving
its range. Multivariate statistical process monitoring catches those, and it is
what the commercial systems from AspenTech, AVEVA and Honeywell are built on.

Tennessee Eastman is the standard public benchmark: a simulated plant with 52
measured variables and 21 documented faults, published by Downs and Vogel in
1993 and used in the fault-detection literature ever since. It has labelled
ground truth for every fault, which real plant data almost never does — which is
exactly what makes it possible to report honest isolation numbers here.

## Results

Fitted on 500 samples of fault-free operation. Evaluated on 21 held-out faulty
runs of 960 samples each, fault injected at sample 160.

| Metric | Value |
| --- | --- |
| Mean detection rate, 18 detectable faults | **78.4%** |
| False alarm rate, held-out normal run | **3.9%** |
| False alarm rate under the sustained-alarm rule | **0.4%** |
| Median detection delay | **44 min** |
| Mean detection rate on faults 3, 9, 15 | 10.0% |
| Top-3 isolation accuracy | **50.4%** |
| Components retained | 24 of 52 |

Faults 3, 9 and 15 are reported separately because no published method detects
them reliably; their signature is not distinguishable from normal operation.
Folding them into the headline would understate a system that is working
correctly. Per-fault numbers are in [`reports/benchmark.md`](reports/benchmark.md),
and CI fails the build if the mean detection rate or isolation accuracy drifts
outside a set band.

## Architecture

```
d00.dat (fault-free)
    ├── fit split (350)         → standardise → PCA, 24 components
    └── calibration split (150) → empirical T² and SPE limits
                                        ↓
                          artifacts/monitor.json  (30 KB)
                                        ↓
                        baked into a Lambda container image
                                        ↓
CSV → S3 batches/ → Lambda → T²/SPE → alarm? → contribution ranking
                                                       ↓
                                          Bedrock writes the summary
                                                       ↓
                                              S3 results/ + CloudWatch
```

Two statistics, because faults arrive in two ways. **T²** measures abnormal
movement *inside* the subspace the process normally varies in. **SPE** measures
movement *orthogonal* to it, meaning the correlation structure has broken. A
sensor drifting out of agreement with its neighbours shows up in SPE; a genuine
operating-point excursion shows up in T².

An alarm requires three consecutive exceedances. A single sample crossing a
limit is noise, operators do not act on it, and counting it as a detection
flatters the numbers.

### The LLM narrates; it does not diagnose

The contribution analysis produces a ranked list of instruments. Bedrock turns
that list into prose. It never sees raw process data, is never asked what is
wrong, and cannot add a variable to the suspect list or invent a root cause.
Every number in its output is passed through from the statistics.

That constraint is deliberate. A wrong number from PCA is a bug a test catches.
A confident fabrication from a language model is indistinguishable from a
correct answer, and on a plant it would be acted on. So the model has no
authority over any claim that matters.

If Bedrock is unavailable the system falls back to a deterministic template. A
monitoring system must not stop reporting faults because a language model is
down.

### Worked example

Fault 4 is a step change in reactor cooling water inlet temperature.

```
before the fault :   1.2% flagged
after the fault  :  97.0% flagged
detected in      : 0 minutes

1. XMEAS(21) Reactor cooling water outlet temperature
2. XMEAS(14) Product separator underflow (stream 10)
3. XMEAS(9)  Reactor temperature
```

And the generated summary:

> A severe abnormal condition has been detected by the process monitor. The
> Reactor cooling water outlet temperature from XMEAS(21) is moderately above
> its normal operating range, the Product separator underflow (stream 10) from
> XMEAS(14) is moderately above its normal operating range, and the Reactor
> temperature from XMEAS(9) is far above its normal operating range. Check the
> cooling water system first.

The top contributor is the reactor cooling water outlet temperature, which is
the correct physical answer.

## Four things that did not work

These were the interesting parts. Each is a case where the obvious approach
reported success while being wrong.

### 1. The textbook control limits were wrong by a factor of four

The analytic SPE limit (Jackson & Mudholkar) is standard and appears in every
reference on the subject. Configured for a 1% false alarm rate, it produced
**17.7%** false alarms on a held-out normal run.

Diagnosing it ruled out the obvious explanations. There was no distribution
shift between the training and test runs — the largest mean shift across all 52
variables was 0.46σ. In-sample false alarms were 0.2%, so the model fit fine.
The cause was the residual subspace being overfit:

| Mean SPE on | Value |
| --- | --- |
| The split the projection was fitted on | 4.85 |
| Held-out normal samples | 8.07 |
| A completely separate normal run | 9.01 |

The analytic limit is derived from residual eigenvalues estimated on the same
data the projection was fitted to, and the small-eigenvalue tail of a sample
covariance is biased low. So the limit sits at roughly half of where it should,
and new data walks straight through it.

Fitting the projection on one chronological split and calibrating the limits as
percentiles on a disjoint one took false alarms to 3.9%, and to **0.4%** under
the sustained-alarm rule. The split is chronological rather than random on
purpose: these statistics have lag-1 autocorrelation around 0.5, so a random
split would leak neighbouring samples across it and restore the same optimism.

A monitoring system with 17.7% false alarms gets muted by operators in the first
week, and the textbook formula hands you one without complaining.

### 2. Reconstruction-based contributions lost to plain contributions

Plain SPE contributions have a known weakness called smearing: a fault in one
variable inflates the residuals of everything correlated with it.
Reconstruction-based contribution (RBC, Alcala & Qin 2009) was designed to fix
exactly this.

| Method | Top-3 isolation accuracy |
| --- | --- |
| Plain contributions | **50.4%** |
| RBC | 47.8% |

Scored across the 10 faults whose root cause maps to specific instrumentation
without argument. RBC won narrowly on faults 1 and 7 and lost clearly on 11 and
14. The likely reason is that RBC assumes a fault direction aligned with a
single variable axis, while TEP faults propagate through control loops — by the
time the fault is visible, several variables have genuinely moved.

### 3. A mutable image tag pinned the Lambda to three-week-old code

The Lambda pointed at `tep-guard:latest`. Pushing a new image to the same tag
left Terraform seeing `image_uri` unchanged — still the literal string
`...:latest` — so it never updated the function. Lambda had resolved the image
*digest* at creation and kept running that one.

Everything reported success. Docker built, the push succeeded, Terraform applied
cleanly, environment variables updated correctly, IAM was correct. The function
silently executed old code, and the symptom was a feature that appeared to
deploy but never took effect.

Fixed by tagging images with the git SHA, so `image_uri` changes on every
deploy, plus an explicit digest comparison in `deploy.sh` that forces an update
and fails loudly if they still disagree.

### 4. Counting shifted variables cannot separate a fault from drift

A fault means the plant broke and the monitor is right. Drift means the plant
moved to a new operating point and the monitor's assumptions expired. Both
produce high alarm rates, and different people need to be told.

The obvious heuristic — a fault moves a few variables, drift moves many — fails.
Fault 6 is a total loss of the A feed, which cascades through 39 of the 52
variables. By count it is indistinguishable from drift.

What separates them is time. A fault has an onset: the batch is normal, then it
is not. Drift is already present when the batch starts. Comparing the opening
quarter of a batch against the closing quarter classifies every TEP fault
correctly while still flagging a synthetic plant-wide shift as drift.

## Infrastructure

Terraform provisions S3, ECR, the Lambda function, a scoped IAM role, and a
CloudWatch log group. Dropping a CSV into `batches/` triggers the function.

```bash
./deploy.sh            # build, push, apply, verify digest, smoke test
./deploy.sh destroy    # remove everything, then confirm nothing survived
```

The IAM role gets `s3:GetObject` on `batches/*`, `s3:PutObject` on `results/*`,
log writes, and `bedrock:InvokeModel` on one specific model — not
`AmazonS3FullAccess` and not a Bedrock wildcard.

Cost traps and the specific Terraform line defusing each are in
[`COSTS.md`](COSTS.md). The short version: no VPC (a NAT Gateway is ~$32/month
whether or not traffic flows), an ECR lifecycle policy (every push orphans the
previous ~250 MB image), an explicit log group (Lambda-created ones default to
never expire and survive `terraform destroy`), and no S3 versioning.

## CI

Three jobs on every push:

- **Tests** — 29 tests, then reruns the benchmark and fails if mean detection
  rate leaves [0.74, 0.82] or isolation leaves [0.46, 0.55]. A refactor can keep
  every unit test green while quietly changing the headline numbers.
- **Lambda image** — builds the real container, asserts `import scipy` *fails*
  inside it, and asserts the baked-in model is calibrated.
- **Terraform** — format check, validate, and a grep that fails the build if
  `vpc_config` ever appears or log retention goes missing.

The gates check the README's claims rather than just whether the code runs.

## Repository layout

```
src/tepguard/
    data.py       loaders, 52 variable names, 21-fault catalogue
    monitor.py    PCAMonitor: fit, calibrate, T²/SPE, contributions, RBC
    metrics.py    detection rate, FAR, sustained delay, top-k isolation
    explain.py    Bedrock narration with deterministic fallback
    drift.py      distribution shift vs process fault
lambda/
    handler.py    three event shapes, validation, structured logging
    Dockerfile    AWS base image, numpy only
infra/            Terraform
scripts/          download, benchmark, demo, local invoke, memory tuning
tests/            29 tests
```

`scipy` is imported lazily and only at fit time. Once the limits are baked into
`monitor.json`, inference needs nothing but numpy, keeping roughly 100 MB out of
the container. A CI step asserts it.

## Notes on the data

Tennessee Eastman is public simulation output, not plant data. No proprietary or
client data is used anywhere in this project.

One trap worth naming: `d00.dat` ships transposed as (52, 500) while every other
file is (samples, variables). Load it without the transpose and you get a
52-sample, 500-variable matrix. PCA still runs, no error is raised, and every
number downstream is meaningless. There is a test asserting the shape.

The downloader validates every file by loading it and checking its shape rather
than checking that a file exists, because an interrupted download leaves a
valid-looking truncated file that silently produces a plausible wrong answer.

### On the isolation ground truth

Isolation accuracy is scored only on the 10 faults whose root cause maps to
specific instrumentation without argument — for example fault 4 mapping to
XMV(10) and XMEAS(21). For faults like 13 (slow drift in reaction kinetics) or
the unknown faults 16–20 there is no defensible single answer, and inventing one
would make the metric look rigorous while measuring nothing. The mapping is in
`FAULTS` in `src/tepguard/data.py` and is open to argument.

## Limitations

- Isolation at 50.4% top-3 is useful for narrowing a search, not for automatic
  root-cause attribution.
- Drift detection is implemented and tested but not yet wired into the Lambda
  handler.
- Lambda memory is set to 512 MB against 112 MB observed usage. Since Lambda
  scales CPU with memory, the cheaper setting is an empirical question;
  `scripts/tune_memory.py` measures it and has not yet been run.
- The plain-language severity bands improved readability but lost the causal
  framing an earlier prompt produced — the summary no longer explains *why* the
  largest-moving variable is not the cause.
- Single-model monitoring only. Real plants need per-operating-mode models, and a
  mode change here would register as drift.

## References

- Downs & Vogel (1993), *A plant-wide industrial process control problem*,
  Computers & Chemical Engineering 17(3):245–255
- Jackson & Mudholkar (1979), *Control procedures for residuals associated with
  principal component analysis*, Technometrics 21(3):341–349
- Alcala & Qin (2009), *Reconstruction-based contribution for process
  monitoring*, Automatica 45(7):1593–1600
- Chiang, Russell & Braatz (2001), *Fault Detection and Diagnosis in Industrial
  Systems*
