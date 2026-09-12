![CI](https://github.com/RahulGoyal7890/tep-guard/actions/workflows/ci.yml/badge.svg)

<<<<<<< HEAD
# tep-guard
=======
# TEP-Guard

Serverless multivariate process monitoring for a chemical plant, benchmarked
on the Tennessee Eastman Process. Detects that something has gone wrong, then
names the instruments responsible.

Runs on AWS Lambda, deployed with Terraform, tested in CI. Costs about a
dollar a month at demo scale, and nothing when idle.

```bash
make data && make bench && make test && make invoke
```

## Why this exists

Chemical plants run hundreds of correlated sensors. Univariate alarms on each
one miss the failures that matter, because a fault often shows up as the
*relationship* between variables breaking rather than any single tag leaving
its range. Multivariate statistical process monitoring catches those, and it
is what commercial systems from AspenTech, AVEVA and Honeywell are built on.

Tennessee Eastman is the standard public benchmark for this problem: a
simulated plant with 52 measured variables and 21 documented faults, published
by Downs and Vogel in 1993 and used in the fault detection literature ever
since. It has labelled ground truth for every fault, which real plant data
almost never does.

## Results

Fitted on 500 samples of fault-free operation. Evaluated on 21 held-out
faulty runs of 960 samples each, fault injected at sample 160.

| Metric | Value |
| --- | --- |
| Mean detection rate, 18 detectable faults | **78.4%** |
| False alarm rate, held-out normal run | **3.9%** |
| Median detection delay | **44 min** |
| Mean detection rate on faults 3, 9, 15 | 10.0% |
| Top-3 isolation accuracy | **50.4%** |
| Components retained | 24 of 52 |

Faults 3, 9 and 15 are reported separately because no published method
detects them reliably; their signature is not distinguishable from normal
operation. Folding them into the headline mean would understate a system that
is working correctly. Full per-fault numbers are in
[`reports/benchmark.md`](reports/benchmark.md).

## Two things that did not work

Portfolio projects usually only report the wins. These are the two results
that went against what the literature suggested, and they were more
interesting than the parts that worked.

### Reconstruction-based contributions lost to plain contributions

Plain SPE contributions have a known weakness called smearing: a fault in one
variable inflates the residuals of everything correlated with it, so the
contribution plot blames innocent instruments. Reconstruction-based
contribution (RBC, Alcala & Qin 2009) was designed to fix exactly this.

On this benchmark it did not.

| Method | Top-3 isolation accuracy |
| --- | --- |
| Plain contributions | **50.4%** |
| RBC | 47.8% |

Scored across the 10 faults whose root cause maps to specific instrumentation
without argument. RBC won on faults 1 and 7 by a small margin and lost
clearly on 11 and 14. The likely reason is that RBC assumes a fault direction
aligned with a single variable axis; TEP faults propagate through control
loops, so by the time the fault is visible several variables have genuinely
moved and the single-variable reconstruction is the wrong model. Both methods
are implemented and the comparison reruns with `make bench`.

### The textbook control limits were wrong by a factor of four

The analytic SPE limit (Jackson & Mudholkar) is standard and appears in every
reference on the subject. Configured for a 1% false alarm rate, it produced
**17.7%** false alarms on a held-out normal run.

Diagnosing it ruled out the obvious explanations. There was no distribution
shift between the training and test runs: the largest mean shift across all
52 variables was 0.46 sigma. In-sample false alarms were 0.2%, so the model
fit fine. The cause was the residual subspace being overfit:

| Mean SPE on | Value |
| --- | --- |
| The split the projection was fitted on | 4.85 |
| Held-out normal samples | 8.07 |
| A completely separate normal run | 9.01 |

The analytic limit is derived from residual eigenvalues estimated on the same
data the projection was fitted to, and the small-eigenvalue tail of a sample
covariance is biased low. So the limit sits at roughly half of where it
should, and new data walks straight through it.

The fix is to fit the projection on one chronological split and calibrate the
limits as percentiles on a disjoint one. That took the false alarm rate to
3.9%, and to **0.4%** under the sustained-alarm rule the system actually uses
for detection. The split is chronological rather than random on purpose:
these statistics have lag-1 autocorrelation around 0.5, so a random split
would leak neighbouring samples across it and restore the same optimism.

This is the single most useful thing in the repo. A monitoring system with
17.7% false alarms gets muted by operators in the first week, and the
textbook formula gives you one without complaining.

## How it works

```
d00.dat (fault-free)
    |
    +-- fit split (350)  ->  standardize -> PCA, 24 components
    |
    +-- calibration split (150)  ->  empirical T2 and SPE limits
                                          |
                                          v
                            artifacts/monitor.json  (30 KB)
                                          |
                          baked into the Lambda container image
                                          |
new batch --> S3 --> Lambda --> T2 / SPE --> alarm? --> contribution ranking
                                                              |
                                                              v
                                                     top-3 suspect instruments
```

Two statistics, because faults arrive in two ways. **T²** measures abnormal
movement *inside* the subspace the process normally varies in. **SPE**
measures movement *orthogonal* to it, meaning the correlation structure has
broken. A sensor drifting out of agreement with its neighbours shows up in
SPE; a genuine operating-point excursion shows up in T².

An alarm requires three consecutive exceedances. A single sample crossing a
limit is noise, operators do not act on it, and counting it as a detection
flatters the numbers.

### Worked example

Fault 4 is a step change in reactor cooling water inlet temperature. Running
`make invoke`:

```
normal (pre-fault):   2/160 flagged (1.3%)
faulty:             232/240 flagged (96.7%) in 3 ms
  sample 0: T2=162.8 SPE=99.4 via T2
    XMEAS(21) Reactor cooling water outlet temperature      15.746
    XMEAS(14) Product separator underflow (stream 10)       13.542
    XMEAS(9)  Reactor temperature                            9.371
```

The top contributor is the reactor cooling water outlet temperature, which is
the correct physical answer.

## Repository layout

```
src/tepguard/
    data.py       loaders, 52 variable names, 21-fault catalogue
    monitor.py    PCAMonitor: fit, calibrate, T2/SPE, contributions, RBC
    metrics.py    detection rate, FAR, sustained delay, top-k isolation
lambda/
    handler.py    three event shapes, validation, structured logging
    Dockerfile    AWS base image, numpy only
scripts/
    download_data.py, run_benchmark.py, local_invoke.py
tests/            29 tests
infra/            Terraform (day 2)
```

`scipy` is imported lazily and only at fit time. Once the limits are baked
into `monitor.json`, inference needs nothing but numpy, which keeps roughly
100 MB out of the container image. There is a test that blocks the `scipy`
import and scores a batch anyway.

## Notes on the data

Tennessee Eastman is public simulation output, not plant data. No proprietary
or client data is used anywhere in this project.

One trap worth naming: `d00.dat` ships transposed as (52, 500) while every
other file is (samples, variables). Load it without the transpose and you get
a 52-sample, 500-variable matrix. PCA still runs, no error is raised, and
every number downstream is meaningless. `tests/test_tepguard.py` asserts the
shape.

### On the isolation ground truth

Isolation accuracy is only scored on the 10 faults whose root cause maps to
specific instrumentation without argument, for example fault 4 (reactor
cooling water inlet temperature) mapping to XMV(10) and XMEAS(21). For faults
like 13 (slow drift in reaction kinetics) or the unknown faults 16 to 20,
there is no defensible single answer, and inventing one would make the metric
look rigorous while measuring nothing. The mapping is in `FAULTS` in
`src/tepguard/data.py` and is open to argument.

## References

- Downs & Vogel (1993), *A plant-wide industrial process control problem*,
  Computers & Chemical Engineering 17(3):245-255
- Jackson & Mudholkar (1979), *Control procedures for residuals associated
  with principal component analysis*, Technometrics 21(3):341-349
- Alcala & Qin (2009), *Reconstruction-based contribution for process
  monitoring*, Automatica 45(7):1593-1600
- Chiang, Russell & Braatz (2001), *Fault Detection and Diagnosis in
  Industrial Systems*

## Status

- [x] Day 1: data, monitor, calibration, 29 tests, benchmark, Lambda handler
- [ ] Day 2: Terraform (S3, ECR, Lambda, IAM), verified teardown
- [ ] Day 3: Bedrock layer turning contributions into an operator-facing
      explanation
- [ ] Day 4: GitHub Actions CI, input drift monitoring
- [ ] Day 5: architecture diagram, demo GIF
>>>>>>> 0e808a4 (PCA process monitoring on Tennessee Eastman: detection, isolation, calibrated limits)
