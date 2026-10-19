import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  App,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  Upload,
} from "antd";
import {
  Download,
  Upload as UploadIcon,
  Plus,
  ArrowUpRight,
  Inbox,
} from "lucide-react";
import dayjs from "dayjs";
import utc from "dayjs/plugin/utc";
import timezone from "dayjs/plugin/timezone";
import relativeTime from "dayjs/plugin/relativeTime";
import { createQueuedRefresh } from "./request-state";
import "dayjs/locale/zh-cn";
dayjs.extend(utc);
dayjs.extend(timezone);
dayjs.extend(relativeTime);
dayjs.locale("zh-cn");
dayjs.tz.setDefault("Asia/Shanghai");
export type Row = Record<string, any>;
export const categories = [
  "显卡",
  "CPU",
  "内存",
  "SSD",
  "主板",
  "电源",
  "机箱",
  "散热",
  "显示器",
  "其他",
];
export const labels = [
  "接近阶段低点",
  "建议观望",
  "可能涨价",
  "平稳",
  "数据不足",
];
export const colors = [
  "#315cf6",
  "#1aa58a",
  "#eaa23f",
  "#9a72e8",
  "#e77682",
  "#3db4d1",
];
export const money = (n: number | null | undefined) =>
  n == null
    ? "—"
    : `¥ ${new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n / 100)}`;
export const date = (v?: string, format = "MM-DD HH:mm") =>
  v ? dayjs(v).tz("Asia/Shanghai").format(format) : "—";
export const ago = (v?: string) => (v ? dayjs(v).fromNow() : "尚未采集");
export const pct = (n?: number | null) =>
  n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
export const opts = (values: string[]) =>
  values.map((v) => ({ label: v, value: v }));
export const statusNames: Row = {
  api_not_configured: "API 未配置",
  api_auth_error: "API 鉴权失败",
  api_permission_error: "API 权限不足",
  api_rate_limit: "API 频率受限",
  api_http_error: "API 网关错误",
  api_network_error: "API 网络错误",
  api_response_invalid: "API 响应无效",
  api_empty: "API 未返回商品",
  api_error: "API 业务错误",
  api_sku_required: "API 需要 SKU",
  api_sku_unsupported: "API 不支持指定 SKU",
  source_unsupported: "当前来源不支持",
  success: "采集成功",
  not_run: "等待采集",
  price_masked: "金额隐藏",
  price_missing: "未读到价格",
  price_region_missing: "价格区域缺失",
  login_or_redirect: "需要登录",
  sku_unverified: "规格待核对",
  sku_mismatch: "商品规格不匹配",
  item_mismatch: "商品不匹配",
  crawler_error: "采集失败",
  setup_error: "浏览器启动失败",
  timeout: "采集超时",
  anomaly: "异常价待确认",
  blocked: "遇到验证页",
  page_missing: "商品页面不存在",
  in_stock: "有货",
  out_of_stock: "缺货",
  unknown: "未知",
  low_stock: "低库存",
  delisted: "已下架",
  regular: "普通价",
  coupon: "券后价",
  member: "会员价",
  pending: "等待发送",
  sent: "已发送",
  retry: "等待重试",
  failed: "失败",
  queued: "排队中",
  running: "采集中",
  cancelled: "已取消",
  inapp: "站内",
  bark: "Bark",
  serverchan: "Server酱",
  wecom: "企业微信",
  feishu: "飞书",
  email: "邮件",
};

export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!res.ok) {
    const body = await res
      .json()
      .catch(() => ({ detail: `请求失败 ${res.status}` }));
    if (res.status === 401 && body.code === "login_required") {
      window.location.reload();
    }
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail),
    );
  }
  return res.json();
}
export const post = (path: string, data: Row = {}) =>
  api(path, { method: "POST", body: JSON.stringify(data) });
export const put = (path: string, data: Row) =>
  api(path, { method: "PUT", body: JSON.stringify(data) });
export const patch = (path: string, data: Row) =>
  api(path, { method: "PATCH", body: JSON.stringify(data) });
export const remove = (path: string) => api(path, { method: "DELETE" });
export function download(path: string) {
  const a = document.createElement("a");
  a.href = `/api${path}`;
  a.download = "";
  a.click();
}
export function downloadJSON(name: string, value: any) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

