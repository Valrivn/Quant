"""
RPO Imputation Validation — Phase 4 (Validation)  (big-pickle-worker)

Validates the Phase-2 RPO imputation model against the Phase-1 local cache:

    input : data/factors/rpo.parquet
            (date, cik, ticker, deferred_rev, contract_liab, rpo_actual,
             rpo_imputed, rpo_is_imputed, imputation_model_version, filed_date)
    report: data/validation/rpo_imputation_report.json
    plots : data/validation/rpo_imputation_residuals.png (if matplotlib)

Pre-registered thresholds (plan draft 2026-09-09):
    train 2018-2021, test 2022-2024 (in-sample model, out-of-sample scoring)
    OLS:  rpo_actual ~ deferred_rev + contract_liab   (on UN-imputed rows)
    PASS  OOS R-squared  > 0.80
    PASS  Shapiro-Wilk p > 0.05            (test residuals ~ normal)
    PASS  Durbin-Watson  in (1.5, 2.5)     (~= 2 -> low autocorrelation)
    PASS  |beta_train - beta_test| / |beta_train| < 0.15 per coefficient

If any check fails the report carries:
    RPO_IMPUTATION_FAILED = True
(consumed by the orchestration layer / gates log.)

CLI:
    python -m validation.rpo_imputation_test --run
    python -m validation.rpo_imputation_test --test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_RPO = Path("data/factors/rpo.parquet")
DEFAULT_REPORT = Path("data/validation/rpo_imputation_report.json")
TRAIN_START, TRAIN_END = "2018-01-01", "2021-12-31"
TEST_START, TEST_END = "2022-01-01", "2024-12-31"

FEATURES = ["deferred_rev", "contract_liab"]
TARGET = "rpo_actual"


def _ols_metrics(X: pd.DataFrame, y: pd.Series) -> Dict[str, Any]:
    """statsmodels OLS fit — returns coefs, residuals, metrics dict."""
    import statsmodels.api as sm

    Xc = sm.add_constant(X.astype(float))
    model = sm.OLS(y.astype(float), Xc).fit()
    resid = model.resid
    r2 = float(model.rsquared)
    n = len(resid)
    # Durbin-Watson
    if n > 2:
        dw = float(np.sum(np.diff(resid.values) ** 2) / np.sum(resid ** 2))
    else:
        dw = float("nan")
    return {
        "params": {k: float(v) for k, v in model.params.items()},
        "se": {k: float(v) for k, v in model.bse.items()},
        "pvalues": {k: float(v) for k, v in model.pvalues.items()},
        "rsquared": r2,
        "residuals": resid,
        "n": n,
        "durbin_watson": dw,
        "aic": float(model.aic),
        "bic": float(model.bic),
    }


def run_validation(rpo_path: Path, report_path: Path,
                   plot: bool = True) -> Dict[str, Any]:
    df = pd.read_parquet(rpo_path)
    df["date"] = pd.to_datetime(df["date"])
    # real labels only (rows where rpo_actual is disclosed)
    lab = df.dropna(subset=[TARGET] + FEATURES).copy()
    train = lab[(lab["date"] >= TRAIN_START) & (lab["date"] <= TRAIN_END)]
    test = lab[(lab["date"] >= TEST_START) & (lab["date"] <= TEST_END)]

    if len(train) < 20 or len(test) < 10:
        return {
            "status": "INSUFFICIENT_DATA",
            "reason": f"train={len(train)} test={len(test)} rows (need >=20/>=10)",
            "RPO_IMPUTATION_FAILED": True,
            "train_rows": len(train), "test_rows": len(test),
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }

    tr_metrics = _ols_metrics(train[FEATURES].reset_index(drop=True),
                              train[TARGET].reset_index(drop=True))
    te_metrics = _ols_metrics(test[FEATURES].reset_index(drop=True),
                              test[TARGET].reset_index(drop=True))

    # OOS scoring: train model applied to test features
    import statsmodels.api as sm
    Xtr = sm.add_constant(train[FEATURES].astype(float).reset_index(drop=True))
    m_tr = sm.OLS(train[TARGET].reset_index(drop=True).astype(float), Xtr).fit()
    Xte = sm.add_constant(test[FEATURES].astype(float).reset_index(drop=True))
    yte = test[TARGET].reset_index(drop=True).astype(float)
    yhat = m_tr.predict(Xte)
    ss_res = float(((yte - yhat) ** 2).sum())
    ss_tot = float(((yte - yte.mean()) ** 2).sum())
    oos_r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    test_resid = (yte - yhat).values
    from scipy import stats as _stats
    if len(test_resid) >= 3:
        sw_stat, sw_p = _stats.shapiro(test_resid)
    else:
        sw_stat, sw_p = float("nan"), float("nan")

    # beta stability across train/test (shared names)
    b_tr, b_te = tr_metrics["params"], te_metrics["params"]
    stability: Dict[str, Any] = {}
    for k in FEATURES:
        b1, b2 = b_tr.get(k, 0.0), b_te.get(k, 0.0)
        if b1 != 0:
            stability[k] = abs(b2 - b1) / abs(b1)
        else:
            stability[k] = float("nan")

    thresholds = {
        "oos_r2": 0.80, "shapiro_p": 0.05,
        "durbin_watson": [1.5, 2.5], "beta_stability": 0.15,
    }
    checks = {
        "oos_r2_greater_0_80": {"value": oos_r2, "threshold": 0.80, "direction": ">",
                                "passed": bool(oos_r2 > 0.80)},
        "shapiro_p_greater_0_05": {"value": float(sw_p), "threshold": 0.05, "direction": ">",
                                   "passed": bool(sw_p > 0.05)},
        "durbin_watson_in_1_5_2_5": {"value": te_metrics.get("durbin_watson"),
                                     "threshold": [1.5, 2.5], "direction": "in",
                                     "passed": bool(1.5 < te_metrics.get("durbin_watson", 0) < 2.5)},
        **{f"beta_stability_{k}": {"value": stability[k], "threshold": 0.15,
                                   "direction": "<",
                                   "passed": bool(np.isfinite(stability[k]) and stability[k] < 0.15)}
           for k in FEATURES},
    }
    failed = [name for name, c in checks.items() if not c["passed"]]

    report = {
        "status": "FAIL" if failed else "PASS",
        "RPO_IMPUTATION_FAILED": bool(failed),
        "thresholds": thresholds,
        "round": {"train": [TRAIN_START, TRAIN_END], "test": [TEST_START, TEST_END]},
        "train_rows": int(len(train)), "test_rows": int(len(test)),
        "train": tr_metrics | {"residuals": None},
        "test": te_metrics | {"residuals": None},
        "oos_r2_on_train_model": oos_r2,
        "shapiro": {"statistic": float(sw_stat), "p_value": float(sw_p)},
        "beta_stability": stability,
        "checks": checks,
        "failed_checks": failed,
        "columns": list(df.columns),
        "date_range_full": {"min": str(df["date"].min().date()),
                            "max": str(df["date"].max().date())},
        "imputed_share": float(df.get("rpo_is_imputed", pd.Series(dtype=float)).mean()),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }

    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True,
                                      default=str), encoding="utf-8")

    if plot and len(test_resid) >= 3:
        try:
            _make_plot(test, yhat, test_resid, tr_metrics, report_path.parent)
            report["plot_written"] = str(report_path.parent / "rpo_imputation_residuals.png")
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True,
                                              default=str), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            report["plot_written"] = f"failed: {exc}"

    print(f"[rpo] status={report['status']} oos_r2={oos_r2:.3f} "
          f"shapiro_p={sw_p:.3f} dw={te_metrics.get('durbin_watson'):.3f} "
          f"beta_stab={ {k: round(v, 3) for k, v in stability.items()} }")
    if failed:
        print(f"[rpo] FAILED checks: {failed}")
    print(f"[rpo] report: {report_path}")
    return report


def _make_plot(test: pd.DataFrame, yhat: pd.Series, resid: np.ndarray,
               tr_metrics: Dict[str, Any], out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    yte = test["rpo_actual"].reset_index(drop=True).astype(float)
    axes[0].scatter(yte, yhat, s=12, alpha=0.6)
    lims = [min(yte.min(), yhat.min()), max(yte.max(), yhat.max())]
    axes[0].plot(lims, lims, "r--", lw=1)
    axes[0].set_xlabel("actual rpo (test)")
    axes[0].set_ylabel("predicted rpo (train model)")
    axes[0].set_title("OOS fit (2018-21 model on 2022-24)")
    axes[1].hist(resid, bins=20, alpha=0.7)
    axes[1].axvline(0, color="r", ls="--", lw=1)
    axes[1].set_title("test residuals")
    fig.suptitle("RPO imputation validation")
    fig.tight_layout()
    png = out_dir / "rpo_imputation_residuals.png"
    fig.savefig(png, dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Self-test (synthetic)
# ----------------------------------------------------------------------------

def _synthetic_rpo(seed: int = 11, broken: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    tickers = [f"T{i:02d}" for i in range(20)]
    dates = pd.DatetimeIndex(
        pd.date_range("2018-01-01", "2024-12-31", freq="YE")
        .union(pd.date_range("2018-01-01", "2024-12-31", freq="QE-DEC"))
        .unique().sort_values())
    b1, b2 = 0.9, 1.1
    for d in dates:
        for t in tickers:
            dr = rng.uniform(5e6, 5e8)
            cl = rng.uniform(1e6, 2e8)
            if broken and d >= pd.Timestamp(TEST_START):
                noise = rng.normal(0, 3e8)   # test coefficients flip sign
                rpo = -0.9 * dr - 1.1 * cl + noise
            else:
                rpo = b1 * dr + b2 * cl + rng.normal(0, 2e6)
            rows.append({"date": d, "ticker": t, "deferred_rev": dr,
                         "contract_liab": cl, "rpo_actual": rpo,
                         "rpo_imputed": rpo, "rpo_is_imputed": False})
    return pd.DataFrame(rows)


def run_tests(tmp: Path) -> int:
    import shutil
    print("RPO imputation validation self-test")
    print("=" * 60)
    tmp.mkdir(parents=True, exist_ok=True)
    good = _synthetic_rpo(seed=11, broken=False)
    good_path = tmp / "rpo.parquet"
    good.to_parquet(good_path, index=False)

    report_good = run_validation(good_path, tmp / "good_report.json", plot=True)
    assert report_good["status"] == "PASS", report_good
    assert report_good["oos_r2_on_train_model"] > 0.80
    assert report_good["RPO_IMPUTATION_FAILED"] is False
    print("[OK] well-specified model PASSES all pre-registered checks")
    assert (tmp / "rpo_imputation_residuals.png").exists()
    print("[OK] residual plot written")

    bad = _synthetic_rpo(seed=13, broken=True)
    bad_path = tmp / "rpo_broken.parquet"
    bad.to_parquet(bad_path, index=False)
    report_bad = run_validation(bad_path, tmp / "bad_report.json", plot=False)
    assert report_bad["status"] == "FAIL"
    assert report_bad["RPO_IMPUTATION_FAILED"] is True
    assert any("beta_stability" in c for c in report_bad["failed_checks"])
    print("[OK] broken relation -> FAIL + RPO_IMPUTATION_FAILED=True")

    # JSON report re-read
    reread = json.loads((tmp / "bad_report.json").read_text(encoding="utf-8"))
    assert reread["RPO_IMPUTATION_FAILED"] is True
    print("[OK] report JSON round-trips")
    print("\nAll RPO imputation test self-tests passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="RPO imputation model validation")
    parser.add_argument("--rpo", type=str, default=str(DEFAULT_RPO))
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if args.test:
        tmp = Path("data/validation/_tests/rpo_imputation")
        raise SystemExit(run_tests(tmp))
    if not args.run:
        parser.print_help()
        return 1
    run_validation(Path(args.rpo), Path(args.report), plot=not args.no_plot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())