import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
import { RefreshCw } from "lucide-react";
import {
  ContextHelp,
  Panel,
  Row,
  date,
  downloadJSON,
  money,
  patch,
  post,
  useMonitor,
  useResource,
} from "./lib";

export function ForecastNumbers({
  forecast: f,
  compact = false,
}: {
  forecast: Row;
  compact?: boolean;
}) {
  const basis = f.price_basis_name || "当前价格口径";
  const probability =
    f.probability == null ? "积累中" : `${f.probability.toFixed(1)}%`;
  const range = f.interval
    ? `${money(f.interval[0])} ～ ${money(f.interval[1])}`
    : "验证通过后展示";
  if (compact)
    return (
      <div className="mt16 small">
        <p>
          未来 {f.window} 天降价 ≥ {f.drop_threshold || 3}%：
          <strong>{probability}</strong>
        </p>
        {f.interval && <p>观测最低价范围：{range}</p>}
        <p className="muted">
          {basis} ·{" "}
          {f.model_status === "shadow"
            ? "模型观察中，标签仍用规则"
            : f.model === "lightgbm-v1"
              ? "已通过历史回测"
              : f.model_reason || "规则参考"}
        </p>
      </div>
    );
  return (
    <>
      <Descriptions
        className="mt16"
        bordered
        size="small"
        column={1}
        items={[
          {
            key: "probability",
            label: `未来 ${f.window} 天降价至少 ${f.drop_threshold || 3}%`,
            children: probability,
          },
          {
            key: "minimum",
            label: "预计观测最低价",
            children: f.expected_min == null ? "积累中" : money(f.expected_min),
          },
          {
            key: "interval",
            label: "最低价范围（目标 80% 覆盖）",
            children: range,
          },
          { key: "basis", label: "本次价格口径", children: basis },
          {
            key: "model",
            label: "判断来源",
            children:
              f.model === "lightgbm-v1"
                ? f.shadow_mode
                  ? "已验证模型 · 影子观察"
                  : "已验证模型"
                : "规则参考，模型待积累 / 验证",
          },
          ...(f.interval_coverage != null
            ? [
                {
                  key: "coverage",
                  label: "历史回测区间覆盖率",
                  children: `${f.interval_coverage}%`,
                },
              ]
            : []),
          ...(f.suggested_label && f.shadow_mode
            ? [
                {
                  key: "shadow",
                  label: "模型建议（未接管标签）",
                  children: f.suggested_label,
                },
              ]
            : []),
        ]}
      />
      <p className="muted small mt12">
        {f.model_reason}。预测针对次日起 {f.window}{" "}
        个自然日中采集到的同口径最低价；未采集到的短时优惠无法评估。
      </p>
    </>
  );
}

const requiredNames: Row = {
  samples: "成熟训练样本",
  origin_days: "成熟决策天数",
  positive: "降价样本",
  negative: "未降价样本",
  series: "SKU / 价格口径序列",
};
const states: Row = {
  running: "训练中",
  insufficient: "样本积累中",
  rejected: "候选未通过",
  active: "已验证并保存",
  error: "训练失败",
  interrupted: "训练已中断，可重试",
};

