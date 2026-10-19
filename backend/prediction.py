"""Local model lifecycle. Daily background training, atomic promotion, safe fallback."""

import asyncio
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from . import prediction_data as data, store as s, training_queue

MODEL_VERSION = "lightgbm-v1"
REQUIREMENTS = {
    "samples": 240,
    "origin_days": 90,
    "positive": 30,
    "negative": 30,
    "series": 2,
}
TRAINING = None
_SCHEDULE = {}
_LOAD_LOCK = threading.RLock()
_PACK_CACHE = {}


_TENANTS = {}

def runtime():
    if s.TENANT.get() == "admin":
        return sys.modules[__name__]
    from types import SimpleNamespace
    return _TENANTS.setdefault(str(s.data_dir()), SimpleNamespace(TRAINING=None, _SCHEDULE={}, _PACK_CACHE={}))


def now():
    return datetime.now(timezone.utc)


def config_key(config):
    values = {
        k: config[k] for k in ("forecast_days", "drop_threshold", "include_coupon")
    }
    return hashlib.sha256(
        s.dumps([MODEL_VERSION, data.VERSION, values]).encode()
    ).hexdigest()


def correction_revision():
    with s.connection() as db:
        row = db.execute(
            "SELECT count(*) n, max(created_at) stamp FROM audit WHERE kind IN ('correction','manual','daily_price')"
        ).fetchone()
    return f"{row['n']}:{row['stamp']}"


def read_pack(config=None, as_of=None, refresh=False):
    config, as_of = config or s.settings(), as_of or now()
    cache_key = (str(s.db_path()), s.REVISION, config_key(config), as_of.isoformat())
    with _LOAD_LOCK:
        if not refresh and runtime()._PACK_CACHE.get("key") == cache_key:
            return runtime()._PACK_CACHE["value"]
    from .domain import records

    products = {p["id"]: p for p in s.entities("product")}
    rows = [r for r in records() if r["product_id"] in products]
    series = data.daily_series(rows, config, as_of)
    value = {"series": series, "events": s.entities("event"), "rows": rows}
    with _LOAD_LOCK:
        runtime()._PACK_CACHE.update(key=cache_key, value=value)
    return value


def root():
    folder = s.data_dir() / "forecast-models"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def atomic_json(path, value):
    from crawler import save_json

    save_json(path, value)
    path.chmod(0o600)


def latest_run():
    with s.connection() as db:
        row = db.execute(
            "SELECT * FROM model_runs ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return (
        {
            **json.loads(row["report"]),
            "id": row["id"],
            "state": row["status"],
            "started_at": row["created_at"],
            "finished_at": row["finished_at"],
        }
        if row
        else None
    )


def active_report(config=None, at=None):
    config, at = config or s.settings(), at or now()
    try:
        metadata = json.loads((root() / "active.json").read_text())
        if not isinstance(metadata, dict):
            return None
        if (
            metadata["config_key"] != config_key(config)
            or metadata["data_version"] != data.VERSION
            or metadata["correction_revision"] != correction_revision()
        ):
            return None
        if (
            not data.timestamp(metadata["trained_at"])
            <= at
            < data.timestamp(metadata["expires_at"])
        ):
            return None
        if metadata.get("features") != data.FEATURES:
            return None
        if len(metadata["id"]) != 32 or any(
            c not in "0123456789abcdef" for c in metadata["id"]
        ):
            return None
        files = metadata.get("files", {})
        if not isinstance(files, dict) or set(files) != {
            "classifier.txt",
            "q10.txt",
            "q50.txt",
            "q90.txt",
        }:
            return None
        for name, checksum in files.items():
            path = root() / metadata["id"] / name
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != checksum
            ):
                return None
        return metadata
    except (OSError, ValueError, KeyError, TypeError):
        return None


def unavailable_reason(config=None, at=None):
    config, at = config or s.settings(), at or now()
    try:
        metadata = json.loads((root() / "active.json").read_text())
        if not isinstance(metadata, dict):
            return "模型元数据校验失败，暂用规则判断"
        if metadata.get("config_key") != config_key(config):
            return "预测窗口、降价阈值或价格口径已改变，等待重新训练"
        if data.timestamp(metadata["expires_at"]) <= at:
            return "模型已超过 30 天有效期，等待重新训练"
        if metadata.get("correction_revision") != correction_revision():
            return "历史价格已被人工修正，等待重新训练"
        if data.timestamp(metadata["trained_at"]) > at:
            return "该历史时点尚无可用模型"
        return "模型文件或版本校验未通过，暂用规则判断"
    except (OSError, ValueError, KeyError, TypeError):
        return "等待足够的成熟样本与通过回测的模型"


