import React, { useEffect, useRef, useState } from "react";
import {
  App,
  Button,
  Checkbox,
  DatePicker,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tooltip,
} from "antd";
import { Plus, ExternalLink } from "lucide-react";
import * as echarts from "echarts";
import dayjs from "dayjs";
import { interfaceFont } from "./typography";
import {
  Row,
  api,
  post,
  put,
  categories,
  opts,
  useMonitor,
  money,
  colors,
  Blank,
  useReducedMotion,
} from "./lib";

export function ProductEditor({
  open,
  onClose,
  product,
}: {
  open: boolean;
  onClose: () => void;
  product?: Row | null;
}) {
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const { action } = useMonitor();
  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue(
        product
          ? {
              ...product,
              msrp: product.msrp ? product.msrp / 100 : null,
              release_date: product.release_date
                ? dayjs(product.release_date)
                : null,
            }
          : {
              category: "显卡",
              group: "观察中",
              shop_type: "普通店",
              interval_hours: 4,
              watch: true,
              baseline: true,
              paused: false,
              tags: [],
              expected_options: [],
            },
      );
    }
  }, [open, product, form]);
  return (
    <Drawer
      title={product ? "编辑商品" : "添加监测商品"}
      open={open}
      onClose={onClose}
      width={620}
      extra={
        <Button type="primary" loading={busy} onClick={() => form.submit()}>
          保存商品
        </Button>
      }
    >
      <div className="drawer-intro">
        粘贴淘宝或天猫的完整商品链接，选定规格单独跟踪。也可先填写名称导入历史价格，链接稍后补充；无链接时保持暂停采集。
      </div>
      <Form
        form={form}
        layout="vertical"
        onFinish={async (values) => {
          setBusy(true);
          try {
            const body = {
              ...values,
              msrp: values.msrp ? Math.round(values.msrp * 100) : null,
              release_date: values.release_date?.format("YYYY-MM-DD") || null,
            };
            await action(
              () =>
                product
                  ? put(`/entities/product/${product.id}`, {
                      ...body,
                      version: product.version,
                    })
                  : post("/entities/product", body),
              product ? "商品已更新" : "商品已添加",
            );
            onClose();
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item
          name="url"
          label="商品链接"
          extra="参考商品可暂留空；补充链接不会删除或自动合并已有参考价格。"
          rules={[
            {
              pattern: /^https:\/\/(item\.taobao\.com|detail\.tmall\.com)\//,
              message: "仅支持淘宝 / 天猫完整链接",
            },
          ]}
        >
          <Input.TextArea
            rows={2}
            placeholder="淘宝 / 天猫链接，或 https://item.jd.com/数字SKU.html"
          />
        </Form.Item>
        <Form.Item
          name="name"
          label="商品别名"
          dependencies={["url"]}
          rules={[
            ({ getFieldValue }) => ({
              validator(_, value) {
                return getFieldValue("url")?.trim() || value?.trim()
                  ? Promise.resolve()
                  : Promise.reject(new Error("链接暂空时，请填写商品名称"));
              },
            }),
          ]}
        >
          <Input
            placeholder="例如：ROG RTX 5070 Ti 16GB（留空自动识别）"
            maxLength={300}
          />
        </Form.Item>
        <div className="form-grid">
          <Form.Item name="category" label="品类">
            <Select options={opts(categories)} />
          </Form.Item>
          <Form.Item name="brand" label="品牌">
            <Input placeholder="ASUS / AMD / Samsung" />
          </Form.Item>
          <Form.Item name="model" label="同款型号">
            <Input placeholder="跨店比价归组，如 RTX 5070 Ti" />
          </Form.Item>
          <Form.Item name="spec" label="具体规格">
            <Input placeholder="容量、版本、盒装 / 散片" />
          </Form.Item>
          <Form.Item name="shop" label="店铺名称">
            <Input />
          </Form.Item>
          <Form.Item name="shop_type" label="店铺类型">
            <Select options={opts(["官方旗舰", "授权店", "普通店"])} />
          </Form.Item>
          <Form.Item name="msrp" label="官方建议价（元）">
            <InputNumber min={0.01} precision={2} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="release_date" label="发布日期">
            <DatePicker style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="group" label="分组">
            <Select
              mode={undefined}
              options={opts(["观察中", "待购清单", "已购入"])}
              showSearch
            />
          </Form.Item>
          <Form.Item name="interval_hours" label="采集间隔">
            <Select
              options={[
                { label: "每小时", value: 1 },
                { label: "每 4 小时", value: 4 },
                { label: "每天", value: 24 },
                { label: "每 30 分钟", value: 0.5 },
              ]}
            />
          </Form.Item>
        </div>
        <Form.Item name="tags" label="标签">
          <Select
            mode="tags"
            tokenSeparators={[",", "，"]}
            placeholder="输入标签，按回车添加"
          />
        </Form.Item>
        <Form.Item
          name="expected_options"
          label="预期选中规格"
          extra="SKU 无法从页面直接验证时，逐项填写页面规格选项的原文。全部与实际选中项匹配后，才采纳该 SKU 的价格。"
        >
          <Select
            mode="tags"
            placeholder="例如：黑色、16GB、官方标配"
            tokenSeparators={["，", ","]}
          />
        </Form.Item>
        <Form.Item name="notes" label="备注">
          <Input.TextArea rows={3} />
        </Form.Item>
        <div className="switch-row">
          <span>加入关注看板</span>
          <Form.Item noStyle name="watch" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        <div className="switch-row">
          <span>纳入同款比价基准</span>
          <Form.Item noStyle name="baseline" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        <div className="switch-row">
          <span>暂停自动采集</span>
          <Form.Item noStyle name="paused" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
      </Form>
    </Drawer>
  );
}

type ChartProps = {
  series: Row[];
  events?: Row[];
  notes?: Row[];
  height?: number;
  mode?: string;
  raw?: boolean;
  showOriginal?: boolean;
  onPoint?: (row: Row) => void;
  onSelect?: (ids: string[]) => void;
  chartRef?: React.MutableRefObject<echarts.ECharts | null>;
  step?: boolean;
  envelope?: boolean;
  calendarDays?: boolean;
  paddingDays?: number;
};
export function PriceChart({
  series,
  events = [],
  notes = [],
  height = 350,
  mode = "absolute",
  raw = false,
  showOriginal = false,
  onPoint,
  onSelect,
  chartRef,
  step = true,
  envelope = false,
  calendarDays = false,
  paddingDays = 0,
}: ChartProps) {
  const el = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();
  const local = useRef<echarts.ECharts | null>(null);
  const callbacks = useRef({ onPoint, onSelect, series });
  callbacks.current = { onPoint, onSelect, series };
  const dark = document.documentElement.dataset.theme === "dark";
  useEffect(() => {
    if (!el.current) return;
    const chart = echarts.init(el.current);
    local.current = chart;
    if (chartRef) chartRef.current = chart;
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(el.current);
    chart.on("click", (p: any) => {
      if (p.data?.record) callbacks.current.onPoint?.(p.data.record);
    });
    chart.on("brushEnd", (p: any) => {
      const a = p.areas?.[0];
      if (!a) return;
      const range = a.coordRange;
      if (range) {
        const ids = callbacks.current.series
          .flatMap((s) => s.records || [])
          .filter(
            (r) =>
              new Date(r.observed_at).getTime() >= range[0] &&
              new Date(r.observed_at).getTime() <= range[1],
          )
          .map((r) => r.id);
        callbacks.current.onSelect?.(ids);
      }
    });
    return () => {
      resize.disconnect();
      chart.dispose();
      local.current = null;
      if (chartRef) chartRef.current = null;
    };
  }, []);
  useEffect(() => {
    const chart = local.current;
    if (!chart) return;
    const convert = (n: number | null, s: Row) =>
      n == null
        ? null
        : mode === "percent"
          ? s.base
            ? (n / s.base) * 100 - 100
            : null
          : mode === "msrp"
            ? s.msrp
              ? (n / s.msrp) * 100
              : null
            : n / 100;
    const shown = (r: Row) =>
      raw
        ? r.raw_price
        : r.chart_price !== undefined
          ? r.chart_price
          : r.valid
            ? r.price
            : null;
    const chartTime = (value: string) => {
      if (!calendarDays) return value;
      // A backend day is already a Shanghai calendar date. Parsing it as a
      // browser-local midnight first would shift it on browsers in another TZ.
      if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
      return dayjs(value).tz("Asia/Shanghai").format("YYYY-MM-DD");
    };
    const knownDays = [
      ...new Set(
        series.flatMap((s) =>
          (s.records || [])
            .filter((r: Row) => shown(r) != null)
            .map((r: Row) => r.day || chartTime(r.observed_at)),
        ),
      ),
    ].sort() as string[];
    const days: string[] = [];
    if (calendarDays && knownDays.length) {
      const end = dayjs
        .tz(knownDays[knownDays.length - 1], "Asia/Shanghai")
        .add(paddingDays, "day");
      for (
        let day = dayjs
          .tz(knownDays[0], "Asia/Shanghai")
          .subtract(paddingDays, "day");
        !day.isAfter(end);
        day = day.add(1, "day")
      )
        days.push(day.format("YYYY-MM-DD"));
    }
    const chartSeries: Row[] = [];
    const separate = series.flatMap((s) => {
      const reference =
        s.records?.filter((r: Row) => r.reference_product_id) || [];
      const actual =
        s.records?.filter((r: Row) => !r.reference_product_id) || [];
      return reference.length && actual.length
        ? [
            { ...s, name: `${s.name} · 参考历史`, records: reference },
            { ...s, name: `${s.name} · 淘宝记录`, records: actual },
          ]
        : [s];
    });
    separate.forEach((s, i) => {
      const baseRow = s.records?.find((r: Row) => shown(r) != null);
      const base = baseRow ? shown(baseRow) : null;
      const meta = { ...s, base };
      const dayRecords = new Map(
        (s.records || []).map((r: Row) => [
          r.day || chartTime(r.observed_at),
          r,
        ]),
      );
      const rows: (Row | undefined)[] = calendarDays
        ? days.map((day) => dayRecords.get(day) as Row | undefined)
        : s.records || [];
      const rowData = rows.map((r: Row | undefined, index: number) => ({
        value: [
          calendarDays ? days[index] : r!.observed_at,
          convert(r ? shown(r) : null, meta),
        ],
        record: r,
      }));
      chartSeries.push({
        name: s.name,
        type: "line",
        step: step ? "end" : false,
        connectNulls: false,
        showSymbol: s.records?.length < 20,
        symbol: ["circle", "rect", "triangle", "diamond"][i % 4],
        symbolSize: 6,
        lineStyle: {
          width: 2.4,
          type: i % 3 === 0 ? "solid" : i % 3 === 1 ? "dashed" : "dotted",
        },
        itemStyle: {
          color: dark
            ? echarts.color.lift(colors[i % colors.length], 0.25)
            : colors[i % colors.length],
        },
        data: rowData,
        markArea:
          i === 0
            ? {
                silent: true,
                itemStyle: {
                  color: dark ? "rgba(224,174,67,.08)" : "rgba(246,197,94,.11)",
                },
                label: { color: "#a47c31", fontSize: 13 },
                data: [
                  ...events
                    .filter((e) => !e.ignored)
                    .map((e) => [
                      { name: e.name, xAxis: chartTime(e.start) },
                      { xAxis: chartTime(dayjs(e.end).add(1, "day").toISOString()) },
                    ]),
                  ...notes
                    .filter((n) => n.on_chart && n.type === "range")
                    .map((n) => [
                      {
                        name: n.text,
                        xAxis: chartTime(n.start),
                        itemStyle: { color: "rgba(49,92,246,.06)" },
                      },
                      { xAxis: chartTime(n.end) },
                    ]),
                ],
              }
            : undefined,
        markLine:
          i === 0
            ? {
                symbol: "none",
                lineStyle: { color: "#a0a9be", type: "dashed" },
                label: { fontSize: 13 },
                data: notes
                  .filter((n) => n.on_chart && n.type === "point")
                  .map((n) => ({
                    name: n.text,
                    xAxis: chartTime(n.start),
                    label: { formatter: n.text },
                  })),
              }
            : undefined,
      });
      if (showOriginal)
        chartSeries.push({
          name: `${s.name} · 划线价`,
          type: "line",
          step: "end",
          symbol: "none",
          lineStyle: { color: "#b6becc", type: "dashed", width: 1 },
          data: (s.records || []).map((r: Row) => [
            chartTime(r.observed_at),
            convert(r.original_price, meta),
          ]),
        });
      const manual = (s.records || []).filter(
        (r: Row) => !raw && (r.corrected || (r.manual && !r.import_source)),
      );
      if (manual.length)
        chartSeries.push({
          name: `${s.name} · 人工修正`,
          type: "scatter",
          symbolSize: 9,
          itemStyle: { color: "#e8a843", borderColor: "#fff", borderWidth: 2 },
          data: manual.map((r: Row) => ({
            value: [
              chartTime(r.observed_at),
              convert(r.valid ? r.price : null, meta),
            ],
            record: r,
          })),
        });
    });
    if (envelope && series.length) {
      const times = calendarDays
        ? days
        : ([
            ...new Set(
              series.flatMap((s) =>
                (s.records || []).map((r: Row) => r.observed_at),
              ),
            ),
          ].sort() as string[]);
      const values = times.map((t) => {
        const prices = series
          .filter((s) => s.baseline !== false)
          .map((s) => {
            const r = calendarDays
              ? (s.records || []).find(
                  (row: Row) => (row.day || chartTime(row.observed_at)) === t,
                )
              : [...(s.records || [])]
                  .reverse()
                  .find((r: Row) => r.observed_at <= t);
            return r &&
              r.valid &&
              r.price != null &&
              (calendarDays ||
                dayjs(t).diff(dayjs(r.observed_at), "hour", true) <=
                  (s.interval_hours || 4) * 2)
              ? shown(r)
              : null;
          })
          .filter((p) => p != null);
        return [t, prices.length ? Math.min(...prices) / 100 : null];
      });
      chartSeries.push({
        name: "最低价包络线",
        type: "line",
        step: "end",
        symbol: "none",
        lineStyle: { color: "#303b4f", width: 1.5, type: "dotted" },
        data: values,
      });
    }
    chart.setOption(
      {
        animation: !reducedMotion,
        animationDuration: reducedMotion ? 0 : 520,
        animationDurationUpdate: reducedMotion ? 0 : 260,
        animationEasing: "cubicOut",
        animationEasingUpdate: "cubicOut",
        backgroundColor: "transparent",
        textStyle: {
          fontFamily: interfaceFont,
          fontSize: 14,
          color: dark ? "#b2bfd2" : "#68778a",
        },
        tooltip: {
          trigger: "axis",
          renderMode: "richText",
          confine: true,
          backgroundColor: dark ? "#232d42" : "#fff",
          borderColor: dark ? "#35425c" : "#e7ebf2",
          textStyle: { color: dark ? "#e2e8f2" : "#34415a", fontSize: 15 },
          valueFormatter: (v: any) =>
            v == null
              ? "暂无价格"
              : mode === "absolute"
                ? `¥ ${Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}`
                : `${Number(v).toFixed(1)}%`,
        },
        legend: {
          type: "scroll",
          bottom: 0,
          icon: "roundRect",
          itemWidth: 16,
          itemHeight: 3,
          textStyle: { color: dark ? "#aab5c9" : "#69758d", fontSize: 14 },
        },
        grid: { left: 16, right: 20, top: 28, bottom: 85, containLabel: true },
        xAxis: {
          type: calendarDays ? "category" : "time",
          data: calendarDays ? days : undefined,
          boundaryGap: calendarDays ? false : undefined,
          min: calendarDays ? 0 : undefined,
          max: calendarDays && days.length ? days.length - 1 : undefined,
          axisLine: { lineStyle: { color: dark ? "#313b50" : "#edf0f5" } },
          axisTick: { show: false },
          axisLabel: {
            fontSize: 13,
            formatter: calendarDays
              ? (value: string) => value.slice(5)
              : "{MM}-{dd}",
            hideOverlap: true,
          },
        },
        yAxis: {
          type: "value",
          scale: true,
          splitNumber: 4,
          axisLabel: {
            fontSize: 13,
            formatter: (v: number) =>
              mode === "absolute" ? `¥${v.toLocaleString()}` : `${v}%`,
          },
          splitLine: {
            lineStyle: { color: dark ? "#2a344a" : "#eff2f7", type: "dashed" },
          },
        },
        dataZoom: [
          { type: "inside" },
          {
            type: "slider",
            bottom: 30,
            height: 15,
            borderColor: "transparent",
            backgroundColor: dark ? "#242d41" : "#f4f6fb",
            fillerColor: "rgba(49,92,246,.1)",
            handleSize: 12,
            textStyle: { fontSize: 12 },
            dataBackground: { lineStyle: { color: "#bbc7ee" } },
          },
        ],
        toolbox: {
          show: !!onSelect,
          right: 10,
          feature: {
            brush: {
              type: ["lineX", "clear"],
              title: { lineX: "框选价格记录", clear: "清除选择" },
            },
          },
        },
        brush: onSelect
          ? {
              xAxisIndex: 0,
              brushMode: "single",
              brushType: "lineX",
              throttleType: "debounce",
              throttleDelay: 300,
            }
          : undefined,
        series: chartSeries,
      },
      true,
    );
  }, [
    series,
    events,
    notes,
    mode,
    raw,
    showOriginal,
    step,
    envelope,
    dark,
    reducedMotion,
    calendarDays,
    paddingDays,
  ]);
  const hasData = series.some((s) => s.records?.length);
  return (
    <div className="chart-container" style={{ height }}>
      <div
        ref={el}
        style={{
          height: "100%",
          width: "100%",
          visibility: hasData ? "visible" : "hidden",
        }}
      />
      {!hasData && (
        <div className="chart-empty">
          <svg width="300" height="80" viewBox="0 0 300 80">
            <path
              d="M0 30H58V42H116V28H174V58H232V46H300"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeDasharray="5 5"
            />
          </svg>
          <h4>等待第一条价格轨迹</h4>
          <span>采集成功后，历史价格将在这里逐步积累</span>
        </div>
      )}
    </div>
  );
}