type ContextValue = {
  data: Row;
  reload: () => Promise<void>;
  loading: boolean;
  action: (fn: () => Promise<any>, success?: string) => Promise<any>;
};
const Context = createContext<ContextValue>(null!);
export const useMonitor = () => useContext(Context);
export function useReducedMotion() {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}
export function Provider({ children }: { children: React.ReactNode }) {
  const [data, setData] = useState<Row>(() => ({
    products: [],
    events: [],
    rules: [],
    views: [],
    settings: {},
    health: {},
    session: {},
  }));
  const [loading, setLoading] = useState(true);
  const { message } = App.useApp();
  const refreshState = useMemo(() => createQueuedRefresh(() => api<Row>("/state"), setData), []);
  const reload = useCallback(async () => {
    try { await refreshState(); }
    finally { setLoading(false); }
  }, [refreshState]);
  useEffect(() => {
    let disposed = false;
    let stream: EventSource | undefined;
    let signature = "";
    api("/bootstrap")
      .then(() => {
        if (!disposed) return reload();
      })
      .then(() => {
        if (disposed) return;
        stream = new EventSource("/api/stream");
        stream.onmessage = (event) => {
          const update = JSON.parse(event.data);
          const next = JSON.stringify([update.health, update.session]);
          if (next !== signature) {
            signature = next;
            reload().catch(() => {});
          }
        };
      })
      .catch((e) => {
        message.error(e.message);
        setLoading(false);
      });
    const id = setInterval(() => {
      if (!disposed) reload().catch(() => {});
    }, 30000);
    return () => {
      disposed = true;
      clearInterval(id);
      stream?.close();
    };
  }, [reload]);
  const action = async (fn: () => Promise<any>, success?: string) => {
    let result;
    try {
      result = await fn();
    } catch (e: any) {
      message.error(e.message);
      throw e;
    }
    if (success) message.success(success);
    try { await reload(); }
    catch { message.warning({key:"refresh-after-action",content:"操作已完成，但页面刷新失败，请稍后刷新页面。"}); }
    return result;
  };
  return (
    <Context.Provider value={{ data, reload, loading, action }}>
      {children}
    </Context.Provider>
  );
}
export function useResource<T>(path: string, initial: T) {
  const [state, setState] = useState<{ owner: object | null; value: T; busy: boolean; error: string | null }>({owner:null,value:initial,busy:true,error:null});
  const active = useRef<object | null>(null);
  const { message } = App.useApp();
  const { data } = useMonitor();
  const entry = useMemo(() => {
    let controller = new AbortController();
    let lastError = "";
    const owner = {};
    const refresh = createQueuedRefresh(() => api<T>(path,{signal:controller.signal}), value => {
      if (active.current === owner) {
        lastError = "";
        setState({owner,value,busy:true,error:null});
      }
    });
    async function load(): Promise<void> {
      if (active.current !== owner) return;
      if (controller.signal.aborted) controller = new AbortController();
      setState(previous => ({owner,value:previous.owner === owner ? previous.value : initial,busy:true,error:null}));
      try { await refresh(); }
      catch (error:any) {
        if (active.current !== owner) return;
        if (error.name === "AbortError") {
          if (!controller.signal.aborted) return load();
          return;
        }
        const text = error.message || "数据加载失败，请重试";
        setState(previous => ({...previous,error:text}));
        if (lastError !== text) message.error({key:`resource:${path}`,content:text});
        lastError = text;
      } finally {
        if (active.current === owner) setState(previous => ({...previous,busy:false}));
      }
    }
    return {owner,load,abort:() => controller.abort()};
  }, [path]);
  active.current = entry.owner;
  useEffect(() => {
    active.current = entry.owner;
    return () => {
      if (active.current === entry.owner) active.current = null;
      entry.abort();
    };
  }, [entry]);
  useEffect(() => {
    entry.load().catch(() => {});
  }, [entry, data]);
  const current = state.owner === entry.owner ? state : {value:initial,busy:true,error:null};
  return { ...current, reload: entry.load, setValue: (value:T) => setState(previous => ({...previous,owner:entry.owner,value})) };
}
export function TrendTag({ label }: { label: string }) {
  const color: Row = {
    建议观望: "gold",
    接近阶段低点: "green",
    可能涨价: "red",
    平稳: "blue",
    数据不足: "default",
  };
  return (
    <Tag bordered={false} color={color[label] || "default"}>
      {label}
    </Tag>
  );
}
export function Status({ value }: { value: string }) {
  const color = ["success", "in_stock", "sent"].includes(value)
    ? "success"
    : [
          "anomaly",
          "price_masked",
          "sku_unverified",
          "pending",
          "queued",
          "running",
          "retry",
        ].includes(value)
      ? "warning"
      : [
            "crawler_error",
            "failed",
            "timeout",
            "setup_error",
            "login_or_redirect",
          ].includes(value)
        ? "error"
        : "default";
  return (
    <Tag bordered={false} color={color}>
      {statusNames[value] || value}
    </Tag>
  );
}
export function PageTitle({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <Space wrap>{actions}</Space>
    </div>
  );
}
export function Panel({
  title,
  extra,
  children,
  className = "",
}: {
  title?: React.ReactNode;
  extra?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || extra) && (
        <div className="panel-heading">
          <h3>{title}</h3>
          <Space size={6}>{extra}</Space>
        </div>
      )}
      <div className="panel-body">{children}</div>
    </section>
  );
}
export function ContextHelp({title="使用说明",children}:{title?:string;children:React.ReactNode}) {
  return <details className="context-help"><summary>{title}</summary><div>{children}</div></details>;
}
export function Metric({
  label,
  value,
  hint,
  icon,
  accent = false,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  icon?: React.ReactNode;
  accent?: boolean;
}) {
  return (
    <div className={`metric ${accent ? "metric-accent" : ""}`}>
      <div className="metric-label">
        {label}
        <span>{icon}</span>
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-hint">{hint || "—"}</div>
    </div>
  );
}
export function Blank({
  title = "这里还没有数据",
  text = "添加商品并完成首次采集后，数据会显示在这里。",
  action,
}: {
  title?: string;
  text?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="blank">
      <div className="blank-icon">
        <Inbox size={26} />
      </div>
      <h3>{title}</h3>
      <p>{text}</p>
      {action}
    </div>
  );
}
export const pagination = {
  defaultPageSize: 10,
  showSizeChanger: true,
  showTotal: (n: number) => `共 ${n} 条`,
  pageSizeOptions: [10, 20, 50, 100],
};
export function ImportDialog({
  kind,
  open,
  onClose,
}: {
  kind: string;
  open: boolean;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const { action } = useMonitor();
  const { message } = App.useApp();
  return (
    <Modal
      title="导入并预览"
      open={open}
      onCancel={onClose}
      width={700}
      okText="确认导入"
      okButtonProps={{ disabled: !preview?.new, loading: busy }}
      onOk={async () => {
        setBusy(true);
        try {
          await action(
            () => post(`/import/${kind}`, { text, preview: false }),
            kind === "product" ? "导入完成，商品默认暂停采集" : "导入完成",
          );
          onClose();
          setText("");
          setPreview(null);
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="muted">
        支持 JSON / CSV，单次最多 1000 条。重复或无效记录会跳过；预览后才写入。
      </p>
      <div className="csv-format-help">
        <div>CSV 使用 UTF-8 编码、英文列名；金额不带 ¥ 或千位分隔符。</div>
        {kind === "product" ? (
          <div>
            <code>url</code> 可暂空，暂空时必填 <code>name</code>{" "}
            并暂停采集。链接保留 <code>id</code> 和 <code>skuId</code>
            ，确保规格及训练数据归属一致。
            <code>msrp</code> 单位为分；标签、规格选项使用 JSON 数组。
          </div>
        ) : kind === "observations" ? (
          <div>
            必填列 <code>product_id, observed_at, price, price_unit</code>。
            商品 ID 从商品清单导出获取；时间须含时区，金额单位填{" "}
            <code>yuan</code>（元）或
            <code>cent</code>（分）。<code>price_basis</code>{" "}
            区分普通价、补贴价、券后价、会员价；未提供链接和口径的历史用{" "}
            <code>reference</code>。 有效人工价格会作为真实数据参与训练。
          </div>
        ) : (
          <div>
            请按模板保留英文列名；日期使用 YYYY-MM-DD，布尔值使用 true / false。
          </div>
        )}
        <Space wrap>
          <Button
            size="small"
            onClick={() => download(`/import-template/${kind}`)}
          >
            下载 CSV 模板
          </Button>
          <Button
            size="small"
            type="link"
            onClick={() => download("/import-guide")}
          >
            完整格式说明
          </Button>
        </Space>
      </div>
      <Upload
        accept=".json,.csv"
        showUploadList={false}
        beforeUpload={async (file) => {
          const t = await file.text();
          setText(t);
          setPreview(null);
          return false;
        }}
      >
        <Button icon={<UploadIcon size={14} />}>选择文件</Button>
      </Upload>
      <Input.TextArea
        rows={8}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
        placeholder={
          kind === "product"
            ? "url,name,category,spec\nhttps://item.taobao.com/item.htm?id=123&skuId=456,商品名称,显卡,规格"
            : kind === "observations"
              ? "product_id,observed_at,price,price_unit,price_basis\n商品ID,2026-10-18T12:00:00+08:00,1999.00,yuan,regular"
              : ""
        }
        style={{ marginTop: 14, fontFamily: "var(--font-interface)" }}
      />
      <Button
        style={{ marginTop: 12 }}
        disabled={!text}
        onClick={async () => {
          try {
            setPreview(await post(`/import/${kind}`, { text, preview: true }));
          } catch (e: any) {
            message.error(e.message);
          }
        }}
      >
        预览导入
      </Button>
      {preview && (
        <div className="import-preview">
          <Tag color="green">新增 {preview.new}</Tag>
          <Tag>跳过 {preview.skipped}</Tag>
          {preview.rows
            .filter((r: Row) => r.error)
            .map((r: Row) => (
              <p key={r.row}>
                第 {r.row} 行：{r.error}
              </p>
            ))}
        </div>
      )}
    </Modal>
  );
}

export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    return this.state.error ? (
      <div className="blank">
        <h2>页面遇到问题</h2>
        <p>请刷新后重试，已保存的数据不受影响。</p>
        <Button onClick={() => location.reload()}>刷新页面</Button>
      </div>
    ) : (
      this.props.children
    );
  }
}
