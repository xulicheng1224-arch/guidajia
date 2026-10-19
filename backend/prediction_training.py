"""CPU LightGBM models with chronological calibration and purged backtests."""

import math
from collections import Counter
from datetime import timedelta

import numpy as np
import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from threadpoolctl import threadpool_limits

from . import prediction_data as data

REQUIREMENTS = {
    "samples": 240,
    "origin_days": 90,
    "positive": 30,
    "negative": 30,
    "series": 2,
}
PARAMETERS = {
    "num_leaves": 7,
    "max_depth": 3,
    "learning_rate": 0.04,
    "min_data_in_leaf": 15,
    "lambda_l2": 5,
    "verbosity": -1,
    "num_threads": 2,
    "deterministic": True,
    "force_col_wise": True,
    "seed": 42,
}
ROUNDS = 100


def encoder(samples):
    return {
        name: {
            value: i
            for i, value in enumerate(
                sorted({r["features"].get(name, "unknown") for r in samples})
            )
        }
        for name in data.CATEGORICAL
    }


def matrix(samples, vocab):
    return np.array(
        [
            [
                (
                    float(row["features"].get(name))
                    if row["features"].get(name) is not None
                    else np.nan
                )
                for name in data.NUMERIC
            ]
            + [
                vocab[name].get(row["features"].get(name, "unknown"), -1)
                for name in data.CATEGORICAL
            ]
            for row in samples
        ],
        dtype=float,
    )


def train_booster(samples, vocab, objective, alpha=None):
    y = [r["target"] if objective == "binary" else r["log_ratio"] for r in samples]
    dataset = lgb.Dataset(
        matrix(samples, vocab),
        label=y,
        feature_name=data.FEATURES,
        categorical_feature=data.CATEGORICAL,
    )
    parameters = {**PARAMETERS, "objective": objective}
    if alpha is not None:
        parameters["alpha"] = alpha
    return lgb.train(parameters, dataset, num_boost_round=ROUNDS)


def sigmoid(values):
    return 1 / (1 + np.exp(-np.clip(values, -35, 35)))


def enough_classes(samples, minimum=1):
    counts = Counter(r["target"] for r in samples)
    return counts[0] >= minimum and counts[1] >= minimum


def fit_bundle(training, calibration):
    vocab = encoder(training)
    with threadpool_limits(limits=2):
        classifier = train_booster(training, vocab, "binary")
        raw = classifier.predict(
            matrix(calibration, vocab), raw_score=True, num_threads=2
        )
        calibrator = LogisticRegression(C=1, max_iter=300, random_state=42).fit(
            np.asarray(raw).reshape(-1, 1), [r["target"] for r in calibration]
        )
        models = {"classifier": classifier}
        for alpha in (0.1, 0.5, 0.9):
            models[f"q{int(alpha * 100)}"] = train_booster(
                training, vocab, "quantile", alpha
            )
        quantiles = np.sort(
            np.array(
                [
                    models[k].predict(matrix(calibration, vocab), num_threads=2)
                    for k in ("q10", "q50", "q90")
                ]
            ).T,
            axis=1,
        )
        actual = np.array([r["log_ratio"] for r in calibration])
        scores = np.maximum.reduce(
            [quantiles[:, 0] - actual, actual - quantiles[:, 2], np.zeros(len(actual))]
        )
        level = min(1.0, math.ceil((len(scores) + 1) * 0.8) / len(scores))
        expansion = float(np.quantile(scores, level, method="higher"))
    return {
        "models": models,
        "encoder": vocab,
        "calibration": {
            "coefficient": float(calibrator.coef_[0][0]),
            "intercept": float(calibrator.intercept_[0]),
            "expansion": expansion,
        },
        "base_rate": (sum(r["target"] for r in training) + 1) / (len(training) + 2),
    }


def infer(bundle, samples):
    x = matrix(samples, bundle["encoder"])
    models = bundle["models"]
    cal = bundle["calibration"]
    with threadpool_limits(limits=2):
        raw = models["classifier"].predict(x, raw_score=True, num_threads=2)
        probabilities = sigmoid(cal["coefficient"] * raw + cal["intercept"])
        raw_quantiles = np.array(
            [models[k].predict(x, num_threads=2) for k in ("q10", "q50", "q90")]
        ).T
    quantiles = np.sort(raw_quantiles, axis=1)
    low = np.exp(np.clip(quantiles[:, 0] - cal["expansion"], -5, 5))
    median = np.exp(np.clip(quantiles[:, 1], -5, 5))
    high = np.exp(np.clip(quantiles[:, 2] + cal["expansion"], -5, 5))
    return probabilities, low, median, high


def split_before(samples, test_start, horizon):
    # Both the training labels and calibration labels must finish before the
    # subsequent block begins. This purges overlapping outcome windows.
    calibration_end = test_start - timedelta(days=horizon)
    calibration_start = calibration_end - timedelta(days=max(21, horizon))
    training = [
        r
        for r in samples
        if r["origin"] < calibration_start and r["label_end"] <= calibration_start
    ]
    calibration = [
        r
        for r in samples
        if calibration_start <= r["origin"] <= calibration_end
        and r["label_end"] <= test_start
    ]
    return training, calibration


