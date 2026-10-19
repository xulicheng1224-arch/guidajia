"""Version 1 CSV interchange: fixed headers, explicit units and lossless decimals."""

import csv
import io
import json
from decimal import Decimal, InvalidOperation

from .models import MODELS

OBSERVATION_INPUT = [
    "product_id",
    "observed_at",
    "price",
    "price_unit",
    "original_price",
    "stock",
    "price_type",
    "price_basis",
    "item_id",
    "requested_sku_id",
    "selected_sku_id",
    "sku_verified",
    "exclude_prediction",
    "reason",
    "data_quality",
    "import_source",
    "source_cell",
    "source_note",
]
OBSERVATION_COLUMNS = [
    "id",
    *OBSERVATION_INPUT,
    "status",
    "valid",
    "source",
    "corrected",
    "raw_price",
    "raw_original_price",
    "reference_product_id",
]
METADATA = ["id", "kind", "version", "created_at", "updated_at", "deleted_at"]


def spreadsheet_risk(value):
    # Spreadsheet importers may ignore whitespace/control prefixes before '='.
    stripped = value
    while stripped and (stripped[0].isspace() or ord(stripped[0]) < 32 or stripped[0] in "\ufeff\u200b"):
        stripped = stripped[1:]
    return stripped.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))


def decode_text(value):
    if value.startswith("'") and (value[1:].startswith("'") or spreadsheet_risk(value[1:])):
        return value[1:]
    # Compatibility with exports produced before the apostrophe guard.
    return value[1:] if value.startswith((" =", " +", " -", " @")) else value


def columns(kind, template=False):
    if kind == "observations":
        return OBSERVATION_INPUT if template else OBSERVATION_COLUMNS
    if kind in MODELS:
        fields = list(MODELS[kind].model_fields)
        return fields if template else [*METADATA, *fields]
    return {
        "audit": [
            "id",
            "entity_id",
            "kind",
            "action",
            "before_data",
            "after_data",
            "reason",
            "created_at",
        ],
        "logs": [
            "id",
            "product_id",
            "level",
            "message",
            "duration_ms",
            "details",
            "created_at",
        ],
    }.get(kind, [])


def render(kind, rows, template=False):
    fields = columns(kind, template)
    # Diagnostic exports keep any additional fields, after their stable prefix.
    if kind in ("audit", "logs"):
        fields += sorted({k for row in rows for k in row} - set(fields))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        encoded = {}
        for key in fields:
            value = row.get(key)
            if isinstance(value, (list, dict, bool)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif isinstance(value, str) and (spreadsheet_risk(value) or value.startswith("'")):
                value = "'" + value
            encoded[key] = value
        writer.writerow(encoded)
    return "\ufeff" + output.getvalue()


def parse(kind, text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
    headers = reader.fieldnames
    if not headers or any(not h for h in headers) or len(set(headers)) != len(headers):
        raise ValueError("CSV 必须有非空且不重复的英文列名，请下载模板")
    allowed = set(columns(kind))
    # Old product exports included scheduler internals; accept and discard them.
    allowed.update(("last_scheduled", "next_run") if kind == "product" else ())
    unknown = set(headers) - allowed
    if unknown:
        raise ValueError("未知 CSV 列：" + "、".join(sorted(unknown)))
    required = {
        "product": set(),
        "observations": {"product_id", "observed_at", "price", "price_unit"},
    }.get(kind)
    if kind == "product" and not {"url", "name"}.intersection(headers):
        raise ValueError("商品 CSV 至少包含 url 或 name 列；无链接时必须填写名称")
    if required is None:
        required = {k for k, v in MODELS[kind].model_fields.items() if v.is_required()}
    if required - set(headers):
        raise ValueError(
            "CSV 缺少必填列：" + "、".join(sorted(required - set(headers)))
        )
    rows = []
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise ValueError(
                f"第 {reader.line_num} 行列数与表头不一致；含逗号的内容必须用双引号包裹"
            )
        if not any(v.strip() for v in row.values()):
            continue
        # Empty optional cells mean use model defaults, including nullable dates.
        value = {
            k: decode_text(v)
            for k, v in row.items()
            if v != "" and k not in ("last_scheduled", "next_run")
        }
        for key in required:
            if not value.get(key, "").strip():
                value[key] = ""  # Report as a row error instead of using a default.
        rows.append(value)
        if len(rows) > 1000:
            raise ValueError("单次最多导入 1000 条")
    return rows


def amount(value, unit, name="price"):
    if unit not in ("cent", "yuan"):
        raise ValueError("price_unit 必须为 cent（分）或 yuan（元）")
    try:
        price = Decimal(str(value)) * (100 if unit == "yuan" else 1)
        if (
            not price.is_finite()
            or price <= 0
            or price > 10_000_000_000
            or price != price.to_integral_value()
        ):
            raise ValueError(f"{name} 必须为正金额；分为整数，元最多两位小数")
        return int(price)
    except InvalidOperation:
        raise ValueError(f"{name} 不是有效金额") from None


def boolean(value, default=False):
    if value in (None, ""):
        return default
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("布尔列只能填写 true 或 false")
