import React, { useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import { CalendarDays } from "lucide-react";
import dayjs from "dayjs";
import { Row, money, post, useResource, ContextHelp } from "./lib";

const bases = [
  { value: "regular", label: "普通价" },
  { value: "subsidy", label: "补贴价" },
  { value: "coupon", label: "券后价" },
  { value: "member", label: "会员价" },
  { value: "reference", label: "表格参考价" },
];

export function DailyPrices({
  productId,
  reference = false,
  mutate,
}: {
  productId: string;
  reference?: boolean;
  mutate: (fn: () => Promise<any>, text: string) => Promise<void>;
}) {
  const [basis, setBasis] = useState(reference ? "reference" : "regular");
  const {
    value: report,
    reload,
    busy,
  } = useResource<Row>(`/product/${productId}/daily-prices?basis=${basis}`, {});
  const [editor, setEditor] = useState<Row | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  const ready =
    report.basis === basis && Boolean(report.sku_id || report.reference);
  const today = dayjs().tz("Asia/Shanghai").format("YYYY-MM-DD");
  const fillDay = (day: string) => {
    const row = report.rows?.find((r: Row) => r.day === day);
    setEditor({
      day,
      basis,
      item_id: report.item_id,
      sku_id: report.sku_id,
      version: row?.version || 0,
    });
    form.setFieldsValue({
      day: dayjs(day),
      price: row?.price == null ? null : row.price / 100,
      reason: "",
    });
  };
  const change = async (fn: () => Promise<any>, text: string) => {
    await mutate(fn, text);
    await reload();
  };
  return (
    <>
      <ContextHelp title="日价格编辑说明">按北京时间、SKU 和价格口径编辑。人工价格优先，参与展示和训练；原始观测保留，修改可撤销。</ContextHelp>
      <div className="table-toolbar">
        <Space wrap>
          <Select
            aria-label="日价格口径"
            value={basis}
            onChange={setBasis}
            options={bases}
            style={{ width: 130 }}
          />
          <Button
            type="primary"
            icon={<CalendarDays size={14} />}
            disabled={!ready || busy}
            onClick={() => fillDay(today)}
          >
            编辑 / 补录某一天
          </Button>
        </Space>
        <span className="muted small" style={{ overflowWrap: "anywhere" }}>
          {basis === "reference"
            ? "参考价格独立保存，不绑定淘宝 SKU"
            : `SKU：${report.basis === basis ? report.sku_id || "尚未确认" : "读取中…"}`}
        </span>
      </div>
      {!busy && !ready && report.basis === basis && (
        <Alert
          type="warning"
          showIcon
          message="请先在商品链接中指定 skuId，或完成一次 SKU 核验采集，再编辑日价格。"
          style={{ marginBottom: 16 }}
        />
      )}
      <Table
        rowKey="day"
        loading={busy || report.basis !== basis}
        dataSource={report.basis === basis ? report.rows || [] : []}
        scroll={{ x: 860 }}
        pagination={{ pageSize: 15, showSizeChanger: true }}
        columns={[
          { title: "日期", dataIndex: "day", width: 115 },
          { title: "当日价格", dataIndex: "price", width: 130, render: money },
          {
            title: "原始收盘价",
            dataIndex: "raw_price",
            width: 130,
            render: money,
          },
          { title: "观测数", dataIndex: "observations", width: 80 },
          {
            title: "状态",
            width: 120,
            render: (_: any, r: Row) => (
              <Tag color={r.override_id ? "gold" : "default"}>
                {r.override_id
                  ? "已按天编辑"
                  : r.effective_manual
                    ? "人工价优先"
                    : r.data_quality === "carried_forward"
                      ? "沿用前值"
                      : r.data_quality === "measured"
                        ? "实测导入"
                        : "观测收盘"}
              </Tag>
            ),
          },
          {
            title: "修改原因",
            dataIndex: "reason",
            width: 180,
            render: (v: string, r: Row) => v || r.source_note || "—",
          },
          {
            title: "操作",
            width: 180,
            render: (_: any, r: Row) => (
              <Space size={0}>
                <Button
                  type="link"
                  size="small"
                  disabled={!ready}
                  onClick={() => fillDay(r.day)}
                >
                  编辑当日价格
                </Button>
                {r.override_id && (
                  <Popconfirm
                    title="撤销这一天的人工价格？"
                    description="恢复原始观测；补录且无观测的日期将移除。"
                    onConfirm={() =>
                      change(
                        () =>
                          post(`/daily-prices/${r.override_id}/undo`, {
                            version: r.version,
                          }),
                        "当日人工价格已撤销",
                      ).catch(() => {})
                    }
                  >
                    <Button type="link" size="small">
                      撤销
                    </Button>
                  </Popconfirm>
                )}
              </Space>
            ),
          },
        ]}
        locale={{ emptyText: "暂无该口径的价格记录，可选择日期补录" }}
      />
      <Modal
        title="编辑当日历史价格"
        open={Boolean(editor)}
        confirmLoading={saving}
        onCancel={() => {
          if (!saving) setEditor(null);
        }}
        onOk={() => form.submit()}
        destroyOnClose
      >
        <p className="muted small" style={{ overflowWrap: "anywhere" }}>
          SKU：{editor?.sku_id} ·{" "}
          {bases.find((b) => b.value === editor?.basis)?.label} · 北京时间
        </p>
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values) => {
            if (saving || !editor) return;
            setSaving(true);
            try {
              await change(
                () =>
                  post(`/product/${productId}/daily-prices`, {
                    ...editor,
                    price: values.price,
                    reason: values.reason,
                  }),
                "当日价格已保存",
              );
              setEditor(null);
            } catch {
              /* The shared action displays the server error. Keep the form. */
            } finally {
              setSaving(false);
            }
          }}
        >
          <Form.Item
            name="day"
            label="日期"
            rules={[{ required: true, message: "请选择日期" }]}
          >
            <DatePicker
              allowClear={false}
              disabled={saving}
              style={{ width: "100%" }}
              disabledDate={(value) => value.format("YYYY-MM-DD") > today}
              onChange={(value) => {
                if (value) fillDay(value.format("YYYY-MM-DD"));
              }}
            />
          </Form.Item>
          <Form.Item
            name="price"
            label="当日价格（元）"
            rules={[{ required: true, message: "请输入大于 0 的价格" }]}
          >
            <InputNumber
              disabled={saving}
              min={0.01}
              precision={2}
              style={{ width: "100%" }}
            />
          </Form.Item>
          <Form.Item
            name="reason"
            label="修改原因"
            rules={[
              { required: true, whitespace: true, message: "请填写修改原因" },
            ]}
          >
            <Input.TextArea
              disabled={saving}
              maxLength={5000}
              rows={3}
              placeholder="例如：核对订单后补录实际价格"
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
