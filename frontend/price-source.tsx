import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Switch,
  Tag,
} from "antd";
import { Save } from "lucide-react";
import { put, useMonitor, ContextHelp } from "./lib";

export function PriceSourcePanel({
  onDirty,
}: {
  onDirty: (dirty: boolean) => void;
}) {
  const { data, action } = useMonitor();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [clearTokens, setClearTokens] = useState<Record<string, boolean>>({});
  const selected = Form.useWatch("mode", form);
  const config = data.settings;
  const profiles = config.price_apis || {};
  const reset = () => {
    form.setFieldsValue({
      mode: config.price_source || "crawler",
      providers: Object.fromEntries(
        ["taobao", "jd"].map((name) => [
          name,
          { ...profiles[name], app_secret: "", token: "" },
        ]),
      ),
    });
    setClearTokens({});
    onDirty(false);
  };
  useEffect(reset, [config.price_source_revision]);
  return (
    <>
      <div className="setting-section-heading">
        <h2>价格来源</h2>
      </div>
      <div className="muted mb16">当前来源 <Tag>{config.price_source === "api" ? "官方 API" : "淘宝爬虫"}</Tag></div>
      <Form
        form={form}
        name="price-source"
        layout="vertical"
        onValuesChange={() => onDirty(true)}
        onFinish={async (values) => {
          setBusy(true);
          try {
            const providers = Object.fromEntries(
              ["taobao", "jd"].map((name) => {
                const value = values.providers?.[name] || {};
                const previous = profiles[name] || {};
                return [
                  name,
                  {
                    enabled: !!value.enabled,
                    method: value.method,
                    app_key: value.app_key || "",
                    app_secret:
                      value.app_secret ||
                      (previous.app_secret ? "__saved__" : ""),
                    token:
                      value.token ||
                      (previous.token && !clearTokens[name] ? "__saved__" : ""),
                  },
                ];
              }),
            );
            await action(
              () =>
                put("/price-source", {
                  mode: values.mode,
                  ...(values.mode === "api" ? { providers } : {}),
                }),
              "价格来源已保存，采集队列将按此来源执行",
            );
            onDirty(false);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item name="mode" label="价格获取分支">
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            options={[
              { label: "淘宝爬虫", value: "crawler" },
              { label: "官方 API", value: "api" },
            ]}
          />
        </Form.Item>
        {selected !== config.price_source && <Alert
          className="mb24"
          showIcon
          type={selected === "api" ? "warning" : "info"}
          message={
            selected === "api"
              ? "保存后暂停爬虫价格采集"
              : "保存后切换为淘宝爬虫"
          }
        />}
        <div hidden={selected !== "api"}>
          {(["taobao", "jd"] as const).map((name) => (
            <div className="mb24" key={name}>
              <div className="form-section-title">
                {name === "taobao"
                  ? "淘宝 / 天猫 · 淘宝开放平台"
                  : "京东 · 京东联盟开放平台"}
              </div>
              <Form.Item
                name={["providers", name, "enabled"]}
                label={`启用${name === "taobao" ? "淘宝" : "京东"} API`}
                valuePropName="checked"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name={["providers", name, "method"]}
                label="官方商品查询接口"
              >
                <Select
                  options={
                    name === "taobao"
                      ? [
                          {
                            value: "taobao.tbk.item.info.get",
                            label: "淘宝客商品详情 · 商品级折扣价",
                          },
                          {
                            value: "taobao.item.sku.get",
                            label: "SKU 查询 · 需要卖家授权，仅限授权范围",
                          },
                        ]
                      : [
                          {
                            value:
                              "jd.union.open.goods.promotiongoodsinfo.query",
                            label: "推广商品详情 · 京东单价",
                          },
                          {
                            value: "jd.union.open.goods.query",
                            label: "商品查询 · priceInfo.price",
                          },
                        ]
                  }
                />
              </Form.Item>
              <Form.Item name={["providers", name, "app_key"]} label="AppKey">
                <Input
                  autoComplete="off"
                  placeholder="填写官方开放平台分配的 AppKey"
                />
              </Form.Item>
              <Form.Item
                name={["providers", name, "app_secret"]}
                label="AppSecret"
                extra={
                  profiles[name]?.app_secret
                    ? "已保存，留空保留原值；填写新值可替换。"
                    : "仅保存在当前账号，不会回显密钥。"
                }
              >
                <Input.Password
                  autoComplete="new-password"
                  placeholder={
                    profiles[name]?.app_secret
                      ? "已保存 · 输入新值可替换"
                      : "填写 AppSecret"
                  }
                />
              </Form.Item>
              <Form.Item
                name={["providers", name, "token"]}
                label={
                  name === "taobao"
                    ? "授权 Session（SKU 查询必填）"
                    : "Access Token（接口要求授权时填写）"
                }
              >
                <Input.Password
                  autoComplete="new-password"
                  placeholder={
                    profiles[name]?.token
                      ? "已保存 · 留空保留原值"
                      : "填写官方授权令牌，非浏览器 Cookie"
                  }
                />
              </Form.Item>
              {profiles[name]?.token && (
                <Checkbox
                  className="mb16"
                  checked={!!clearTokens[name]}
                  onChange={(e) => {
                    setClearTokens({
                      ...clearTokens,
                      [name]: e.target.checked,
                    });
                    onDirty(true);
                  }}
                >
                  清除已保存的授权令牌
                </Checkbox>
              )}
              <ContextHelp title="接口权限与价格口径">
                {name === "taobao" ? (
                  <>
                    淘宝客简版详情只返回商品级折扣价，不提供指定 SKU 核验；带
                    skuId 的链接会返回规格不支持，不能用商品价替代。SKU
                    查询需要卖家授权及接口权限，返回 SKU 标价。无法确认 SKU
                    的商品级价格只展示，不进入规格模型训练。
                    <a
                      href="https://developer.alibaba.com/docs/api.htm?apiId=24518"
                      target="_blank"
                      rel="noreferrer"
                    >
                      {" "}
                      官方文档
                    </a>
                  </>
                ) : (
                  <>
                    通过京东商品链接中的 SKU 查询并核对返回
                    SKU。默认读取官方单价，不将会员价、拼购价或预估券后价当成普通价。可查询范围以应用权限和联盟商品范围为准。
                    <a
                      href="https://union.jd.com/openplatform"
                      target="_blank"
                      rel="noreferrer"
                    >
                      {" "}
                      官方文档
                    </a>
                  </>
                )}
              </ContextHelp>
            </div>
          ))}
        </div>
        <div className="settings-save">
          <Button
            type="primary"
            icon={<Save size={14} />}
            htmlType="submit"
            loading={busy}
          >
            保存价格来源
          </Button>
          <Button onClick={reset}>放弃修改</Button>
        </div>
      </Form>
    </>
  );
}
