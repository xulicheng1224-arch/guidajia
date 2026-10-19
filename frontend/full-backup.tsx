import React, { useState } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Space,
  Table,
} from "antd";
import { ContextHelp, api, download, post, Row, useMonitor, useResource } from "./lib";

function AdminFullBackup() {
  const { message, modal } = App.useApp();
  const { value: backups, reload } = useResource<Row[]>(
    "/security/full-backups",
    [],
  );
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState<Row | null>(null);
  const [password, setPassword] = useState("");
  const [checkBusy, setCheckBusy] = useState(false);
  return (
    <div className="mt24">
      <div className="form-section-title">管理员完整备份与恢复</div>
      <Alert
        showIcon
        type="warning"
        message="请妥善保存备份密码，丢失后无法解密。恢复前必须停止服务。"
      />
      <Form
        name="full-backup"
        className="mt16"
        form={form}
        layout="vertical"
        initialValues={{ include_cookies: false }}
        onFinish={async (values) => {
          setBusy(true);
          try {
            const result = await post("/security/full-backups", {
              password: values.password,
              include_cookies: values.include_cookies,
            });
            message.success(`完整备份已创建，包含 ${result.user_count} 个账号`);
            form.resetFields();
            await reload();
          } catch (error: any) {
            message.error(error.message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item
          name="password"
          label="完整备份密码"
          rules={[
            {
              required: true,
              min: 12,
              max: 256,
              message: "请输入 12–256 位备份密码",
            },
          ]}
        >
          <Input.Password
            autoComplete="new-password"
            maxLength={256}
            style={{ maxWidth: 480 }}
          />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认备份密码"
          dependencies={["password"]}
          rules={[
            { required: true, message: "请再次输入备份密码" },
            ({ getFieldValue }) => ({
              validator: (_, value) =>
                value === getFieldValue("password")
                  ? Promise.resolve()
                  : Promise.reject(new Error("两次密码不一致")),
            }),
          ]}
        >
          <Input.Password
            autoComplete="new-password"
            maxLength={256}
            style={{ maxWidth: 480 }}
          />
        </Form.Item>
        <Form.Item name="include_cookies" valuePropName="checked">
          <Checkbox>包含所有账号各自保存的淘宝 Cookie</Checkbox>
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          创建加密完整备份
        </Button>
      </Form>
      <Table
        className="mt20"
        rowKey="name"
        dataSource={backups}
        pagination={{ pageSize: 5 }}
        scroll={{ x: 700 }}
        columns={[
          { title: "完整备份文件", dataIndex: "name" },
          {
            title: "大小",
            dataIndex: "size",
            render: (size) => `${(size / 1024 / 1024).toFixed(2)} MB`,
          },
          {
            title: "操作",
            render: (_, row) => (
              <Space wrap>
                <Button
                  size="small"
                  onClick={() => download(`/security/full-backups/${row.name}`)}
                >
                  下载
                </Button>
                <Button
                  size="small"
                  onClick={() => {
                    setPassword("");
                    setChecking(row);
                  }}
                >
                  校验备份
                </Button>
                <Button
                  size="small"
                  onClick={async () => {
                    const command = `.venv/bin/python restore_backup.py data/system-backups/${row.name} --full`;
                    try {
                      if (!navigator.clipboard?.writeText) throw new Error("clipboard_unavailable");
                      await navigator.clipboard.writeText(command);
                      message.success("恢复命令已复制；请先停止服务，再在项目目录的终端执行");
                    } catch {
                      modal.info({title:"恢复前请先停止服务",content:<pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere",userSelect:"text"}}>{command}</pre>});
                    }
                  }}
                >
                  复制恢复命令
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <ContextHelp title="备份范围与恢复步骤">
        <p>完整备份包含全部账号、用户数据、模型和设置，可选包含 Cookie。网页登录会话不会恢复，恢复后各账号自动采集默认暂停。</p>
        <p>先保存备份文件并停止服务。在项目目录执行复制的恢复命令，输入备份密码；如文件已移动，请替换文件路径。</p>
        <p>校验通过后才替换数据，恢复前数据会另存副本。完成后启动服务并重新登录。命令增加 --check-only 可只校验。</p>
      </ContextHelp>
      <Modal
        title="校验完整备份"
        open={!!checking}
        confirmLoading={checkBusy}
        okText="解密并校验"
        onCancel={() => {
          setChecking(null);
          setPassword("");
        }}
        onOk={async () => {
          setCheckBusy(true);
          try {
            const result = await post(
              `/security/full-backups/${checking!.name}/check`,
              { password },
            );
            message.success(
              `校验通过：${result.user_count} 个账号，${result.include_cookies ? "包含" : "未包含"}淘宝 Cookie`,
            );
            setChecking(null);
            setPassword("");
          } catch (error: any) {
            message.error(error.message);
          } finally {
            setCheckBusy(false);
          }
        }}
      >
        <p>校验不会更改当前账号或业务数据。</p>
        <Input.Password
          aria-label="校验备份密码"
          value={password}
          autoComplete="off"
          onChange={(e) => setPassword(e.target.value)}
        />
      </Modal>
    </div>
  );
}

export function FullBackupPanel() {
  const { data } = useMonitor();
  return data.access?.role === "admin" ? <AdminFullBackup /> : null;
}