export function ForecastModelPanel() {
  const { data, action } = useMonitor();
  const { value: model, reload } = useResource<Row>("/forecast-model", {});
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    if (!model.running && !model.queued && !starting) return;
    const timer = window.setInterval(() => reload().catch(() => {}), 2500);
    return () => window.clearInterval(timer);
  }, [model.running, model.queued, starting, reload]);
  const active = model.active;
  const last = model.last_run;
  const metrics = last?.metrics || active?.metrics;
  const running = starting || model.running || model.queued;
  const status = !data.settings.ml_enabled
    ? "模型已关闭"
    : model.queued
      ? `训练排队中 · 第 ${model.training_queue?.position || 1} 位`
      : running
        ? "训练 / 回测中"
        : active
          ? data.settings.shadow_mode
            ? "已验证 · 影子观察"
            : "已验证 · 正式应用"
          : states[last?.state] || "历史数据积累中";
  return (
    <Panel
      className="mb20"
      title="预测模型与数据积累"
      extra={<Tag color={active ? "green" : "blue"}>{status}</Tag>}
    >
      {last?.state === "error" && <Alert
        showIcon
        type="warning"
        message={last.message || "模型训练失败，请重试或查看训练记录。"}
      />}
      {running && (
        <Alert
          className="mt12"
          showIcon
          type="info"
          message={
            model.queued
              ? `等待训练名额，当前排队第 ${model.training_queue?.position || 1} 位`
              : "正在训练与回测"
          }
        />
      )}
      {model.notice && <ContextHelp title="模型说明">{model.notice}</ContextHelp>}
      {model.reference_data?.points > 0 && (
        <ContextHelp title={`参考价格 · ${model.reference_data.points} 条`}>
          {model.reference_data.measured} 条实测、{model.reference_data.carried_forward} 条沿用前值。人工数据与采集数据同等参与训练，同日同规格同口径优先使用人工价格。
        </ContextHelp>
      )}
      <Space wrap className="mt16 mb16" size={20}>
        <Space>
          <span>启用学习模型</span>
          <Switch
            checked={data.settings.ml_enabled}
            disabled={data.access?.read_only}
            aria-label="启用学习模型"
            onChange={(v) =>
              action(() => patch("/settings", { ml_enabled: v }))
                .then(reload)
                .catch(() => {})
            }
          />
        </Space>
        <Space>
          <span>每日自动训练</span>
          <Switch
            checked={data.settings.ml_auto_train}
            disabled={data.access?.read_only}
            aria-label="每日自动训练"
            onChange={(v) =>
              action(() => patch("/settings", { ml_auto_train: v }))
                .then(reload)
                .catch(() => {})
            }
          />
        </Space>
        <Button
          icon={<RefreshCw size={14} />}
          loading={!!running}
          disabled={data.access?.read_only}
          onClick={async () => {
            setStarting(true);
            try {
              await action(
                () => post("/forecast-model/train"),
                "已提交后台训练与回测",
              );
              await reload();
            } catch {
              // The shared action handler already displays the request error.
            } finally {
              setStarting(false);
            }
          }}
        >
          训练并回测
        </Button>
        <Button onClick={() => downloadJSON("预测模型报告.json", model)}>
          下载验证报告
        </Button>
      </Space>
      <p className="muted small">
        已收集 {model.progress?.daily_points || 0} 个日级价格点，覆盖{" "}
        {model.progress?.observed_days || 0}{" "}
        个自然日。下面是训练的最低数据门槛，达到后还需通过独立回测。
      </p>
      <Table
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={Object.entries(model.requirements || {}).map(
          ([key, required]) => ({
            key,
            name: requiredNames[key],
            current: model.progress?.[key] || 0,
            required,
          }),
        )}
        columns={[
          { title: "数据条件", dataIndex: "name" },
          { title: "当前", dataIndex: "current" },
          { title: "最低要求", dataIndex: "required" },
          {
            title: "状态",
            render: (_: any, row: Row) => (
              <Tag color={row.current >= row.required ? "green" : "default"}>
                {row.current >= row.required ? "已达到" : "积累中"}
              </Tag>
            ),
          },
        ]}
      />
      {last?.reasons?.length > 0 && (
        <p className="muted small mt12">
          {last.reasons
            .map((reason: string) =>
              reason.replace(
                /^(samples|origin_days|positive|negative|series):/,
                (key: string) => `${requiredNames[key.slice(0, -1)]}：`,
              ),
            )
            .join("；")}
        </p>
      )}
      <p className="muted small">
        最近检查：{date(last?.started_at, "YYYY-MM-DD HH:mm")} · 当前窗口{" "}
        {model.horizon || data.settings.forecast_days} 天 / 降价阈值{" "}
        {model.drop_threshold ?? data.settings.drop_threshold}%
        {active ? ` · 模型到期 ${date(active.expires_at)}` : ""}
      </p>
      {metrics && (
        <Collapse
          items={[
            {
              key: "validation",
              label: "查看最近候选模型的回测与校准结果",
              children: (
                <>
                  <Descriptions
                    size="small"
                    bordered
                    column={1}
                    items={[
                      {
                        key: "n",
                        label: "独立时间回测",
                        children: `${metrics.folds?.length || 0} 个时间块 / ${metrics.samples || 0} 个样本 / ${metrics.test_days || 0} 个决策日`,
                      },
                      {
                        key: "brier",
                        label: "降价概率误差（越低越好）",
                        children: `${metrics.brier ?? "—"}，基线 ${metrics.baseline_brier ?? "—"}`,
                      },
                      {
                        key: "mae",
                        label: "最低价误差（相对当时价格）",
                        children: `${metrics.mae_percent ?? "—"}%，价格不变基线 ${metrics.baseline_mae_percent ?? "—"}%`,
                      },
                      {
                        key: "coverage",
                        label: "区间实际覆盖率",
                        children: `${metrics.interval_coverage ?? "—"}%（目标 80%）`,
                      },
                      {
                        key: "wait",
                        label: "建议等待后的实际降价占比",
                        children:
                          metrics.wait_precision == null
                            ? "尚无足够建议等待样本"
                            : `${metrics.wait_precision}% / ${metrics.wait_samples} 个样本`,
                      },
                      {
                        key: "decision",
                        label: "验证结果",
                        children: `概率：${metrics.probability_ready ? "通过" : "未通过"}；区间：${metrics.interval_ready ? "通过" : "未通过"}`,
                      },
                    ]}
                  />
                  {metrics.calibration_bins?.length > 0 && (
                    <Table
                      className="mt16"
                      size="small"
                      rowKey="range"
                      pagination={false}
                      dataSource={metrics.calibration_bins}
                      columns={[
                        { title: "预测概率组", dataIndex: "range" },
                        { title: "样本", dataIndex: "samples" },
                        {
                          title: "平均预测",
                          dataIndex: "predicted",
                          render: (v) => `${v}%`,
                        },
                        {
                          title: "实际降价",
                          dataIndex: "actual",
                          render: (v) => `${v}%`,
                        },
                      ]}
                    />
                  )}
                </>
              ),
            },
          ]}
        />
      )}
    </Panel>
  );
}
