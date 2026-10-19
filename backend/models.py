from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from .collector import normalize_url


class Product(BaseModel):
    name: str = Field(default="", max_length=300)
    url: str = ""
    category: str = Field(default="显卡", max_length=50)
    brand: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=200)
    spec: str = Field(default="", max_length=300)
    shop: str = Field(default="", max_length=100)
    shop_type: Literal["官方旗舰", "授权店", "普通店"] = "普通店"
    group: str = Field(default="观察中", max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)
    expected_options: list[str] = Field(default_factory=list, max_length=30)
    msrp: int | None = Field(default=None, ge=1, le=10000000000)
    release_date: date | None = None
    notes: str = Field(default="", max_length=5000)
    interval_hours: float = Field(default=4, ge=0.1, le=8760)
    paused: bool = False
    watch: bool = True
    baseline: bool = True

    @field_validator("url")
    @classmethod
    def url_valid(cls, value):
        return normalize_url(value.strip()) if value.strip() else ""

    @model_validator(mode="after")
    def reference_without_link(self):
        if not self.url:
            if not self.name.strip():
                raise ValueError("未填写链接时，必须填写商品名称")
            self.paused = True
        return self


def product_key(product):
    if product.get("url"):
        return ("url", product["url"])
    return (
        "reference",
        *(
            str(product.get(k, "")).strip().casefold()
            for k in ("name", "brand", "model", "spec")
        ),
    )


class Rule(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    scope: Literal["all", "product", "group", "tag", "category"] = "all"
    scope_value: str = Field(default="", max_length=200)
    condition: Literal["target", "drop", "low", "restock", "event"] = "target"
    value: float = Field(default=1000, ge=0, le=100000000)
    days: int = Field(default=7, ge=1, le=3650)
    cooldown_minutes: int = Field(default=60, ge=0, le=525600)
    confirmations: int = Field(default=2, ge=1, le=10)
    include_coupon: bool = False
    channels: list[
        Literal["inapp", "bark", "serverchan", "wecom", "feishu", "email"]
    ] = Field(default_factory=lambda: ["inapp"], min_length=1)
    template: bool = False

    @model_validator(mode="after")
    def scoped(self):
        if self.scope != "all" and not self.scope_value:
            raise ValueError("请选择监控对象")
        return self


class Event(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    type: Literal["大促", "新品发布", "官宣降价", "供应变化", "政策窗口"] = "大促"
    start: date
    end: date
    confidence: Literal["官宣", "传闻", "待确认"] = "官宣"
    scope: Literal["all", "product", "category", "brand"] = "all"
    scope_value: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=5000)
    known_at: datetime | None = None
    ignored: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("结束日期不能早于开始日期")
        return self


class Note(BaseModel):
    product_id: str
    type: Literal["note", "point", "range"] = "note"
    text: str = Field(min_length=1, max_length=5000)
    start: datetime | None = None
    end: datetime | None = None
    on_chart: bool = True

    @model_validator(mode="after")
    def ordered(self):
        if self.type != "note" and not self.start:
            raise ValueError("标注需要开始时间")
        if self.type == "range" and (not self.end or self.end < self.start):
            raise ValueError("区间标注需要有效结束时间")
        return self


class View(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    config: dict = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def view_config(cls, config):
        cards = config.get("dashboard_cards")
        known = {"watch", "distribution", "drops", "alerts", "health", "events"}
        if cards is not None and (
            not isinstance(cards, list)
            or any(v not in known for v in cards)
            or len(cards) != len(set(cards))
        ):
            raise ValueError("看板卡片配置无效")
        sizes = config.get("card_sizes", {})
        if not isinstance(sizes, dict) or any(
            k not in known or not isinstance(v, int) or not 1 <= v <= 3
            for k, v in sizes.items()
        ):
            raise ValueError("卡片宽度应为 1–3 栏")
        filters = config.get("filters", {})
        if not isinstance(filters, dict) or (
            "tags" in filters and not isinstance(filters["tags"], list)
        ):
            raise ValueError("筛选视图格式错误")
        comparison = config.get("comparison", {})
        if not isinstance(comparison, dict) or (
            "ids" in comparison
            and (not isinstance(comparison["ids"], list) or len(comparison["ids"]) > 10)
        ):
            raise ValueError("比较视图最多包含 10 件商品")
        if "axis" in comparison and comparison["axis"] not in (
            "absolute",
            "percent",
            "msrp",
        ):
            raise ValueError("比较坐标口径无效")
        return config


MODELS = {"product": Product, "rule": Rule, "event": Event, "note": Note, "view": View}