def rolling_folds(samples, horizon):
    dates = sorted({r["origin"] for r in samples})
    if len(dates) < 10:
        return []
    starts = sorted({int(len(dates) * fraction) for fraction in (0.65, 0.78, 0.90)})
    folds = []
    for index, offset in enumerate(starts):
        start = dates[offset]
        finish = (
            dates[starts[index + 1]]
            if index + 1 < len(starts)
            else dates[-1] + timedelta(days=1)
        )
        training, calibration = split_before(samples, start, horizon)
        testing = [r for r in samples if start <= r["origin"] < finish]
        if (
            len(training) >= 60
            and len(calibration) >= 20
            and len(testing) >= 12
            and enough_classes(training, 5)
            and enough_classes(calibration, 3)
        ):
            folds.append((training, calibration, testing))
    return folds


def calibration_bins(actual, probability):
    rows = []
    for i in range(5):
        mask = (probability >= i / 5) & (
            probability < (i + 1) / 5 if i < 4 else probability <= 1
        )
        if np.any(mask):
            rows.append(
                {
                    "range": f"{i*20}–{(i+1)*20}%",
                    "samples": int(np.sum(mask)),
                    "predicted": round(float(np.mean(probability[mask])) * 100, 2),
                    "actual": round(float(np.mean(actual[mask])) * 100, 2),
                }
            )
    return rows


def evaluate(samples, horizon):
    predicted, folds_report = [], []
    for training, calibration, testing in rolling_folds(samples, horizon):
        bundle = fit_bundle(training, calibration)
        probabilities, low, median, high = infer(bundle, testing)
        for i, row in enumerate(testing):
            predicted.append(
                {
                    "origin": row["origin"],
                    "target": row["target"],
                    "ratio": math.exp(row["log_ratio"]),
                    "probability": float(probabilities[i]),
                    "low": float(low[i]),
                    "median": float(median[i]),
                    "high": float(high[i]),
                    "baseline": bundle["base_rate"],
                }
            )
        folds_report.append(
            {
                "train_samples": len(training),
                "calibration_samples": len(calibration),
                "test_samples": len(testing),
                "train_label_end": max(r["label_end"] for r in training).isoformat(),
                "calibration_start": min(r["origin"] for r in calibration).isoformat(),
                "calibration_label_end": max(
                    r["label_end"] for r in calibration
                ).isoformat(),
                "test_start": min(r["origin"] for r in testing).isoformat(),
                "test_end": max(r["origin"] for r in testing).isoformat(),
            }
        )
    if not predicted:
        return {
            "folds": [],
            "samples": 0,
            "probability_ready": False,
            "interval_ready": False,
            "reasons": ["尚无足够的独立时间块完成训练、校准与回测"],
        }
    y = np.array([r["target"] for r in predicted])
    p = np.array([r["probability"] for r in predicted])
    ratio = np.array([r["ratio"] for r in predicted])
    mid = np.array([r["median"] for r in predicted])
    low, high = np.array([r["low"] for r in predicted]), np.array(
        [r["high"] for r in predicted]
    )
    base = np.array([r["baseline"] for r in predicted])
    bins = calibration_bins(y, p)
    brier = float(brier_score_loss(y, p))
    base_brier = min(
        float(brier_score_loss(y, base)), float(brier_score_loss(y, np.zeros(len(y))))
    )
    ece = sum(
        abs(r["predicted"] - r["actual"]) / 100 * r["samples"] for r in bins
    ) / len(y)
    mae, base_mae = float(np.mean(np.abs(mid - ratio))), float(
        np.mean(np.abs(1 - ratio))
    )
    coverage = float(np.mean((low <= ratio) & (ratio <= high)))
    width = float(np.mean(high - low))
    adequate = (
        len(folds_report) >= 2
        and len(y) >= 60
        and len({r["origin"] for r in predicted}) >= max(28, 2 * horizon)
        and min(int(sum(y)), int(len(y) - sum(y))) >= 10
    )
    probability_ready = adequate and brier < base_brier * 0.98 and ece <= 0.15
    interval_ready = (
        adequate and mae < base_mae * 0.98 and coverage >= 0.75 and width <= 0.5
    )
    chosen = p >= 0.65
    reasons = []
    if not adequate:
        reasons.append("独立回测天数、样本量或正负样本仍不足")
    if not probability_ready:
        reasons.append("概率模型尚未同时通过基线提升与校准误差门槛")
    if not interval_ready:
        reasons.append("区间模型尚未同时通过误差、覆盖率与宽度门槛")
    return {
        "folds": folds_report,
        "samples": len(y),
        "test_days": len({r["origin"] for r in predicted}),
        "positive": int(sum(y)),
        "negative": int(len(y) - sum(y)),
        "brier": round(brier, 6),
        "baseline_brier": round(base_brier, 6),
        "log_loss": round(
            float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])), 6
        ),
        "auc": round(float(roc_auc_score(y, p)), 4) if len(set(y)) == 2 else None,
        "calibration_error": round(ece, 4),
        "mae_percent": round(mae * 100, 3),
        "baseline_mae_percent": round(base_mae * 100, 3),
        "interval_coverage": round(coverage * 100, 2),
        "interval_width_percent": round(width * 100, 2),
        "wait_samples": int(sum(chosen)),
        "wait_precision": (
            round(float(np.mean(y[chosen])) * 100, 2) if np.any(chosen) else None
        ),
        "wait_saving_percent": (
            round(float(np.mean(1 - ratio[chosen])) * 100, 2)
            if np.any(chosen)
            else None
        ),
        "calibration_bins": bins,
        "probability_ready": bool(probability_ready),
        "interval_ready": bool(interval_ready),
        "reasons": reasons,
    }
