"""Tests for tepguard.

The data-dependent tests skip cleanly if data/raw is absent, so CI can run
the pure-math tests without a 26 MB download.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tepguard import PCAMonitor, load_training, load_test
from tepguard.data import (
    FAULTS,
    FAULT_ONSET_INDEX,
    N_VARIABLES,
    UNDETECTABLE_FAULTS,
    VARIABLE_TAGS,
    fault_labels,
)
from tepguard.metrics import (
    detection_delay_minutes,
    detection_rate,
    evaluate_detection,
    false_alarm_rate,
    first_sustained_alarm,
    topk_hit_rate,
)

DATA = Path(__file__).resolve().parents[1] / "data" / "raw"
needs_data = pytest.mark.skipif(
    not (DATA / "d00.dat").exists(), reason="TEP data not downloaded"
)


# ------------------------------------------------------------ synthetic --


@pytest.fixture
def synthetic():
    """Normal data with a genuine low-rank correlation structure."""
    rng = np.random.default_rng(0)
    n, m, k = 600, 12, 3
    latent = rng.normal(size=(n, k))
    mixing = rng.normal(size=(k, m))
    return latent @ mixing + 0.05 * rng.normal(size=(n, m))


def test_fit_selects_low_rank(synthetic):
    m = PCAMonitor(variance_threshold=0.90).fit(synthetic)
    assert 1 <= m.k_ <= 5, "should recover roughly the 3 latent dimensions"
    assert m.t2_limit_ > 0 and m.spe_limit_ > 0


def test_statistics_are_nonnegative(synthetic):
    mon = PCAMonitor().fit(synthetic)
    t2, spe = mon.statistics(synthetic)
    assert (t2 >= 0).all() and (spe >= 0).all()
    assert t2.shape == (synthetic.shape[0],)


def test_constant_column_does_not_divide_by_zero(synthetic):
    X = np.column_stack([synthetic, np.full(len(synthetic), 7.0)])
    mon = PCAMonitor().fit(X)
    t2, spe = mon.statistics(X)
    assert np.isfinite(t2).all() and np.isfinite(spe).all()


def test_unfitted_monitor_raises():
    with pytest.raises(RuntimeError, match="not fitted"):
        PCAMonitor().statistics(np.zeros((3, 4)))


def test_wrong_variable_count_raises(synthetic):
    mon = PCAMonitor().fit(synthetic)
    with pytest.raises(ValueError, match="Expected"):
        mon.statistics(np.zeros((5, synthetic.shape[1] + 1)))


def test_spe_detects_a_broken_correlation(synthetic):
    """Corrupting one variable breaks correlation, so SPE should spike."""
    mon = PCAMonitor().fit_calibrate(synthetic)
    bad = synthetic[-1].copy()
    bad[0] += 20 * synthetic[:, 0].std()
    _, spe_bad = mon.statistics(bad.reshape(1, -1))
    assert spe_bad[0] > mon.spe_limit_


def test_contributions_point_at_the_corrupted_variable(synthetic):
    mon = PCAMonitor().fit(synthetic)
    bad = synthetic[-1].copy()
    bad[4] += 20 * synthetic[:, 4].std()
    assert mon.rank_contributions(bad.reshape(1, -1), "plain", top=1) == [4]
    assert mon.rank_contributions(bad.reshape(1, -1), "rbc", top=1) == [4]


def test_rbc_and_plain_are_not_the_same_ranking(synthetic):
    """If these agreed everywhere the RBC-vs-plain comparison would be empty."""
    mon = PCAMonitor().fit(synthetic)
    rng = np.random.default_rng(1)
    disagreements = 0
    for _ in range(40):
        x = synthetic[rng.integers(len(synthetic))] + rng.normal(0, 2, synthetic.shape[1])
        if mon.rank_contributions(x, "plain", 3) != mon.rank_contributions(x, "rbc", 3):
            disagreements += 1
    assert disagreements > 0


def test_unknown_contribution_method_raises(synthetic):
    with pytest.raises(ValueError, match="Unknown contribution method"):
        PCAMonitor().fit(synthetic).rank_contributions(synthetic[0], "magic")


def test_serialization_roundtrip(synthetic):
    mon = PCAMonitor().fit_calibrate(synthetic)
    clone = PCAMonitor.from_dict(json.loads(json.dumps(mon.to_dict())))
    a, b = mon.statistics(synthetic), clone.statistics(synthetic)
    assert np.allclose(a[0], b[0]) and np.allclose(a[1], b[1])
    assert clone.calibrated_ is True


def test_calibration_split_is_disjoint_and_chronological(synthetic):
    mon = PCAMonitor().fit_calibrate(synthetic, calibration_fraction=0.30)
    assert mon.n_train_ == 420 and mon.n_calibration_ == 180


def test_tiny_calibration_split_raises(synthetic):
    with pytest.raises(ValueError, match="too small"):
        PCAMonitor().fit_calibrate(synthetic, calibration_fraction=0.01)


# -------------------------------------------------------------- metrics --


def test_false_alarm_and_detection_rate():
    alarms = np.zeros(960, dtype=bool)
    alarms[:16] = True  # 10% of the 160 pre-fault samples
    alarms[160:] = True
    assert false_alarm_rate(alarms) == pytest.approx(0.10)
    assert detection_rate(alarms) == pytest.approx(1.0)


def test_sustained_alarm_ignores_isolated_spikes():
    alarms = np.zeros(960, dtype=bool)
    alarms[165] = True  # lone spike, must not count
    alarms[200:203] = True  # three in a row, must count
    assert first_sustained_alarm(alarms, consecutive=3) == 200
    assert detection_delay_minutes(alarms) == pytest.approx((200 - 160) * 3)


def test_undetected_fault_returns_none():
    assert detection_delay_minutes(np.zeros(960, dtype=bool)) is None
    m = evaluate_detection(np.zeros(960, dtype=bool), fault=3)
    assert m.detected is False and m.detection_delay_minutes is None


def test_topk_hit_rate():
    contrib = np.array([[9.0, 1.0, 8.0, 7.0], [1.0, 2.0, 3.0, 9.0]])
    assert topk_hit_rate(contrib, (0,), k=3) == pytest.approx(0.5)
    assert topk_hit_rate(contrib, (3,), k=3) == pytest.approx(1.0)
    assert np.isnan(topk_hit_rate(contrib, (), k=3))


def test_fault_labels_switch_at_onset():
    y = fault_labels(960, fault=1)
    assert not y[FAULT_ONSET_INDEX - 1] and y[FAULT_ONSET_INDEX]
    assert not fault_labels(960, fault=0).any()


def test_fault_catalogue_is_complete():
    assert set(FAULTS) == set(range(22))
    assert len(VARIABLE_TAGS) == N_VARIABLES


# ---------------------------------------------------------------- data --


@needs_data
def test_d00_transpose_quirk_is_handled():
    """The bug that silently ruins every TEP project."""
    X = load_training(DATA)
    assert X.shape == (500, N_VARIABLES)


@needs_data
def test_test_runs_have_expected_shape():
    for fault in (0, 1, 21):
        assert load_test(fault, DATA).shape == (960, N_VARIABLES)


@needs_data
def test_unknown_fault_raises():
    with pytest.raises(KeyError):
        load_test(99, DATA)


@needs_data
def test_calibration_beats_analytic_limits_out_of_sample():
    """The central empirical claim of the README, asserted."""
    X = load_training(DATA)
    normal = load_test(0, DATA)
    analytic = PCAMonitor().fit(X).score(normal).alarm.mean()
    empirical = PCAMonitor().fit_calibrate(X).score(normal).alarm.mean()
    assert empirical < analytic / 2


@needs_data
@pytest.mark.parametrize("fault", [1, 4, 6, 14])
def test_easy_faults_are_detected_fast(fault):
    mon = PCAMonitor().fit_calibrate(load_training(DATA))
    result = mon.score(load_test(fault, DATA))
    metrics = evaluate_detection(result.alarm, fault)
    assert metrics.detection_rate > 0.90
    assert metrics.detection_delay_minutes is not None
    assert metrics.detection_delay_minutes <= 60


@needs_data
@pytest.mark.parametrize("fault", UNDETECTABLE_FAULTS)
def test_known_undetectable_faults_stay_undetected(fault):
    """3, 9 and 15 have no published reliable detection. If this test starts
    passing with a high rate, suspect a leak rather than a breakthrough."""
    mon = PCAMonitor().fit_calibrate(load_training(DATA))
    result = mon.score(load_test(fault, DATA))
    assert detection_rate(result.alarm) < 0.30
