import React, { useEffect, useMemo, useRef, useState } from "react";
import { CollectAllButton } from "./collect-all";
import { CollectionFrequencyButton } from "./collection-frequency";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  App,
  Alert,
  Button,
  Checkbox,
  DatePicker,
  Descriptions,
  Dropdown,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import {
  ArrowDownRight,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Bell,
  CalendarDays,
  Check,
  ChevronDown,
  CircleHelp,
  Cpu,
  Download,
  ExternalLink,
  GripVertical,
  LayoutGrid,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Star,
  TrendingDown,
  X,
} from "lucide-react";
import dayjs from "dayjs";
import * as echarts from "echarts";
import {
  Blank,
  ImportDialog,
  Metric,
  PageTitle,
  Panel,
  Row,
  Status,
  TrendTag,
  ago,
  api,
  categories,
  colors,
  date,
  download,
  downloadJSON,
  labels,
  money,
  opts,
  pagination,
  patch,
  pct,
  post,
  put,
  remove,
  useMonitor,
  useResource,
} from "./lib";
import { PriceChart, ProductEditor } from "./components";
import { RuleEditor } from "./operations";
import { CollectionDiagnostics } from "./diagnostics";
import { ForecastNumbers } from "./forecast";
import { DailyPrices } from "./daily-prices";

const iconFor = (category: string) =>
  category === "显卡"
    ? "GPU"
    : category === "CPU"
      ? "CPU"
      : category === "SSD"
        ? "SSD"
        : category === "内存"
          ? "RAM"
          : "HW";
export function ProductName({ product }: { product: Row }) {
  return (
    <Link to={`/products/${product.id}`} className="product-cell">
      <span className={`product-glyph category-${product.category}`}>
        <Cpu size={21} />
        <small>{iconFor(product.category)}</small>
      </span>
      <span>
        <strong>{product.name}</strong>
        <small>
          {product.brand && `${product.brand} · `}
          {product.spec || "尚未填写规格"}
          {!product.url && " · 待补链接"}
        </small>
      </span>
    </Link>
  );
}

