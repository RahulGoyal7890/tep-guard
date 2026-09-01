"""PCA-based multivariate process monitoring.

Implements the standard two-statistic scheme:

    T^2   variation *inside* the retained principal subspace, i.e. the
          process moving abnormally far along directions it normally moves in
    SPE   variation *orthogonal* to it, i.e. the correlation structure between
          variables breaking down (also written Q)

A fault can show up in either. Sensor drift on one instrument usually breaks
correlation and lands in SPE; a genuine operating-point excursion usually
lands in T^2.

Control limits are analytic rather than empirical:
    T^2   F-distribution (Jackson, 1991)
    SPE   Jackson & Mudholkar (1979) chi-square-power approximation

Dependencies are deliberately numpy + scipy only. This class is what gets
baked into the Lambda container, and pulling scikit-learn in for a
decomposition we can do in six lines roughly triples the image size.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_EPS = 1e-12


@dataclass
class MonitorResult:
    """Per-sample monitoring output."""

    t2: np.ndarray  # (n,)
    spe: np.ndarray  # (n,)
    t2_alarm: np.ndarray  # (n,) bool
    spe_alarm: np.ndarray  # (n,) bool

    @property
    def alarm(self) -> np.ndarray:
        """A sample is flagged if either statistic exceeds its limit."""
        return self.t2_alarm | self.spe_alarm


@dataclass
class PCAMonitor:
    """Fit on fault-free data, then score new samples.

    Parameters
    ----------
    variance_threshold
        Cumulative explained-variance fraction used to pick the number of
        retained components. 0.90 is the usual choice for TEP.
    alpha
        False alarm rate the control limits are designed for. 0.01 means the
        limits should produce roughly 1% alarms on in-control data.
    n_components
        Override the variance threshold with a fixed component count.
    """

    variance_threshold: float = 0.80
    alpha: float = 0.005
    n_components: int | None = None

    # Fitted state
    mean_: np.ndarray | None = field(default=None, repr=False)
    std_: np.ndarray | None = field(default=None, repr=False)
    eigenvalues_: np.ndarray | None = field(default=None, repr=False)
    loadings_: np.ndarray | None = field(default=None, repr=False)  # (m, k)
    k_: int | None = None
    n_train_: int | None = None
    t2_limit_: float | None = None
    spe_limit_: float | None = None
    calibrated_: bool = False
    n_calibration_: int | None = None

    # ---------------------------------------------------------------- fit --

    def fit(self, X: np.ndarray) -> "PCAMonitor":
        X = np.asarray(X, dtype=np.float64)
        if X.ndim != 2:
            raise ValueError(f"Expected 2-D training array, got shape {X.shape}")
        n, m = X.shape
        if n <= 2:
            raise ValueError("Need more than two training samples")

        self.mean_ = X.mean(axis=0)
        std = X.std(axis=0, ddof=1)
        # Constant columns would divide by zero. Leave them at unit scale;
        # they contribute nothing to either statistic.
        self.std_ = np.where(std < _EPS, 1.0, std)
        Z = (X - self.mean_) / self.std_

        # Eigendecomposition of the correlation matrix. eigh returns ascending
        # order, so reverse it.
        cov = np.cov(Z, rowvar=False)
        eigvals, eigvecs = np.linalg.eigh(cov)
        order = np.argsort(eigvals)[::-1]
        eigvals = np.clip(eigvals[order], 0.0, None)
        eigvecs = eigvecs[:, order]

        if self.n_components is not None:
            k = int(self.n_components)
        else:
            ratio = np.cumsum(eigvals) / max(eigvals.sum(), _EPS)
            k = int(np.searchsorted(ratio, self.variance_threshold) + 1)
        k = max(1, min(k, m - 1))  # need at least one residual dimension

        self.k_ = k
        self.n_train_ = n
        self.eigenvalues_ = eigvals
        self.loadings_ = eigvecs[:, :k]
        self.t2_limit_ = self._t2_limit(n, k, self.alpha)
        self.spe_limit_ = self._spe_limit(eigvals[k:], self.alpha)
        return self

    def calibrate(self, X_cal: np.ndarray) -> "PCAMonitor":
        """Replace the analytic limits with percentiles of held-out normal data.

        The analytic limits assume the residual eigenvalues are known. They
        are not: they are estimated from the same 500 samples the projection
        was fitted on, and the small-eigenvalue tail of a sample covariance is
        biased low. Measured on TEP, mean SPE is 4.85 on the fit split and
        9.01 on a held-out normal run, so the analytic limit is roughly half
        of where it should sit and false alarms hit 17.7% against a 1% design.

        Calibrating on data the projection never saw fixes it. See
        reports/limits_calibration.md for the numbers.
        """
        self._check_fitted()
        t2, spe = self.statistics(X_cal)
        q = 100.0 * (1.0 - self.alpha)
        self.t2_limit_ = float(np.percentile(t2, q))
        self.spe_limit_ = float(np.percentile(spe, q))
        self.calibrated_ = True
        self.n_calibration_ = int(np.atleast_2d(X_cal).shape[0])
        return self

    def fit_calibrate(
        self, X: np.ndarray, calibration_fraction: float = 0.30
    ) -> "PCAMonitor":
        """Fit the projection and calibrate limits on disjoint splits of X.

        The split is chronological, not random. These are time series with
        lag-1 autocorrelation around 0.5, so a random split would leak
        neighbouring samples across it and re-introduce the same optimism.
        """
        X = np.asarray(X, dtype=np.float64)
        n_cal = int(round(X.shape[0] * calibration_fraction))
        if n_cal < 30:
            raise ValueError(
                f"Calibration split of {n_cal} samples is too small to "
                "estimate a percentile. Use more training data."
            )
        n_fit = X.shape[0] - n_cal
        return self.fit(X[:n_fit]).calibrate(X[n_fit:])

    # --------------------------------------------------------------- score --

    def _standardize(self, X: np.ndarray) -> np.ndarray:
        self._check_fitted()
        X = np.atleast_2d(np.asarray(X, dtype=np.float64))
        if X.shape[1] != self.mean_.shape[0]:
            raise ValueError(
                f"Expected {self.mean_.shape[0]} variables, got {X.shape[1]}"
            )
        return (X - self.mean_) / self.std_

    def statistics(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (T^2, SPE) for each row of X."""
        Z = self._standardize(X)
        P = self.loadings_
        scores = Z @ P
        lam = np.clip(self.eigenvalues_[: self.k_], _EPS, None)
        t2 = np.sum(scores**2 / lam, axis=1)
        residual = Z - scores @ P.T
        spe = np.sum(residual**2, axis=1)
        return t2, spe

    def score(self, X: np.ndarray) -> MonitorResult:
        t2, spe = self.statistics(X)
        return MonitorResult(
            t2=t2,
            spe=spe,
            t2_alarm=t2 > self.t2_limit_,
            spe_alarm=spe > self.spe_limit_,
        )

    # -------------------------------------------------------- contributions --

    def contributions(self, x: np.ndarray) -> np.ndarray:
        """Plain SPE contribution per variable: the squared residual.

        Simple, and the classic criticism is smearing -- a fault in one
        variable inflates the residual of everything correlated with it.
        """
        Z = self._standardize(x)
        P = self.loadings_
        residual = Z - (Z @ P) @ P.T
        return np.squeeze(residual**2)

    def reconstruction_contributions(self, x: np.ndarray) -> np.ndarray:
        """Reconstruction-based contribution (RBC), Alcala & Qin (2009).

        For each variable, the amount SPE would drop if that variable were
        reconstructed along its own direction. Designed to fix smearing.

        Whether it actually beats plain contributions on TEP is an empirical
        question, and scripts/run_benchmark.py answers it.
        """
        Z = self._standardize(x)
        P = self.loadings_
        m = P.shape[0]
        C = np.eye(m) - P @ P.T  # residual projector
        num = (Z @ C) ** 2  # (n, m) numerator per variable
        den = np.clip(np.diag(C), _EPS, None)  # xi' C xi
        return np.squeeze(num / den)

    def rank_contributions(
        self, x: np.ndarray, method: str = "plain", top: int = 3
    ) -> list[int]:
        """Column indices of the largest contributors, most guilty first."""
        if method == "plain":
            contrib = self.contributions(x)
        elif method == "rbc":
            contrib = self.reconstruction_contributions(x)
        else:
            raise ValueError(f"Unknown contribution method: {method}")
        contrib = np.atleast_2d(contrib)
        if contrib.shape[0] > 1:
            contrib = contrib.mean(axis=0)
        contrib = np.ravel(contrib)
        return list(np.argsort(contrib)[::-1][:top])

    # --------------------------------------------------------------- limits --

    @staticmethod
    def _t2_limit(n: int, k: int, alpha: float) -> float:
        """T^2 limit via the F distribution.

        scipy is imported lazily here and in _spe_limit. Both are fit-time
        only -- once limits are baked into monitor.json, inference needs
        nothing but numpy. That keeps scipy out of the Lambda image.
        """
        from scipy import stats

        if n <= k:
            raise ValueError("Need more training samples than components")
        f_crit = stats.f.ppf(1.0 - alpha, k, n - k)
        return float(k * (n - 1) * (n + 1) / (n * (n - k)) * f_crit)

    @staticmethod
    def _spe_limit(residual_eigenvalues: np.ndarray, alpha: float) -> float:
        """SPE limit via the Jackson & Mudholkar approximation."""
        from scipy import stats

        lam = np.clip(np.asarray(residual_eigenvalues, dtype=np.float64), 0.0, None)
        if lam.size == 0:
            return float("inf")
        theta1 = lam.sum()
        theta2 = np.sum(lam**2)
        theta3 = np.sum(lam**3)
        if theta1 < _EPS or theta2 < _EPS:
            return float("inf")
        h0 = 1.0 - (2.0 * theta1 * theta3) / (3.0 * theta2**2)
        # h0 near zero makes the power blow up; the literature falls back to
        # a plain chi-square match on the first two moments.
        if abs(h0) < 1e-3:
            dof = 2.0 * theta1**2 / theta2
            return float(theta2 / theta1 * stats.chi2.ppf(1.0 - alpha, dof))
        c_alpha = stats.norm.ppf(1.0 - alpha)
        term = (
            c_alpha * np.sqrt(2.0 * theta2 * h0**2) / theta1
            + 1.0
            + theta2 * h0 * (h0 - 1.0) / theta1**2
        )
        return float(theta1 * np.power(max(term, _EPS), 1.0 / h0))

    # ----------------------------------------------------------- persistence --

    def _check_fitted(self) -> None:
        if self.loadings_ is None:
            raise RuntimeError("PCAMonitor is not fitted. Call fit() first.")

    def to_dict(self) -> dict:
        self._check_fitted()
        return {
            "format_version": 1,
            "variance_threshold": self.variance_threshold,
            "alpha": self.alpha,
            "k": self.k_,
            "n_train": self.n_train_,
            "mean": self.mean_.tolist(),
            "std": self.std_.tolist(),
            "eigenvalues": self.eigenvalues_.tolist(),
            "loadings": self.loadings_.tolist(),
            "t2_limit": self.t2_limit_,
            "spe_limit": self.spe_limit_,
            "calibrated": self.calibrated_,
            "n_calibration": self.n_calibration_,
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()))
        return path

    @classmethod
    def from_dict(cls, payload: dict) -> "PCAMonitor":
        obj = cls(
            variance_threshold=payload["variance_threshold"],
            alpha=payload["alpha"],
        )
        obj.k_ = payload["k"]
        obj.n_train_ = payload["n_train"]
        obj.mean_ = np.asarray(payload["mean"], dtype=np.float64)
        obj.std_ = np.asarray(payload["std"], dtype=np.float64)
        obj.eigenvalues_ = np.asarray(payload["eigenvalues"], dtype=np.float64)
        obj.loadings_ = np.asarray(payload["loadings"], dtype=np.float64)
        obj.t2_limit_ = payload["t2_limit"]
        obj.spe_limit_ = payload["spe_limit"]
        obj.calibrated_ = payload.get("calibrated", False)
        obj.n_calibration_ = payload.get("n_calibration")
        return obj

    @classmethod
    def load(cls, path: str | Path) -> "PCAMonitor":
        return cls.from_dict(json.loads(Path(path).read_text()))
