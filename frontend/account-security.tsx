import React, { useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
} from "antd";
import { KeyRound, ShieldCheck } from "lucide-react";
import { api, date, patch, post, Row, useMonitor } from "./lib";

const passwordRules = [
  { required: true, message: "请输入密码" },
  { min: 8, max: 128, message: "密码须为 8–128 位" },
  {
    validator: (_: unknown, value: string) =>
      !value || (/[\p{L}]/u.test(value) && /\p{N}/u.test(value))
        ? Promise.resolve()
        : Promise.reject(new Error("密码须包含字母和数字")),
  },
];

export function AccountSecurity() {
  const { data, action } = useMonitor();
  const { message } = App.useApp();
  const user = data.access?.user || {};
  const admin = user.role === "admin";
  const [form] = Form.useForm();
  const [resetForm] = Form.useForm();
  const [users, setUsers] = useState<Row[]>([]);
  const [target, setTarget] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const loadUsers = async () => setUsers(await api<Row[]>("/security/users"));
  useEffect(() => {
    if (admin) loadUsers().catch((e) => message.error(e.message));
  }, [admin]);
  const confirmRule =
    (field: string) =>
    ({ getFieldValue }: any) => ({
      validator(_: unknown, value: string) {
        return value === getFieldValue(field)
          ? Promise.resolve()
          : Promise.reject(new Error("两次输入的密码不一致"));
      },
    });
  return (
    <>
      <div className="security-status">
        <ShieldCheck size={27} />
        <div>
          <strong>{user.email}</strong>
          <p>
            {admin ? "唯一管理员" : "普通用户"} ·
            当前账号的商品、价格、Cookie、设置和模型独立保存
          </p>
        </div>
        <Tag color={admin ? "blue" : "success"}>
          {admin ? "管理员" : "普通用户"}
        </Tag>
      </div>
      <div className="form-section-title mt24">修改我的密码</div>
      <Form
        name="account-password"
        form={form}
        layout="vertical"
        style={{ maxWidth: 480 }}
        onFinish={async (values) => {
          setBusy(true);
          try {
            await post("/account/password", {
              current_password: values.current,
              password: values.password,
            });
            message.success("密码已修改，请重新登录");
            form.resetFields();
            window.location.reload();
          } catch (error: any) {
            message.error(error.message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item
          name="current"
          label="当前密码"
          rules={[{ required: true, message: "请输入当前密码" }]}
        >
          <Input.Password autoComplete="current-password" maxLength={128} />
        </Form.Item>
        <Form.Item name="password" label="新密码" rules={passwordRules}>
          <Input.Password autoComplete="new-password" maxLength={128} />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={["password"]}
          rules={[{ required: true }, confirmRule("password")]}
        >
          <Input.Password autoComplete="new-password" maxLength={128} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          修改密码并重新登录
        </Button>
      </Form>
      <p className="muted">
        修改密码会退出此账号在所有设备上的登录。普通用户忘记密码时，请联系管理员重置。
      </p>
      {admin && (
        <>
          <div className="form-section-title mt24">注册账号管理</div>
          <p className="muted">
            新注册账号统一为普通用户。这里可以管理账号状态；每个账号的工作空间独立，管理员页面展示自己的数据。
          </p>
          <Table
            rowKey="id"
            dataSource={users}
            pagination={{ pageSize: 10 }}
            scroll={{ x: 680 }}
            columns={[
              { title: "账号", dataIndex: "email" },
              {
                title: "权限",
                render: (_, row) => (
                  <Tag color={row.role === "admin" ? "blue" : "default"}>
                    {row.role === "admin" ? "唯一管理员" : "普通用户"}
                  </Tag>
                ),
              },
              {
                title: "状态",
                render: (_, row) => (
                  <Tag color={row.disabled ? "red" : "green"}>
                    {row.disabled ? "已停用" : "正常"}
                  </Tag>
                ),
              },
              {
                title: "最近登录",
                dataIndex: "last_login",
                render: (value) => date(value, "YYYY-MM-DD HH:mm"),
              },
              {
                title: "操作",
                render: (_, row) =>
                  row.role === "admin" ? (
                    <span className="muted">当前管理员</span>
                  ) : (
                    <Space>
                      <Button
                        size="small"
                        icon={<KeyRound size={13} />}
                        onClick={() => {
                          resetForm.resetFields();
                          setTarget(row);
                        }}
                      >
                        重置密码
                      </Button>
                      <Popconfirm
                        title={
                          row.disabled
                            ? "重新启用该账号？"
                            : "停用该账号并退出其所有登录？"
                        }
                        onConfirm={async () => {
                          await action(
                            () =>
                              patch(`/security/users/${row.id}`, {
                                disabled: !row.disabled,
                              }),
                            row.disabled ? "账号已启用" : "账号已停用",
                          );
                          await loadUsers();
                        }}
                      >
                        <Button size="small" danger={!row.disabled}>
                          {row.disabled ? "启用" : "停用"}
                        </Button>
                      </Popconfirm>
                    </Space>
                  ),
              },
            ]}
          />
          <Modal
            open={!!target}
            title={`重置密码 · ${target?.email || ""}`}
            onCancel={() => setTarget(null)}
            okText="重置并退出该账号的登录"
            confirmLoading={busy}
            destroyOnHidden
            onOk={() => resetForm.submit()}
          >
            <Form
              name="account-user-password"
              form={resetForm}
              layout="vertical"
              onFinish={async (values) => {
                setBusy(true);
                try {
                  await action(
                    () =>
                      post(`/security/users/${target!.id}/password`, {
                        password: values.password,
                      }),
                    "密码已重置，请将新密码告知账号使用者",
                  );
                  resetForm.resetFields();
                  setTarget(null);
                } catch {
                  /* action displays the request error. */
                } finally {
                  setBusy(false);
                }
              }}
            >
              <Form.Item name="password" label="新密码" rules={passwordRules}>
                <Input.Password autoComplete="new-password" maxLength={128} />
              </Form.Item>
              <Form.Item
                name="confirm"
                label="确认密码"
                dependencies={["password"]}
                rules={[{ required: true }, confirmRule("password")]}
              >
                <Input.Password autoComplete="new-password" maxLength={128} />
              </Form.Item>
            </Form>
          </Modal>
        </>
      )}
    </>
  );
}
