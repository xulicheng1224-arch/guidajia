import React, { useEffect, useState } from "react";
import { Alert, Button, Collapse, Select, Space, Tag, Tooltip } from "antd";
import { CollectionDiagnostics } from "./diagnostics";
import { date, post, useMonitor } from "./lib";

export function CookieVerification() {
  const { data, action, reload } = useMonitor();
  const [selected, setSelected] = useState<string>();
  const isAdmin = data.access?.role === "admin";
  const verification = data.session.verification || {};
  const apiMode = data.settings.price_source === "api";
  const products = data.products.filter(
    (p: any) => p.url && !p.url.includes("item.jd.com"),
  );
  const productId =
    products.find((p: any) => p.id === selected)?.id || products[0]?.id;
  const busy = ["queued", "running"].includes(verification.state);
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => reload().catch(() => {}), 2000);
    return () => clearInterval(timer);
  }, [busy, reload]);
  return (
    <div className="mt24">
      <div className="form-section-title">Cookie 实际商品校验</div>
      <Space wrap className="mb16">
        {isAdmin && (
          <Select
            aria-label="选择 Cookie 校验商品"
            style={{ width: 300, maxWidth: "100%" }}
            value={productId}
            placeholder="选择已填写淘宝链接的商品"
            options={products.map((p: any) => ({ value: p.id, label: p.name }))}
            onChange={setSelected}
          />
        )}
        <Tooltip title={apiMode ? "切回爬虫来源后可校验" : !data.session.exists ? "请先上传 Cookie 或保存登录会话" : isAdmin && !productId ? "请先添加淘宝商品链接" : ""}><span><Button
          loading={busy}
          disabled={apiMode || !data.session.exists || (isAdmin && !productId)}
          onClick={() =>
            action(
              () =>
                post(
                  "/session/verify",
                  isAdmin ? { product_id: productId } : {},
                ),
              "已提交当前账号的 Cookie 校验",
            ).catch(() => {})
          }
        >
          校验 Cookie
        </Button></span></Tooltip>
        <Tag
          color={
            verification.state === "passed"
              ? "green"
              : busy
                ? "blue"
                : "default"
          }
        >
          {(
            {
              not_checked: "待校验",
              queued: "排队中",
              running: "校验中",
              passed: "已通过",
              login_required: "登录失效",
              verification_required: "需要验证",
              indeterminate: "无法确认",
              cancelled: "已取消",
            } as any
          )[verification.state] || "待校验"}
        </Tag>
      </Space>
      {!apiMode && ["login_required", "verification_required", "indeterminate"].includes(verification.state) && <Alert
        showIcon
        type={
          verification.state === "passed"
            ? "success"
            : ["login_required", "verification_required"].includes(
                  verification.state,
                )
              ? "warning"
              : "info"
        }
        message={
          (apiMode
            ? "当前启用官方 API 分支，Cookie 校验已暂停；切回爬虫后可校验。"
            : verification.message) ||
          "上传成功仅表示文件已保存，实际采集后才能确认会话是否可用。"
        }
      />}
      {verification.checked_at && <div className="muted small">最近校验：{date(verification.checked_at, "YYYY-MM-DD HH:mm:ss")}</div>}
      {verification.diagnostics && (
        <Collapse
          className="mt12"
          items={[
            {
              key: "details",
              label: "查看校验详情",
              children: (
                <CollectionDiagnostics details={verification.diagnostics} />
              ),
            },
          ]}
        />
      )}
    </div>
  );
}