def progress(config=None):
    config = config or s.settings()
    pack = read_pack(config)
    # Cache expensive matured-label assembly independently from log writes.
    key = hashlib.sha256(
        s.dumps(
            [
                (key, [(b["observed_at"], b["price"], b["minimum"]) for b in bars])
                for key, bars in pack["series"].items()
            ]
        ).encode()
    ).hexdigest()
    return _progress(
        str(s.db_path()),
        config_key(config),
        key,
        data.day(now()).isoformat(),
        s.dumps(
            {
                k: config[k]
                for k in ("forecast_days", "drop_threshold", "include_coupon")
            }
        ),
    )


@lru_cache(maxsize=2)
def _progress(database, configuration, digest, today, config_json):
    config = json.loads(config_json)
    pack = read_pack(config)
    samples = data.training_samples(pack["series"], pack["events"], config, now())
    return data.summary(pack["series"], samples)


def report():
    config = s.settings()
    active = active_report(config)
    # Capture liveness before the persisted run: if training finishes during
    # this request, keep polling once more instead of showing a stale running
    # row together with running=False and stopping the UI's poll loop.
    pending = bool(runtime().TRAINING and not runtime().TRAINING.done())
    queue = training_queue.status()
    running = queue["state"] == "running" or (queue["state"] == "idle" and pending)
    last = latest_run()
    reference_rows = [
        r for r in read_pack(config)["rows"] if r.get("reference_product_id")
    ]
    return {
        "engine": MODEL_VERSION,
        "enabled": config["ml_enabled"],
        "auto_train": config["ml_auto_train"],
        "shadow_mode": config["shadow_mode"],
        "requirements": REQUIREMENTS,
        "progress": progress(config),
        "reference_data": {
            "points": len(reference_rows),
            "measured": sum(
                r.get("data_quality") != "carried_forward" for r in reference_rows
            ),
            "carried_forward": sum(
                r.get("data_quality") == "carried_forward" for r in reference_rows
            ),
        },
        "active": (
            {
                k: active[k]
                for k in (
                    "id",
                    "trained_at",
                    "expires_at",
                    "metrics",
                    "probability_ready",
                    "interval_ready",
                    "data",
                )
                if k in active
            }
            if active
            else None
        ),
        "last_run": last,
        "running": running,
        "queued": queue["state"] == "queued",
        "training_queue": queue,
        "horizon": config["forecast_days"],
        "drop_threshold": config["drop_threshold"],
        "basis_names": data.BASIS_NAMES,
        "notice": None if active else unavailable_reason(config),
    }


def finish(run_id, state, value):
    with s.connection() as db:
        db.execute(
            "UPDATE model_runs SET status=?,finished_at=?,report=? WHERE id=?",
            (state, s.now(), s.dumps(value), run_id),
        )


