"""Read completed jobs in completion order, independently of queue order."""

import json

from diagnostics import describe

from . import store as s

PAGE_SIZE = 100


def cursor(stamp, job_id):
    return s.dumps([stamp, job_id])


def feed(after=None, limit=PAGE_SIZE):
    with s.connection() as db:
        if after is None:
            latest = db.execute(
                "SELECT finished_at,id FROM jobs WHERE status IN ('success','failed') "
                "AND finished_at IS NOT NULL ORDER BY finished_at DESC,id DESC LIMIT 1"
            ).fetchone()
            return {
                "items": [],
                "cursor": cursor(*latest) if latest else cursor("", ""),
                "has_more": False,
            }
        try:
            values = json.loads(after)
        except (ValueError, TypeError):
            raise ValueError("采集结果游标格式错误") from None
        if (
            not isinstance(values, list)
            or len(values) != 2
            or any(not isinstance(v, str) for v in values)
            or len(after) > 256
        ):
            raise ValueError("采集结果游标格式错误")
        stamp, job_id = values
        rows = db.execute(
            "SELECT j.*,json_extract(e.data,'$.name') AS product_name,"
            "e.id AS entity_id,e.deleted_at AS product_deleted_at "
            "FROM jobs j LEFT JOIN entities e ON e.id=j.product_id "
            "WHERE j.status IN ('success','failed') AND j.finished_at IS NOT NULL "
            "AND (j.finished_at,j.id)>(?,?) "
            "ORDER BY j.finished_at,j.id LIMIT ?",
            (stamp, job_id, limit + 1),
        ).fetchall()
    items = []
    for row in rows[:limit]:
        cookie_check = json.loads(row["context"] or "{}").get("purpose") == "cookie_verification"
        result = json.loads(row["result"] or "{}")
        code = (
            result.get("status")
            or row["error"]
            or ("success" if row["status"] == "success" else "crawler_error")
        )
        success = row["status"] == "success" and result.get("price") is not None
        # Stored job amounts are cents; diagnostics display page amounts in yuan.
        details = describe({**result, "status": code, "price": None})
        if success and "detected_price" not in details:
            details["detected_price"] = f"{result['price'] / 100:.2f}"
        items.append(
            {
                "id": row["id"],
                "product_id": row["product_id"],
                "product_name": row["product_name"] or ("Cookie 校验" if cookie_check else "未命名商品"),
                "product_available": bool(
                    row["entity_id"] and not row["product_deleted_at"]
                ),
                "status": "success" if success else "failed",
                "code": code,
                "price": result.get("price") if success else None,
                "finished_at": row["finished_at"],
                "attempts": row["attempts"],
                "diagnostics": details,
            }
        )
    return {
        "items": items,
        "cursor": cursor(items[-1]["finished_at"], items[-1]["id"]) if items else after,
        "has_more": len(rows) > limit,
    }
