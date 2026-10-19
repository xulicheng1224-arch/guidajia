"""One effective daily price policy shared by charts, statistics and training."""

import math

from . import prediction_data as pd

FIELDS = ("price", "original_price", "exclude_prediction")
MARKERS = ("human_override_id", "human_override_observation_id", "effective_manual")


def valid_price(row):
    value = row.get("price")
    return (
        row.get("valid", row.get("status") == "success")
        and row.get("stock") not in ("out_of_stock", "delisted")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def priority(row):
    if row.get("daily_override_id"):
        return 3
    if row.get("price_modified_at"):
        return 2
    return 1 if row.get("source") in ("manual", "import") else 0


def resolve(rows, as_of=None):
    """Prefer day edits, point edits, human entries, then crawler prices.

    Start from each point's own corrected value on every call. This lets an
    historical as_of exclude a manual observation that happened later that day,
    even when the caller supplies rows already resolved for today's UI.
    """
    clean = []
    winners = {}
    for original in rows:
        row = dict(original)
        base = row.pop("priority_base", None)
        if base:
            row.update(base)
        for field in MARKERS:
            row.pop(field, None)
        if as_of is not None and pd.timestamp(row["observed_at"]) > as_of:
            continue
        clean.append(row)
        series = pd.identity(row)
        rank = priority(row)
        if not series or not rank or not valid_price(row):
            continue
        key = (row["product_id"], series, pd.day(row["observed_at"]))
        order = (
            rank,
            pd.timestamp(
                row.get("price_modified_at")
                or row.get("recorded_at")
                or row["observed_at"]
            ),
            pd.timestamp(row["observed_at"]),
            row.get("id", ""),
        )
        if key not in winners or order > winners[key][0]:
            winners[key] = (order, row)
    for row in clean:
        series = pd.identity(row)
        if not series or not valid_price(row):
            continue
        selected = winners.get((row["product_id"], series, pd.day(row["observed_at"])))
        if not selected:
            continue
        winner = selected[1]
        if row is winner:
            continue
        row["priority_base"] = {key: row.get(key) for key in FIELDS}
        row.update({key: winner.get(key) for key in FIELDS})
        row["exclude_prediction"] = bool(
            row["priority_base"].get("exclude_prediction")
            or winner.get("exclude_prediction")
        )
        row.update(
            effective_manual=True,
            human_override_id=winner.get("daily_override_id")
            or winner.get("correction_id")
            or winner["id"],
            human_override_observation_id=winner["id"],
        )
    return clean
