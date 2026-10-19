import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  App,
  Alert,
  Badge,
  Button,
  Checkbox,
  DatePicker,
  Descriptions,
  Drawer,
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
  Activity,
  ArrowRight,
  ArrowUpRight,
  Bell,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  Filter,
  History,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  Undo2,
  X,
} from "lucide-react";
import dayjs from "dayjs";
import { CollectionDiagnostics } from "./diagnostics";
import { CollectAllButton } from "./collect-all";
import { CollectionFrequencyButton } from "./collection-frequency";
import { ForecastModelPanel } from "./forecast";
import {
  ContextHelp,
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
  labels,
  money,
  opts,
  pagination,
  patch,
  pct,
  post,
  put,
  remove,
  statusNames,
  useMonitor,
  useResource,
} from "./lib";

const conditions: Row = {
  target: "目标价",
  drop: "窗口内降幅",
  low: "窗口内历史新低",
  restock: "缺货转有货",
  event: "大促前汇总",
};
const channelOptions = [
  { label: "站内通知", value: "inapp" },
  { label: "Bark", value: "bark" },
  { label: "Server酱", value: "serverchan" },
  { label: "企业微信", value: "wecom" },
  { label: "飞书", value: "feishu" },
  { label: "邮件", value: "email" },
];
export function RuleEditor({
  open,
  onClose,
  rule,
  productId,
}: {
  open: boolean;
  onClose: () => void;
  rule?: Row | null;
  productId?: string;
}) {
  const { data, action } = useMonitor();
  const [form] = Form.useForm();
  const [preview, setPreview] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const { message } = App.useApp();
  const scope = Form.useWatch("scope", form) || "all";
  const condition = Form.useWatch("condition", form) || "target";
  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue(
        rule || {
          name: "",
          enabled: true,
          scope: productId ? "product" : "all",
          scope_value: productId || "",
          condition: "target",
          value: 1000,
          days: 7,
          cooldown_minutes: 60,
          confirmations: 2,
          include_coupon: false,
          channels: ["inapp"],
        },
      );
      setPreview(null);
    }
  }, [open, rule, productId, form]);
  const values =
    scope === "product"
      ? data.products.map((p: Row) => ({ label: p.name, value: p.id }))
      : scope === "category"
        ? opts(categories)
        : scope === "group"
          ? opts([
              ...new Set(data.products.map((p: Row) => p.group)),
            ] as string[])
          : scope === "tag"
            ? opts([
                ...new Set(data.products.flatMap((p: Row) => p.tags)),
              ] as string[])
            : [];
  const save = async (v: Row, template = false) => {
    setBusy(true);
    try {
      const body = {
        ...v,
        scope_value: v.scope === "all" ? "" : v.scope_value,
        template,
      };
      await action(
        () =>
          rule?.id && !template
            ? put(`/entities/rule/${rule.id}`, {
                ...body,
                version: rule.version,
              })
            : post("/entities/rule", body),
        template ? "已保存为模板" : "提醒规则已保存",
      );
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Drawer
      title={rule ? "编辑提醒规则" : "新建提醒规则"}
      open={open}
      onClose={onClose}
      width={530}
      extra={
        <Button type="primary" loading={busy} onClick={() => form.submit()}>
          保存规则
        </Button>
      }
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(v) => save(v)}
        onValuesChange={() => setPreview(null)}
      >
        <Form.Item
          name="name"
          label="规则名称"
          rules={[{ required: true, message: "请输入规则名称" }]}
        >
          <Input placeholder="例如：显卡降到目标价时通知我" maxLength={200} />
        </Form.Item>
        <div className="form-section-title">
          <span>01</span> 监控对象
        </div>
        <Form.Item name="scope">
          <Select
            options={[
              { label: "全部商品", value: "all" },
              { label: "单个商品", value: "product" },
              { label: "分组", value: "group" },
              { label: "标签", value: "tag" },
              { label: "品类", value: "category" },
            ]}
            onChange={() => form.setFieldValue("scope_value", "")}
          />
        </Form.Item>
        {scope !== "all" && (
          <Form.Item
            name="scope_value"
            rules={[{ required: true, message: "请选择监控对象" }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择监控对象"
              options={values}
            />
          </Form.Item>
        )}
        <div className="form-section-title">
          <span>02</span> 触发条件
        </div>
        <Form.Item name="condition">
          <Select
            options={Object.entries(conditions).map(([value, label]) => ({
              value,
              label,
            }))}
          />
        </Form.Item>
        <div className="form-grid">
          {["target", "drop"].includes(condition) && (
            <Form.Item
              name="value"
              label={condition === "target" ? "目标价（元）" : "降幅（%）"}
              rules={[{ required: true }]}
            >
              <InputNumber
                min={0.01}
                max={condition === "drop" ? 100 : 100000000}
                style={{ width: "100%" }}
              />
            </Form.Item>
          )}
          {["drop", "low", "event"].includes(condition) && (
            <Form.Item
              name="days"
              label={condition === "event" ? "提前多少天" : "时间窗口（天）"}
              rules={[{ required: true }]}
            >
              <InputNumber
                min={1}
                max={3650}
                precision={0}
                style={{ width: "100%" }}
              />
            </Form.Item>
          )}
        </div>
        {!["restock", "event"].includes(condition) && (
          <>
            <Form.Item name="confirmations" label="新价连续一致次数">
              <Select
                options={[
                  { label: "1 次，立即触发", value: 1 },
                  { label: "2 次，降低误报", value: 2 },
                  { label: "3 次，更保守", value: 3 },
                ]}
              />
            </Form.Item>
            <Form.Item name="include_coupon" valuePropName="checked">
              <Checkbox>计入需领券和会员价格</Checkbox>
            </Form.Item>
          </>
        )}
        <div className="form-section-title">
          <span>03</span> 冷却与渠道
        </div>
        <Form.Item name="cooldown_minutes" label="同一商品冷却时间（分钟）">
          <InputNumber min={0} max={525600} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item
          name="channels"
          rules={[{ required: true, message: "至少选择一个渠道" }]}
        >
          <Checkbox.Group options={channelOptions} />
        </Form.Item>
        <p className="muted small">
          站内通知始终保留。外部渠道需要先在设置中配置地址或邮箱。
        </p>
        <Form.Item name="enabled" label="启用规则" valuePropName="checked">
          <Switch />
        </Form.Item>
        <div className="rule-preview">
          <strong>历史触发预览</strong>
          <p>
            {preview
              ? `过去 90 天将触发 ${preview.triggers} 次，涉及 ${preview.products} 个商品。`
              : "用真实历史观测模拟，保存之前先了解规则触发频率。"}
          </p>
          <Button
            size="small"
            onClick={async () => {
              try {
                const v = await form.validateFields();
                setPreview(await post("/rules/preview", v));
              } catch (e: any) {
                if (e.message) message.error(e.message);
              }
            }}
          >
            预览历史触发
          </Button>
        </div>
        <Space className="mt20">
          <Button type="primary" loading={busy} htmlType="submit">
            保存规则
          </Button>
          <Button
            onClick={async () => {
              try {
                save(await form.validateFields(), true);
              } catch {}
            }}
          >
            另存为模板
          </Button>
          <Button onClick={onClose}>取消</Button>
        </Space>
      </Form>
    </Drawer>
  );
}

export function Alerts() {
  const { data, action } = useMonitor();
  const navigate = useNavigate();
  const [editing, setEditing] = useState<Row | null>(null);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState<string>();
  const { value: deliveries } = useResource<Row[]>("/deliveries", []);
  const rules: Row[] = data.rules.filter((r: Row) => !r.template);
  const channels = data.settings.channels || {};
  const { modal } = App.useApp();
  const describe = (r: Row) =>
    r.condition === "target"
      ? `≤ ${money(Math.round(r.value * 100))}`
      : r.condition === "drop"
        ? `${r.days} 天内 ≥ ${r.value}%`
        : r.condition === "low"
          ? `${r.days} 天新低`
          : r.condition === "event"
            ? `提前 ${r.days} 天`
            : "缺货 → 有货";
  return (
    <>
      <PageTitle
        eyebrow="NEVER MISS THE RIGHT PRICE"
        title="提醒中心"
        description="设好条件，合适的价格会主动找到你。"
        actions={
          <>
            <Dropdown
              menu={{
                items: data.rules
                  .filter((r: Row) => r.template)
                  .map((r: Row) => ({ label: r.name, key: r.id })),
                onClick: ({ key }) => {
                  const t = data.rules.find((r: Row) => r.id === key);
                  setEditing({
                    ...t,
                    id: null,
                    version: null,
                    name: `${t.name} · 副本`,
                    template: false,
                  });
                  setOpen(true);
                },
              }}
            >
              <Button icon={<Copy size={14} />}>
                规则模板 <ChevronRight size={13} />
              </Button>
            </Dropdown>
            <Button
              type="primary"
              icon={<Plus size={15} />}
              onClick={() => {
                setEditing(null);
                setOpen(true);
              }}
            >
              新建规则
            </Button>
          </>
        }
      />
      <div className="overview-stats three">
        <Metric
          label="已启用规则"
          value={rules.filter((r) => r.enabled).length}
          hint={`${rules.length} 条规则已配置`}
          icon={<Bell size={17} />}
          accent
        />
        <Metric
          label="近 30 天触发"
          value={
            deliveries.filter(
              (d) =>
                d.channel === "inapp" &&
                dayjs(d.created_at).isAfter(dayjs().subtract(30, "day")),
            ).length
          }
          hint="去重后的站内提醒"
        />
        <Metric
          label="待处理投递"
          value={
            deliveries.filter((d) =>
              ["failed", "retry", "pending"].includes(d.status),
            ).length
          }
          hint="发送结果与失败原因可在下方查看"
        />
      </div>
      <Panel
        title="提醒规则"
      >
        <Table
          rowKey="id"
          dataSource={rules}
          scroll={{ x: 1000 }}
          pagination={pagination}
          columns={[
            {
              title: "启用",
              width: 65,
              render: (_: any, r: Row) => (
                <Switch
                  size="small"
                  checked={r.enabled}
                  onChange={(enabled) =>
                    action(() =>
                      put(`/entities/rule/${r.id}`, {
                        enabled,
                        version: r.version,
                      }),
                    ).catch(() => {})
                  }
                />
              ),
            },
            {
              title: "规则名称",
              dataIndex: "name",
              width: 220,
              render: (v: string) => <strong>{v}</strong>,
            },
            {
              title: "监控对象",
              render: (_: any, r: Row) =>
                r.scope === "all"
                  ? "全部商品"
                  : r.scope === "product"
                    ? data.products.find((p: Row) => p.id === r.scope_value)
                        ?.name || "商品已删除"
                    : `${({ group: "分组", tag: "标签", category: "品类" } as Row)[r.scope]}：${r.scope_value}`,
            },
            {
              title: "触发条件",
              render: (_: any, r: Row) => (
                <span>
                  {conditions[r.condition]}
                  <small className="cell-sub">{describe(r)}</small>
                </span>
              ),
            },
            {
              title: "渠道",
              render: (_: any, r: Row) => (
                <Space size={3} wrap>
                  {r.channels.map((c: string) => (
                    <Tag key={c} bordered={false}>
                      {statusNames[c]}
                    </Tag>
                  ))}
                </Space>
              ),
            },
            {
              title: "冷却",
              dataIndex: "cooldown_minutes",
              render: (v: number) => `${v} 分钟`,
            },
            {
              title: "操作",
              render: (_: any, r: Row) => (
                <Space size={2}>
                  <Button
                    size="small"
                    type="link"
                    onClick={() => {
                      setEditing(r);
                      setOpen(true);
                    }}
                  >
                    编辑
                  </Button>
                  <Button
                    size="small"
                    type="link"
                    onClick={() => {
                      setEditing({
                        ...r,
                        id: null,
                        version: null,
                        name: r.name + " · 副本",
                      });
                      setOpen(true);
                    }}
                  >
                    复制
                  </Button>
                  <Popconfirm
                    title="删除规则？"
                    onConfirm={() =>
                      action(
                        () => remove(`/entities/rule/${r.id}`),
                        "规则已移至回收站",
                      )
                    }
                  >
                    <Button size="small" type="link" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
          locale={{
            emptyText: (
              <Blank
                title="让价格提醒替你盯着"
                text="目标价、降幅、监测新低和到货，按自己的购买节奏设置。"
                action={
                  <Button onClick={() => setOpen(true)}>创建第一条规则</Button>
                }
              />
            ),
          }}
        />
      </Panel>
      <div className="two-column mt20">
        <Panel
          title="触发与投递历史"
          extra={
            <Select
              allowClear
              value={status}
              onChange={setStatus}
              placeholder="全部状态"
              style={{ width: 120 }}
              options={opts(["sent", "pending", "retry", "failed"]).map(
                (o) => ({ ...o, label: statusNames[o.value] }),
              )}
            />
          }
        >
          <Table
            rowKey="id"
            dataSource={deliveries.filter(
              (d) => !status || d.status === status,
            )}
            pagination={pagination}
            scroll={{ x: 650 }}
            columns={[
              {
                title: "时间",
                dataIndex: "created_at",
                width: 110,
                render: (v: string) => date(v),
              },
              {
                title: "提醒内容",
                dataIndex: "message",
                width: 260,
                render: (v: string, r: Row) => (
                  <div>
                    {v}
                    {r.error && (
                      <small className="cell-sub warning-text">{r.error}</small>
                    )}
                  </div>
                ),
              },
              {
                title: "渠道",
                dataIndex: "channel",
                render: (v: string) => statusNames[v],
              },
              {
                title: "状态",
                dataIndex: "status",
                render: (v: string) => <Status value={v} />,
              },
              {
                title: "操作",
                render: (_: any, r: Row) => (
                  <Space size={0}>
                    <Button
                      size="small"
                      type="link"
                      onClick={() => navigate(`/products/${r.product_id}`)}
                    >
                      商品
                    </Button>
                    {r.channel !== "inapp" && (
                      <Button
                        size="small"
                        type="link"
                        onClick={() =>
                          action(
                            () => post(`/deliveries/${r.id}/retry`),
                            "已加入重发队列",
                          ).catch(() => {})
                        }
                      >
                        重发
                      </Button>
                    )}
                  </Space>
                ),
              },
            ]}
          />
        </Panel>
        <Panel
          title="通知渠道"
          extra={
            <Button
              type="text"
              size="small"
              icon={<Settings2 size={14} />}
              onClick={() => navigate("/settings?tab=notifications")}
            >
              配置
            </Button>
          }
        >
          <div className="channel-list">
            {channelOptions.map((c) => (
              <div key={c.value}>
                <div className={`channel-icon channel-${c.value}`}>
                  <Bell size={17} />
                </div>
                <div>
                  <strong>{c.label}</strong>
                  <small>
                    {c.value === "inapp"
                      ? "本地通知中心"
                      : channels[c.value]?.configured
                        ? "已保存连接配置"
                        : "尚未配置"}
                  </small>
                </div>
                {c.value === "inapp" ? (
                  <Tag color="success" bordered={false}>
                    可用
                  </Tag>
                ) : (
                  <Button
                    size="small"
                    disabled={!channels[c.value]?.enabled}
                    onClick={() =>
                      action(
                        () => post(`/channels/${c.value}/test`),
                        "测试通知已发送",
                      ).catch(() => {})
                    }
                  >
                    测试发送
                  </Button>
                )}
              </div>
            ))}
          </div>
          <div className="setting-note">
            <Clock3 size={15} />
            <div>
              <strong>免打扰时段</strong>
              <p>
                {data.settings.quiet_start} — {data.settings.quiet_end}
                ，期间外部通知排队，站内提醒正常记录。
              </p>
            </div>
          </div>
        </Panel>
      </div>
      <RuleEditor
        open={open}
        rule={editing}
        onClose={() => {
          setOpen(false);
          setEditing(null);
        }}
      />
      {/* Copy opens a fresh editor using a template with no identity. */}
    </>
  );
}

const eventTypes = ["大促", "新品发布", "官宣降价", "供应变化", "政策窗口"];
const eventColors: Row = {
  大促: "#e2a239",
  新品发布: "#6687ed",
  官宣降价: "#22a589",
  供应变化: "#8e99af",
  政策窗口: "#a27cdb",
};
export function EventEditor({
  open,
  onClose,
  event,
  start,
}: {
  open: boolean;
  onClose: () => void;
  event?: Row | null;
  start?: string;
}) {
  const { data, action } = useMonitor();
  const [form] = Form.useForm();
  const scope = Form.useWatch("scope", form) || "all";
  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue(
        event
          ? { ...event, start: dayjs(event.start), end: dayjs(event.end) }
          : {
              type: "大促",
              confidence: "官宣",
              scope: "all",
              start: dayjs(start),
              end: dayjs(start),
            },
      );
    }
  }, [open, event, start, form]);
  const options =
    scope === "product"
      ? data.products.map((p: Row) => ({ label: p.name, value: p.id }))
      : scope === "category"
        ? opts(categories)
        : opts([
            ...new Set(data.products.map((p: Row) => p.brand).filter(Boolean)),
          ] as string[]);
  return (
    <Drawer
      width={500}
      open={open}
      onClose={onClose}
      title={event ? "编辑事件" : "新增日历事件"}
      extra={
        <Button type="primary" onClick={() => form.submit()}>
          保存事件
        </Button>
      }
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={async (v) => {
          const body = {
            ...v,
            start: v.start.format("YYYY-MM-DD"),
            end: v.end.format("YYYY-MM-DD"),
            scope_value: v.scope === "all" ? "" : v.scope_value,
          };
          await action(
            () =>
              event?.id
                ? put(`/entities/event/${event.id}`, {
                    ...body,
                    version: event.version,
                  })
                : post("/entities/event", body),
            "日历事件已保存",
          );
          onClose();
        }}
      >
        <Form.Item name="type" label="事件类型">
          <Select options={opts(eventTypes)} />
        </Form.Item>
        <Form.Item
          name="name"
          label="事件名称"
          rules={[{ required: true, message: "请输入事件名称" }]}
        >
          <Input placeholder="例如：双 11 显卡促销窗口" />
        </Form.Item>
        <div className="form-grid">
          <Form.Item name="start" label="开始日期" rules={[{ required: true }]}>
            <DatePicker style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="end" label="结束日期" rules={[{ required: true }]}>
            <DatePicker style={{ width: "100%" }} />
          </Form.Item>
        </div>
        <Form.Item name="scope" label="作用范围">
          <Select
            options={[
              { label: "全部商品", value: "all" },
              { label: "指定商品", value: "product" },
              { label: "品类", value: "category" },
              { label: "品牌", value: "brand" },
            ]}
            onChange={() => form.setFieldValue("scope_value", "")}
          />
        </Form.Item>
        {scope !== "all" && (
          <Form.Item
            name="scope_value"
            rules={[{ required: true, message: "请选择作用对象" }]}
          >
            <Select showSearch options={options} optionFilterProp="label" />
          </Form.Item>
        )}
        <Form.Item name="confidence" label="置信度">
          <Segmented options={["官宣", "传闻", "待确认"]} />
        </Form.Item>
        <Form.Item name="source" label="信息来源">
          <Input placeholder="公告链接或来源说明" />
        </Form.Item>
        <Form.Item name="notes" label="备注">
          <Input.TextArea rows={4} />
        </Form.Item>
        <ContextHelp title="事件与预测">得知时间由系统记录，防止回测使用未来信息。传闻与待确认事件不参与趋势判断。</ContextHelp>
      </Form>
    </Drawer>
  );
}

export function CalendarPage() {
  const { data, action } = useMonitor();
  const [month, setMonth] = useState(dayjs().startOf("month"));
  const [mode, setMode] = useState("month");
  const [types, setTypes] = useState(eventTypes);
  const [editor, setEditor] = useState(false);
  const [event, setEvent] = useState<Row | null>(null);
  const [start, setStart] = useState<string>();
  const [dragging, setDragging] = useState<Row | null>(null);
  const [importing, setImporting] = useState(false);
  const events: Row[] = data.events.filter(
    (e: Row) => types.includes(e.type) && !e.ignored,
  );
  const weekday = (month.day() + 6) % 7;
  const first = month.subtract(weekday, "day");
  const days = Array.from({ length: 42 }, (_, i) => first.add(i, "day"));
  const pending = events.filter((e) => e.confidence === "待确认");
  const today = dayjs().format("YYYY-MM-DD");
  const drop = async (day: dayjs.Dayjs) => {
    if (!dragging) return;
    const duration = dayjs(dragging.end).diff(dayjs(dragging.start), "day");
    await action(
      () =>
        put(`/entities/event/${dragging.id}`, {
          start: day.format("YYYY-MM-DD"),
          end: day.add(duration, "day").format("YYYY-MM-DD"),
          version: dragging.version,
        }),
      "日期已更新",
    ).catch(() => {});
    setDragging(null);
  };
  return (
    <>
      <PageTitle
        eyebrow="TIMING MATTERS"
        title="事件日历"
        description="把大促、新品与供应变化，放回价格变化的时间线上。"
        actions={
          <>
            <Button
              onClick={() => setImporting(true)}
              icon={<Download size={14} />}
            >
              导入事件
            </Button>
            <Button
              onClick={() =>
                action(
                  () => post("/events/generate", { year: month.year() }),
                  "固定日期参考已生成，确认公告后可启用",
                )
              }
            >
              生成当年固定日期
            </Button>
            <Button
              type="primary"
              icon={<Plus size={15} />}
              onClick={() => {
                setEvent(null);
                setStart(today);
                setEditor(true);
              }}
            >
              新增事件
            </Button>
          </>
        }
      />
      <div className="calendar-toolbar">
        <Checkbox.Group
          value={types}
          onChange={(v) => setTypes(v as string[])}
          options={eventTypes.map((t) => ({
            label: (
              <span>
                <i
                  className="legend-dot"
                  style={{ background: eventColors[t] }}
                />
                {t}
              </span>
            ),
            value: t,
          }))}
        />
        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { label: "月视图", value: "month" },
            { label: "全年时间线", value: "timeline" },
          ]}
        />
      </div>
      <div className="calendar-grid-layout">
        <Panel className="calendar-panel">
          <div className="calendar-nav">
            <Space>
              <Button
                type="text"
                icon={<ChevronLeft size={17} />}
                onClick={() =>
                  setMonth(month.subtract(mode === "month" ? 1 : 12, "month"))
                }
              />
              <h2>
                {month.format(mode === "month" ? "YYYY 年 M 月" : "YYYY 年")}
              </h2>
              <Button
                type="text"
                icon={<ChevronRight size={17} />}
                onClick={() =>
                  setMonth(month.add(mode === "month" ? 1 : 12, "month"))
                }
              />
              <Button
                size="small"
                onClick={() => setMonth(dayjs().startOf("month"))}
              >
                今天
              </Button>
            </Space>
            <span className="muted small">
              点击事件编辑 · 拖动调整日期 · 虚线表示传闻
            </span>
          </div>
          {mode === "month" ? (
            <>
              <div className="calendar-week">
                {["一", "二", "三", "四", "五", "六", "日"].map((d) => (
                  <span key={d}>周{d}</span>
                ))}
              </div>
              <div className="calendar-days">
                {days.map((day) => {
                  const stamp = day.format("YYYY-MM-DD");
                  const active = events.filter(
                    (e) => e.start <= stamp && e.end >= stamp,
                  );
                  return (
                    <div
                      key={stamp}
                      className={`calendar-day ${day.month() !== month.month() ? "other-month" : ""} ${stamp === today ? "today" : ""}`}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={() => drop(day)}
                      onDoubleClick={() => {
                        setEvent(null);
                        setStart(stamp);
                        setEditor(true);
                      }}
                    >
                      <button
                        className="day-number"
                        onClick={() => {
                          setEvent(null);
                          setStart(stamp);
                          setEditor(true);
                        }}
                      >
                        {day.date()}
                      </button>
                      {active.slice(0, 3).map((e) => (
                        <button
                          draggable
                          onDragStart={() => setDragging(e)}
                          key={e.id}
                          title={e.name}
                          className={`calendar-event ${e.confidence === "传闻" ? "rumor" : ""}`}
                          style={{
                            background: `${eventColors[e.type]}18`,
                            color: eventColors[e.type],
                            borderColor: eventColors[e.type],
                          }}
                          onClick={() => {
                            setEvent(e);
                            setEditor(true);
                          }}
                        >
                          <span />
                          {e.name}
                        </button>
                      ))}
                      {active.length > 3 && (
                        <small>+{active.length - 3} 个事件</small>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <div className="year-timeline">
              <div className="timeline-months">
                <span />
                {Array.from({ length: 12 }, (_, i) => (
                  <b key={i}>{i + 1} 月</b>
                ))}
              </div>
              {eventTypes
                .filter((t) => types.includes(t))
                .map((type) => (
                  <div className="timeline-lane" key={type}>
                    <span>{type}</span>
                    <div className="timeline-track">
                      {Array.from({ length: 12 }, (_, i) => (
                        <div
                          className="timeline-drop"
                          key={i}
                          onDragOver={(e) => e.preventDefault()}
                          onDrop={() => drop(month.month(i).startOf("month"))}
                        />
                      ))}
                      {events
                        .filter(
                          (e) =>
                            e.type === type &&
                            dayjs(e.start).year() === month.year(),
                        )
                        .map((e, i) => {
                          const yearStart = month.startOf("year");
                          const total = yearStart
                            .add(1, "year")
                            .diff(yearStart, "day");
                          return (
                            <button
                              key={e.id}
                              draggable
                              onDragStart={() => setDragging(e)}
                              onClick={() => {
                                setEvent(e);
                                setEditor(true);
                              }}
                              style={{
                                left: `${(dayjs(e.start).diff(yearStart, "day") / total) * 100}%`,
                                width: `${Math.max(6, ((dayjs(e.end).diff(dayjs(e.start), "day") + 1) / total) * 100)}%`,
                                top: 12 + (i % 3) * 28,
                                background: `${eventColors[type]}22`,
                                color: eventColors[type],
                              }}
                            >
                              {e.name}
                            </button>
                          );
                        })}
                    </div>
                  </div>
                ))}
            </div>
          )}
        </Panel>
        <div className="calendar-side">
          <Panel
            title="待确认事件"
            extra={
              <Tag bordered={false} color="gold">
                {pending.length}
              </Tag>
            }
          >
            {pending.length ? (
              pending.map((e) => (
                <div className="pending-event" key={e.id}>
                  <Tag bordered={false}>{e.type}</Tag>
                  <strong>{e.name}</strong>
                  <small>
                    {date(e.known_at)} · {e.source || "未填写来源"}
                  </small>
                  <Space>
                    <Button
                      size="small"
                      onClick={() =>
                        action(
                          () =>
                            put(`/entities/event/${e.id}`, {
                              confidence: "官宣",
                              version: e.version,
                            }),
                          "事件已确认",
                        ).catch(() => {})
                      }
                    >
                      确认
                    </Button>
                    <Button
                      size="small"
                      type="text"
                      onClick={() => {
                        setEvent(e);
                        setEditor(true);
                      }}
                    >
                      编辑
                    </Button>
                    <Button
                      size="small"
                      type="text"
                      onClick={() =>
                        action(
                          () =>
                            put(`/entities/event/${e.id}`, {
                              ignored: true,
                              version: e.version,
                            }),
                          "事件已忽略",
                        ).catch(() => {})
                      }
                    >
                      忽略
                    </Button>
                  </Space>
                </div>
              ))
            ) : (
              <Blank
                title="没有待确认事件"
                text="导入的固定日期参考和待确认公告会出现在这里。"
              />
            )}
          </Panel>
          <ContextHelp title="日历操作说明">
            <p className="muted">
              双击日期格新增事件。拖动事件可平移日期，在编辑面板中调整起止时间。
            </p>
            <p className="muted">
              只有已确认事件参与趋势参考；实际活动日期以公告为准。
            </p>
          </ContextHelp>
        </div>
      </div>
      <Panel title="事件清单" className="mt20">
        <Table
          rowKey="id"
          dataSource={events}
          pagination={pagination}
          columns={[
            {
              title: "事件",
              dataIndex: "name",
              render: (v: string, r: Row) => (
                <Button
                  type="link"
                  onClick={() => {
                    setEvent(r);
                    setEditor(true);
                  }}
                >
                  {v}
                </Button>
              ),
            },
            { title: "类型", dataIndex: "type" },
            { title: "开始", dataIndex: "start" },
            { title: "结束", dataIndex: "end" },
            {
              title: "置信度",
              dataIndex: "confidence",
              render: (v: string) => (
                <Tag bordered={false} color={v === "官宣" ? "green" : "gold"}>
                  {v}
                </Tag>
              ),
            },
            {
              title: "操作",
              render: (_: any, r: Row) => (
                <Space>
                  <Button
                    type="link"
                    size="small"
                    onClick={() => {
                      setEvent({
                        ...r,
                        id: null,
                        version: null,
                        name: r.name + " · 副本",
                      });
                      setEditor(true);
                    }}
                  >
                    复制
                  </Button>
                  <Popconfirm
                    title="删除事件？"
                    onConfirm={() =>
                      action(
                        () => remove(`/entities/event/${r.id}`),
                        "事件已移至回收站",
                      )
                    }
                  >
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Panel>
      <EventEditor
        open={editor}
        event={event}
        start={start}
        onClose={() => {
          setEditor(false);
          setEvent(null);
        }}
      />
      <ImportDialog
        kind="event"
        open={importing}
        onClose={() => setImporting(false)}
      />
    </>
  );
}

export function Collection() {
  const { data, action } = useMonitor();
  const { value: logs } = useResource<Row[]>("/logs", []);
  const { value: jobs } = useResource<Row[]>("/jobs", []);
  const [form] = Form.useForm();
  const [dirty, setDirty] = useState(false);
  const [logProduct, setLogProduct] = useState<string>();
  const [diagnostic, setDiagnostic] = useState<Row | null>(null);
  const [level, setLevel] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [selected, setSelected] = useState<React.Key[]>([]);
  const { modal } = App.useApp();
  const h = data.health,
    config = data.settings;
  const circuitActive =
    h.circuit_until && dayjs(h.circuit_until).isAfter(dayjs());
  const products: Row[] = data.products;
  useEffect(() => {
    if (!dirty) form.setFieldsValue(config);
  }, [config, dirty, form]);
  const apply = async (v: Row) => {
    await action(() => patch("/settings", v), "采集策略已保存");
    setDirty(false);
  };
  return (
    <>
      <PageTitle
        eyebrow="COLLECTION CONTROL"
        title="采集管理"
        description="每次采集都有迹可循，失败也有明确的原因。"
        actions={
          <>
            <CollectAllButton />
            <CollectionFrequencyButton />
            <Button
              icon={<RefreshCw size={14} />}
              onClick={() =>
                action(
                  () => post("/collection/retry"),
                  "失败商品已加入队列",
                ).catch(() => {})
              }
            >
              重试失败商品
            </Button>
            <Button
              type={h.paused ? "primary" : "default"}
              icon={h.paused ? <Play size={14} /> : <Pause size={14} />}
              onClick={() =>
                action(
                  () => post(`/collection/${h.paused ? "resume" : "pause"}`),
                  h.paused ? "已恢复定时采集" : "已停止定时采集，手动采集仍可执行",
                ).catch(() => {})
              }
            >
              {h.paused ? "恢复定时采集" : "停止定时采集"}
            </Button>
          </>
        }
      />
      <div className="overview-stats">
        <Metric
          label="24 小时成功率"
          value={h.rate == null ? "—" : `${h.rate}%`}
          hint={`${h.success || 0} / ${h.total || 0} 次采集成功`}
          accent
        />
        <Metric
          label="最近成功"
          value={<span className="metric-time">{ago(h.last_success)}</span>}
          hint={date(h.last_success)}
        />
        <Metric
          label="连续失败商品"
          value={products.filter((p) => p.failures > 0).length}
        />
        <Metric
          label="队列中任务"
          value={h.queued || 0}
          hint={`正在采集 ${h.running?.length || 0} / ${h.max_concurrency || 5} · 等待 ${h.waiting || 0}`}
        />
      </div>
      {h.waiting > 0 && h.queue && (["cookie_missing", "login_open", "verification_required", "session_expired", "circuit", "source_switch", "server_capacity"].includes(h.queue.reason) || ((h.queue.wait_seconds || 0) >= 60 && !["paused", "batch_cooldown", "previous_batch"].includes(h.queue.reason))) && <Alert className="mb20" showIcon type="warning" message={h.queue.message} />}
      {h.batch && (
        <div className="collection-progress" role="status">
          <span>{`一键采集：已完成 ${h.batch.completed} / ${h.batch.total} 个，正在采集 ${h.batch.running} 个，等待 ${h.batch.waiting} 个`}</span>
          {h.batch.next_batch_at && <span>下一批 {date(h.batch.next_batch_at, "HH:mm:ss")}</span>}
        </div>
      )}
      <Panel title="数据源与登录状态">
        <div className="source-row">
          <div className="source-logo">{config.price_source === "api" ? "API" : "淘"}</div>
          <div>
            <strong>{config.price_source === "api" ? "淘宝 / 京东 · 官方 API" : "淘宝 / 天猫"}</strong>
            <small>{config.price_source === "api" ? "官方签名接口 · 爬虫已暂停 · 失败不回退" : "DOM 页面采集 · 官方 API 分支已暂停"}</small>
          </div>
          <Tag
            bordered={false}
            color={circuitActive ? "warning" : h.paused ? "default" : "green"}
          >
            {circuitActive ? "自动采集熔断" : h.paused ? "定时采集已暂停" : "正常"}
          </Tag>
          <div className="source-meta">
            <span>
              连续失败 <b>{h.failures || 0}</b>
            </span>
            <span>
              冷却结束 <b>{date(h.circuit_until)}</b>
            </span>
          </div>
          <Button
            size="small"
            onClick={() =>
              action(() => post("/collection/probe"), "已放行探测任务").catch(
                () => {},
              )
            }
          >
            立即探测
          </Button>
          <Tooltip title="清除熔断冷却与连续失败计数，调度仍遵循全局暂停和登录状态。">
            <Button
              size="small"
              disabled={!circuitActive}
              onClick={() =>
                action(
                  () => post("/collection/uncircuit"),
                  "熔断已解除，连续失败计数已清零",
                ).catch(() => {})
              }
            >
              解除熔断
            </Button>
          </Tooltip>
          <Button
            size="small"
            danger
            onClick={() =>
              action(() => post("/collection/circuit"), "已手动熔断").catch(
                () => {},
              )
            }
          >
            手动熔断
          </Button>
        </div>
        <div className="session-banner">
          <ShieldCheck size={16} />
          <span>{config.price_source === "api" ? "当前使用本账号的官方 API 凭证，不依赖淘宝 Cookie。" : data.session.message}</span>
          <Link to={`/settings?tab=${config.price_source === "api" ? "source" : "account"}`}>
            {config.price_source === "api" ? "管理 API" : "管理登录"} <ArrowUpRight size={12} />
          </Link>
        </div>
      </Panel>
      <div className="collection-layout mt20">
        <div>
          <Panel
            title="商品采集状态"
            extra={
              <Space>
                <Select
                  placeholder="全部结果"
                  allowClear
                  value={status}
                  onChange={setStatus}
                  options={[...new Set(products.map((p) => p.last_status))].map(
                    (v) => ({ label: statusNames[v] || v, value: v }),
                  )}
                  style={{ width: 150 }}
                />
                <Button
                  disabled={!selected.length}
                  size="small"
                  onClick={() =>
                    action(
                      () =>
                        post("/products/bulk", {
                          ids: selected,
                          action: "crawl",
                        }),
                      "已加入采集队列",
                    ).catch(() => {})
                  }
                >
                  {config.price_source === "api" ? "API 采集已选" : "重抓已选"}
                </Button>
              </Space>
            }
          >
            <Table
              rowKey="id"
              dataSource={products.filter(
                (p) => !status || p.last_status === status,
              )}
              scroll={{ x: 1000 }}
              rowSelection={{
                selectedRowKeys: selected,
                onChange: setSelected,
              }}
              pagination={pagination}
              columns={[
                {
                  title: "商品",
                  width: 180,
                  render: (_: any, p: Row) => (
                    <Link
                      to={`/products/${p.id}`}
                      className="table-product-name"
                    >
                      {p.name}
                    </Link>
                  ),
                },
                {
                  title: "间隔",
                  render: (_: any, p: Row) =>
                    p.paused ? "已暂停" : `${p.interval_hours} 小时`,
                },
                {
                  title: "最近结果",
                  dataIndex: "last_status",
                  render: (v: string, p: Row) => (
                    <>
                      <Status value={v} />
                      {p.last_diagnostics?.message && v !== "success" && (
                        <small className="cell-sub warning-text">
                          {p.last_diagnostics.message}
                        </small>
                      )}
                    </>
                  ),
                },
                { title: "连续失败", dataIndex: "failures" },
                { title: "最后成功", dataIndex: "last_success", render: ago },
                {
                  title: "下次采集",
                  render: (_: any, p: Row) =>
                    p.paused ? "—" : date(p.next_run, "HH:mm"),
                },
                {
                  title: "操作",
                  width: 170,
                  render: (_: any, p: Row) => (
                    <Space size={0}>
                      <Button
                        size="small"
                        type="link"
                        disabled={!p.url}
                        onClick={() =>
                          action(
                            () => post(`/product/${p.id}/crawl`),
                            "已加入队列",
                          ).catch(() => {})
                        }
                      >
                        {config.price_source === "api" ? "API 采集" : "重抓"}
                      </Button>
                      <Button
                        size="small"
                        type="link"
                        onClick={() =>
                          action(
                            () =>
                              put(`/entities/product/${p.id}`, {
                                paused: !p.paused,
                                version: p.version,
                              }),
                            "状态已更新",
                          ).catch(() => {})
                        }
                      >
                        {p.paused ? "恢复" : "暂停"}
                      </Button>
                      <Button
                        size="small"
                        type="link"
                        onClick={() => {
                          setLogProduct(p.id);
                          if (p.last_diagnostics)
                            setDiagnostic(p.last_diagnostics);
                        }}
                      >
                        日志
                      </Button>
                    </Space>
                  ),
                },
              ]}
              locale={{
                emptyText: (
                  <Blank
                    title="尚无采集商品"
                    text="在商品库添加链接后，调度状态会显示在这里。"
                  />
                ),
              }}
            />
            <div className="table-note">
              解析失败不会写入有效价格；登录失效或验证页需要完成验证并更新登录状态。
            </div>
          </Panel>
          <Panel
            title="采集日志"
            className="mt20"
            extra={
              <Button
                size="small"
                icon={<Download size={12} />}
                onClick={() =>
                  download(
                    `/export/logs?format=csv${logProduct ? "&product_id=" + logProduct : ""}`,
                  )
                }
              >
                导出
              </Button>
            }
          >
            <div className="filterbar compact">
              <Select
                allowClear
                placeholder="全部商品"
                value={logProduct}
                onChange={setLogProduct}
                style={{ width: 220 }}
                options={products.map((p) => ({ label: p.name, value: p.id }))}
              />
              <Select
                allowClear
                placeholder="全部级别"
                value={level}
                onChange={setLevel}
                style={{ width: 120 }}
                options={[
                  { label: "信息", value: "info" },
                  { label: "警告", value: "warning" },
                  { label: "错误", value: "error" },
                ]}
              />
              <span className="muted small">
                保留 {config.log_days} 天，最多展示最近 1000 条
              </span>
            </div>
            <Table
              rowKey="id"
              size="small"
              dataSource={logs.filter(
                (r) =>
                  (!logProduct || r.product_id === logProduct) &&
                  (!level || r.level === level),
              )}
              pagination={pagination}
              scroll={{ x: 750 }}
              columns={[
                {
                  title: "时间",
                  dataIndex: "created_at",
                  width: 145,
                  render: (v: string) => date(v, "MM-DD HH:mm:ss"),
                },
                {
                  title: "级别",
                  dataIndex: "level",
                  width: 70,
                  render: (v: string) => (
                    <Tag
                      bordered={false}
                      color={
                        v === "error"
                          ? "red"
                          : v === "warning"
                            ? "gold"
                            : "blue"
                      }
                    >
                      {v === "error"
                        ? "错误"
                        : v === "warning"
                          ? "警告"
                          : "信息"}
                    </Tag>
                  ),
                },
                {
                  title: "商品",
                  width: 150,
                  render: (_: any, r: Row) =>
                    products.find((p) => p.id === r.product_id)?.name || "系统",
                },
                { title: "信息", dataIndex: "message" },
                {
                  title: "详情",
                  width: 80,
                  render: (_: any, r: Row) =>
                    r.details ? (
                      <Button
                        type="link"
                        size="small"
                        onClick={() => setDiagnostic(r.details)}
                      >
                        查看详情
                      </Button>
                    ) : (
                      "—"
                    ),
                },
                {
                  title: "耗时",
                  width: 80,
                  render: (_: any, r: Row) =>
                    r.duration_ms
                      ? `${(r.duration_ms / 1000).toFixed(1)}s`
                      : "—",
                },
              ]}
            />
          </Panel>
          <Panel title="最近任务" className="mt20">
            <Table
              rowKey="id"
              dataSource={jobs}
              size="small"
              scroll={{ x: 900 }}
              pagination={pagination}
              columns={[
                {
                  title: "商品",
                  render: (_: any, r: Row) =>
                    products.find((p) => p.id === r.product_id)?.name ||
                    (r.purpose === "cookie_verification" ? "Cookie 校验" : "已删除商品"),
                },
                {
                  title: "入队时间",
                  dataIndex: "created_at",
                  render: (v: string) => date(v),
                },
                {
                  title: "状态",
                  dataIndex: "status",
                  render: (v: string, r: Row) => r.pacing ? <Tag color="blue">等待访问间隔</Tag> : <Status value={v} />,
                },
                { title: "尝试次数", dataIndex: "attempts" },
                { title: "排队说明", width: 240, render: (_: any, r: Row) => r.queue ? <><span>{r.queue.message}</span><small className="cell-sub">第 {r.queue.position || "—"} 位 · 已等待 {Math.floor(r.queue.wait_seconds / 60)} 分钟</small>{r.queue.blocks?.find((b: Row) => b.resume_at)?.resume_at && <small className="cell-sub">最早继续：{date(r.queue.blocks.find((b: Row) => b.resume_at).resume_at, "HH:mm:ss")}</small>}</> : r.pacing ? <><span>{r.pacing.message}</span>{r.pacing.resume_at && <small className="cell-sub">最早启动：{date(r.pacing.resume_at, "HH:mm:ss")}</small>}<small className="cell-sub">等待不计入 120 秒采集时限</small></> : "—" },
                {
                  title: "结果",
                  render: (_: any, r: Row) => (
                    <>
                      {r.error
                        ? statusNames[r.error] || r.error
                        : r.result
                          ? money(JSON.parse(r.result).price)
                          : "—"}
                      {r.diagnostics?.message && r.error && (
                        <small className="cell-sub warning-text">
                          {r.diagnostics.message}
                        </small>
                      )}
                    </>
                  ),
                },
                {
                  title: "诊断",
                  width: 90,
                  render: (_: any, r: Row) =>
                    r.diagnostics ? (
                      <Button
                        type="link"
                        size="small"
                        onClick={() => setDiagnostic(r.diagnostics)}
                      >
                        查看详情
                      </Button>
                    ) : (
                      "—"
                    ),
                },
                { title: "操作", render: (_: any, r: Row) => r.status === "queued" ? <Button type="link" size="small" onClick={() => action(() => post(`/jobs/${r.id}/cancel`), "排队任务已取消").catch(() => {})}>取消排队</Button> : "—" },
              ]}
            />
          </Panel>
        </div>
        <Modal
          title="采集诊断详情"
          open={!!diagnostic}
          onCancel={() => setDiagnostic(null)}
          footer={null}
          width={760}
          styles={{ body: { maxHeight: "72vh", overflowY: "auto" } }}
        >
          <CollectionDiagnostics details={diagnostic} />
        </Modal>
        <Panel title="采集策略" className="policy-panel">
          <Form
            form={form}
            layout="vertical"
            onValuesChange={() => setDirty(true)}
            onFinish={apply}
          >
            <div className="form-section-title">浏览器与渲染</div>
            {data.access?.role === "admin" && <Form.Item name="browser" label="采集浏览器">
              <Select
                options={[
                  { label: "Microsoft Edge", value: "msedge" },
                  { label: "Google Chrome", value: "chrome" },
                  { label: "Chromium", value: "chromium" },
                ]}
              />
            </Form.Item>}
            <Form.Item name="wait_seconds" label="页面渲染等待（秒）">
              <InputNumber min={0} max={120} style={{ width: "100%" }} />
            </Form.Item>
            {data.access?.role === "admin" && <Form.Item name="crawler_headed" label="显示采集浏览器窗口" valuePropName="checked"
              extra="在采集端显示浏览器窗口。">
              <Switch checkedChildren="显示窗口" unCheckedChildren="无窗口" />
            </Form.Item>}
            <div className="form-section-title">限速与重试</div>
            <ContextHelp title="采集限额与执行规则">每个账号最多并发 5 个，普通用户合计最多 50 个，管理员独立计数。采集限时 120 秒，超时终止；排队和启动等待不计入时限。一键采集每批最多 5 个，批次间隔 8 秒。暂停和熔断仅影响自动采集，人工采集始终可以执行。</ContextHelp>
            <Form.Item
              name="max_concurrency"
              label="最大并发采集数"
            >
              <InputNumber
                min={1}
                max={5}
                precision={0}
                style={{ width: "100%" }}
              />
            </Form.Item>
            <Form.Item
              name="crawler_start_gap"
              label="同账号商品启动间隔（秒）"
            >
              <InputNumber min={1} max={120} precision={0} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item
              name="request_gap"
              label="每个并发名额的采集间隔（秒，另加随机抖动）"
            >
              <InputNumber min={1} max={300} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="max_retries" label="失败最多重试次数">
              <InputNumber
                min={0}
                max={3}
                precision={0}
                style={{ width: "100%" }}
              />
            </Form.Item>
            <div className="form-section-title">熔断保护</div>
            <div className="form-grid">
              <Form.Item name="circuit_failures" label="连续失败次数">
                <InputNumber min={1} max={100} />
              </Form.Item>
              <Form.Item name="circuit_hours" label="冷却小时">
                <InputNumber min={0.01} max={72} />
              </Form.Item>
            </div>
            <Form.Item name="anomaly_percent" label="偏离近期价多少 % 先待确认">
              <InputNumber min={1} max={1000} style={{ width: "100%" }} />
            </Form.Item>
            <div className="form-section-title">大促期加密采集</div>
            <Form.Item
              name="event_boost"
              label="启用事件窗口加密"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
            <Form.Item name="event_boost_days" label="活动前后天数">
              <InputNumber min={0} max={30} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item
              name="event_interval_minutes"
              label="活动采集间隔（分钟）"
            >
              <InputNumber min={6} max={1440} style={{ width: "100%" }} />
            </Form.Item>
            <Button type="primary" htmlType="submit" block disabled={!dirty}>
              保存采集策略
            </Button>
            <Button
              className="mt12"
              block
              onClick={() => {
                form.setFieldsValue({
                  ...(data.access?.role === "admin" ? { browser: "msedge", crawler_headed: false } : {}),
                  crawler_start_gap: 5,
                  wait_seconds: 12,
                  request_gap: 8,
                  max_retries: 1,
                  max_concurrency: 5,
                  circuit_failures: 5,
                  circuit_hours: 1,
                  anomaly_percent: 40,
                  event_boost: false,
                  event_boost_days: 3,
                  event_interval_minutes: 30,
                });
                setDirty(true);
              }}
            >
              恢复默认值
            </Button>
          </Form>
        </Panel>
      </div>
    </>
  );
}

export function Forecasts() {
  const { data, action } = useMonitor();
  const [form] = Form.useForm();
  const [preview, setPreview] = useState<Row[] | null>(null);
  const [dirty, setDirty] = useState(false);
  const [filter, setFilter] = useState<string>();
  const [category, setCategory] = useState<string>();
  const [shadow, setShadow] = useState(data.settings.shadow_mode);
  const [history, setHistory] = useState<Row[]>([]);
  const { modal } = App.useApp();
  useEffect(() => {
    form.setFieldsValue(data.settings);
  }, [form]);
  const products: Row[] = data.products;
  const shown = products.filter(
    (p) =>
      (!filter || p.label === filter) && (!category || p.category === category),
  );
  const counts = (items: Row[], key = "label") =>
    labels.map((label) => ({
      label,
      count: items.filter((i) => i[key] === label).length,
    }));
  useEffect(() => {
    api<Row[]>("/forecast-review")
      .then(setHistory)
      .catch(() => {});
  }, [data]);
  return (
    <>
      <PageTitle
        eyebrow="SIGNALS, NOT PROMISES"
        title="预测中心"
        description="让历史数据提供线索，让每个判断都有依据。"
        actions={
          <>
            <Tag bordered={false}>规则 + LightGBM</Tag>
            <Button
              icon={<RefreshCw size={14} />}
              onClick={() =>
                action(
                  () => post("/forecasts/recalculate"),
                  "趋势参考已重算",
                ).catch(() => {})
              }
            >
              立即重算全部
            </Button>
          </>
        }
      />
      <ForecastModelPanel />
      <div className="overview-stats">
        <Metric
          label="已出趋势参考"
          value={products.filter((p) => p.forecast.label !== "数据不足").length}
          hint={`${products.length} 件商品参与计算`}
          accent
        />
        <Metric
          label="数据不足"
          value={products.filter((p) => p.forecast.label === "数据不足").length}
          hint={`至少观测 ${data.settings.min_days} 天、变价 ${data.settings.min_changes} 次`}
        />
        <Metric
          label="概率 / 区间模型"
          value={
            <span className="metric-time">
              {
                products.filter(
                  (p) => p.forecast.probability != null || p.forecast.interval,
                ).length
              }{" "}
              件
            </span>
          }
          hint="数据与验证通过后才显示模型数值"
        />
        <Metric
          label="影子模式"
          value={
            <span className="metric-time">
              {data.settings.shadow_mode ? "已开启" : "已关闭"}
            </span>
          }
          hint="开启时模型只观察，展示标签保留规则判断"
        />
      </div>
      <div className="collection-layout">
        <div>
          <Panel
            title="商品趋势参考"
            extra={
              <Space>
                <Select
                  allowClear
                  placeholder="全部标签"
                  value={filter}
                  onChange={setFilter}
                  options={opts(labels)}
                  style={{ width: 150 }}
                />
                <Select
                  allowClear
                  placeholder="全部品类"
                  value={category}
                  onChange={setCategory}
                  options={opts(categories)}
                  style={{ width: 115 }}
                />
              </Space>
            }
          >
            <Table
              rowKey="id"
              dataSource={shown}
              pagination={pagination}
              scroll={{ x: 1400 }}
              columns={[
                {
                  title: "商品",
                  width: 200,
                  render: (_: any, p: Row) => (
                    <Link to={`/products/${p.id}`}>{p.name}</Link>
                  ),
                },
                {
                  title: "趋势标签",
                  width: 120,
                  render: (_: any, p: Row) => (
                    <TrendTag label={p.forecast.label} />
                  ),
                },
                {
                  title: "降价概率",
                  width: 110,
                  render: (_: any, p: Row) =>
                    p.forecast.probability == null
                      ? "积累中"
                      : `${p.forecast.probability}%`,
                },
                {
                  title: "观测最低价范围",
                  width: 240,
                  render: (_: any, p: Row) => (
                    <>
                      {p.forecast.interval
                        ? `${money(p.forecast.interval[0])} ～ ${money(p.forecast.interval[1])}`
                        : "验证通过后展示"}
                      <small className="cell-sub">
                        {p.forecast.price_basis_name || "—"}
                      </small>
                    </>
                  ),
                },
                {
                  title: "数据质量",
                  width: 100,
                  render: (_: any, p: Row) => (
                    <Tag
                      bordered={false}
                      color={
                        p.forecast.quality === "正常" ? "green" : "default"
                      }
                    >
                      {p.forecast.quality}
                    </Tag>
                  ),
                },
                {
                  title: "观测 / 变价",
                  width: 130,
                  render: (_: any, p: Row) =>
                    `${p.forecast.observed_days} 天 / ${p.forecast.changes} 次`,
                },
                {
                  title: "主要因素",
                  width: 270,
                  render: (_: any, p: Row) => (
                    <span className="small">
                      {p.forecast.factors.join("；")}
                    </span>
                  ),
                },
                {
                  title: "人工覆盖",
                  width: 100,
                  render: (_: any, p: Row) =>
                    p.overridden ? <Tag color="blue">{p.label}</Tag> : "—",
                },
                {
                  title: "操作",
                  width: 100,
                  render: (_: any, p: Row) => (
                    <Link to={`/products/${p.id}`}>详情 / 覆盖</Link>
                  ),
                },
              ]}
            />
          </Panel>
          <Panel
            title="命中回顾"
            className="mt20"
            extra={
              <Space>
                <span className="muted small">影子模式</span>
                <Switch
                  size="small"
                  checked={data.settings.shadow_mode}
                  aria-label="影子模式"
                  onChange={(value) =>
                    action(
                      () => patch("/settings", { shadow_mode: value }),
                      "设置已更新",
                    ).catch(() => {})
                  }
                />
              </Space>
            }
          >
            <p className="muted small">
              只评估完整走完预测窗口、同 SKU /
              同价格口径且后续观测覆盖充分的记录。缺失窗口不作为未降价；重复重算按日去重。
            </p>
            <Table
              rowKey="label"
              dataSource={history}
              pagination={false}
              columns={[
                {
                  title: "当时标签",
                  dataIndex: "label",
                  render: (v: string) => <TrendTag label={v} />,
                },
                { title: "样本数", dataIndex: "samples" },
                {
                  title: "之后实际降价占比",
                  dataIndex: "drop_rate",
                  render: (v: number | null) =>
                    v == null ? "—" : `${v.toFixed(1)}%`,
                },
                {
                  title: "窗口最低价差（回看）",
                  dataIndex: "saving",
                  render: (v: number | null) =>
                    v == null ? "—" : `${v.toFixed(1)}%`,
                },
              ]}
              locale={{
                emptyText: (
                  <Blank
                    title="评估需要一些时间"
                    text="系统会保留每次判断，预测窗口结束后用真实价格回顾。"
                  />
                ),
              }}
            />
          </Panel>
        </div>
        <Panel title="标签与阈值" className="policy-panel">
          <Form
            form={form}
            layout="vertical"
            onValuesChange={() => {
              setDirty(true);
              setPreview(null);
            }}
            onFinish={async (v) => {
              if (!preview) return;
              await action(() => patch("/settings", v), "阈值已生效");
              setDirty(false);
              setPreview(null);
            }}
          >
            <Form.Item name="forecast_days" label="预测窗口（天）">
              <Select
                options={[
                  { label: "14 天", value: 14 },
                  { label: "30 天", value: 30 },
                ]}
              />
            </Form.Item>
            <Form.Item name="drop_threshold" label="降价幅度阈值（%）">
              <InputNumber min={0.1} max={100} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="low_percentile" label="接近低点的价格分位（%）">
              <InputNumber min={0} max={100} style={{ width: "100%" }} />
            </Form.Item>
            <div className="form-section-title">数据不足判定</div>
            <Form.Item name="min_days" label="至少观测天数">
              <InputNumber
                min={7}
                max={365}
                precision={0}
                style={{ width: "100%" }}
              />
            </Form.Item>
            <Form.Item name="min_changes" label="至少确认变价次数">
              <InputNumber
                min={1}
                max={100}
                precision={0}
                style={{ width: "100%" }}
              />
            </Form.Item>
            {preview && (
              <div className="threshold-preview">
                <strong>标签分布预览</strong>
                {counts(products.map((p) => p.forecast)).map((d) => (
                  <div key={d.label}>
                    <span>{d.label}</span>
                    <b>
                      {d.count} →{" "}
                      {preview.filter((p) => p.label === d.label).length}
                    </b>
                  </div>
                ))}
              </div>
            )}
            <Space direction="vertical" style={{ width: "100%" }}>
              <Button
                block
                onClick={async () => {
                  const v = await form.validateFields();
                  setPreview(
                    await post("/forecasts/recalculate", {
                      preview: true,
                      thresholds: v,
                    }),
                  );
                }}
              >
                预览分布变化
              </Button>
              <Button
                block
                type="primary"
                htmlType="submit"
                disabled={!preview}
              >
                确认生效
              </Button>
              <Button
                block
                onClick={() => {
                  form.setFieldsValue({
                    forecast_days: 14,
                    drop_threshold: 3,
                    low_percentile: 20,
                    min_days: 30,
                    min_changes: 3,
                  });
                  setDirty(true);
                  setPreview(null);
                }}
              >
                恢复默认值
              </Button>
            </Space>
          </Form>
        </Panel>
      </div>
    </>
  );
}

export function Audit() {
  const { action } = useMonitor();
  const { value: rows, reload } = useResource<Row[]>("/audit", []);
  const { value: trash, reload: reloadTrash } = useResource<Row[]>(
    "/trash",
    [],
  );
  const [tab, setTab] = useState("audit");
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string>();
  const [selection, setSelection] = useState<React.Key[]>([]);
  const [diff, setDiff] = useState<Row | null>(null);
  const [period, setPeriod] = useState<any>(null);
  const [periodPreview, setPeriodPreview] = useState<Row[] | null>(null);
  const { modal, message } = App.useApp();
  const kindNames: Row = {
    product: "商品",
    rule: "提醒规则",
    event: "日历事件",
    note: "备注标注",
    view: "视图",
    correction: "价格修正",
    manual: "手动价格",
    daily_price: "按天编辑价格",
    change: "变价标记",
  };
  const actionNames: Row = {
    create: "新增",
    update: "修改",
    delete: "删除",
    restore: "恢复",
    bulk: "批量",
    rollback: "回滚",
    purge: "永久删除",
  };
  const rollback = async (r: Row, force = false) => {
    try {
      await post(`/audit/${r.id}/rollback`, { force });
      await reload();
      await action(async () => true, "操作已回滚");
    } catch (e: any) {
      if (e.message.includes("后续修改"))
        modal.confirm({
          title: "检测到后续修改",
          content:
            "该对象在这次操作后又发生过变化。继续会覆盖后续字段，请先核对差异。",
          okText: "覆盖并回滚",
          okButtonProps: { danger: true },
          onOk: () => rollback(r, true),
        });
      else {
        message.error(e.message);
        throw e;
      }
    }
  };
  const rollbackMany = (selected: Row[]) =>
    modal.confirm({
      title: `回滚 ${selected.length} 条修改？`,
      content:
        "将按时间从新到旧执行，遇到冲突时停止，已完成的回滚仍会保留审计记录。",
      onOk: async () => {
        for (const r of selected
          .filter((r) => !r.reverted_at && r.action !== "purge")
          .sort((a, b) => b.created_at.localeCompare(a.created_at))) {
          await post(`/audit/${r.id}/rollback`, {});
        }
        await reload();
        await action(async () => true, "批量回滚完成");
        setSelection([]);
        setPeriodPreview(null);
      },
    });
  const filtered = rows.filter(
    (r) =>
      (!kind || r.kind === kind) &&
      `${r.reason} ${r.after_data?.name || r.before_data?.name || ""} ${r.entity_id}`.includes(
        q,
      ),
  );
  const diffFields = diff
    ? [
        ...new Set([
          ...Object.keys(diff.before_data || {}),
          ...Object.keys(diff.after_data || {}),
        ]),
      ].filter(
        (k) =>
          !["version", "updated_at"].includes(k) &&
          JSON.stringify(diff.before_data?.[k]) !==
            JSON.stringify(diff.after_data?.[k]),
      )
    : [];
  return (
    <>
      <PageTitle
        eyebrow="EVERY CHANGE HAS A HISTORY"
        title="审计与回收站"
        description="保留原始事实，让修改可追溯、删除可恢复。"
        actions={
          <>
            <Button
              icon={<Download size={14} />}
              onClick={() => download("/export/audit?format=csv")}
            >
              导出审计日志
            </Button>
            <Button
              icon={<Undo2 size={14} />}
              disabled={
                !rows.find((r) => !r.reverted_at && r.action !== "purge")
              }
              onClick={() => {
                const r = rows.find(
                  (r) => !r.reverted_at && r.action !== "purge",
                );
                if (r)
                  modal.confirm({
                    title: "撤销最近一次修改？",
                    onOk: () => rollback(r),
                  });
              }}
            >
              撤销最近修改
            </Button>
          </>
        }
      />
      <Panel>
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: "audit",
              label: "审计日志",
              children: (
                <>
                  <div className="filterbar compact">
                    <Input
                      prefix={<Search size={14} />}
                      placeholder="搜索名称、对象 ID 或备注"
                      value={q}
                      onChange={(e) => setQ(e.target.value)}
                      style={{ width: 280 }}
                    />
                    <Select
                      allowClear
                      placeholder="全部对象类型"
                      value={kind}
                      onChange={setKind}
                      style={{ width: 145 }}
                      options={Object.entries(kindNames).map(
                        ([value, label]) => ({ value, label }),
                      )}
                    />
                    <div className="spacer" />
                    <DatePicker.RangePicker
                      showTime
                      value={period}
                      onChange={setPeriod}
                    />
                    <Button
                      disabled={!period}
                      onClick={() =>
                        setPeriodPreview(
                          filtered.filter(
                            (r) =>
                              dayjs(r.created_at).isAfter(period[0]) &&
                              dayjs(r.created_at).isBefore(period[1]) &&
                              !r.reverted_at &&
                              r.action !== "purge",
                          ),
                        )
                      }
                    >
                      预览时段回滚
                    </Button>
                  </div>
                  {selection.length > 0 && (
                    <div className="bulk-toolbar">
                      <span>已选 {selection.length} 项</span>
                      <Button
                        size="small"
                        onClick={() =>
                          rollbackMany(
                            rows.filter((r) => selection.includes(r.id)),
                          )
                        }
                      >
                        回滚所选
                      </Button>
                    </div>
                  )}
                  <Table
                    rowKey="id"
                    dataSource={filtered}
                    rowSelection={{
                      selectedRowKeys: selection,
                      onChange: setSelection,
                      getCheckboxProps: (r) => ({
                        disabled: !!r.reverted_at || r.action === "purge",
                      }),
                    }}
                    pagination={pagination}
                    scroll={{ x: 1050 }}
                    columns={[
                      {
                        title: "时间",
                        dataIndex: "created_at",
                        width: 150,
                        render: (v: string) => date(v, "MM-DD HH:mm:ss"),
                      },
                      {
                        title: "对象类型",
                        dataIndex: "kind",
                        width: 100,
                        render: (v: string) => kindNames[v] || v,
                      },
                      {
                        title: "对象",
                        width: 210,
                        render: (_: any, r: Row) => (
                          <span>
                            {r.after_data?.name ||
                              r.before_data?.name ||
                              r.entity_id.slice(0, 12)}
                          </span>
                        ),
                      },
                      {
                        title: "操作",
                        dataIndex: "action",
                        render: (v: string) => actionNames[v] || v,
                      },
                      { title: "备注", dataIndex: "reason" },
                      {
                        title: "状态",
                        render: (_: any, r: Row) => (
                          <Tag
                            bordered={false}
                            color={r.reverted_at ? "default" : "green"}
                          >
                            {r.reverted_at ? "已回滚" : "有效"}
                          </Tag>
                        ),
                      },
                      {
                        title: "操作",
                        width: 150,
                        render: (_: any, r: Row) => (
                          <Space size={0}>
                            <Button
                              type="link"
                              size="small"
                              onClick={() => setDiff(r)}
                            >
                              查看差异
                            </Button>
                            <Button
                              type="link"
                              size="small"
                              disabled={!!r.reverted_at || r.action === "purge"}
                              onClick={() =>
                                modal.confirm({
                                  title: "回滚这次修改？",
                                  onOk: () => rollback(r),
                                })
                              }
                            >
                              回滚
                            </Button>
                          </Space>
                        ),
                      },
                    ]}
                    locale={{
                      emptyText: (
                        <Blank
                          title="每一次修改都会留在这里"
                          text="修改商品、修正价格、删除和恢复，都会自动记录。"
                        />
                      ),
                    }}
                  />
                </>
              ),
            },
            {
              key: "trash",
              label: `回收站（${trash.length}）`,
              children: (
                <>
                  <div className="table-toolbar">
                    <p className="muted">
                      恢复商品后仍为暂停状态，需手动恢复采集。永久删除不可撤销。
                    </p>
                    <Button
                      danger
                      disabled={!trash.length}
                      icon={<Trash2 size={14} />}
                      onClick={() =>
                        modal.confirm({
                          title: "永久清空回收站？",
                          content: `将永久删除 ${trash.length} 个对象及相关价格历史。建议先在设置中备份。`,
                          okText: "永久删除全部",
                          okButtonProps: { danger: true },
                          onOk: async () => {
                            for (const item of trash)
                              await remove(`/trash/${item.id}`);
                            await reloadTrash();
                            await action(async () => true, "回收站已清空");
                          },
                        })
                      }
                    >
                      清空回收站
                    </Button>
                  </div>
                  <Table
                    rowKey="id"
                    dataSource={trash}
                    pagination={pagination}
                    columns={[
                      {
                        title: "名称",
                        render: (_: any, r: Row) => r.name || r.text || r.id,
                      },
                      {
                        title: "类型",
                        dataIndex: "kind",
                        render: (v: string) => kindNames[v] || v,
                      },
                      {
                        title: "删除时间",
                        dataIndex: "deleted_at",
                        render: (v: string) => date(v),
                      },
                      {
                        title: "保留剩余",
                        dataIndex: "remaining_days",
                        render: (v: number) => `${v} 天`,
                      },
                      {
                        title: "操作",
                        render: (_: any, r: Row) => (
                          <Space>
                            <Button
                              size="small"
                              type="link"
                              onClick={async () => {
                                await action(
                                  () => post(`/trash/${r.id}/restore`),
                                  "已恢复",
                                );
                                await reloadTrash();
                              }}
                            >
                              恢复
                            </Button>
                            <Popconfirm
                              title="永久删除此对象？"
                              description="历史数据将同时删除，此操作不可撤销。"
                              okButtonProps={{ danger: true }}
                              onConfirm={async () => {
                                await action(
                                  () => remove(`/trash/${r.id}`),
                                  "已永久删除",
                                );
                                await reloadTrash();
                              }}
                            >
                              <Button size="small" type="link" danger>
                                永久删除
                              </Button>
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                    locale={{
                      emptyText: (
                        <Blank
                          title="回收站是空的"
                          text="软删除的商品、规则、事件与视图会在这里保留。"
                        />
                      ),
                    }}
                  />
                </>
              ),
            },
          ]}
        />
      </Panel>
      <Modal
        title="修改差异"
        open={!!diff}
        onCancel={() => setDiff(null)}
        width={800}
        footer={
          <Space>
            <Button onClick={() => setDiff(null)}>关闭</Button>
            <Button
              type="primary"
              disabled={!!diff?.reverted_at || diff?.action === "purge"}
              onClick={async () => {
                await rollback(diff!);
                setDiff(null);
              }}
            >
              回滚此次修改
            </Button>
          </Space>
        }
      >
        <p className="muted">
          {date(diff?.created_at, "YYYY-MM-DD HH:mm:ss")} ·{" "}
          {diff?.reason || "未填写备注"}
        </p>
        <Table
          rowKey="field"
          pagination={false}
          dataSource={diffFields.map((field) => ({
            field,
            before: JSON.stringify(diff?.before_data?.[field] ?? null),
            after: JSON.stringify(diff?.after_data?.[field] ?? null),
          }))}
          columns={[
            { title: "字段", dataIndex: "field", width: 160 },
            {
              title: "原值",
              dataIndex: "before",
              render: (v: string) => <span className="diff-old">{v}</span>,
            },
            {
              title: "新值",
              dataIndex: "after",
              render: (v: string) => <span className="diff-new">{v}</span>,
            },
          ]}
        />
      </Modal>
      <Modal
        title="时段回滚预览"
        open={!!periodPreview}
        onCancel={() => setPeriodPreview(null)}
        onOk={() => {
          if (periodPreview?.length) rollbackMany(periodPreview);
        }}
        okButtonProps={{ disabled: !periodPreview?.length }}
        okText="继续回滚"
      >
        <p>
          将影响 {periodPreview?.length || 0} 条修改，涉及{" "}
          {new Set(periodPreview?.map((r) => r.entity_id)).size} 个对象。
        </p>
        <ul>
          {periodPreview?.slice(0, 12).map((r) => (
            <li key={r.id}>
              {date(r.created_at)} · {kindNames[r.kind]} ·{" "}
              {r.reason || actionNames[r.action]}
            </li>
          ))}
        </ul>
      </Modal>
    </>
  );
}
