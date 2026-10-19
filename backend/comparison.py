"""Calendar-day comparison prices, with the same human precedence as training."""

from collections import defaultdict
from . import domain as d, prediction_data as pd
from .price_resolution import priority


def daily(product, raw=False):
    grouped = defaultdict(dict)
    for row in d.records(product["id"]):
        identity = pd.identity(row)
        amount = row.get("raw_price") if raw else row.get("chart_price")
        if (
            not identity
            or not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or amount <= 0
        ):
            continue
        day = pd.day(row["observed_at"]).isoformat()
        order = (
            0 if raw else priority(row),
            (
                pd.timestamp(
                    row.get("price_modified_at")
                    or row.get("recorded_at")
                    or row["observed_at"]
                )
                if not raw
                else pd.timestamp(row["observed_at"])
            ),
            pd.timestamp(row["observed_at"]),
            row["id"],
        )
        previous = grouped[identity].get(day)
        if previous is None or order > previous[0]:
            grouped[identity][day] = (order, row)
    series = []
    for identity, days in sorted(grouped.items()):
        values = []
        for day, (_, row) in sorted(days.items()):
            values.append(
                {
                    **row,
                    "day": day,
                    "source_observed_at": row["observed_at"],
                    "observed_at": day + "T00:00:00+08:00",
                }
            )
        sample = values[-1]
        basis = pd.price_basis(sample)
        label = pd.BASIS_NAMES.get(basis, basis)
        sku = pd.effective_sku(sample)
        suffix = "参考历史" if basis == "reference" else f"SKU {sku} · {label}"
        series.append(
            {
                "series_key": identity,
                "name": f"{product['name']} · {suffix}",
                "records": values,
                "basis": basis,
                "sku_id": sku,
            }
        )
    return {"timezone": "Asia/Shanghai", "unit": "day", "series": series}