export function Dashboard() {
  const { data, action } = useMonitor();
  const navigate = useNavigate();
  const [editor, setEditor] = useState(false);
  const [editing, setEditing] = useState(false);
  const [dragged, setDragged] = useState<string | null>(null);
  const [cardOrder, setCardOrder] = useState<string[]>(
    data.settings.dashboard_cards || [],
  );
  const [sizes, setSizes] = useState<Row>(data.settings.card_sizes || {});
  const [configure, setConfigure] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<Row[]>([]);
  const [saveView, setSaveView] = useState(false);
  const [viewName, setViewName] = useState("");
  const products: Row[] = data.products,
    h = data.health;
  useEffect(() => {
    if (!editing) {
      setCardOrder(data.settings.dashboard_cards || []);
      setSizes(data.settings.card_sizes || {});
    }
  }, [data.settings.dashboard_cards, data.settings.card_sizes, editing]);
  useEffect(() => {
    api<Row[]>("/deliveries")
      .then(setDeliveries)
      .catch(() => {});
  }, [data]);
  const watch = products.filter((p) => p.watch).slice(0, 5);
  const drops = products
    .filter((p) => p.drop_7d != null && p.drop_7d < 0)
    .sort((a, b) => a.drop_7d - b.drop_7d)
    .slice(0, 5);
  const events: Row[] = data.events
    .filter((e: Row) => !e.ignored && e.end >= dayjs().format("YYYY-MM-DD"))
    .sort((a: Row, b: Row) => a.start.localeCompare(b.start))
    .slice(0, 4);
  const distribution = labels.map((label) => ({
    label,
    count: products.filter((p) => p.label === label).length,
  }));
  const today = deliveries.filter(
    (d) =>
      d.channel === "inapp" &&
      date(d.created_at, "YYYY-MM-DD") === dayjs().format("YYYY-MM-DD"),
  );
  const cardTitles: Row = {
    watch: "关注商品",
    distribution: "趋势标签分布",
    drops: "近 7 天降价榜",
    alerts: "今日价格提醒",
    health: "采集健康度",
    events: "即将到来的事件",
  };
  const extra = (id: string, link: string) =>
    editing ? (
      <Space>
        <Button
          type="text"
          size="small"
          icon={<Settings2 size={14} />}
          onClick={() => setConfigure(id)}
        />
        <Button
          type="text"
          size="small"
          icon={<X size={14} />}
          onClick={() => setCardOrder(cardOrder.filter((c) => c !== id))}
        />
      </Space>
    ) : (
      <Link className="subtle-link" to={link}>
        查看全部 <ArrowUpRight size={12} />
      </Link>
    );
  const cardContent: Row = {
    watch: (
      <>
        {watch.length ? (
          watch.map((p) => (
            <div className="watch-row" key={p.id}>
              <ProductName product={p} />
              <div className="watch-price">
                <strong>{money(p.current_price)}</strong>
                <small className={p.drop_7d < 0 ? "down" : ""}>
                  {pct(p.drop_7d)} <span>/ 7 天</span>
                </small>
              </div>
              <TrendTag label={p.label} />
            </div>
          ))
        ) : (
          <Blank
            title="把想入手的硬件放在这里"
            text="添加商品并开启关注，随时掌握最新价格。"
            action={
              <Button onClick={() => setEditor(true)} icon={<Plus size={14} />}>
                添加关注商品
              </Button>
            }
          />
        )}
      </>
    ),
    distribution: (
      <div className="distribution-list">
        {distribution.map((d, i) => (
          <button
            key={d.label}
            onClick={() =>
              navigate(`/products?label=${encodeURIComponent(d.label)}`)
            }
          >
            <span className="legend-dot" style={{ background: colors[i] }} />
            <span>{d.label}</span>
            <div className="distribution-track">
              <div
                style={{
                  width: `${products.length ? (d.count / products.length) * 100 : 0}%`,
                  background: colors[i],
                }}
              />
            </div>
            <strong>{d.count}</strong>
          </button>
        ))}
      </div>
    ),
    drops: drops.length ? (
      <div className="rank-list">
        {drops.map((p, i) => (
          <Link to={`/products/${p.id}`} key={p.id}>
            <span className={`rank rank-${i}`}>
              {String(i + 1).padStart(2, "0")}
            </span>
            <div>
              <strong>{p.name}</strong>
              <small>{money(p.current_price)}</small>
            </div>
            <b className="down">{pct(p.drop_7d)}</b>
          </Link>
        ))}
      </div>
    ) : (
      <Blank
        title="降价，还需要一点耐心"
        text="监测到近 7 天的降价后，会自动更新榜单。"
      />
    ),
    alerts: today.length ? (
      <div className="event-list">
        {today.slice(0, 5).map((n) => (
          <Link to={`/products/${n.product_id}`} key={n.id}>
            <span className="event-dot" />
            <span>{n.message}</span>
            <small>{date(n.created_at, "HH:mm")}</small>
          </Link>
        ))}
      </div>
    ) : (
      <Blank
        title="今天还没有价格提醒"
        text="设置目标价，让合适的价格主动找到你。"
        action={
          <Button type="link" onClick={() => navigate("/alerts")}>
            创建提醒 <ArrowRight size={13} />
          </Button>
        }
      />
    ),
    health: (
      <div className="health-card">
        <div className="health-ring">
          <Progress
            type="circle"
            percent={h.rate || 0}
            size={95}
            strokeWidth={7}
            strokeColor="#1aa58a"
            trailColor="var(--line)"
            format={() => (h.rate == null ? "—" : `${h.rate}%`)}
          />
          <div>
            <strong>24 小时采集成功率</strong>
            <p>
              {h.total
                ? `${h.success} / ${h.total} 次采集成功`
                : "尚无采集记录"}
            </p>
          </div>
        </div>
        <div className="health-line">
          <span>最近成功</span>
          <b>{ago(h.last_success)}</b>
        </div>
        <div className="health-line">
          <span>连续失败商品</span>
          <b>{products.filter((p) => p.failures > 0).length} 个</b>
        </div>
        <div className="health-line">
          <span>熔断状态</span>
          <Tag color={h.circuit_until ? "warning" : "success"} bordered={false}>
            {h.circuit_until ? "冷却中" : "未触发"}
          </Tag>
        </div>
      </div>
    ),
    events: events.length ? (
      <div className="event-list">
        {events.map((e) => (
          <Link to="/calendar" key={e.id}>
            <span className="event-date">
              <b>{dayjs(e.start).format("DD")}</b>
              <small>{dayjs(e.start).format("MM 月")}</small>
            </span>
            <div>
              <strong>{e.name}</strong>
              <small>
                {e.type} · {e.confidence}
              </small>
            </div>
            <ArrowUpRight size={14} />
          </Link>
        ))}
      </div>
    ) : (
      <Blank
        title="为下一次升级做准备"
        text="添加大促、新品发布和供应变化事件。"
        action={
          <Button type="link" onClick={() => navigate("/calendar")}>
            添加日历事件 <Plus size={13} />
          </Button>
        }
      />
    ),
  };
  return (
    <>
      <PageTitle
        eyebrow="YOUR HARDWARE, IN FOCUS"
        title="总览看板"
        description="关注每一次价格变化，把升级留给合适的时机。"
        actions={
          <>
            <Select
              className="view-select"
              value={data.settings.default_view || "default"}
              options={[
                { label: "默认工作视图", value: "default" },
                ...data.views.map((v: Row) => ({ label: v.name, value: v.id })),
              ]}
              onChange={async (id) => {
                const v = data.views.find((v: Row) => v.id === id);
                await action(() =>
                  patch("/settings", {
                    default_view: id === "default" ? null : id,
                    ...(v?.config?.dashboard_cards
                      ? {
                          dashboard_cards: v.config.dashboard_cards,
                          card_sizes: v.config.card_sizes || {},
                        }
                      : {}),
                  }),
                ).catch(() => {});
              }}
            />
            <Button
              icon={<LayoutGrid size={14} />}
              onClick={() => setEditing(!editing)}
            >
              {editing ? "退出编辑" : "编辑布局"}
            </Button>
            <Button
              type="primary"
              icon={<Plus size={15} />}
              onClick={() => setEditor(true)}
            >
              添加商品
            </Button>
          </>
        }
      />
      <div className="overview-stats">
        <Metric
          label="正在监测"
          value={
            <>
              {products.filter((p) => !p.paused).length}
              <small>件商品</small>
            </>
          }
          hint={`${products.length} 件商品已收录 · ${products.filter((p) => p.paused).length} 件已暂停`}
          icon={<Cpu size={17} />}
          accent
        />
        <Metric
          label="近 7 天价格下降"
          value={
            <>
              {products.filter((p) => p.drop_7d < 0).length}
              <small>件商品</small>
            </>
          }
          hint={
            <span>
              <TrendingDown size={12} /> 价格变化基于有效观测
            </span>
          }
          icon={<TrendingDown size={17} />}
        />
        <Metric
          label="接近阶段低点"
          value={
            <>
              {products.filter((p) => p.label === "接近阶段低点").length}
              <small>件商品</small>
            </>
          }
          hint="历史价格与趋势规则综合参考"
          icon={<Sparkles size={17} />}
        />
        <Metric
          label="今日触发提醒"
          value={
            <>
              {today.length}
              <small>条提醒</small>
            </>
          }
          hint={
            <Link to="/alerts">
              查看提醒与发送记录 <ArrowUpRight size={11} />
            </Link>
          }
          icon={<Bell size={17} />}
        />
      </div>
      {!products.length && (
        <div className="welcome-strip">
          <div className="welcome-symbol">
            <Cpu size={24} />
          </div>
          <div>
            <strong>你的下一次硬件升级，从这里开始</strong>
            <p>登录淘宝，添加商品链接。我们会为你记录每一次价格变化。</p>
          </div>
          <Button onClick={() => navigate("/settings?tab=account")}>
            配置淘宝登录 <ArrowRight size={14} />
          </Button>
        </div>
      )}
      <div className="section-heading">
        <h2>
          我的工作台 <span>WORKSPACE</span>
        </h2>
        <Space>
          <span className="muted small">
            {dayjs().format("YYYY 年 M 月 D 日 dddd")}
          </span>
          {!editing && (
            <Button size="small" type="text" onClick={() => setSaveView(true)}>
              保存为新视图
            </Button>
          )}
        </Space>
      </div>
      {editing && (
        <Alert
          className="layout-alert"
          type="info"
          showIcon
          message="拖动卡片标题调整顺序；通过设置调整宽度，完成后保存。"
          action={
            <Space>
              <Button
                size="small"
                onClick={() => {
                  setEditing(false);
                  setCardOrder(data.settings.dashboard_cards);
                  setSizes(data.settings.card_sizes);
                }}
              >
                取消
              </Button>
              <Button
                size="small"
                type="primary"
                onClick={async () => {
                  await action(
                    () =>
                      patch("/settings", {
                        dashboard_cards: cardOrder,
                        card_sizes: sizes,
                      }),
                    "布局已保存",
                  );
                  setEditing(false);
                }}
              >
                完成
              </Button>
            </Space>
          }
        />
      )}
      <div className="dashboard-grid">
        {cardOrder.map((id) => (
          <div
            className={`dashboard-card card-${id} ${editing ? "editing" : ""}`}
            key={id}
            style={{
              gridColumn: `span ${sizes[id] || (id === "watch" ? 2 : 1)}`,
            }}
            onDragOver={(e) => {
              if (editing) e.preventDefault();
            }}
            onDrop={() => {
              if (!dragged || dragged === id) return;
              const a = cardOrder.filter((c) => c !== dragged);
              a.splice(a.indexOf(id), 0, dragged);
              setCardOrder(a);
              setDragged(null);
            }}
          >
            <Panel
              title={
                <span
                  draggable={editing}
                  onDragStart={() => setDragged(id)}
                  className={editing ? "drag-title" : ""}
                >
                  {editing && <GripVertical size={14} />}
                  <span className={`card-title-dot dot-${id}`} />
                  {cardTitles[id]}
                </span>
              }
              extra={extra(
                id,
                (
                  {
                    watch: "/products",
                    distribution: "/forecasts",
                    drops: "/products",
                    alerts: "/alerts",
                    health: "/collection",
                    events: "/calendar",
                  } as Row
                )[id],
              )}
            >
              {cardContent[id]}
            </Panel>
          </div>
        ))}
        {editing && (
          <Dropdown
            menu={{
              items: Object.keys(cardTitles)
                .filter((id) => !cardOrder.includes(id))
                .map((id) => ({ key: id, label: cardTitles[id] })),
              onClick: ({ key }) => setCardOrder([...cardOrder, key]),
            }}
          >
            <button className="add-card">
              <Plus size={23} />
              添加卡片
            </button>
          </Dropdown>
        )}
      </div>
      <div className="insight-footnote">
        <ShieldCheckIcon />
        <span>
          价格来自淘宝 /
          天猫商品页面，失败记录不覆盖历史有效价格。趋势仅作购买时机参考。
        </span>
      </div>
      <ProductEditor open={editor} onClose={() => setEditor(false)} />
      <Modal
        title="卡片显示设置"
        open={!!configure}
        onCancel={() => setConfigure(null)}
        onOk={() => setConfigure(null)}
      >
        <p>卡片宽度</p>
        <Segmented
          options={[
            { label: "1 栏", value: 1 },
            { label: "2 栏", value: 2 },
            { label: "3 栏", value: 3 },
          ]}
          value={sizes[configure || ""] || (configure === "watch" ? 2 : 1)}
          onChange={(value) => setSizes({ ...sizes, [configure!]: value })}
        />
      </Modal>
      <Modal
        title="保存当前工作台为视图"
        open={saveView}
        onCancel={() => setSaveView(false)}
        onOk={async () => {
          if (!viewName.trim()) return;
          await action(
            () =>
              post("/entities/view", {
                name: viewName,
                config: { dashboard_cards: cardOrder, card_sizes: sizes },
              }),
            "视图已保存",
          );
          setSaveView(false);
          setViewName("");
        }}
      >
        <Input
          placeholder="视图名称"
          value={viewName}
          onChange={(e) => setViewName(e.target.value)}
        />
      </Modal>
    </>
  );
}
function ShieldCheckIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 20 20" fill="none">
      <path
        d="M10 2 16 5v5c0 4-6 7-6 7s-6-3-6-7V5l6-3Z"
        stroke="currentColor"
      />
      <path d="m7 9 2 2 4-4" stroke="currentColor" />
    </svg>
  );
}

