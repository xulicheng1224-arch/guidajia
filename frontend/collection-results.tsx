import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Collapse, Modal, Space, Tag } from "antd";
import { CircleCheck, CircleAlert } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api, date, money, Row, statusNames, useMonitor } from "./lib";
import { CollectionDiagnostics } from "./diagnostics";

type ResultPage = { items: Row[]; cursor: string; has_more: boolean };

export function CollectionResults() {
  const { data, loading } = useMonitor();
  const navigate = useNavigate();
  const [items, setItems] = useState<Row[]>([]);
  const pending = useRef<Row[]>([]);
  const cursor = useRef<string | null>(null);
  const storageKey = `monitor-collection-results-v1:${data.access?.user?.id || "anonymous"}`;

  useEffect(() => {
    if (loading) return;
    let disposed = false;
    let busy = false;
    const controller = new AbortController();
    pending.current = [];
    setItems([]);
    try {
      cursor.current = sessionStorage.getItem(storageKey);
    } catch {
      cursor.current = null;
    }
    const poll = async () => {
      // Keep each dialog bounded. Further results remain queued on the server
      // and are read after the user acknowledges this page of results.
      if (disposed || busy || pending.current.length >= 100) return;
      busy = true;
      try {
        const baseline = cursor.current === null;
        const page = await api<ResultPage>(
          `/collection/results?limit=${100 - pending.current.length}${baseline ? "" : `&after=${encodeURIComponent(cursor.current!)}`}`,
          { signal: controller.signal },
        );
        if (disposed) return;
        cursor.current = page.cursor;
        if (baseline) {
          try {
            sessionStorage.setItem(storageKey, page.cursor);
          } catch {
            /* Optional per-tab persistence. */
          }
        }
        if (page.items.length) {
          const known = new Set(pending.current.map((item) => item.id));
          pending.current = [
            ...pending.current,
            ...page.items.filter((item) => !known.has(item.id)),
          ];
          setItems(pending.current);
        }
      } catch {
        // Preserve the cursor through transient disconnects; retry next poll.
      } finally {
        busy = false;
      }
    };
    const wake = () => {
      if (!document.hidden) void poll();
    };
    void poll();
    const timer = window.setInterval(poll, 2000);
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      disposed = true;
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [loading, storageKey]);

  const acknowledge = () => {
    if (cursor.current) {
      try {
        sessionStorage.setItem(storageKey, cursor.current);
      } catch {
        /* The current page still deduplicates without storage. */
      }
    }
    pending.current = [];
    setItems([]);
  };
  const successes = items.filter((item) => item.status === "success").length;
  const failures = items.length - successes;

  return (
    <Modal
      title="采集结果"
      open={items.length > 0}
      width={760}
      className="collection-result-modal"
      onCancel={acknowledge}
      maskClosable={false}
      styles={{ body: { maxHeight: "65vh", overflowY: "auto" } }}
      footer={
        <Space wrap>
          {!data.access?.read_only && (
            <Button
              onClick={() => {
                acknowledge();
                navigate("/collection");
              }}
            >
              查看采集管理
            </Button>
          )}
          <Button type="primary" onClick={acknowledge}>
            知道了
          </Button>
        </Space>
      }
    >
      <Alert
        showIcon
        type={failures ? "warning" : "success"}
        message={`已完成 ${items.length} 个商品：成功 ${successes} 个，失败 ${failures} 个`}
      />
      <div
        className="collection-result-list"
        aria-live="polite"
        aria-relevant="additions"
      >
        {items.map((item) => {
          const success = item.status === "success";
          const diagnostics = item.diagnostics || {};
          return (
            <article className="collection-result-item" key={item.id}>
              <div className="collection-result-heading">
                <span
                  className={
                    success
                      ? "collection-result-success"
                      : "collection-result-failure"
                  }
                >
                  {success ? (
                    <CircleCheck size={19} />
                  ) : (
                    <CircleAlert size={19} />
                  )}
                </span>
                {item.product_available ? (
                  <Link
                    to={`/products/${item.product_id}`}
                    onClick={acknowledge}
                  >
                    {item.product_name}
                  </Link>
                ) : (
                  <strong>{item.product_name}</strong>
                )}
                <Tag color={success ? "success" : "error"}>
                  {success ? "采集成功" : "采集失败"}
                </Tag>
              </div>
              <div className="collection-result-price">
                {success
                  ? `采集价 ${money(item.price)}`
                  : `价格 None · ${statusNames[item.code] || "采集失败"}`}
              </div>
              {!success && (
                <div className="collection-result-reason">
                  <p>
                    {diagnostics.message ||
                      "采集未取得有效价格，请查看诊断详情。"}
                  </p>
                  {diagnostics.exception_message && (
                    <p>{diagnostics.exception_message}</p>
                  )}
                  {diagnostics.suggestion && <p>{diagnostics.suggestion}</p>}
                </div>
              )}
              <small className="collection-result-time">
                {date(item.finished_at, "YYYY-MM-DD HH:mm:ss")} · 尝试{" "}
                {item.attempts} 次
              </small>
              <Collapse
                ghost
                size="small"
                items={[
                  {
                    key: "diagnostics",
                    label: "查看诊断详情",
                    children: <CollectionDiagnostics details={diagnostics} />,
                  },
                ]}
              />
            </article>
          );
        })}
      </div>
    </Modal>
  );
}
