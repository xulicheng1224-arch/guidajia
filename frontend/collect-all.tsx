import React, { useState } from "react";
import { App, Button, Tooltip } from "antd";
import { RefreshCw } from "lucide-react";
import { post, useMonitor } from "./lib";

export function CollectAllButton() {
  const { data, action } = useMonitor();
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const apiMode = data.settings.price_source === "api";
  if (data.access?.read_only) return null;
  const collect = async () => {
    setBusy(true);
    try {
      const result = await action(() => post("/collection/all"));
      if (result.already_active) {
        message.info("已有一键采集任务正在进行，本次未重复添加");
        return;
      }
      const queued = result.added + result.reused;
      const summary = `${apiMode ? "官方 API" : "爬虫"} · 已排队 ${queued} 个，正在采集 ${result.existing} 个；跳过 ${result.skipped_no_url} 个无链接商品、${result.skipped_invalid_url} 个无效链接、${result.skipped_source || 0} 个当前来源不支持的商品`;
      if (queued || result.existing) message.success(summary, 8);
      else message.info(summary, 8);
    } catch {
      // The shared action displays request failures.
    } finally {
      setBusy(false);
    }
  };
  return (
    <Tooltip title="人工采集优先执行，不受自动采集暂停、熔断或历史 Cookie 校验失败影响，包含暂停自动监测的商品；每批最多 5 个并发，上一批结束后等待 8 秒。无链接、无效链接和当前来源不支持的商品会跳过。">
      <Button
        type="primary"
        icon={<RefreshCw size={15} />}
        loading={busy}
        onClick={collect}
      >
        {apiMode ? "一键 API 采集全部" : "一键采集全部"}
      </Button>
    </Tooltip>
  );
}
