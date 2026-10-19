import React, { useState } from "react";
import { App, Button, Form, InputNumber, Modal, Space } from "antd";
import { Clock3 } from "lucide-react";
import { post, useMonitor } from "./lib";

export function CollectionFrequencyButton() {
  const { data, action } = useMonitor();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  if (data.access?.read_only) return null;
  return (
    <>
      <Button
        icon={<Clock3 size={15} />}
        disabled={!data.products.length}
        onClick={() => {
          const values = [
            ...new Set(data.products.map((p: any) => p.interval_hours)),
          ];
          form.setFieldsValue({
            interval_hours: values.length === 1 ? values[0] : 1,
          });
          setOpen(true);
        }}
      >
        统一采集周期
      </Button>
      <Modal
        title="统一修改自动采集周期"
        open={open}
        confirmLoading={busy}
        okText="应用到全部商品"
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
      >
        <p className="drawer-intro">
          应用到全部 {data.products.length}{" "}
          件商品，保留暂停状态及已排队、运行中的任务。一键采集的批次间隔仍为 8
          秒。
        </p>
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values) => {
            setBusy(true);
            try {
              const result = await action(() =>
                post("/collection/frequency", values),
              );
              message.success(
                `已统一 ${result.total} 件商品的周期：每 ${result.interval_hours} 小时采集，更新 ${result.updated} 件`,
              );
              setOpen(false);
            } catch {
              /* Shared action displays errors. */
            } finally {
              setBusy(false);
            }
          }}
        >
          <Form.Item
            name="interval_hours"
            label="自动采集周期（小时）"
            rules={[{ required: true, type: "number", min: 0.1, max: 8760 }]}
            extra="根据上次调度时间重新计算下次采集；暂停的商品不会自动恢复。"
          >
            <InputNumber
              min={0.1}
              max={8760}
              precision={2}
              style={{ width: "100%" }}
            />
          </Form.Item>
          <Space wrap>
            {[0.5, 1, 4, 12, 24].map((hours) => (
              <Button
                key={hours}
                onClick={() => form.setFieldsValue({ interval_hours: hours })}
              >
                {hours === 0.5
                  ? "每半小时"
                  : hours === 24
                    ? "每天"
                    : `每 ${hours} 小时`}
              </Button>
            ))}
          </Space>
        </Form>
      </Modal>
    </>
  );
}
