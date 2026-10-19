import csv
import io
import json

import pytest

from backend import csv_io, domain as d, prediction_data as pd, store as s
from tests.test_system import client, product


def encode(headers, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(
        {k: json.dumps(v) if isinstance(v, bool) else v for k, v in row.items()}
        for row in rows
    )
    return "\ufeff" + output.getvalue()


def test_stable_empty_exports_and_templates(client):
    for kind in ("product", "observations", "event", "rule", "view"):
        response = client.get(f"/api/export/{kind}?format=csv")
        assert response.status_code == 200
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert list(csv.reader(io.StringIO(response.text.lstrip("\ufeff")))) == [
            csv_io.columns(kind)
        ]
        template = client.get(f"/api/import-template/{kind}")
        assert list(csv.reader(io.StringIO(template.text.lstrip("\ufeff")))) == [
            csv_io.columns(kind, True)
        ]
    assert client.get("/api/export/product?format=xlsx").status_code == 422
    assert client.get("/api/import-template/not-real").status_code == 400
    assert client.get("/api/import-guide").status_code == 200


def test_product_csv_roundtrip_quotes_arrays_nullable_and_defaults(client):
    p = product(
        client,
        name='=示例, "显卡"',
        url="https://item.taobao.com/item.htm?id=123&skuId=456",
        tags=["显卡", "有,逗号"],
        expected_options=["16GB", "黑色"],
        notes="第一行\n第二行",
        watch=False,
        msrp=199900,
    )
    exported = client.get("/api/export/product?format=csv").text
    before = s.payload(p)
    client.post(
        "/api/products/bulk", json={"ids": [p["id"]], "action": "delete"}
    ).raise_for_status()
    result = client.post(
        "/api/import/product", json={"text": exported, "preview": False}
    ).json()
    assert result["new"] == 1, result
    current = s.entities("product")[0]
    assert s.payload(current) == {**before, "paused": True}
    assert current["id"] != p["id"]
    # Optional empty cells use model defaults rather than failing date/int parsing.
    text = encode(
        csv_io.columns("product", True),
        [{"url": "https://item.taobao.com/item.htm?id=999&skuId=888"}],
    )
    result = client.post("/api/import/product", json={"text": text}).json()
    assert result["new"] == 1, result
    assert result["rows"][0]["data"]["interval_hours"] == 4
    assert result["rows"][0]["data"]["release_date"] is None
    assert result["rows"][0]["data"]["paused"] is True


@pytest.mark.parametrize(
    "text",
    [
        "名称,链接\n显卡,test",
        "url,url\na,b",
        'url,name\n"unclosed',
        "url,name\na,b,c",
        "url,name\na",
        "url,,name\na,b,c",
    ],
)
def test_csv_file_errors_are_clear_and_do_not_write(client, text):
    response = client.post("/api/import/product", json={"text": text, "preview": False})
    assert response.status_code == 422, response.text
    assert "文件格式错误" in response.json()["detail"]
    assert s.entities("product") == []


def test_csv_real_prices_preserve_money_identity_basis_and_duplicate_scope(client):
    p = product(client, url="https://item.taobao.com/item.htm?id=123&skuId=456")
    headers = [
        "product_id",
        "observed_at",
        "price",
        "price_unit",
        "original_price",
        "price_basis",
    ]
    row = dict(
        product_id=p["id"],
        observed_at="2026-01-01T12:00:00+08:00",
        price="19.99",
        price_unit="yuan",
        original_price="29.99",
        price_basis="subsidy",
    )
    rows = [
        row,
        {**row, "price_basis": "regular"},
        {
            **row,
            "price": "1999",
            "original_price": "2999",
            "price_unit": "cent",
            "observed_at": "2026-01-01T04:00:00Z",
        },
    ]
    result = client.post(
        "/api/import/observations",
        json={"text": encode(headers, rows), "preview": False},
    ).json()
    assert (result["new"], result["skipped"]) == (2, 1), result
    records = d.records(p["id"])
    assert {pd.identity(r) for r in records} == {"123:456:subsidy", "123:456:regular"}
    assert all(
        r["price"] == 1999
        and r["original_price"] == 2999
        and r["manual"]
        and r["valid"]
        for r in records
    )
    exported = client.get("/api/export/observations?format=csv").text
    result = client.post(
        "/api/import/observations", json={"text": exported, "preview": False}
    ).json()
    assert result["new"] == 0 and result["skipped"] == 2, result


@pytest.mark.parametrize(
    "changes",
    [
        {"price": "nan"},
        {"price": "inf"},
        {"price": "-1"},
        {"price": "1.234"},
        {"price": "1.5", "price_unit": "cent"},
        {"price_unit": "dollar"},
        {"item_id": "999"},
        {"requested_sku_id": "999"},
        {"requested_sku_id": "4.56e2"},
        {"observed_at": "2026-01-01"},
        {"observed_at": "2026-01-01T12:00:00"},
        {"stock": "maybe"},
        {"price_basis": "unknown"},
        {"sku_verified": "yes"},
    ],
)
def test_invalid_price_csv_rows_skip_without_crashing_or_pollution(client, changes):
    p = product(client, url="https://item.taobao.com/item.htm?id=123&skuId=456")
    row = {
        "product_id": p["id"],
        "observed_at": "2026-01-01T12:00:00+08:00",
        "price": "19.99",
        "price_unit": "yuan",
        **changes,
    }
    result = client.post(
        "/api/import/observations",
        json={"text": encode(list(row), [row]), "preview": False},
    )
    assert result.status_code == 200, result.text
    assert result.json()["skipped"] == 1 and result.json()["rows"][0]["error"]
    assert not d.records(p["id"])


def test_exported_invalid_rows_do_not_become_truth_and_corrections_are_preserved(
    client,
):
    p = product(client, url="https://item.taobao.com/item.htm?id=123&skuId=456")
    row = {
        "product_id": p["id"],
        "observed_at": "2026-01-01T12:00:00+08:00",
        "price": 1999,
        "price_unit": "cent",
        "status": "crawler_error",
        "valid": False,
    }
    rows = [
        row,
        {
            **row,
            "observed_at": "2026-01-02T12:00:00+08:00",
            "valid": True,
            "corrected": True,
            "exclude_prediction": True,
            "price_basis": "coupon",
        },
    ]
    result = client.post(
        "/api/import/observations",
        json={"text": encode(csv_io.columns("observations"), rows), "preview": False},
    ).json()
    assert result["new"] == 1 and result["skipped"] == 1, result
    saved = d.records(p["id"])[0]
    assert saved["manual"] and saved["valid"] and saved["exclude_prediction"] is True
    assert pd.identity(saved) == "123:456:coupon"
