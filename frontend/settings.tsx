import { AccountSecurity } from "./account-security";
import { CookieVerification } from "./cookie-verification";
import { FullBackupPanel } from "./full-backup";
import { PriceSourcePanel } from "./price-source";
import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  App,
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  TimePicker,
  Tooltip,
  Upload as AntUpload,
} from "antd";
import {
  ArrowRight,
  Bell,
  Check,
  ChevronRight,
  CircleHelp,
  Copy,
  Database,
  Download,
  ExternalLink,
  Eye,
  FolderArchive,
  HardDrive,
  KeyRound,
  Laptop,
  LayoutGrid,
  LockKeyhole,
  LogIn,
  Monitor,
  Moon,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  ContextHelp,
  Blank,
  ImportDialog,
  PageTitle,
  Panel,
  Row,
  ago,
  api,
  date,
  download,
  opts,
  pagination,
  patch,
  post,
  put,
  remove,
  useMonitor,
  useResource,
} from "./lib";

function SettingsForm({
  id,
  children,
  fields,
  reset,
  onDirty,
}: {
  id: string;
  children: React.ReactNode;
  fields: string[];
  reset: number;
  onDirty: (dirty: boolean) => void;
}) {
  const { data, action } = useMonitor();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    form.setFieldsValue(data.settings);
    onDirty(false);
  }, [reset]);
  return (
    <Form
      form={form}
      id={`settings-${id}`}
      initialValues={data.settings}
      layout="vertical"
      onValuesChange={() => onDirty(true)}
      onFinish={async (v) => {
        setBusy(true);
        try {
          const body = Object.fromEntries(
            fields.filter((k) => v[k] !== undefined).map((k) => [k, v[k]]),
          );
          await action(() => patch("/settings", body), "设置已保存");
          onDirty(false);
        } finally {
          setBusy(false);
        }
      }}
    >
      {children}
      <div className="settings-save">
        <Button
          type="primary"
          icon={<Save size={14} />}
          loading={busy}
          htmlType="submit"
        >
          保存设置
        </Button>
        <Button
          onClick={() => {
            form.resetFields();
            form.setFieldsValue(data.settings);
            onDirty(false);
          }}
        >
          放弃修改
        </Button>
      </div>
    </Form>
  );
}