export function Products() {
  const { data, action } = useMonitor();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") || "");
  const [group, setGroup] = useState(params.get("group") || "all");
  const [category, setCategory] = useState<string>();
  const [label, setLabel] = useState(params.get("label") || undefined);
  const [stock, setStock] = useState<string>();
  const [shop, setShop] = useState<string>();
  const [min, setMin] = useState<number | null>(null);
  const [max, setMax] = useState<number | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  const [selection, setSelection] = useState<React.Key[]>([]);
  const [edit, setEdit] = useState<Row | null>(null);
  const [editor, setEditor] = useState(false);
  const [importing, setImporting] = useState(false);
  const [visible, setVisible] = useState<string[]>(() => {
    try {
      return (
        JSON.parse(localStorage.getItem(`product-columns:${data.access?.user?.id}`) || "null") || [
          "category",
          "price",
          "lowest",
          "changes",
          "label",
          "stock",
          "last",
          "group",
          "tags",
        ]
      );
    } catch {
      return [];
    }
  });
  const { modal } = App.useApp();
  useEffect(() => {
    setQ(params.get("q") || "");
    setLabel(params.get("label") || undefined);
  }, [params]);
  const products: Row[] = data.products;
  const filtered = products.filter(
    (p) =>
      (group === "all" || p.group === group) &&
      (!category || p.category === category) &&
      (!label || p.label === label) &&
      (!stock || p.stock === stock) &&
      (!shop || p.shop_type === shop) &&
      (min == null ||
        (p.current_price != null && p.current_price >= min * 100)) &&
      (max == null ||
        (p.current_price != null && p.current_price <= max * 100)) &&
      tags.every((t) => p.tags.includes(t)) &&
      `${p.name} ${p.spec} ${p.model} ${p.brand} ${p.tags.join(" ")}`
        .toLowerCase()
        .includes(q.toLowerCase()),
  );
  const bulk = async (actionName: string) => {
    let value: any =
      actionName === "interval" ? 4 : actionName === "tags" ? [] : "";
    if (["pause", "resume", "crawl"].includes(actionName)) {
      await action(
        () => post("/products/bulk", { ids: selection, action: actionName }),
        "操作已完成",
      );
      setSelection([]);
      return;
    }
    modal.confirm({
      title: (
        {
          delete: "将所选商品移至回收站？",
          group: "修改分组",
          tags: "批量添加标签",
          interval: "调整采集频率",
        } as Row
      )[actionName],
      content:
        actionName === "delete" ? (
          "历史价格将保留，恢复后商品处于暂停状态。"
        ) : actionName === "interval" ? (
          <Select
            defaultValue={4}
            options={[
              { label: "每小时", value: 1 },
              { label: "每 4 小时", value: 4 },
              { label: "每天", value: 24 },
            ]}
            onChange={(v) => (value = v)}
            style={{ width: "100%" }}
          />
        ) : actionName === "tags" ? (
          <Select
            mode="tags"
            placeholder="输入标签"
            onChange={(v) => (value = v)}
            style={{ width: "100%" }}
          />
        ) : (
          <Input
            placeholder="输入分组名称"
            onChange={(e) => (value = e.target.value)}
          />
        ),
      onOk: async () => {
        await action(
          () =>
            post("/products/bulk", {
              ids: selection,
              action: actionName,
              value,
            }),
          "批量操作完成",
        );
        setSelection([]);
      },
    });
  };
  const columns: Row[] = [
    {
      title: "商品 / 规格",
      key: "name",
      fixed: "left",
      width: 285,
      render: (_: any, p: Row) => <ProductName product={p} />,
    },
    { title: "品类", key: "category", width: 80, dataIndex: "category" },
    {
      title: "当前价",
      key: "price",
      width: 140,
      sorter: (a: Row, b: Row) =>
        (a.current_price ?? Infinity) - (b.current_price ?? Infinity),
      render: (_: any, p: Row) => (
        <div className="price-cell">
          <b>{money(p.current_price)}</b>
          {p.stale ? (
            <small className="warning-text">历史有效价 · 本次失败</small>
          ) : p.price_type && p.price_type !== "regular" ? (
            <small>{p.price_type === "coupon" ? "券后价" : "会员价"}</small>
          ) : null}
        </div>
      ),
    },
    {
      title: "距监测最低",
      key: "lowest",
      width: 110,
      render: (_: any, p: Row) => (
        <span className={p.above_low === 0 ? "down" : "muted"}>
          {p.above_low === 0 ? "监测最低" : pct(p.above_low)}
        </span>
      ),
    },
    {
      title: "30 天变价",
      key: "changes",
      width: 100,
      render: (_: any, p: Row) => (
        <span>
          {p.changes_30d} <span className="muted">次</span>
        </span>
      ),
    },
    {
      title: "趋势参考",
      key: "label",
      width: 130,
      render: (_: any, p: Row) => <TrendTag label={p.label} />,
    },
    {
      title: "库存",
      key: "stock",
      width: 100,
      render: (_: any, p: Row) => <Status value={p.stock} />,
    },
    {
      title: "最后成功采集",
      key: "last",
      width: 150,
      render: (_: any, p: Row) => (
        <div>
          <span>{ago(p.last_success)}</span>
          <small className="cell-sub">
            {p.paused
              ? "已暂停"
              : p.last_status === "not_run"
                ? "等待采集"
                : p.stale
                  ? "本次采集失败"
                  : ""}
          </small>
        </div>
      ),
    },
    { title: "分组", key: "group", width: 100, dataIndex: "group" },
    {
      title: "标签",
      key: "tags",
      width: 140,
      render: (_: any, p: Row) =>
        p.tags.length ? (
          p.tags.map((t: string) => (
            <Tag key={t} bordered={false}>
              {t}
            </Tag>
          ))
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: "",
      key: "actions",
      width: 50,
      fixed: "right",
      render: (_: any, p: Row) => (
        <Dropdown
          trigger={["click"]}
          menu={{
            items: [
              { key: "edit", label: "编辑商品" },
              { key: "crawl", label: data.settings.price_source === "api" ? "立即 API 采集" : "立即重抓", disabled: !p.url },
              { key: "pause", label: p.paused ? "恢复采集" : "暂停采集" },
              { key: "compare", label: "加入比较" },
              { key: "delete", label: "移至回收站", danger: true },
            ],
            onClick: ({ key }) => {
              if (key === "edit") {
                setEdit(p);
                setEditor(true);
              } else if (key === "compare") navigate(`/compare?ids=${p.id}`);
              else if (key === "delete")
                modal.confirm({
                  title: "移至回收站？",
                  onOk: () =>
                    action(
                      () => remove(`/entities/product/${p.id}`),
                      "已移至回收站",
                    ),
                });
              else if (key === "crawl")
                action(
                  () => post(`/product/${p.id}/crawl`),
                  "已加入采集队列",
                ).catch(() => {});
              else
                action(
                  () =>
                    put(`/entities/product/${p.id}`, {
                      paused: !p.paused,
                      version: p.version,
                    }),
                  p.paused ? "已恢复采集" : "已暂停采集",
                ).catch(() => {});
            },
          }}
        >
          <Button
            type="text"
            size="small"
            aria-label="商品操作"
            icon={<MoreHorizontal size={17} />}
          />
        </Dropdown>
      ),
    },
  ];
  return (
    <>
      <PageTitle
        eyebrow="PRODUCT LIBRARY"
        title="商品库"
        description="把候选清单交给时间，找到值得入手的价格。"
        actions={
          <>
            <CollectAllButton />
            <CollectionFrequencyButton />
            <Button
              icon={<Download size={14} />}
              onClick={() => download("/export/product?format=csv")}
            >
              导出
            </Button>
            <Button
              icon={<ArrowDownRight size={15} />}
              onClick={() => setImporting(true)}
            >
              导入链接 / CSV
            </Button>
            <Button
              type="primary"
              icon={<Plus size={15} />}
              onClick={() => {
                setEdit(null);
                setEditor(true);
              }}
            >
              新增商品
            </Button>
          </>
        }
      />
      <div className="library-summary">
        <span>
          <b>{products.length}</b> 件商品
        </span>
        <span className="separator" />
        <span>
          <i className="tiny-dot" />
          {products.filter((p) => !p.paused).length} 件正在监测
        </span>
        <span className="separator" />
        <span>{products.filter((p) => p.paused).length} 件已暂停</span>
      </div>
      <Panel className="library-panel">
        <div className="library-tabs">
          <div>
            {[
              "all",
              "待购清单",
              "观察中",
              ...new Set(
                products
                  .map((p) => p.group)
                  .filter((g) => g && !["待购清单", "观察中"].includes(g)),
              ),
            ].map((g) => (
              <button
                key={g}
                className={group === g ? "selected" : ""}
                onClick={() => setGroup(g)}
              >
                {g === "all" ? "全部商品" : g}
                <span>
                  {g === "all"
                    ? products.length
                    : products.filter((p) => p.group === g).length}
                </span>
              </button>
            ))}
          </div>
          <Space>
            <Select
              placeholder="已保存视图"
              allowClear
              style={{ width: 140 }}
              options={data.views
                .filter((v: Row) => v.config?.filters)
                .map((v: Row) => ({ label: v.name, value: v.id }))}
              onChange={(id) => {
                const f = data.views.find((v: Row) => v.id === id)?.config
                  ?.filters;
                if (f) {
                  setQ(f.q || "");
                  setCategory(f.category);
                  setLabel(f.label);
                  setGroup(f.group || "all");
                  setTags(f.tags || []);
                }
              }}
            />
            <Dropdown
              trigger={["click"]}
              popupRender={() => (
                <div className="column-picker">
                  {columns
                    .filter((c) => !["name", "actions"].includes(c.key))
                    .map((c) => (
                      <Checkbox
                        key={c.key}
                        checked={visible.includes(c.key)}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...visible, c.key]
                            : visible.filter((v) => v !== c.key);
                          setVisible(next);
                          localStorage.setItem(
                            `product-columns:${data.access?.user?.id}`,
                            JSON.stringify(next),
                          );
                        }}
                      >
                        {c.title}
                      </Checkbox>
                    ))}
                </div>
              )}
            >
              <Button type="text" icon={<Settings2 size={14} />}>
                列设置
              </Button>
            </Dropdown>
          </Space>
        </div>
        <div className="filterbar">
          <Input
            prefix={<Search size={14} />}
            placeholder="搜索名称、型号、标签"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            allowClear
            style={{ width: 235 }}
          />
          <Select
            placeholder="全部品类"
            allowClear
            value={category}
            onChange={setCategory}
            options={opts(categories)}
            style={{ width: 115 }}
          />
          <Select
            placeholder="趋势参考"
            allowClear
            value={label}
            onChange={setLabel}
            options={opts(labels)}
            style={{ width: 140 }}
          />
          <Select
            placeholder="库存"
            allowClear
            value={stock}
            onChange={setStock}
            options={[
              { label: "有货", value: "in_stock" },
              { label: "缺货", value: "out_of_stock" },
              { label: "未知", value: "unknown" },
            ]}
            style={{ width: 100 }}
          />
          <Select
            placeholder="店铺类型"
            allowClear
            value={shop}
            onChange={setShop}
            options={opts(["官方旗舰", "授权店", "普通店"])}
            style={{ width: 125 }}
          />
          <InputNumber
            prefix="¥"
            placeholder="最低"
            value={min}
            min={0}
            onChange={setMin}
            style={{ width: 100 }}
          />
          <span className="muted">–</span>
          <InputNumber
            placeholder="最高"
            value={max}
            min={0}
            onChange={setMax}
            style={{ width: 100 }}
          />
          <Select
            mode="multiple"
            placeholder="标签"
            value={tags}
            onChange={setTags}
            options={opts([...new Set(products.flatMap((p) => p.tags))])}
            style={{ minWidth: 110 }}
          />
          <Button
            type="link"
            onClick={() => {
              setQ("");
              setCategory(undefined);
              setLabel(undefined);
              setGroup("all");
              setShop(undefined);
              setStock(undefined);
              setTags([]);
              setMin(null);
              setMax(null);
              setParams({});
            }}
          >
            清除
          </Button>
          <Button
            size="small"
            type="text"
            onClick={() => {
              let name = "";
              modal.confirm({
                title: "保存筛选为视图",
                content: (
                  <Input
                    placeholder="视图名称"
                    onChange={(e) => (name = e.target.value)}
                  />
                ),
                onOk: () => {
                  if (!name.trim()) return Promise.reject();
                  return action(
                    () =>
                      post("/entities/view", {
                        name,
                        config: {
                          filters: { q, category, label, group, tags },
                        },
                      }),
                    "视图已保存",
                  );
                },
              });
            }}
          >
            + 保存视图
          </Button>
        </div>
        {selection.length > 0 && (
          <div className="bulk-toolbar">
            <strong>已选 {selection.length} 项</strong>
            <Button size="small" onClick={() => bulk("group")}>
              改分组
            </Button>
            <Button size="small" onClick={() => bulk("tags")}>
              打标签
            </Button>
            <Button size="small" onClick={() => bulk("interval")}>
              采集频率
            </Button>
            <Button size="small" onClick={() => bulk("pause")}>
              暂停
            </Button>
            <Button size="small" onClick={() => bulk("resume")}>
              恢复
            </Button>
            <Button size="small" onClick={() => bulk("crawl")}>
              {data.settings.price_source === "api" ? "API 采集" : "重抓"}
            </Button>
            <Button
              size="small"
              onClick={() => navigate(`/compare?ids=${selection.join(",")}`)}
            >
              比较
            </Button>
            <Button size="small" danger onClick={() => bulk("delete")}>
              删除
            </Button>
            <Button type="text" size="small" onClick={() => setSelection([])}>
              取消选择
            </Button>
          </div>
        )}
        <Table
          rowKey="id"
          columns={
            columns.filter(
              (c) =>
                ["name", "actions"].includes(c.key) || visible.includes(c.key),
            ) as any
          }
          dataSource={filtered}
          rowSelection={{ selectedRowKeys: selection, onChange: setSelection }}
          scroll={{ x: 1250 }}
          pagination={{ ...pagination, showTotal: undefined }}
          locale={{
            emptyText: (
              <Blank
                title="开始建立你的硬件清单"
                text={
                  products.length
                    ? "没有符合当前筛选的商品。"
                    : "添加淘宝或天猫链接，自动记录价格、变化和购买时机。"
                }
                action={
                  !products.length && (
                    <Button
                      type="primary"
                      icon={<Plus size={15} />}
                      onClick={() => setEditor(true)}
                    >
                      添加第一个商品
                    </Button>
                  )
                }
              />
            ),
          }}
          onRow={(record) => ({
            onDoubleClick: () => {
              setEdit(record);
              setEditor(true);
            },
          })}
        />
      </Panel>
      <ProductEditor
        open={editor}
        product={edit}
        onClose={() => setEditor(false)}
      />
      <ImportDialog
        kind="product"
        open={importing}
        onClose={() => setImporting(false)}
      />
    </>
  );
}

