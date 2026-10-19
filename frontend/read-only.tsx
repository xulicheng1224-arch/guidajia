import React, { useEffect, useState } from "react";
import {
  Link,
  Route,
  Routes,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { Alert, Input, Select, Space, Table, Tabs, Tag } from "antd";
import {
  Blank,
  Metric,
  PageTitle,
  Panel,
  Row,
  date,
  money,
  opts,
  pagination,
  useMonitor,
  useResource,
  api,
  categories,
} from "./lib";
import { PriceChart } from "./components";
import { ForecastModelPanel, ForecastNumbers } from "./forecast";

const bases = [
  { value: "regular", label: "普通价" },
  { value: "subsidy", label: "补贴价" },
  { value: "coupon", label: "券后价" },
  { value: "member", label: "会员价" },
  { value: "reference", label: "表格参考价" },
];

const productColumns = [
  {
    title: "商品 / 规格",
    width: 230,
    render: (_: any, p: Row) => (
      <>
        <Link to={`/products/${p.id}`}>{p.name}</Link>
        <small className="cell-sub">{p.spec || "—"}</small>
      </>
    ),
  },
  { title: "品类", dataIndex: "category", width: 100 },
  {
    title: "当前有效价",
    dataIndex: "current_price",
    width: 150,
    render: money,
    sorter: (a: Row, b: Row) =>
      (a.current_price ?? Infinity) - (b.current_price ?? Infinity),
  },
  { title: "监测最低价", dataIndex: "lowest", width: 150, render: money },
  { title: "趋势参考", dataIndex: "label", width: 150 },
  {
    title: "最近价格记录",
    dataIndex: "last_success",
    width: 160,
    render: (v: string) => date(v, "YYYY-MM-DD HH:mm"),
  },
];

function ViewerProducts({ overview = false }: { overview?: boolean }) {
  const { data } = useMonitor();
  const [params] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") || "");
  const [category, setCategory] = useState<string>();
  useEffect(() => setQuery(params.get("q") || ""), [params]);
  const products: Row[] = data.products;
  const shown = products.filter(
    (p) =>
      (!category || p.category === category) &&
      `${p.name} ${p.model} ${p.spec} ${p.tags?.join(" ")}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  return (
    <>
      <PageTitle
        title={overview ? "总览看板" : "商品库"}
        description="查看硬件价格、历史记录和预测结果。"
      />
      {overview && (
        <div className="overview-stats">
          <Metric label="监控商品" value={products.length} accent />
          <Metric
            label="有效价格"
            value={products.filter((p) => p.current_price != null).length}
          />
          <Metric
            label="接近阶段低点"
            value={products.filter((p) => p.label === "接近阶段低点").length}
          />
          <Metric
            label="采集状态"
            value={data.health.paused ? "已暂停" : "运行中"}
          />
        </div>
      )}
      <Panel title="商品价格">
        <Space wrap className="mb16">
          <Input
            aria-label="筛选商品"
            placeholder="搜索名称、型号、规格"
            allowClear
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ width: 230 }}
          />
          <Select
            aria-label="筛选品类"
            placeholder="全部品类"
            allowClear
            value={category}
            onChange={setCategory}
            options={opts(categories)}
            style={{ width: 130 }}
          />
        </Space>
        <Table
          rowKey="id"
          dataSource={shown}
          columns={productColumns}
          scroll={{ x: 1000 }}
          pagination={pagination}
        />
      </Panel>
    </>
  );
}

function ViewerDays({
  id,
  reference = false,
}: {
  id: string;
  reference?: boolean;
}) {
  const [basis, setBasis] = useState(reference ? "reference" : "regular");
  const { value, busy } = useResource<Row>(
    `/product/${id}/daily-prices?basis=${basis}`,
    {},
  );
  return (
    <>
      <Space wrap className="mb16">
        <Select
          aria-label="日价格口径"
          value={basis}
          onChange={setBasis}
          options={bases}
          style={{ width: 130 }}
        />
        <span className="muted small">
          北京时间 ·{" "}
          {basis === "reference"
            ? "独立参考价格"
            : `SKU ${value.sku_id || "尚未确认"}`}
        </span>
      </Space>
      <Table
        rowKey="day"
        loading={busy}
        dataSource={value.basis === basis ? value.rows || [] : []}
        pagination={pagination}
        scroll={{ x: 650 }}
        columns={[
          { title: "日期", dataIndex: "day", width: 120 },
          { title: "当日价格", dataIndex: "price", render: money, width: 140 },
          {
            title: "原始收盘价",
            dataIndex: "raw_price",
            render: money,
            width: 140,
          },
          {
            title: "来源",
            width: 110,
            render: (_: any, r: Row) => (
              <Tag color={r.override_id ? "gold" : "default"}>
                {r.override_id
                  ? "人工价格"
                  : r.effective_manual
                    ? "人工价优先"
                    : r.data_quality === "carried_forward"
                      ? "沿用前值"
                      : r.data_quality === "measured"
                        ? "实测导入"
                        : "价格观测"}
              </Tag>
            ),
          },
          { title: "备注", dataIndex: "reason" },
        ]}
      />
    </>
  );
}

function ViewerProduct() {
  const { id } = useParams();
  const { value, busy } = useResource<Row>(`/product/${id}`, {});
  const [raw, setRaw] = useState(false);
  const p = value.product;
  if (!p)
    return <Blank title={busy ? "正在读取商品…" : "商品不存在或已移除"} />;
  const records: Row[] = value.records || [];
  return (
    <>
      <Link to="/products">← 返回商品库</Link>
      <PageTitle
        title={p.name}
        description={[p.model, p.spec, p.shop].filter(Boolean).join(" · ")}
      />
      <div className="overview-stats">
        <Metric
          label="当前有效价"
          value={money(p.current_price)}
          hint={
            p.price_basis === "reference"
              ? p.current_price_quality === "carried_forward"
                ? "人工导入历史 · 参与模型训练"
                : "表格实测参考价"
              : p.current_price_manual
                ? "包含人工核实价格"
                : "商品页面有效价格"
          }
          accent
        />
        <Metric label="监测最低价" value={money(p.lowest)} />
        <Metric label="趋势参考" value={p.label} />
        <Metric
          label="最后成功采集"
          value={date(p.last_crawl_success, "MM-DD HH:mm")}
        />
      </div>
      <Panel
        title="价格轨迹"
        extra={
          <Select
            aria-label="图表价格类型"
            value={raw ? "raw" : "effective"}
            onChange={(v) => setRaw(v === "raw")}
            options={[
              { value: "effective", label: "有效价格（含人工价格）" },
              { value: "raw", label: "原始采集价格" },
            ]}
            style={{ width: 200 }}
          />
        }
      >
        <PriceChart series={[{ name: p.name, records }]} raw={raw} />
      </Panel>
      <Panel className="mt20">
        <Tabs
          items={[
            {
              key: "daily",
              label: "每日价格",
              children: (
                <ViewerDays
                  id={id!}
                  reference={Boolean(p.has_reference_history)}
                />
              ),
            },
            {
              key: "records",
              label: "价格记录",
              children: (
                <Table
                  rowKey="id"
                  dataSource={[...records].reverse()}
                  pagination={pagination}
                  scroll={{ x: 800 }}
                  columns={[
                    {
                      title: "时间",
                      dataIndex: "observed_at",
                      width: 175,
                      render: (v: string) => date(v, "YYYY-MM-DD HH:mm:ss"),
                    },
                    {
                      title: "有效价格",
                      dataIndex: "chart_price",
                      width: 140,
                      render: money,
                    },
                    {
                      title: "原始价格",
                      dataIndex: "raw_price",
                      width: 140,
                      render: money,
                    },
                    {
                      title: "来源",
                      width: 120,
                      render: (_: any, r: Row) =>
                        r.effective_manual
                          ? "人工价优先"
                          : r.data_quality === "carried_forward" && !r.corrected
                            ? "沿用前值"
                            : r.import_source
                              ? "实测导入"
                              : r.corrected
                                ? "人工修正"
                                : r.manual
                                  ? "人工补录"
                                  : "爬虫采集",
                    },
                    {
                      title: "状态",
                      width: 90,
                      render: (_: any, r: Row) =>
                        r.valid ? "有效" : "无效 / 失败",
                    },
                    { title: "备注", dataIndex: "reason" },
                  ]}
                />
              ),
            },
            {
              key: "forecast",
              label: "预测参考",
              children: (
                <>
                  <ForecastNumbers forecast={p.forecast} />
                  <p>{p.forecast.factors?.join("；")}</p>
                </>
              ),
            },
            {
              key: "notes",
              label: "备注与标注",
              children: (
                <Table
                  rowKey="id"
                  dataSource={value.notes || []}
                  columns={[
                    {
                      title: "时间",
                      dataIndex: "created_at",
                      render: (v: string) => date(v),
                    },
                    { title: "内容", dataIndex: "text" },
                  ]}
                  pagination={pagination}
                />
              ),
            },
          ]}
        />
      </Panel>
    </>
  );
}

function ViewerForecasts() {
  const { data } = useMonitor();
  return (
    <>
      <PageTitle
        title="预测中心"
        description="查看数据积累、模型验证和价格预测。"
      />
      <ForecastModelPanel />
      <Panel title="预测结果">
        <Table
          rowKey="id"
          dataSource={data.products}
          pagination={pagination}
          scroll={{ x: 900 }}
          columns={[
            {
              title: "商品",
              width: 240,
              render: (_: any, p: Row) => (
                <Link to={`/products/${p.id}`}>{p.name}</Link>
              ),
            },
            { title: "趋势参考", dataIndex: "label", width: 140 },
            {
              title: "模型结果",
              render: (_: any, p: Row) => (
                <ForecastNumbers forecast={p.forecast} />
              ),
            },
          ]}
        />
      </Panel>
    </>
  );
}

function ViewerCompare() {
  const { data } = useMonitor();
  const [params] = useSearchParams();
  const [ids, setIds] = useState<string[]>(
    (params.get("ids") || "").split(",").filter(Boolean).slice(0, 10),
  );
  const [series, setSeries] = useState<Row[]>([]);
  useEffect(() => {
    let active = true;
    Promise.all(ids.map((id) => api<Row>(`/product/${id}`)))
      .then((values) => {
        if (active)
          setSeries(
            values.map((v) => ({ name: v.product.name, records: v.records })),
          );
      })
      .catch(() => {
        if (active) setSeries([]);
      });
    return () => {
      active = false;
    };
  }, [ids, data]);
  return (
    <>
      <PageTitle title="比较页" description="选择商品对比价格历史。" />
      <Panel>
        <Select
          aria-label="选择比较商品"
          mode="multiple"
          value={ids}
          onChange={(v) => setIds(v.slice(0, 10))}
          options={data.products.map((p: Row) => ({
            label: `${p.name} ${p.spec}`,
            value: p.id,
          }))}
          style={{ width: "100%", marginBottom: 20 }}
        />
        <PriceChart series={series} />
      </Panel>
    </>
  );
}

function ViewerEvents() {
  const { data } = useMonitor();
  return (
    <>
      <PageTitle title="事件日历" />
      <Panel>
        <Table
          rowKey="id"
          dataSource={data.events}
          pagination={pagination}
          scroll={{ x: 650 }}
          columns={[
            { title: "事件", dataIndex: "name", width: 230 },
            { title: "开始", dataIndex: "start", width: 130 },
            { title: "结束", dataIndex: "end", width: 130 },
            { title: "可信度", dataIndex: "confidence", width: 110 },
          ]}
        />
      </Panel>
    </>
  );
}

function ViewerLog({ title, path }: { title: string; path: string }) {
  const { value, busy } = useResource<Row[]>(path, []);
  return (
    <>
      <PageTitle title={title} />
      <Panel>
        <Table
          rowKey="id"
          loading={busy}
          dataSource={value}
          pagination={pagination}
          scroll={{ x: 800 }}
          columns={[
            {
              title: "时间",
              width: 175,
              render: (_: any, r: Row) =>
                date(r.created_at || r.observed_at, "YYYY-MM-DD HH:mm:ss"),
            },
            {
              title: "类型",
              width: 120,
              render: (_: any, r: Row) => r.kind || r.level || r.channel || "—",
            },
            {
              title: "状态 / 操作",
              width: 150,
              render: (_: any, r: Row) => r.status || r.action || "—",
            },
            {
              title: "内容",
              render: (_: any, r: Row) =>
                r.message || r.reason || r.error || "—",
            },
          ]}
        />
      </Panel>
    </>
  );
}

export function ReadOnlyWorkspace() {
  return (
    <>
      <Alert
        className="mb16"
        type="info"
        showIcon
        message="只读访问"
        description="可以查看价格、历史记录和预测；当前令牌没有修改、采集或管理权限。"
      />
      <Routes>
        <Route path="/" element={<ViewerProducts overview />} />
        <Route path="/products" element={<ViewerProducts />} />
        <Route path="/products/:id" element={<ViewerProduct />} />
        <Route path="/compare" element={<ViewerCompare />} />
        <Route path="/calendar" element={<ViewerEvents />} />
        <Route path="/forecasts" element={<ViewerForecasts />} />
        <Route
          path="/alerts"
          element={<ViewerLog title="提醒中心" path="/deliveries" />}
        />
        <Route
          path="/collection"
          element={<ViewerLog title="采集日志" path="/logs" />}
        />
        <Route
          path="/audit"
          element={<ViewerLog title="修改历史" path="/audit" />}
        />
        <Route
          path="*"
          element={
            <Blank
              title="此页面需要管理员权限"
              text="可继续查看商品、历史价格和预测结果。"
              action={<Link to="/products">查看商品库</Link>}
            />
          }
        />
      </Routes>
    </>
  );
}