function ChannelEditor({
  channel,
  open,
  onClose,
}: {
  channel: string;
  open: boolean;
  onClose: () => void;
}) {
  const { data, action } = useMonitor();
  const [form] = Form.useForm();
  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue({
        enabled: true,
        ...data.settings.channels?.[channel],
      });
    }
  }, [open, channel, form]);
  const names: Row = {
    bark: "Bark",
    serverchan: "Server酱",
    wecom: "企业微信",
    feishu: "飞书",
    email: "邮件",
  };
  return (
    <Modal
      title={`配置 ${names[channel]}`}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="保存配置"
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={async (v) => {
          await action(
            () => patch("/settings", { channels: { [channel]: v } }),
            "渠道配置已保存",
          );
          onClose();
        }}
      >
        <Form.Item name="enabled" label="启用渠道" valuePropName="checked">
          <Switch />
        </Form.Item>
        {channel === "email" ? (
          <>
            <Form.Item
              name="host"
              label="SMTP 服务器"
              rules={[{ required: true }]}
            >
              <Input placeholder="smtp.example.com" />
            </Form.Item>
            <Form.Item name="port" label="SMTP 端口">
              <Select
                options={[
                  { label: "465 · SSL", value: 465 },
                  { label: "587 · STARTTLS", value: 587 },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="user"
              label="发件邮箱"
              rules={[{ required: true }, { type: "email" }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="password"
              label="授权码 / 密码"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="输入邮箱授权码" />
            </Form.Item>
            <Form.Item
              name="to"
              label="收件邮箱"
              rules={[{ required: true }, { type: "email" }]}
            >
              <Input />
            </Form.Item>
          </>
        ) : (
          <Form.Item
            name="url"
            label={
              channel === "bark"
                ? "含设备密钥的推送地址"
                : "完整 HTTPS 推送地址"
            }
            rules={[{ required: true }]}
            extra="密钥保存后不会回传明文。__saved__ 表示沿用已保存的值。"
          >
            <Input.Password placeholder="https://…" />
          </Form.Item>
        )}
      </Form>
      <div className="setting-note">
        <LockKeyhole size={16} />
        <p>
          配置保存后，可在提醒中心选择该渠道。测试发送会向你的设备或邮箱发送一条测试通知。
        </p>
      </div>
    </Modal>
  );
}

export function SettingsPage() {
  const { data, action, reload } = useMonitor();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "account";
  const [dirty, setDirty] = useState(false);
  const [reset, setReset] = useState(0);
  const [importKind, setImportKind] = useState<string | null>(null);
  const [channel, setChannel] = useState<string | null>(null);
  const [viewOpen, setViewOpen] = useState(false);
  const [view, setView] = useState<Row | null>(null);
  const [viewName, setViewName] = useState("");
  const [viewJSON, setViewJSON] = useState("{}");
  const [cookieBusy, setCookieBusy] = useState(false);
  const { value: backups, reload: loadBackups } = useResource<Row[]>(
    "/backups",
    [],
  );
  const { modal, message } = App.useApp();
  const config = data.settings;
  const session = data.session;
  const names: Row = {
    source: "价格来源",
    account: "淘宝账号",
    views: "视图管理",
    appearance: "外观与显示",
    charts: "图表默认",
    notifications: "通知",
    data: "数据口径",
    io: "导入导出",
    backup: "保留与备份",
    security: "访问与安全",
  };
  useEffect(() => { setDirty(false); }, [tab]);
  useEffect(() => {
    if (!["opening", "waiting"].includes(session.status)) return;
    const id = setInterval(() => reload().catch(() => {}), 2000);
    return () => clearInterval(id);
  }, [session.status]);
  const changeTab = (key: string) => {
    const proceed = () => {
      setParams({ tab: key });
      setDirty(false);
      setReset((n) => n + 1);
    };
    if (dirty)
      modal.confirm({
        title: "放弃未保存的设置？",
        content: "当前页面有尚未保存的修改。",
        onOk: proceed,
      });
    else proceed();
  };
  const fields = (key: string, children: React.ReactNode, keys: string[]) => (
    <SettingsForm key={key} id={key} fields={keys} reset={reset} onDirty={setDirty}>
      {children}
    </SettingsForm>
  );
  const label = (title: string, description?: string) => (
    <div className="setting-section-heading">
      <h2>{title}</h2>
      {description && <p>{description}</p>}
    </div>
  );
  const items: Row = {
    source: <PriceSourcePanel onDirty={setDirty} />,
    account: (
      <>
        <div className="account-hero">
          <div className="taobao-icon">淘</div>
          <div>
            <h2>淘宝 / 天猫账号</h2>
          </div>
          <Tag
            bordered={false}
            color={
              session.status === "saved"
                ? "green"
                : session.status === "expired"
                  ? "orange"
                  : "default"
            }
          >
            {session.status === "saved"
              ? "已保存会话"
              : session.status === "waiting"
                ? "等待登录"
                : session.status === "expired"
                  ? session.issue === "verification_required" ? "需要人工验证" : "需要重新登录"
                  : session.exists
                    ? "已有会话"
                    : "尚未登录"}
          </Tag>
        </div>
        <div className="account-details">
          <div>
            <span>会话状态</span>
            <strong>{session.message}</strong>
          </div>
          <div>
            <span>最近保存</span>
            <strong>{date(session.saved_at, "YYYY-MM-DD HH:mm:ss")}</strong>
          </div>
        </div>
        <div className="account-actions">
          {data.access?.role === "admin" && <Button
            type="primary"
            icon={<LogIn size={15} />}
            loading={session.status === "opening"}
            disabled={session.status === "waiting"}
            onClick={() =>
              action(
                () => post("/session/login"),
                "正在打开登录窗口",
              ).catch(() => {})
            }
          >
            {session.exists ? "重新打开登录窗口" : "打开登录窗口"}
          </Button>}
          <Button
            icon={<ExternalLink size={15} />}
            href="https://login.taobao.com/havanaone/login/login.htm"
            target="_blank"
            rel="noopener noreferrer"
          >
            在本设备打开淘宝登录页
          </Button>
          <Button
            icon={<Download size={15} />}
            onClick={() => download("/session/export-script/extension")}
          >
            下载免依赖 Cookie 导出工具 v1.2
          </Button>
          <Button
            icon={<CircleHelp size={15} />}
            onClick={() => modal.info({
              title: "免依赖导出 · Mac / Windows 通用",
              width: 560,
              content: <div>
                <p>1. 下载并完整解压工具包。</p>
                <p>2. Edge 打开 edge://extensions，Chrome 打开 chrome://extensions。</p>
                <p>3. 开启开发人员模式，点击「加载解压缩的扩展」，选择含 manifest.json 的文件夹。</p>
                <p>4. 在同一浏览器登录淘宝，保持淘宝及目标天猫商品页打开，再点击扩展中的导出按钮。</p>
                <p>5. 保存 JSON 后，回到这里点击「上传 Cookie 文件」。</p>
                <p>无需安装额外运行环境。v1.2 会读取淘宝 / 天猫 Cookie（包括分区记录）和已打开页面、内嵌淘宝登录页的本地存储，文件不会自动上传。旧版用户请重新加载扩展，确认版本 1.2.0 后重新导出。</p>
              </div>,
              okText: "知道了",
            })}
          >
            导出工具使用说明
          </Button>
          <AntUpload
            accept=".json,.txt"
            showUploadList={false}
            beforeUpload={async (file) => {
              setCookieBusy(true);
              try {
                const content = await file.text();
                await action(
                  () =>
                    post("/session/import", {
                      filename: file.name,
                      content,
                    }),
                  "淘宝 Cookie 已导入",
                );
              } finally {
                setCookieBusy(false);
              }
              return false;
            }}
          >
            <Button
              icon={<Upload size={15} />}
              loading={cookieBusy}
            >
              上传 Cookie 文件
            </Button>
          </AntUpload>
          {session.status === "waiting" && (
            <Button
              type="primary"
              ghost
              icon={<Check size={14} />}
              onClick={() =>
                action(
                  () => post("/session/complete"),
                  "正在检查并保存登录状态",
                ).catch(() => {})
              }
            >
              完成登录并保存
            </Button>
          )}
          {session.status === "waiting" && (
            <Button
              icon={<RefreshCw size={14} />}
              loading={session.navigating}
              onClick={() =>
                action(
                  () => post("/session/retry"),
                  "正在重新打开登录页",
                ).catch(() => {})
              }
            >
              重试打开登录页
            </Button>
          )}
          {["opening", "waiting"].includes(session.status) && (
            <Button
              onClick={() =>
                action(() => post("/session/cancel")).catch(() => {})
              }
            >
              取消登录
            </Button>
          )}
          <Popconfirm
            title="清除已保存的登录状态？"
            onConfirm={() =>
              action(() => post("/session/clear"), "登录状态已清除")
            }
          >
            <Button
              disabled={!session.exists}
              danger
              type="text"
            >
              清除会话
            </Button>
          </Popconfirm>
        </div>
        {(session.issue || ["opening", "waiting"].includes(session.status)) && <Alert
          className="mt20"
          type={session.issue ? "warning" : "info"}
          showIcon
          message={
            session.issue
              ? session.message
              : data.access?.role === "admin" ? "完成浏览器登录后，回到这里点击「完成登录并保存」。页面加载失败或尚未登录时，窗口会保留供你继续处理。" : "在本设备登录淘宝，导出并上传 Cookie。采集只使用当前账号保存的淘宝会话。"
          }
        />}
        <CookieVerification />
        {data.access?.role === "admin" && fields(
          "account",
          <>
            <div className="form-section-title mt24">浏览器配置</div>
            <Form.Item name="browser" label="用于登录和采集的浏览器">
              <Select
                style={{ maxWidth: 360 }}
                options={[
                  {
                    label: "Microsoft Edge",
                    value: "msedge",
                  },
                  {
                    label: "Google Chrome",
                    value: "chrome",
                  },
                  {
                    label: "Chromium",
                    value: "chromium",
                  },
                ]}
              />
            </Form.Item>

          </>,
          ["browser"],
        )}
      </>
    ),
    views: (
      <>
        {label(
          "视图管理",
          "视图保存在服务端。看板布局、商品筛选和比较配置可以分别保存。",
        )}
        <div className="table-toolbar">
          <Button
            icon={<Upload size={14} />}
            onClick={() => setImportKind("view")}
          >
            导入视图
          </Button>
          <Button
            type="primary"
            icon={<Plus size={14} />}
            onClick={() => {
              setView(null);
              setViewName("");
              setViewJSON(
                JSON.stringify(
                  {
                    dashboard_cards: config.dashboard_cards,
                    card_sizes: config.card_sizes,
                  },
                  null,
                  2,
                ),
              );
              setViewOpen(true);
            }}
          >
            新建视图
          </Button>
        </div>
        <Table
          rowKey="id"
          dataSource={data.views}
          pagination={pagination}
          scroll={{ x: 750 }}
          columns={[
            {
              title: "视图名称",
              dataIndex: "name",
              render: (v: string) => <strong>{v}</strong>,
            },
            {
              title: "包含内容",
              render: (_: any, v: Row) =>
                [
                  v.config.dashboard_cards ? "看板布局" : null,
                  v.config.filters ? "商品筛选" : null,
                  v.config.comparison ? "比较配置" : null,
                ]
                  .filter(Boolean)
                  .join("、") || "自定义配置",
            },
            {
              title: "最后修改",
              dataIndex: "updated_at",
              render: (v: string) => date(v),
            },
            {
              title: "默认",
              render: (_: any, v: Row) => (
                <Radio
                  checked={config.default_view === v.id}
                  onClick={() =>
                    action(
                      () => patch("/settings", { default_view: v.id }),
                      "默认视图已更新",
                    ).catch(() => {})
                  }
                />
              ),
            },
            {
              title: "操作",
              render: (_: any, v: Row) => (
                <Space size={0}>
                  <Button
                    size="small"
                    type="link"
                    onClick={() => {
                      setView(v);
                      setViewName(v.name);
                      setViewJSON(JSON.stringify(v.config, null, 2));
                      setViewOpen(true);
                    }}
                  >
                    编辑
                  </Button>
                  <Button
                    size="small"
                    type="link"
                    onClick={() =>
                      action(
                        () =>
                          post("/entities/view", {
                            name: v.name + " · 副本",
                            config: v.config,
                          }),
                        "视图已复制",
                      ).catch(() => {})
                    }
                  >
                    复制
                  </Button>
                  <Button
                    size="small"
                    type="link"
                    onClick={() => {
                      const blob = new Blob(
                        [
                          JSON.stringify(
                            [{ name: v.name, config: v.config }],
                            null,
                            2,
                          ),
                        ],
                        { type: "application/json" },
                      );
                      const a = document.createElement("a");
                      a.href = URL.createObjectURL(blob);
                      a.download = `${v.name}.json`;
                      a.click();
                      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
                    }}
                  >
                    导出
                  </Button>
                  <Popconfirm
                    title="删除此视图？"
                    onConfirm={() =>
                      action(
                        () => remove(`/entities/view/${v.id}`),
                        "视图已移至回收站",
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
                title="保存一个属于你的工作视图"
                text="在总览、商品库或比较页保存当前布局与筛选。"
              />
            ),
          }}
        />
      </>
    ),
    appearance: fields(
      "appearance",
      <>
        {label("外观与显示", "让工作空间符合你的阅读习惯。")}
        <Form.Item name="theme" label="界面主题">
          <Radio.Group className="theme-options">
            <Radio.Button value="light">
              <Sun size={20} />
              <span>浅色</span>
            </Radio.Button>
            <Radio.Button value="dark">
              <Moon size={20} />
              <span>深色</span>
            </Radio.Button>
            <Radio.Button value="system">
              <Monitor size={20} />
              <span>跟随系统</span>
            </Radio.Button>
          </Radio.Group>
        </Form.Item>
        <Form.Item name="density" label="表格行密度">
          <Segmented
            options={[
              { label: "紧凑", value: "small" },
              { label: "标准", value: "middle" },
              { label: "宽松", value: "large" },
            ]}
          />
        </Form.Item>
        <div className="readonly-settings">
          <div>
            <span>语言</span>
            <b>简体中文</b>
          </div>
          <div>
            <span>时间</span>
            <b>北京时间 · Asia/Shanghai</b>
          </div>
          <div>
            <span>金额</span>
            <b>人民币 · ¥ 1,234.00</b>
          </div>
        </div>
      </>,
      ["theme", "density"],
    ),
    charts: fields(
      "charts",
      <>
        {label("图表默认", "这些偏好用于新打开的价格图表。")}
        <Form.Item
          name="chart_step"
          label="阶梯线"
          valuePropName="checked"
          extra="阶梯线可以准确表示价格持续时间。"
        >
          <Switch checkedChildren="阶梯线" unCheckedChildren="折线" />
        </Form.Item>
        <Form.Item name="default_range" label="默认时间范围">
          <Select
            style={{ width: 220 }}
            options={[
              { label: "7 天", value: 7 },
              { label: "30 天", value: 30 },
              { label: "90 天", value: 90 },
              { label: "365 天", value: 365 },
            ]}
          />
        </Form.Item>
        <Form.Item name="chart_layers" label="默认显示图层">
          <Checkbox.Group
            options={[
              { label: "到手价", value: "sale" },
              { label: "划线价", value: "original" },
              { label: "事件标注", value: "events" },
              { label: "人工标注", value: "notes" },
            ]}
          />
        </Form.Item>
      </>,
      ["chart_step", "default_range", "chart_layers"],
    ),
    notifications: (
      <>
        {label("通知渠道", "配置属于你自己的接收渠道，密钥不回传前端。")}
        <div className="settings-channel-grid">
          {[
            { key: "bark", name: "Bark", caption: "iPhone 原生推送" },
            { key: "serverchan", name: "Server酱", caption: "微信消息推送" },
            { key: "wecom", name: "企业微信", caption: "群机器人 Webhook" },
            { key: "feishu", name: "飞书", caption: "群机器人 Webhook" },
            { key: "email", name: "邮件", caption: "SMTP 安全连接" },
          ].map((c) => (
            <div className="settings-channel" key={c.key}>
              <div className="channel-icon">
                <Bell size={18} />
              </div>
              <h3>{c.name}</h3>
              <p>{c.caption}</p>
              <Tag
                bordered={false}
                color={
                  config.channels?.[c.key]?.enabled &&
                  config.channels?.[c.key]?.configured
                    ? "green"
                    : "default"
                }
              >
                {config.channels?.[c.key]?.configured ? "已配置" : "未配置"}
              </Tag>
              <div>
                <Button size="small" onClick={() => setChannel(c.key)}>
                  配置
                </Button>
                <Button
                  size="small"
                  type="text"
                  disabled={!config.channels?.[c.key]?.enabled}
                  onClick={() =>
                    action(
                      () => post(`/channels/${c.key}/test`),
                      "测试通知已发送",
                    ).catch(() => {})
                  }
                >
                  测试
                </Button>
              </div>
            </div>
          ))}
        </div>
        {fields(
          "notifications",
          <>
            <div className="form-section-title mt24">免打扰设置</div>
            <div className="form-grid">
              <Form.Item name="quiet_start" label="开始时间（北京时间）">
                <Input placeholder="23:00" />
              </Form.Item>
              <Form.Item name="quiet_end" label="结束时间（北京时间）">
                <Input placeholder="08:00" />
              </Form.Item>
            </div>
            <p className="muted small">
              支持跨午夜时段；起止相同表示关闭免打扰。期间通知排队，结束后发送。
            </p>
          </>,
          ["quiet_start", "quiet_end"],
        )}
      </>
    ),
    data: fields(
      "data",
      <>
        {label("数据口径", "统一影响图表、统计、变价事件和趋势参考。")}
        <Form.Item name="confirm_count" label="新价连续几次一致才确认变价">
          <InputNumber min={1} max={10} precision={0} />
        </Form.Item>
        <Form.Item
          name="anomaly_percent"
          label="偏离近期有效价超过多少 % 进入待确认"
        >
          <InputNumber min={1} max={1000} />
        </Form.Item>
        <Form.Item
          name="include_coupon"
          label="计入需领券 / 会员价格"
          valuePropName="checked"
          extra="开启后纳入券后价与会员价。"
        >
          <Switch />
        </Form.Item>
        <Form.Item
          name="trusted_only"
          label="同款比价默认仅使用官方旗舰和授权店"
          valuePropName="checked"
        >
          <Switch />
        </Form.Item>
        <ContextHelp title="规格与价格口径">不同容量、版本、盒装和散片保持独立规格。关闭券后价与会员价时仍保留原始观测，但不纳入常规价格线与统计。</ContextHelp>
      </>,
      ["confirm_count", "anomaly_percent", "include_coupon", "trusted_only"],
    ),
    io: (
      <>
        {label(
          "导入导出",
          "导入先预览、确认后写入。常规导出不包含 Cookie、令牌或通知渠道密钥。",
        )}
        <Table
          rowKey="kind"
          pagination={false}
          dataSource={[
            { kind: "product", name: "商品清单", formats: "CSV / JSON" },
            {
              kind: "observations",
              name: "价格记录（含人工修正）",
              formats: "CSV / JSON",
            },
            { kind: "rule", name: "提醒规则", formats: "JSON" },
            { kind: "event", name: "事件日历", formats: "CSV / JSON" },
            { kind: "view", name: "视图配置", formats: "JSON" },
          ]}
          columns={[
            { title: "内容", dataIndex: "name" },
            { title: "格式", dataIndex: "formats" },
            {
              title: "导出",
              render: (_: any, r: Row) => (
                <Space>
                  <Button
                    size="small"
                    onClick={() => download(`/export/${r.kind}?format=json`)}
                  >
                    JSON
                  </Button>
                  {r.formats.includes("CSV") && (
                    <Button
                      size="small"
                      onClick={() => download(`/export/${r.kind}?format=csv`)}
                    >
                      CSV
                    </Button>
                  )}
                </Space>
              ),
            },
            {
              title: "导入",
              render: (_: any, r: Row) => (
                <Button
                  size="small"
                  icon={<Upload size={13} />}
                  onClick={() => setImportKind(r.kind)}
                >
                  导入
                </Button>
              ),
            },
          ]}
        />
        <ContextHelp title="导入导出规则">导入商品默认暂停采集。价格记录需包含已有商品 ID，导出金额单位为分；具体字段与模板可在导入窗口查看。</ContextHelp>
      </>
    ),
    backup: (
      <>
        {fields(
          "backup",
          <>
            {label(
              "保留与备份",
              "自动生成一致的数据备份。",
            )}
            <div className="form-grid">
              <Form.Item name="observation_years" label="原始观测保留（年）">
                <InputNumber
                  min={1}
                  max={20}
                  precision={0}
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Form.Item name="log_days" label="采集日志保留（天）">
                <InputNumber
                  min={1}
                  max={3650}
                  precision={0}
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Form.Item name="trash_days" label="回收站保留（天）">
                <InputNumber
                  min={1}
                  max={3650}
                  precision={0}
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Form.Item
                name="backup_hour"
                label="每日备份时间（北京时间整点）"
              >
                <InputNumber
                  min={0}
                  max={23}
                  precision={0}
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Form.Item name="backup_keep" label="保留最近备份份数">
                <InputNumber
                  min={1}
                  max={100}
                  precision={0}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            </div>
          </>,
          [
            "observation_years",
            "log_days",
            "trash_days",
            "backup_hour",
            "backup_keep",
          ],
        )}
        <div className="backup-card">
          <div className="backup-symbol">
            <Database size={25} />
          </div>
          <div>
            <strong>账号数据备份</strong>
            <p>最近成功：{date(config.last_backup, "YYYY-MM-DD HH:mm:ss")}</p>
          </div>
          <Button
            type="primary"
            ghost
            icon={<FolderArchive size={14} />}
            onClick={async () => {
              await action(() => post("/backup"), "备份已创建");
              await loadBackups();
            }}
          >
            立即备份
          </Button>
        </div>
        <Table
          rowKey="name"
          dataSource={backups}
          pagination={{ pageSize: 5 }}
          columns={[
            { title: "备份文件", dataIndex: "name" },
            {
              title: "大小",
              dataIndex: "size",
              render: (v: number) => `${(v / 1024).toFixed(0)} KB`,
            },
            {
              title: "下载",
              render: (_: any, r: Row) => (
                <Button
                  size="small"
                  icon={<Download size={13} />}
                  onClick={() => download(`/backups/${r.name}`)}
                >
                  下载
                </Button>
              ),
            },
          ]}
        />
        <ContextHelp title="业务备份范围">仅包含当前账号的业务数据，不包含 Cookie、账号库、模型或通知密钥。恢复前请先停止服务。</ContextHelp>
        <FullBackupPanel />
      </>
    ),
    security: (
      <>
        {label("账号与安全", "管理自己的密码与登录状态。")}
        <AccountSecurity />
      </>
    ),
  };
  return (
    <>
      <PageTitle
        eyebrow="MAKE IT YOURS"
        title="设置"
        description="配置你的工作空间、采集会话与数据偏好。"
        actions={
          <>
            {dirty && <Tag color="gold">有未保存修改</Tag>}
            <Button
              onClick={() => {
                setReset((n) => n + 1);
                setDirty(false);
              }}
              disabled={!dirty}
            >
              放弃修改
            </Button>
            {[
              "account",
              "appearance",
              "charts",
              "notifications",
              "data",
              "backup",
            ].includes(tab) && (
              <Button
                type="primary"
                icon={<Save size={14} />}
                onClick={() => {
                  const form = document.getElementById(
                    `settings-${tab}`,
                  ) as HTMLFormElement;
                  form?.requestSubmit();
                }}
              >
                保存设置
              </Button>
            )}
          </>
        }
      />
      <div className="settings-layout">
        <nav className="settings-nav">
          {Object.entries(names).map(([key, label]) => (
            <button
              key={key}
              className={tab === key ? "selected" : ""}
              onClick={() => changeTab(key)}
            >
              {label}
              <ChevronRight size={13} />
            </button>
          ))}

        </nav>
        <Panel className="settings-main">{items[tab] || items.account}</Panel>
      </div>
      <ChannelEditor
        channel={channel || "bark"}
        open={!!channel}
        onClose={() => setChannel(null)}
      />
      <ImportDialog
        kind={importKind || "product"}
        open={!!importKind}
        onClose={() => setImportKind(null)}
      />
      <Modal
        title={view ? "编辑视图" : "新建视图"}
        open={viewOpen}
        onCancel={() => setViewOpen(false)}
        onOk={async () => {
          try {
            if (!viewName.trim()) {
              message.warning("请输入视图名称");
              return;
            }
            const c = JSON.parse(viewJSON);
            await action(
              () =>
                view
                  ? put(`/entities/view/${view.id}`, {
                      name: viewName,
                      config: c,
                      version: view.version,
                    })
                  : post("/entities/view", { name: viewName, config: c }),
              "视图已保存",
            );
            setViewOpen(false);
          } catch (e: any) {
            message.error(e.message);
          }
        }}
      >
        <Input
          value={viewName}
          onChange={(e) => setViewName(e.target.value)}
          placeholder="视图名称"
        />
        <p className="muted small mt16">视图配置（JSON）</p>
        <Input.TextArea
          value={viewJSON}
          onChange={(e) => setViewJSON(e.target.value)}
          rows={9}
          style={{ fontFamily: "var(--font-interface)" }}
        />
      </Modal>
    </>
  );
}