export function ProductDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, action } = useMonitor();
  const {
    value: detail,
    reload,
    busy,
    error: resourceError,
  } = useResource<Row>(`/product/${id}`, {});
  const p = detail.product;
  const [edit, setEdit] = useState(false);
  const [range, setRange] = useState<number | string>(
    data.settings.default_range || 30,
  );
  const [custom, setCustom] = useState<any>(null);
  const [raw, setRaw] = useState(false);
  const [layers, setLayers] = useState(
    data.settings.chart_layers || ["sale", "events"],
  );
  const [tab, setTab] = useState("records");
  const [record, setRecord] = useState<Row | null>(null);
  const [manual, setManual] = useState(false);
  const [correction, setCorrection] = useState<Row | null>(null);
  const [note, setNote] = useState<Row | null>(null);
  const [noteOpen, setNoteOpen] = useState(false);
  const [rule, setRule] = useState(false);
  const [override, setOverride] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [form] = Form.useForm();
  const [noteForm] = Form.useForm();
  const [overrideForm] = Form.useForm();
  const chart = useRef<echarts.ECharts | null>(null);
  const { modal, message } = App.useApp();
  const [audits, setAudits] = useState<Row[]>([]);
  useEffect(() => {
    if (tab === "audit")
      api<Row[]>("/audit")
        .then((rows) =>
          setAudits(
            rows.filter(
              (r) =>
                r.entity_id === id ||
                r.before_data?.product_id === id ||
                r.after_data?.product_id === id,
            ),
          ),
        )
        .catch(() => {});
  }, [tab, detail, id]);
  if (busy && !p) return <div className="full-loader">正在读取商品…</div>;
  if (!p && resourceError) return <Blank title="商品加载失败" text={resourceError} action={<Button onClick={() => reload()}>重试</Button>} />;
  if (!p)
    return (
      <Blank
        title="商品不存在或已移至回收站"
        action={
          <Button onClick={() => navigate("/products")}>返回商品库</Button>
        }
      />
    );
  const all: Row[] = detail.records || [];
  const rows = all.filter((r) =>
    range === "custom"
      ? !custom ||
        (dayjs(r.observed_at).isAfter(custom[0]) &&
          dayjs(r.observed_at).isBefore(custom[1].endOf("day")))
      : range === "all" ||
        dayjs(r.observed_at).isAfter(dayjs().subtract(Number(range), "day")),
  );
  const events = data.events.filter(
    (e: Row) =>
      e.scope === "all" ||
      (e.scope === "product" && e.scope_value === id) ||
      p[e.scope] === e.scope_value,
  );
  const matchingRules = data.rules.filter(
    (r: Row) =>
      !r.template &&
      (r.scope === "all" ||
        (r.scope === "product" && r.scope_value === id) ||
        (r.scope === "tag" && p.tags.includes(r.scope_value)) ||
        p[r.scope] === r.scope_value),
  );
  const openManual = (r?: Row) => {
    if (r?.daily_override_id) {
      setRecord(null);
      setTab("daily");
      message.info("该记录已按天编辑，请在日价格中修改或撤销");
      return;
    }
    form.resetFields();
    form.setFieldsValue(
      r
        ? {
            price: r.price ? r.price / 100 : null,
            original_price: r.original_price ? r.original_price / 100 : null,
            stock: r.stock,
            reason: "",
            observed_at: dayjs(r.observed_at),
          }
        : { stock: "in_stock", observed_at: dayjs() },
    );
    setCorrection(r || null);
    setManual(true);
    setRecord(null);
  };
  const openNote = (n?: Row) => {
    setNote(n || null);
    noteForm.resetFields();
    noteForm.setFieldsValue(
      n
        ? {
            ...n,
            start: n.start ? dayjs(n.start) : null,
            end: n.end ? dayjs(n.end) : null,
          }
        : { type: "note", on_chart: true, start: dayjs() },
    );
    setNoteOpen(true);
  };
  const mutate = async (fn: () => Promise<any>, text: string) => {
    await action(fn, text);
    await reload();
    setSelected([]);
  };
  const recordColumns: any[] = [
    {
      title: "时间",
      dataIndex: "observed_at",
      width: 155,
      render: (v: string) => date(v),
    },
    {
      title: "来源",
      width: 110,
      render: (_: any, r: Row) => (
        <Tag
          bordered={false}
          color={r.manual ? "gold" : r.corrected ? "blue" : "default"}
        >
          {r.effective_manual
            ? "人工价优先"
            : r.data_quality === "carried_forward" && !r.corrected
              ? "沿用前值"
              : r.import_source
                ? "实测导入"
                : r.manual
                  ? "人工新增"
                  : r.corrected
                    ? `${r.source === "api" ? "API" : "爬虫"} · 已修正`
                    : r.source === "api" ? (r.platform === "jd" ? "京东 API" : "淘宝 API") : "爬虫"}
        </Tag>
      ),
    },
    {
      title: "到手价",
      width: 160,
      render: (_: any, r: Row) => (
        <div>
          <b>{money(r.price)}</b>
          {(r.corrected || r.effective_manual) && (
            <small className="cell-sub">原值 {money(r.raw_price)}</small>
          )}
        </div>
      ),
    },
    { title: "划线价", dataIndex: "original_price", width: 120, render: money },
    {
      title: "库存",
      dataIndex: "stock",
      width: 100,
      render: (v: string) => <Status value={v} />,
    },
    {
      title: "状态",
      width: 130,
      render: (_: any, r: Row) =>
        r.valid ? (
          <Tag color={r.corrected ? "blue" : "green"} bordered={false}>
            {r.corrected ? "人工修正" : "有效"}
          </Tag>
        ) : (
          <Tag
            bordered={false}
            color={r.status === "anomaly" ? "warning" : "default"}
          >
            {r.status === "anomaly" ? "待确认" : "无效 / 失败"}
          </Tag>
        ),
    },
    {
      title: "操作",
      width: 240,
      render: (_: any, r: Row) => (
        <Space size={2}>
          <Button size="small" type="link" onClick={() => openManual(r)}>
            修正
          </Button>
          <Button
            size="small"
            type="link"
            disabled={Boolean(r.daily_override_id)}
            onClick={() =>
              mutate(
                () =>
                  post(`/records/${r.id}/correct`, {
                    valid: !r.valid,
                    reason: r.valid ? "手动标记无效" : "人工确认有效",
                  }),
                "记录已更新",
              ).catch(() => {})
            }
          >
            {r.valid ? "标无效" : "确认有效"}
          </Button>
          {r.corrected && (
            <Button
              size="small"
              type="link"
              onClick={() =>
                mutate(
                  () =>
                    r.daily_override_id
                      ? post(`/daily-prices/${r.daily_override_id}/undo`, {
                          version: r.daily_override_version,
                        })
                      : post(`/records/${r.id}/undo`),
                  "修正已撤销",
                ).catch(() => {})
              }
            >
              撤销
            </Button>
          )}
          <Button size="small" type="link" onClick={() => setRecord(r)}>
            原始值
          </Button>
        </Space>
      ),
    },
  ];
  const tabItems = [
    {
      key: "daily",
      label: "按天编辑价格",
      children: (
        <DailyPrices
          productId={id!}
          reference={Boolean(p.has_reference_history)}
          mutate={mutate}
        />
      ),
    },
    {
      key: "records",
      label: "价格记录",
      children: (
        <>
          <div className="table-toolbar">
            <Space>
              <Button
                icon={<CalendarDays size={14} />}
                onClick={() => setTab("daily")}
              >
                按天编辑价格
              </Button>
              <Button icon={<Plus size={14} />} onClick={() => openManual()}>
                手动新增价格
              </Button>
              <span className="muted small">共 {all.length} 条观测</span>
            </Space>
            <Button
              icon={<Download size={14} />}
              onClick={() =>
                download(`/export/observations?format=csv&product_id=${id}`)
              }
            >
              导出 CSV
            </Button>
          </div>
          <Table
            rowKey="id"
            dataSource={[...all].reverse()}
            columns={recordColumns}
            pagination={pagination}
            scroll={{ x: 1100 }}
            rowSelection={{
              selectedRowKeys: selected,
              onChange: (keys) => setSelected(keys as string[]),
              getCheckboxProps: (r: Row) => ({
                disabled: Boolean(r.daily_override_id),
              }),
            }}
            locale={{
              emptyText: (
                <Blank
                  title="尚无价格观测"
                  text="可以立即采集，或手动补充已核实的历史价格。"
                />
              ),
            }}
          />
        </>
      ),
    },
    {
      key: "changes",
      label: "变价事件",
      children: (
        <>
          <p className="muted small">
            新价连续 {data.settings.confirm_count}{" "}
            次一致后确认变价；标记为假降价的事件不会用于趋势参考。
          </p>
          <Table
            rowKey="id"
            dataSource={[...(detail.changes || [])].reverse()}
            pagination={pagination}
            columns={[
              {
                title: "时间",
                dataIndex: "observed_at",
                render: (v: string) => date(v),
              },
              {
                title: "类型",
                dataIndex: "type",
                render: (v: string) => (
                  <Tag color={v === "降价" ? "green" : "red"}>{v}</Tag>
                ),
              },
              {
                title: "旧价 → 新价",
                render: (_: any, r: Row) =>
                  `${money(r.old_price)} → ${money(r.new_price)}`,
              },
              { title: "幅度", dataIndex: "percent", render: pct },
              {
                title: "旧价持续",
                dataIndex: "duration_days",
                render: (v: number) => `${v} 天`,
              },
              {
                title: "标记",
                render: (_: any, r: Row) => (
                  <Select
                    size="small"
                    value={r.mark || ""}
                    options={[
                      { label: "正常", value: "" },
                      { label: "假降价", value: "假降价" },
                      { label: "促销开始", value: "促销开始" },
                      { label: "促销结束", value: "促销结束" },
                    ]}
                    style={{ width: 100 }}
                    onChange={(mark) =>
                      mutate(
                        () =>
                          post(`/changes/${r.id}`, {
                            mark,
                            excluded: mark === "假降价",
                          }),
                        "标记已更新",
                      ).catch(() => {})
                    }
                  />
                ),
              },
              {
                title: "排除预测",
                render: (_: any, r: Row) => (
                  <Switch
                    size="small"
                    checked={r.excluded}
                    onChange={(excluded) =>
                      mutate(
                        () =>
                          post(`/changes/${r.id}`, { mark: r.mark, excluded }),
                        "已更新",
                      ).catch(() => {})
                    }
                  />
                ),
              },
            ]}
          />
        </>
      ),
    },
    {
      key: "compare",
      label: "同款比价",
      children: (
        <div className="detail-compare">
          <p className="muted">
            依据同款型号与规格匹配店铺；在商品信息中填写统一型号与规格后即可比较。
          </p>
          <Button onClick={() => navigate(`/compare?ids=${id}&mode=shops`)}>
            打开同款各店铺比较 <ArrowUpRight size={14} />
          </Button>
        </div>
      ),
    },
    {
      key: "forecast",
      label: "预测参考",
      children: (
        <>
          <div className="forecast-summary">
            <div>
              <div className="eyebrow">
                未来 {p.forecast.window} 天 ·{" "}
                {p.forecast.model === "lightgbm-v1" ? "已验证模型" : "规则参考"}
              </div>
              <h2>
                <TrendTag label={p.label} />
              </h2>
              {p.forecast.factors.map((f: string) => (
                <p key={f}>· {f}</p>
              ))}
              <ForecastNumbers forecast={p.forecast} />
            </div>
            <Button
              onClick={() => {
                overrideForm.resetFields();
                setOverride(true);
              }}
            >
              临时覆盖展示标签
            </Button>
          </div>
          <Table
            rowKey="id"
            dataSource={detail.forecasts}
            pagination={pagination}
            scroll={{ x: 1100 }}
            columns={[
              {
                title: "计算时间",
                dataIndex: "created_at",
                render: (v: string) => date(v),
              },
              {
                title: "当时标签",
                render: (_: any, r: Row) => <TrendTag label={r.data.label} />,
              },
              {
                title: "当时价格",
                render: (_: any, r: Row) => money(r.data.current_price),
              },
              {
                title: "之后窗口最低价",
                dataIndex: "actual_min",
                render: money,
              },
              {
                title: "当时降价概率",
                render: (_: any, r: Row) =>
                  r.data.probability == null ? "—" : `${r.data.probability}%`,
              },
              {
                title: "当时最低价范围",
                width: 230,
                render: (_: any, r: Row) =>
                  r.data.interval
                    ? `${money(r.data.interval[0])} ～ ${money(r.data.interval[1])}`
                    : "—",
              },
              {
                title: "评估状态",
                render: (_: any, r: Row) =>
                  (
                    ({
                      legacy: "旧快照缺少规格口径",
                      pending: "观察期中",
                      evaluated: "已完成对账",
                      insufficient: "后续观测覆盖不足",
                    }) as Row
                  )[r.evaluation_status] || "—",
              },
            ]}
          />
        </>
      ),
    },
    {
      key: "alerts",
      label: "提醒",
      children: (
        <>
          <div className="table-toolbar">
            <p className="muted">直接规则与所属分组、标签、品类规则。</p>
            <Button icon={<Plus size={14} />} onClick={() => setRule(true)}>
              新建商品提醒
            </Button>
          </div>
          <Table
            rowKey="id"
            dataSource={matchingRules}
            pagination={pagination}
            columns={[
              { title: "规则名称", dataIndex: "name" },
              {
                title: "条件",
                dataIndex: "condition",
                render: (v: string) =>
                  (
                    ({
                      target: "目标价",
                      drop: "窗口降幅",
                      low: "窗口新低",
                      restock: "到货",
                      event: "大促前汇总",
                    }) as Row
                  )[v],
              },
              {
                title: "来源",
                render: (_: any, r: Row) =>
                  r.scope === "product"
                    ? "本商品规则"
                    : `${r.scope} · ${r.scope_value || "全部"}`,
              },
              {
                title: "渠道",
                dataIndex: "channels",
                render: (v: string[]) => v.join(" / "),
              },
              {
                title: "启用",
                render: (_: any, r: Row) => (
                  <Switch
                    checked={r.enabled}
                    onChange={(enabled) =>
                      mutate(
                        () =>
                          put(`/entities/rule/${r.id}`, {
                            enabled,
                            version: r.version,
                          }),
                        "规则已更新",
                      ).catch(() => {})
                    }
                  />
                ),
              },
              {
                title: "操作",
                render: () => (
                  <Button type="link" onClick={() => navigate("/alerts")}>
                    前往提醒中心
                  </Button>
                ),
              },
            ]}
          />
        </>
      ),
    },
    {
      key: "notes",
      label: "备注与标注",
      children: (
        <>
          <div className="table-toolbar">
            <p className="muted">支持点标注和区间标注，可叠加到价格图表。</p>
            <Button icon={<Plus size={14} />} onClick={() => openNote()}>
              添加备注 / 标注
            </Button>
          </div>
          <Table
            rowKey="id"
            dataSource={detail.notes}
            pagination={pagination}
            columns={[
              {
                title: "类型",
                dataIndex: "type",
                render: (v: string) =>
                  (
                    ({
                      note: "备注",
                      point: "点标注",
                      range: "区间标注",
                    }) as Row
                  )[v],
              },
              {
                title: "时间 / 区间",
                render: (_: any, r: Row) =>
                  r.start
                    ? `${date(r.start)}${r.end ? " ~ " + date(r.end) : ""}`
                    : date(r.created_at),
              },
              { title: "内容", dataIndex: "text" },
              {
                title: "图上显示",
                render: (_: any, r: Row) => (
                  <Switch
                    size="small"
                    checked={r.on_chart}
                    onChange={(on_chart) =>
                      mutate(
                        () =>
                          put(`/entities/note/${r.id}`, {
                            on_chart,
                            version: r.version,
                          }),
                        "标注已更新",
                      ).catch(() => {})
                    }
                  />
                ),
              },
              {
                title: "操作",
                render: (_: any, r: Row) => (
                  <Space>
                    <Button
                      type="link"
                      size="small"
                      onClick={() => openNote(r)}
                    >
                      编辑
                    </Button>
                    <Popconfirm
                      title="删除此备注？"
                      onConfirm={() =>
                        mutate(() => remove(`/entities/note/${r.id}`), "已删除")
                      }
                    >
                      <Button type="link" danger size="small">
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        </>
      ),
    },
    {
      key: "audit",
      label: "修改历史",
      children: (
        <Table
          rowKey="id"
          dataSource={audits}
          pagination={pagination}
          columns={[
            {
              title: "时间",
              dataIndex: "created_at",
              render: (v: string) => date(v),
            },
            { title: "对象", dataIndex: "kind" },
            { title: "操作", dataIndex: "action" },
            { title: "备注", dataIndex: "reason" },
            {
              title: "状态",
              render: (_: any, r: Row) => (r.reverted_at ? "已回滚" : "有效"),
            },
            {
              title: "操作",
              render: () => (
                <Button type="link" onClick={() => navigate("/audit")}>
                  查看差异与回滚
                </Button>
              ),
            },
          ]}
        />
      ),
    },
  ];
  return (
    <>
      <Button
        type="text"
        className="back-button"
        icon={<ArrowLeft size={14} />}
        onClick={() => navigate("/products")}
      >
        返回商品库
      </Button>
      <PageTitle
        eyebrow={`${p.category.toUpperCase()} / PRODUCT DETAIL`}
        title={p.name}
        description={`${p.brand || ""} ${p.model || ""} ${p.spec ? "· " + p.spec : ""}`}
        actions={
          <>
            <Button
              icon={<RefreshCw size={14} />}
              disabled={!p.url}
              onClick={() =>
                mutate(
                  () => post(`/product/${id}/crawl`),
                  "已加入采集队列",
                ).catch(() => {})
              }
            >
              {data.settings.price_source === "api" ? "立即 API 采集" : "立即重抓"}
            </Button>
            <Button
              icon={p.paused ? <Play size={14} /> : <Pause size={14} />}
              disabled={!p.url}
              onClick={() =>
                mutate(
                  () =>
                    put(`/entities/product/${id}`, {
                      paused: !p.paused,
                      version: p.version,
                    }),
                  "采集状态已更新",
                ).catch(() => {})
              }
            >
              {p.paused ? "恢复" : "暂停"}
            </Button>
            <Button icon={<Pencil size={14} />} onClick={() => setEdit(true)}>
              编辑
            </Button>
            <Button
              type="primary"
              icon={<Bell size={14} />}
              onClick={() => setRule(true)}
            >
              设置提醒
            </Button>
          </>
        }
      />
      {!p.url && (
        <Alert
          className="mb16"
          type="info"
          showIcon
          message="参考商品 · 待补淘宝链接"
        />
      )}
      {p.stale && (
        <Alert
          className="mb16"
          type="warning"
          showIcon
          message="最近一次采集未取得有效价格，本次价格为 None；下方保留最后成功观测。"
        />
      )}
      {p.last_status === "sku_unverified" && (
        <Alert
          className="mb16"
          type="info"
          showIcon
          message="SKU 需要核对"
          description={
            <div>
              页面当前选中：
              {all.at(-1)?.selected_options?.join(" / ") || "没有读到选中规格"}
              。请在编辑商品中填写「预期选中规格」，确认与目标商品一致后重新采集。
            </div>
          }
        />
      )}
      <div className="overview-stats">
        <Metric
          label={
            p.price_basis === "reference"
              ? "最新参考价"
              : p.stale
                ? "最近有效到手价"
                : "当前到手价"
          }
          value={money(p.current_price)}
          hint={
            p.price_basis === "reference"
              ? p.current_price_quality === "carried_forward"
                ? "人工导入历史 · 参与模型训练"
                : "表格实测参考价 · 口径未注明"
              : p.current_price_daily_day
                ? `人工按天设置 · ${p.current_price_daily_day}`
                : p.current_price_manual
                  ? "人工录入或修正的价格"
                  : p.price_type === "coupon"
                    ? "需领券；可在设置中调整价格口径"
                    : p.current_price_source === "api"
                      ? `${p.current_price_platform === "jd" ? "京东" : "淘宝"}官方 API · ${p.current_price_granularity === "item" ? "商品级价格，未确认 SKU" : "已核对商品与 SKU"}`
                      : "来自商品页面的有效价格"
          }
          accent
        />
        <Metric
          label="距监测最低"
          value={pct(p.above_low)}
          hint={`监测以来最低 ${money(p.lowest)}`}
        />
        <Metric
          label={`趋势参考 · ${p.forecast.window} 天`}
          value={<TrendTag label={p.label} />}
          hint={
            p.overridden
              ? "人工临时覆盖，仅影响展示"
              : p.forecast.model === "lightgbm-v1"
                ? "基于已回测的学习模型"
                : "规则参考，等待数据与模型验证"
          }
        />
        <Metric
          label="最后成功采集"
          value={
            <span className="metric-time">{ago(p.last_crawl_success)}</span>
          }
          hint={date(p.last_crawl_success, "YYYY-MM-DD HH:mm")}
        />
      </div>
      <div className="detail-top-grid">
        <Panel
          title="价格轨迹"
          extra={
            <Button
              size="small"
              type="text"
              icon={<Download size={13} />}
              onClick={() => {
                if (chart.current) {
                  const a = document.createElement("a");
                  a.href = chart.current.getDataURL({
                    type: "png",
                    pixelRatio: 2,
                    backgroundColor: "#fff",
                  });
                  a.download = "价格轨迹.png";
                  a.click();
                }
              }}
            >
              导出图表
            </Button>
          }
        >
          <div className="chart-controls">
            <Space wrap>
              <Segmented
                value={range}
                onChange={setRange}
                options={[
                  { label: "7 天", value: 7 },
                  { label: "30 天", value: 30 },
                  { label: "90 天", value: 90 },
                  { label: "全部", value: "all" },
                  { label: "自定义", value: "custom" },
                ]}
              />
              {range === "custom" && (
                <DatePicker.RangePicker
                  value={custom}
                  onChange={setCustom}
                  size="small"
                />
              )}
            </Space>
            <Select
              size="small"
              value={raw ? "raw" : "effective"}
              onChange={(v) => setRaw(v === "raw")}
              options={[
                { label: "有效价格（含修正）", value: "effective" },
                { label: "原始采集价格", value: "raw" },
              ]}
              style={{ width: 165 }}
            />
          </div>
          <Checkbox.Group
            className="chart-layers"
            value={layers}
            onChange={(v) => setLayers(v as string[])}
            options={[
              { label: "到手价", value: "sale" },
              { label: "划线价", value: "original" },
              { label: "事件标注", value: "events" },
              { label: "人工标注", value: "notes" },
            ]}
          />
          <PriceChart
            series={
              layers.includes("sale")
                ? [{ name: p.name, records: rows }]
                : [
                    {
                      name: p.name,
                      records: rows.map((r) => ({
                        ...r,
                        price: null,
                        raw_price: null,
                      })),
                    },
                  ]
            }
            raw={raw}
            events={layers.includes("events") ? events : []}
            notes={layers.includes("notes") ? detail.notes : []}
            showOriginal={layers.includes("original")}
            onPoint={setRecord}
            onSelect={(ids) =>
              setSelected(
                ids.filter(
                  (key) => !all.find((r) => r.id === key)?.daily_override_id,
                ),
              )
            }
            chartRef={chart}
            step={data.settings.chart_step}
          />
          <div className="table-note">
            <CircleHelp size={13} />{" "}
            点击价格点可修正、标无效；使用图表框选工具批量选择。缺失观测保持断线。
          </div>
        </Panel>
        <div className="detail-side">
          <Panel
            title="商品信息"
            extra={
              <Button size="small" type="text" onClick={() => setEdit(true)}>
                编辑
              </Button>
            }
          >
            <Descriptions
              column={1}
              colon={false}
              size="small"
              items={[
                { key: "spec", label: "规格", children: p.spec || "—" },
                { key: "shop", label: "店铺", children: p.shop || "—" },
                {
                  key: "type",
                  label: "类型",
                  children: !p.url ? "来源未提供" : p.shop_type,
                },
                { key: "msrp", label: "建议价", children: money(p.msrp) },
                {
                  key: "release",
                  label: "发布日期",
                  children: p.release_date || "—",
                },
                {
                  key: "group",
                  label: "分组",
                  children: <Tag bordered={false}>{p.group}</Tag>,
                },
                {
                  key: "tags",
                  label: "标签",
                  children: p.tags.map((t: string) => <Tag key={t}>{t}</Tag>),
                },
              ]}
            />
            {p.url ? (
              <a
                href={p.url}
                target="_blank"
                rel="noreferrer"
                className="external-product"
              >
                打开淘宝商品页 <ExternalLink size={12} />
              </a>
            ) : (
              <Button type="link" onClick={() => setEdit(true)}>
                补充淘宝商品链接
              </Button>
            )}
          </Panel>
          <Panel
            title={
              <>
                <Sparkles size={15} /> 趋势参考
              </>
            }
          >
            <TrendTag label={p.label} />
            {p.forecast.factors.map((f: string) => (
              <p className="factor" key={f}>
                {f}
              </p>
            ))}
            <p className="muted small">
              趋势仅供购买时机参考，不构成价格承诺。
            </p>
            <ForecastNumbers forecast={p.forecast} compact />
            <Button size="small" onClick={() => setOverride(true)}>
              覆盖展示标签
            </Button>
          </Panel>
        </div>
      </div>
      {selected.length > 0 && (
        <div className="bulk-toolbar mb16">
          <strong>已选 {selected.length} 条价格记录</strong>
          <Button
            size="small"
            onClick={() =>
              modal.confirm({
                title: "将选中价格记录标记为无效？",
                content: "原始记录会保留，操作可在审计日志中回滚。",
                onOk: async () => {
                  await mutate(
                    () =>
                      post("/records/bulk", { ids: selected, valid: false }),
                    "已标记无效",
                  );
                  setSelected([]);
                },
              })
            }
          >
            批量标记无效
          </Button>
          <Button
            size="small"
            onClick={() => {
              const r = all.filter((r) => selected.includes(r.id));
              openNote({
                type: "range",
                text: "",
                on_chart: true,
                start: r[0]?.observed_at,
                end: r.at(-1)?.observed_at,
              });
            }}
          >
            添加区间标注
          </Button>
          <Button size="small" type="text" onClick={() => setSelected([])}>
            取消选择
          </Button>
        </div>
      )}
      <Panel className="detail-tabs">
        <Tabs activeKey={tab} onChange={setTab} items={tabItems} />
        <div className="table-note">
          原始采集数据只读；人工修改以覆盖层保存，可随时撤销。
        </div>
      </Panel>
      <ProductEditor open={edit} product={p} onClose={() => setEdit(false)} />
      <RuleEditor open={rule} onClose={() => setRule(false)} productId={id} />
      <Modal
        title={correction ? "修正价格记录" : "手动新增价格"}
        open={manual}
        onCancel={() => setManual(false)}
        onOk={() => form.submit()}
        destroyOnClose
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (v) => {
            await mutate(
              () =>
                correction
                  ? post(`/records/${correction.id}/correct`, {
                      ...v,
                      valid: true,
                    })
                  : post(`/product/${id}/records`, {
                      ...v,
                      observed_at: v.observed_at?.toISOString(),
                    }),
              "价格已保存",
            );
            setManual(false);
          }}
        >
          <div className="form-grid">
            <Form.Item
              name="price"
              label="到手价（元）"
              rules={[{ required: true, message: "请输入价格" }]}
            >
              <InputNumber min={0.01} precision={2} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="original_price" label="划线价（元）">
              <InputNumber min={0.01} precision={2} style={{ width: "100%" }} />
            </Form.Item>
          </div>
          {!correction && (
            <Form.Item
              name="observed_at"
              label="时间"
              rules={[{ required: true }]}
            >
              <DatePicker showTime style={{ width: "100%" }} />
            </Form.Item>
          )}
          <Form.Item name="stock" label="库存">
            <Select
              options={[
                { label: "有货", value: "in_stock" },
                { label: "缺货", value: "out_of_stock" },
                { label: "未知", value: "unknown" },
                { label: "已下架", value: "delisted" },
              ]}
            />
          </Form.Item>
          <Form.Item name="reason" label="修改原因 / 备注">
            <Input.TextArea />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="原始观测与操作"
        open={!!record}
        onCancel={() => setRecord(null)}
        footer={
          <Space>
            <Button
              onClick={() => {
                openNote({
                  type: "point",
                  start: record!.observed_at,
                  text: "",
                  on_chart: true,
                });
                setRecord(null);
              }}
            >
              添加标注
            </Button>
            <Button onClick={() => openManual(record!)}>修正价格</Button>
            <Button
              type="primary"
              onClick={() => {
                setRule(true);
                setRecord(null);
              }}
            >
              设置提醒
            </Button>
          </Space>
        }
      >
        <Descriptions
          column={1}
          items={[
            {
              key: "time",
              label: "时间",
              children: date(record?.observed_at, "YYYY-MM-DD HH:mm:ss"),
            },
            {
              key: "price",
              label: "原始价格",
              children: money(record?.raw_price),
            },
            {
              key: "current",
              label: "有效价格",
              children: record?.valid ? money(record?.price) : "无效",
            },
            {
              key: "status",
              label: "采集状态",
              children: <Status value={record?.status || "unknown"} />,
            },
            {
              key: "sku",
              label: "选中规格",
              children: record?.selected_options?.join(" / ") || "—",
            },
            {
              key: "source",
              label: "导入来源",
              children: record?.import_source || "—",
            },
            {
              key: "cell",
              label: "原表位置",
              children: record?.source_cell || "—",
            },
            {
              key: "source_note",
              label: "数据性质",
              children: record?.source_note || "—",
            },
          ]}
        />
        {(record?.source === "crawler" || record?.diagnostics) && (
          <CollectionDiagnostics details={record?.diagnostics} />
        )}
        <details>
          <summary>查看解析证据</summary>
          <pre className="json-preview">
            {JSON.stringify(record?.methods || {}, null, 2)}
          </pre>
        </details>
      </Modal>
      <Modal
        title={note?.id ? "编辑标注" : "添加备注与标注"}
        open={noteOpen}
        onCancel={() => setNoteOpen(false)}
        onOk={() => noteForm.submit()}
      >
        <Form
          form={noteForm}
          layout="vertical"
          onFinish={async (v) => {
            const body = {
              ...v,
              product_id: id,
              start: v.start?.toISOString() || null,
              end: v.end?.toISOString() || null,
            };
            await mutate(
              () =>
                note?.id
                  ? put(`/entities/note/${note.id}`, {
                      ...body,
                      version: note.version,
                    })
                  : post("/entities/note", body),
              "备注已保存",
            );
            setNoteOpen(false);
          }}
        >
          <Form.Item name="type" label="类型">
            <Select
              options={[
                { label: "备注", value: "note" },
                { label: "点标注", value: "point" },
                { label: "区间标注", value: "range" },
              ]}
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate>
            {() =>
              noteForm.getFieldValue("type") !== "note" && (
                <div className="form-grid">
                  <Form.Item
                    name="start"
                    label="开始时间"
                    rules={[{ required: true }]}
                  >
                    <DatePicker showTime />
                  </Form.Item>
                  {noteForm.getFieldValue("type") === "range" && (
                    <Form.Item
                      name="end"
                      label="结束时间"
                      rules={[{ required: true }]}
                    >
                      <DatePicker showTime />
                    </Form.Item>
                  )}
                </div>
              )
            }
          </Form.Item>
          <Form.Item
            name="text"
            label="内容"
            rules={[{ required: true, message: "请输入内容" }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item name="on_chart" label="图上显示" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="临时覆盖展示标签"
        open={override}
        onCancel={() => setOverride(false)}
        onOk={() => overrideForm.submit()}
        footer={
          <Space>
            {p.overridden && (
              <Button
                danger
                onClick={async () => {
                  await mutate(
                    () => post(`/product/${id}/override`, { label: null }),
                    "已取消覆盖",
                  );
                  setOverride(false);
                }}
              >
                取消覆盖
              </Button>
            )}
            <Button onClick={() => setOverride(false)}>关闭</Button>
            <Button type="primary" onClick={() => overrideForm.submit()}>
              保存
            </Button>
          </Space>
        }
      >
        <p className="muted">仅影响页面标签，不修改模型与历史价格。</p>
        <Form
          form={overrideForm}
          layout="vertical"
          onFinish={async (v) => {
            await mutate(
              () =>
                post(`/product/${id}/override`, {
                  label: v.label,
                  until: v.until.toISOString(),
                  reason: v.reason,
                }),
              "展示标签已更新",
            );
            setOverride(false);
          }}
        >
          <Form.Item name="label" label="展示标签" rules={[{ required: true }]}>
            <Select options={opts(labels)} />
          </Form.Item>
          <Form.Item name="until" label="到期时间" rules={[{ required: true }]}>
            <DatePicker showTime />
          </Form.Item>
          <Form.Item name="reason" label="备注">
            <Input.TextArea />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

export function Compare() {
  const { data, action } = useMonitor();
  const [params] = useSearchParams();
  const [ids, setIds] = useState<string[]>(
    params.get("ids")?.split(",").filter(Boolean) || [],
  );
  const [mode, setMode] = useState(
    params.get("mode") === "shops" ? "shops" : "multi",
  );
  const [axis, setAxis] = useState("absolute");
  const [range, setRange] = useState<number | string>(90);
  const [custom, setCustom] = useState<any>(null);
  const [events, setEvents] = useState(true);
  const [raw, setRaw] = useState(false);
  const [envelope, setEnvelope] = useState(true);
  const [series, setSeries] = useState<Row[]>([]);
  const [baselineOnly, setBaselineOnly] = useState(
    data.settings.trusted_only !== false,
  );
  const chart = useRef<echarts.ECharts | null>(null);
  const { modal } = App.useApp();
  const selectedProducts: Row[] = data.products.filter((p: Row) =>
    ids.includes(p.id),
  );
  const primary = selectedProducts[0];
  const matching: Row[] =
    mode === "shops" && primary?.model && primary?.spec
      ? data.products.filter(
          (p: Row) => p.model === primary.model && p.spec === primary.spec,
        )
      : selectedProducts;
  const active = matching.filter(
    (p) =>
      !baselineOnly ||
      mode !== "shops" ||
      ["官方旗舰", "授权店"].includes(p.shop_type),
  );
  const key = active.map((p) => p.id).join(",");
  useEffect(() => {
    let stop = false;
    Promise.all(
      active.map(async (p) => {
        const detail = await api<Row>(
          `/product/${p.id}/comparison-days?raw=${raw}`,
        );
        return detail.series.map((daily: Row) => ({
          ...p,
          ...daily,
          product_id: p.id,
        }));
      }),
    )
      .then((v) => {
        if (!stop) setSeries(v.flat());
      })
      .catch(() => {});
    return () => {
      stop = true;
    };
  }, [key, data, raw]);
  const cut = series.map((s) => ({
    ...s,
    records: s.records.filter((r: Row) =>
      range === "custom"
        ? !custom ||
          (r.day >= custom[0].format("YYYY-MM-DD") &&
            r.day <= custom[1].format("YYYY-MM-DD"))
        : range === "all" ||
          r.day >=
            dayjs()
              .tz("Asia/Shanghai")
              .subtract(Number(range) - 1, "day")
              .format("YYYY-MM-DD"),
    ),
  }));
  return (
    <>
      <PageTitle
        eyebrow="SIDE BY SIDE"
        title="比较页"
        description="让价格放在同一张图上，差异自然清晰。"
        actions={
          <>
            <Button
              onClick={() => {
                let name = "";
                modal.confirm({
                  title: "保存比较视图",
                  content: (
                    <Input
                      placeholder="视图名称"
                      onChange={(e) => (name = e.target.value)}
                    />
                  ),
                  onOk: () =>
                    name.trim()
                      ? action(
                          () =>
                            post("/entities/view", {
                              name,
                              config: {
                                comparison: { ids, mode, axis, range },
                              },
                            }),
                          "视图已保存",
                        )
                      : Promise.reject(),
                });
              }}
            >
              保存为视图
            </Button>
            <Button
              icon={<Download size={14} />}
              onClick={() => {
                if (chart.current) {
                  const a = document.createElement("a");
                  a.href = chart.current.getDataURL({
                    type: "png",
                    pixelRatio: 2,
                    backgroundColor: "#fff",
                  });
                  a.download = "硬件价格比较.png";
                  a.click();
                }
              }}
            >
              导出图片
            </Button>
            <Button onClick={() => downloadJSON("价格比较.json", cut)}>
              导出数据
            </Button>
          </>
        }
      />
      <Panel>
        <div className="compare-top">
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { label: "多个商品 / 规格", value: "multi" },
              { label: "同款各店铺", value: "shops" },
            ]}
          />
          <Select
            allowClear
            placeholder="加载比较视图"
            style={{ width: 180 }}
            options={data.views
              .filter((v: Row) => v.config?.comparison)
              .map((v: Row) => ({ label: v.name, value: v.id }))}
            onChange={(id) => {
              const c = data.views.find((v: Row) => v.id === id)?.config
                .comparison;
              if (c) {
                setIds(c.ids);
                setMode(c.mode);
                setAxis(c.axis);
                setRange(c.range);
              }
            }}
          />
        </div>
        <Select
          mode="multiple"
          placeholder="选择需要比较的商品 / 规格（最多 10 件）"
          value={ids}
          onChange={(v) => setIds(v.slice(0, 10))}
          options={data.products.map((p: Row) => ({
            label: `${p.name} ${p.spec}`,
            value: p.id,
          }))}
          style={{ width: "100%", marginTop: 18 }}
        />
        {new Set(selectedProducts.map((p) => p.spec)).size > 1 && (
          <Alert
            type="warning"
            showIcon
            className="mt16"
            message="所选商品规格不同，仅作参考对比；每个规格保持独立价格曲线。"
          />
        )}
        {mode === "shops" && (!primary?.model || !primary?.spec) && (
          <Alert
            type="info"
            className="mt16"
            message="同款比价需要为首个商品填写「同款型号」和「具体规格」。"
          />
        )}
        <div className="chart-controls mt16">
          <Space wrap>
            <span className="muted small">坐标口径</span>
            <Segmented
              value={axis}
              onChange={setAxis}
              options={[
                { label: "绝对价格", value: "absolute" },
                { label: "百分比变化", value: "percent" },
                { label: "现价 ÷ 建议价", value: "msrp" },
              ]}
            />
          </Space>
          <Segmented
            value={range}
            onChange={setRange}
            options={[
              { label: "30 天", value: 30 },
              { label: "90 天", value: 90 },
              { label: "全部", value: "all" },
              { label: "自定义", value: "custom" },
            ]}
          />
        </div>
        {range === "custom" && (
          <DatePicker.RangePicker value={custom} onChange={setCustom} />
        )}
        <div className="chart-layers">
          <Checkbox
            checked={events}
            onChange={(e) => setEvents(e.target.checked)}
          >
            显示事件标注
          </Checkbox>
          <Checkbox checked={!raw} onChange={(e) => setRaw(!e.target.checked)}>
            使用有效价格（含人工修正）
          </Checkbox>
          {mode === "shops" && (
            <>
              <Checkbox
                checked={baselineOnly}
                onChange={(e) => setBaselineOnly(e.target.checked)}
              >
                仅官方与授权店作基准
              </Checkbox>
              <Checkbox
                checked={envelope}
                onChange={(e) => setEnvelope(e.target.checked)}
              >
                最低价包络线
              </Checkbox>
            </>
          )}
        </div>
        {axis === "msrp" && active.some((p) => !p.msrp) && (
          <p className="warning-text small">
            未填写建议价的商品不会出现在该坐标口径中。
          </p>
        )}
        <PriceChart
          series={cut}
          calendarDays
          paddingDays={3}
          events={events ? data.events : []}
          mode={axis}
          raw={raw}
          height={410}
          chartRef={chart}
          envelope={mode === "shops" && envelope && axis === "absolute"}
          step={data.settings.chart_step}
        />
        <div className="table-note">
          按北京时间逐日比较，每天取最后有效价格，人工价格优先。不同 SKU
          和价格口径分别成线，已知价格日期范围前后各留 3
          天；缺失日期不补价。百分比变化以所选范围首个日价格为基准。
        </div>
      </Panel>
      <Panel title="对比明细" className="mt20">
        <Table
          rowKey="id"
          dataSource={matching}
          pagination={false}
          scroll={{ x: 1050 }}
          columns={[
            {
              title: "商品 / 规格",
              width: 270,
              render: (_: any, p: Row) => <ProductName product={p} />,
            },
            { title: "店铺类型", dataIndex: "shop_type" },
            { title: "当前价", dataIndex: "current_price", render: money },
            { title: "距监测最低", dataIndex: "above_low", render: pct },
            {
              title: "监测最高 / 最低",
              render: (_: any, p: Row) =>
                `${money(p.highest)} / ${money(p.lowest)}`,
            },
            { title: "30 天变价", dataIndex: "changes_30d" },
            {
              title: "趋势参考",
              render: (_: any, p: Row) => <TrendTag label={p.label} />,
            },
            {
              title: "库存",
              dataIndex: "stock",
              render: (v: string) => <Status value={v} />,
            },
            {
              title: "纳入基准",
              render: (_: any, p: Row) => (
                <Switch
                  size="small"
                  checked={p.baseline}
                  onChange={(baseline) =>
                    action(() =>
                      put(`/entities/product/${p.id}`, {
                        baseline,
                        version: p.version,
                      }),
                    ).catch(() => {})
                  }
                />
              ),
            },
            {
              title: "操作",
              render: (_: any, p: Row) => (
                <Button
                  size="small"
                  type="link"
                  onClick={() => setIds(ids.filter((i) => i !== p.id))}
                >
                  移除
                </Button>
              ),
            },
          ]}
          locale={{
            emptyText: (
              <Blank
                title="选择至少一个商品开始比较"
                text="可以比较不同硬件，也可以跟踪同款在不同店铺的价格。"
              />
            ),
          }}
        />
      </Panel>
    </>
  );
}
