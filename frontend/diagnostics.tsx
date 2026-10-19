import React from "react";
import { Alert, Button, Collapse, Descriptions, Space } from "antd";
import { Row, downloadJSON, statusNames } from "./lib";

const stages: Row = {
  api_request: "请求官方 API",
  api_parse: "解析官方金额",
  api_complete: "官方 API 完成",
  browser_launch: "启动浏览器",
  navigation: "打开商品页",
  login_handoff: "等待登录中转返回",
  login_redirect: "确认停留登录页",
  security_verification: "需要人工安全验证",
  render: "等待页面渲染",
  price_region: "定位价格区域",
  price_parse: "解析金额",
  sku_verification: "核验商品规格",
  subprocess: "采集进程",
  session_state: "检查当前账号的淘宝 Cookie",
  session_save: "保存会话",
  complete: "完成",
};
const verifications: Row = {
  official_api: "官方响应中的商品 / SKU ID",
  dom_input: "页面 SKU 字段",
  page_sku_mapping: "选中选项与页面 SKU 映射",
  expected_options: "人工配置的预期规格",
};

export function CollectionDiagnostics({ details }: { details?: Row | null }) {
  if (!details)
    return (
      <Alert
        type="info"
        showIcon
        message="这条旧记录没有保存详细诊断，重新采集后可查看失败阶段和页面证据。"
      />
    );
  const d = details;
  return (
    <Space
      direction="vertical"
      size={16}
      style={{ width: "100%", overflowWrap: "anywhere" }}
    >
      <Alert
        showIcon
        type={d.code === "success" ? "success" : "warning"}
        message={d.message || statusNames[d.code] || d.code}
        description={d.suggestion}
      />
      <Descriptions
        bordered
        size="small"
        column={1}
        styles={{
          label: { width: 120 },
          content: { overflowWrap: "anywhere" },
        }}
        items={[
          ...(d.price_source === "api" ? [{ key: "api", label: "价格来源 / 接口", children: `${d.platform === "jd" ? "京东" : "淘宝"}官方 API · ${d.api_method || "—"}` }] : []),
          { key: "code", label: "结果代码", children: d.code || "—" },
          {
            key: "stage",
            label: "执行阶段",
            children: stages[d.stage] || d.stage || "旧记录未保存",
          },
          {
            key: "error",
            label: "异常类型 / 代码",
            children:
              [d.error_type, d.error_code].filter(Boolean).join(" / ") || "—",
          },
          {
            key: "exception",
            label: "异常说明",
            children: d.exception_message || "—",
          },
          ...(d.timeout_seconds
            ? [
                {
                  key: "timeout",
                  label: "采集时限",
                  children: `${d.timeout_seconds} 秒 · 超时终止，不自动重试`,
                },
              ]
            : []),
          {
            key: "http",
            label: "页面 HTTP 状态",
            children: d.http_status ?? "—",
          },
          { key: "title", label: "页面标题", children: d.title || "—" },
          ...(d.document_ready_state ? [{
            key: "render", label: "页面渲染证据",
            children: `${d.document_ready_state} · 正文 ${d.body_text_length ?? "—"} 字 · DOM ${d.dom_element_count ?? "—"} 个 · 脚本 ${d.script_count ?? "—"} 个 · 框架 ${d.iframe_count ?? "—"} 个${d.render_state === "blank" ? " · 空白页面" : ""}`,
          }] : []),
          ...(d.page_signals?.length ? [{key:"signals",label:"页面提示",children:d.page_signals.join(" / ")}] : []),
          ...(d.request_failures?.length || d.resource_http_errors?.length || d.script_error_types?.length ? [{
            key:"resources",label:"脚本与资源异常",
            children:<>{(d.request_failures || []).map((r:Row,i:number)=><div key={`network-${i}`}>{r.resource_type} · {r.host} · {r.code}</div>)}{(d.resource_http_errors || []).map((r:Row,i:number)=><div key={`http-${i}`}>{r.resource_type} · {r.host} · HTTP {r.status}</div>)}{d.script_error_types?.length ? <div>脚本错误：{d.script_error_types.join(" / ")}</div> : null}</>,
          }] : []),
          ...(d.verification_required ? [{
            key: "verification", label: "平台验证要求",
            children: `${d.verification_kind === "sms" ? "短信验证码" : "人工验证"} · ${(d.verification_signals || []).join(" / ")}`,
          }] : []),
          ...(d.loaded_cookie_count != null ? [{
            key: "cookies", label: "浏览器会话",
            children: `已加载 ${d.loaded_cookie_count} 条 Cookie，商品域可用 ${d.product_cookie_count ?? "—"} 条`,
          }] : []),
          ...(d.login_wait_ms != null ? [{
            key: "login_wait", label: "登录页确认",
            children: `持续停留 ${(d.login_wait_ms / 1000).toFixed(1)} 秒`,
          }] : []),
          ...(d.browser_start_wait_ms != null ? [{
            key: "pacing", label: "访问节奏",
            children: `当前账号启动间隔 ${d.crawler_start_gap ?? "—"} 秒 · 本次等待 ${(d.browser_start_wait_ms / 1000).toFixed(1)} 秒（不计入采集时限）`,
          }] : []),
          {
            key: "url",
            label: "最终页面",
            children: d.final_url || d.final_host || "—",
          },
          {
            key: "nodes",
            label: "可见价格节点",
            children: d.price_nodes ?? "—",
          },
          { key: "text", label: "价格区域原文", children: d.price_text || "—" },
          {
            key: "price",
            label: "页面读价（元）",
            children: d.detected_price
              ? `¥ ${d.detected_price}（是否有效以采集结果为准）`
              : "未读到完整金额",
          },
          {
            key: "requested",
            label: "目标 SKU",
            children: d.requested_sku_id || "未指定",
          },
          {
            key: "selected",
            label: "页面 SKU",
            children: d.selected_sku_id || "未核验",
          },
          {
            key: "selection",
            label: "实际选中规格",
            children: d.selected_options?.join(" / ") || "—",
          },
          {
            key: "expected",
            label: "预期规格",
            children: d.expected_options?.join(" / ") || "未配置",
          },
          {
            key: "verified",
            label: "规格核验依据",
            children: verifications[d.sku_verification] || "无充分证据",
          },
          {
            key: "duration",
            label: "本次耗时",
            children:
              d.duration_ms != null
                ? `${(d.duration_ms / 1000).toFixed(1)} 秒`
                : "—",
          },
        ]}
      />
      {d.attempts?.length > 0 && (
        <Collapse
          items={d.attempts.map((attempt: Row, index: number) => ({
            key: String(index),
            label: `第 ${attempt.attempt} 次 · ${statusNames[attempt.status] || attempt.status}`,
            children: (
              <>
                <p>{attempt.diagnostics?.message}</p>
                <p>{attempt.diagnostics?.exception_message}</p>
                <p className="muted">
                  {stages[attempt.diagnostics?.stage] ||
                    attempt.diagnostics?.stage ||
                    "—"}
                </p>
              </>
            ),
          }))}
        />
      )}
      <Button onClick={() => downloadJSON("采集诊断.json", d)}>
        下载诊断信息
      </Button>
    </Space>
  );
}