def train():
    config, at = s.settings(), now()
    folder = root()
    with (folder / "training.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "running", "message": "已有训练任务运行中"}
        run_id = s.uid()
        base = {
            "config_key": config_key(config),
            "horizon": config["forecast_days"],
            "drop_threshold": config["drop_threshold"],
            "engine": MODEL_VERSION,
        }
        with s.connection() as db:
            db.execute(
                "INSERT INTO model_runs VALUES(?,?,?,?,?)",
                (run_id, at.isoformat(), None, "running", s.dumps(base)),
            )
        staging = folder / (".staging-" + run_id)
        try:
            source_revision = correction_revision()
            pack = read_pack(config, at, refresh=True)
            samples = data.training_samples(pack["series"], pack["events"], config, at)
            stats = data.summary(pack["series"], samples)
            value = {**base, "data": stats}
            missing = [
                f"{k}: {stats.get(k,0)} / {v}"
                for k, v in REQUIREMENTS.items()
                if stats.get(k, 0) < v
            ]
            if missing:
                value.update(
                    message="继续积累真实历史，尚未达到训练门槛", reasons=missing
                )
                finish(run_id, "insufficient", value)
                return {"state": "insufficient", **value}
            from .prediction_training import evaluate, fit_bundle, split_before

            metrics = evaluate(samples, config["forecast_days"])
            value["metrics"] = metrics
            if not metrics["probability_ready"] and not metrics["interval_ready"]:
                value.update(
                    message="候选模型未通过回测；沿用有效的已验证模型，无可用模型时使用规则",
                    reasons=metrics["reasons"],
                )
                finish(run_id, "rejected", value)
                return {"state": "rejected", **value}
            training, calibration = split_before(samples, at, config["forecast_days"])
            from .prediction_training import enough_classes

            if (
                len(training) < 60
                or len(calibration) < 20
                or not enough_classes(calibration, 3)
                or not enough_classes(training, 5)
            ):
                value.update(
                    message="最终校准窗口样本不足",
                    reasons=["等待最近的正负样本积累并走完预测窗口"],
                )
                finish(run_id, "insufficient", value)
                return {"state": "insufficient", **value}
            bundle = fit_bundle(training, calibration)
            if bundle["calibration"]["coefficient"] <= 0:
                metrics["probability_ready"] = False
            if not metrics["probability_ready"] and not metrics["interval_ready"]:
                value.update(
                    message="最终概率校准未通过",
                    reasons=["模型排序与校准结果不稳定，等待后续数据"],
                )
                finish(run_id, "rejected", value)
                return {"state": "rejected", **value}
            staging.mkdir(mode=0o700)
            files = {}
            for name, model in bundle["models"].items():
                path = staging / f"{name}.txt"
                model.save_model(str(path))
                path.chmod(0o600)
                files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            completed_at = now()
            metadata = {
                **value,
                "id": run_id,
                "trained_at": completed_at.isoformat(),
                "data_cutoff": at.isoformat(),
                "expires_at": (completed_at + timedelta(days=30)).isoformat(),
                "data_version": data.VERSION,
                "features": data.FEATURES,
                "encoder": bundle["encoder"],
                "calibration": bundle["calibration"],
                "files": files,
                "correction_revision": source_revision,
                "probability_ready": metrics["probability_ready"],
                "interval_ready": metrics["interval_ready"],
            }
            atomic_json(staging / "metadata.json", metadata)
            staging.rename(folder / run_id)
            # A settings change during training must not activate a stale target.
            if (
                config_key(s.settings()) != base["config_key"]
                or correction_revision() != source_revision
            ):
                value.update(message="训练期间预测设置或历史修正已改变，候选未启用")
                finish(run_id, "rejected", value)
                return {"state": "rejected", **value}
            atomic_json(folder / "active.json", metadata)
            value.update(message="模型已通过时间回测并保存", model_id=run_id)
            finish(run_id, "active", value)
            s.log(
                "预测模型完成训练、校准和回测",
                details={"model_id": run_id, "samples": stats["samples"]},
            )
            # Retain five complete local artifacts; never remove the active one.
            old = sorted(
                [
                    p
                    for p in folder.iterdir()
                    if p.is_dir()
                    and len(p.name) == 32
                    and all(c in "0123456789abcdef" for c in p.name)
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in old[5:]:
                if path.name != run_id:
                    shutil.rmtree(path)
            return {"state": "active", **value}
        except Exception as exc:
            value = {
                **base,
                "message": "训练未完成，采集与规则判断继续运行",
                "error_type": type(exc).__name__,
                "reasons": ["检查模型依赖与本机运行环境，或稍后重新训练"],
            }
            finish(run_id, "error", value)
            s.log(f"预测训练失败（{type(exc).__name__}）", level="warning")
            return {"state": "error", **value}
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def recover():
    with s.connection() as db:
        db.execute(
            "UPDATE model_runs SET status='interrupted',finished_at=? WHERE status='running'",
            (s.now(),),
        )


async def schedule(force=False):
    config = s.settings()
    if runtime().TRAINING and not runtime().TRAINING.done():
        return False
    if not force and (not config["ml_enabled"] or not config["ml_auto_train"]):
        return False
    key = (str(s.db_path()), now().date().isoformat(), config_key(config))
    last = latest_run()
    if not force and (
        runtime()._SCHEDULE.get("key") == key
        or (
            last
            and last["started_at"][:10] == key[1]
            and last.get("config_key") == key[2]
        )
    ):
        return False
    job_id, added = training_queue.enqueue(config_key(config))
    runtime()._SCHEDULE["key"] = key
    runtime().TRAINING = asyncio.create_task(training_queue.wait(job_id))
    training_queue.kick()
    return added


@lru_cache(maxsize=2)
def load_bundle(directory, model_id, trained_at):
    from . import prediction_training

    metadata = json.loads((Path(directory) / model_id / "metadata.json").read_text())
    models = {}
    for name in ("classifier", "q10", "q50", "q90"):
        path = Path(directory) / model_id / f"{name}.txt"
        if (
            hashlib.sha256(path.read_bytes()).hexdigest()
            != metadata["files"][path.name]
        ):
            raise ValueError("模型文件校验失败")
        models[name] = prediction_training.lgb.Booster(model_file=str(path))
    return {
        "models": models,
        "encoder": metadata["encoder"],
        "calibration": metadata["calibration"],
    }


def predict(product, rows, fallback, config=None, as_of=None):
    from .price_resolution import resolve

    config, at = config or s.settings(), as_of or now()
    rows = resolve(rows, at)
    result = {
        **fallback,
        "model": "rules-v2",
        "probability": None,
        "interval": None,
        "expected_min": None,
        "model_status": "collecting",
        "shadow_mode": config["shadow_mode"],
        "drop_threshold": config["drop_threshold"],
        "window_ends_at": data.end_of_day(
            data.day(at) + timedelta(days=config["forecast_days"])
        ).isoformat(),
    }
    usable = [
        r
        for r in rows
        if data.timestamp(r["observed_at"]) <= at and data.usable(r, config)
    ]
    if not usable:
        result["model_reason"] = "暂无符合 SKU 与价格口径要求的有效价格记录"
        return result
    latest = max(usable, key=lambda r: data.timestamp(r["observed_at"]))
    result.update(
        series_key=data.identity(latest),
        price_basis=data.price_basis(latest),
        price_basis_name=data.BASIS_NAMES[data.price_basis(latest)],
        current_price=latest["price"],
    )
    if not config["ml_enabled"]:
        result.update(model_status="disabled", model_reason="机器学习预测已关闭")
        return result
    metadata = active_report(config, at)
    if not metadata:
        result["model_reason"] = unavailable_reason(config, at)
        return result
    pack = read_pack(config, at)
    key = (product["id"], data.identity(latest))
    features = (
        data.feature_row(key, pack["series"], pack["events"], at)
        if key in pack["series"]
        else None
    )
    if not features:
        result["model_reason"] = (
            "当前规格至少需 7 天历史、近 7 天至少 4 天有效观测，且最近观测不超过 2 天"
        )
        return result
    try:
        from .prediction_training import infer

        bundle = load_bundle(str(root()), metadata["id"], metadata["trained_at"])
        if any(
            features.get(name, "unknown") not in bundle["encoder"][name]
            for name in ("category", "basis")
        ):
            result.update(
                model_status="out_of_scope",
                model_reason="当前品类或价格口径尚无可用训练样本，暂用规则判断",
            )
            return result
        probability, low, median, high = infer(bundle, [{"features": features}])
        result.update(
            model=MODEL_VERSION,
            model_id=metadata["id"],
            model_status="shadow" if config["shadow_mode"] else "active",
            model_reason="已通过按时间回测；仅预测采集可见的同口径价格",
            validation=metadata["metrics"],
            trained_at=metadata["trained_at"],
            quality="已回测",
        )
        if metadata["probability_ready"]:
            result["probability"] = round(float(probability[0]) * 100, 1)
        if metadata["interval_ready"]:
            result.update(
                expected_min=max(1, round(latest["price"] * float(median[0]))),
                interval=[
                    max(1, round(latest["price"] * float(low[0]))),
                    max(1, round(latest["price"] * float(high[0]))),
                ],
                interval_kind="observed_min_target80",
                interval_coverage=metadata["metrics"]["interval_coverage"],
            )
        suggested = fallback["label"]
        if result["probability"] is not None:
            if result["probability"] >= 65:
                suggested = "建议观望"
            elif result["probability"] <= 35:
                suggested = (
                    "接近阶段低点"
                    if features.get("percentile_30d", 1) <= 0.2
                    else "平稳"
                )
            result["factors"] = [
                f"未来 {config['forecast_days']} 天观测到降价至少 {config['drop_threshold']}% 的校准概率 {result['probability']}%",
                f"当前口径：{result['price_basis_name']}",
                *fallback["factors"][:2],
            ]
        result["suggested_label"] = suggested
        if not config["shadow_mode"]:
            result["label"] = suggested
        return result
    except Exception:
        result.update(
            model_status="unavailable",
            model_reason="模型文件无法读取或校验失败，暂用规则判断",
        )
        return result
