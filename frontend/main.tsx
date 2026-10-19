import React, { useEffect, useLayoutEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import {
  BrowserRouter,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  App,
  Badge,
  Button,
  ConfigProvider,
  Drawer,
  Input,
  Space,
  Spin,
  Tooltip,
  theme,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import { interfaceFont } from "./typography";
import {
  Activity,
  ArrowUpRight,
  Bell,
  Boxes,
  CalendarDays,
  ChartNoAxesCombined,
  Check,
  ChevronDown,
  Command,
  Cpu,
  Database,
  GitCompareArrows,
  History,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Search,
  Settings2,
  ShieldCheck,
  Sun,
  X,
} from "lucide-react";
import {
  ErrorBoundary,
  Provider,
  Row,
  ago,
  api,
  patch,
  post,
  useMonitor,
  date,
  useReducedMotion,
} from "./lib";
import { Dashboard, Products, ProductDetail, Compare } from "./products";
import {
  Alerts,
  CalendarPage,
  Collection,
  Forecasts,
  Audit,
} from "./operations";
import { SettingsPage } from "./settings";
import { ReadOnlyWorkspace } from "./read-only";
import { CollectionResults } from "./collection-results";
import "./style.css";
import "./design.css";

const navigation = [
  { to: "/", name: "总览看板", icon: LayoutDashboard },
  { to: "/products", name: "商品库", icon: Boxes },
  { to: "/compare", name: "比较页", icon: GitCompareArrows },
  { to: "/alerts", name: "提醒中心", icon: Bell },
  { to: "/calendar", name: "事件日历", icon: CalendarDays },
  { to: "/collection", name: "采集管理", icon: Activity },
  { to: "/forecasts", name: "预测中心", icon: ChartNoAxesCombined },
  { to: "/audit", name: "审计与回收站", icon: History },
];
function Shell() {
  const { data, loading, action } = useMonitor();
  const reducedMotion = useReducedMotion();
  const location = useLocation();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [mobile, setMobile] = useState(false);
  const [notifications, setNotifications] = useState(false);
  const [items, setItems] = useState<Row[]>([]);
  const [dark, setDark] = useState(false);
  useLayoutEffect(() => {
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);
  useLayoutEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [location.pathname]);
  const handleWorkspaceNavigation = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (event.currentTarget.pathname === location.pathname) {
      window.scrollTo({ top: 0, left: 0, behavior: "instant" });
      setMobile(false);
    }
  };
  document.documentElement.dataset.density = data.settings.density || "middle";
  const mode = data.settings.theme || "light";
  useEffect(() => {
    const q = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => {
      const on = mode === "dark" || (mode === "system" && q.matches);
      setDark(on);
      document.documentElement.dataset.theme = on ? "dark" : "light";
    };
    sync();
    q.addEventListener("change", sync);
    return () => q.removeEventListener("change", sync);
  }, [mode]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        document.getElementById("global-search")?.focus();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => {
    setMobile(false);
  }, [location.pathname]);
  useEffect(() => {
    if (notifications)
      api<Row[]>("/deliveries")
        .then(setItems)
        .catch(() => {});
  }, [notifications, data]);
  const current = location.pathname.startsWith("/products/")
    ? "商品详情"
    : navigation.find((n) => n.to === location.pathname)?.name || "设置";
  const count = data.products.filter((p: Row) => !p.paused).length;
  const h = data.health;
  const healthText = h.running?.length
    ? "正在采集"
    : h.paused
      ? "定时采集已暂停"
      : h.circuit_until && new Date(h.circuit_until) > new Date()
        ? "自动采集熔断中"
        : h.last_success
          ? "采集正常"
          : "等待首次采集";
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: dark ? "#9aa9ff" : "#4655d6",
          colorSuccess: "#21856d",
          colorWarning: "#da9a2c",
          colorError: "#e15d6f",
          borderRadius: 8,
          motion: !reducedMotion,
          fontSize: 16,
          fontFamily: interfaceFont,
          controlHeight: 38,
          colorText: dark ? "#e4ecf8" : "#24344b",
          colorTextBase: dark ? "#e4ecf8" : "#24344b",
          colorBgBase: dark ? "#101a2a" : "#f8fafd",
          colorTextSecondary: dark ? "#a4b4ca" : "#61728a",
          colorBorder: dark ? "#384960" : "#d2ddea",
          colorBgContainer: dark ? "#1d2b40" : "#f8fafd",
        },
        components: {
          Table: {
            headerBg: dark ? "#293b52" : "#edf3f9",
            headerColor: dark ? "#a4b4ca" : "#61728a",
            cellPaddingBlock: 15,
          },
          Button: {
            controlHeight: 38,
            primaryShadow: "0 3px 7px rgba(49,92,246,.13)",
          },
          Card: { paddingLG: 20 },
        },
      }}
    >
      <div className="app-shell">
        <CollectionResults />
        {mobile && (
          <div className="sidebar-scrim" onClick={() => setMobile(false)} />
        )}
        <aside className={`sidebar ${mobile ? "mobile-open" : ""}`}>
          <NavLink to="/" className="brand" onClick={handleWorkspaceNavigation}>
            <div className="brand-symbol">
              <img
                src="/brand/guidajia.jpg"
                alt="鬼打架"
                width={37}
                height={37}
              />
            </div>
            <div>
              <strong>
                鬼打架<span>硬件价格监测</span>
              </strong>
            </div>
            <span className="brand-dot" />
          </NavLink>
          <div className="workspace-badge">
            <span className="workspace-avatar">
              <Cpu size={15} />
            </span>
            <div>
              硬件价格监测
              <small
                title={data.access?.user?.email}
                style={{
                  maxWidth: 135,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {data.access?.user?.email || "个人工作空间"}
              </small>
            </div>
            <ChevronDown size={13} />
          </div>
          <div className="nav-section-label">工作空间</div>
          <nav>
            {navigation.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                onClick={handleWorkspaceNavigation}
                end={n.to === "/"}
                className={({ isActive }) =>
                  `nav-item ${isActive ? "active" : ""}`
                }
              >
                <n.icon size={18} strokeWidth={1.7} />
                <span>{n.name}</span>
                {n.to === "/products" && data.products.length > 0 && (
                  <em>{data.products.length}</em>
                )}
              </NavLink>
            ))}
          </nav>
          <div className="sidebar-bottom">
            {!data.access?.read_only && (
              <NavLink
                to="/settings"
                onClick={handleWorkspaceNavigation}
                className={({ isActive }) =>
                  `nav-item ${isActive ? "active" : ""}`
                }
              >
                <Settings2 size={18} />
                <span>设置</span>
              </NavLink>
            )}

          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar">
            <div className="breadcrumb">
              <Button
                className="mobile-menu"
                type="text"
                icon={<Menu size={18} />}
                onClick={() => setMobile(true)}
              />
              <span>工作空间</span>
              <span className="slash">/</span>
              <strong>{current}</strong>
            </div>
            <div className="topbar-right">
              <Input
                id="global-search"
                className="global-search"
                prefix={<Search size={14} />}
                placeholder="搜索商品、型号、标签"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onPressEnter={() => {
                  navigate(`/products?q=${encodeURIComponent(search)}`);
                  setSearch("");
                }}
              />
              <div className="collector-indicator">
                <span
                  className={`status-light ${h.paused && !h.running?.length ? "off" : h.circuit_until ? "warn" : ""}`}
                />
                <span>{healthText}</span>
              </div>
              <div className="top-divider" />
              <Tooltip title={dark ? "切换浅色模式" : "切换深色模式"}>
                <Button
                  aria-label="切换主题"
                  disabled={data.access?.read_only}
                  type="text"
                  icon={dark ? <Sun size={17} /> : <Moon size={17} />}
                  onClick={() =>
                    action(() =>
                      patch("/settings", { theme: dark ? "light" : "dark" }),
                    ).catch(() => {})
                  }
                />
              </Tooltip>
              <Tooltip title="通知中心">
                <Button
                  type="text"
                  aria-label="通知中心"
                  icon={<Bell size={18} />}
                  onClick={() => setNotifications(true)}
                />
              </Tooltip>
              {data.access?.user ? (
                <Tooltip title={`${data.access.user.email} · 退出登录`}>
                  <Button
                    type="text"
                    aria-label="退出登录"
                    icon={<LogOut size={18} />}
                    onClick={() =>
                      post("/access/logout")
                        .then(() => window.location.reload())
                        .catch(() => {})
                    }
                  />
                </Tooltip>
              ) : (
                <div className="profile-avatar">
                  <img
                    src="/brand/guidajia.jpg"
                    alt="鬼打架头像"
                    width={28}
                    height={28}
                  />
                </div>
              )}
            </div>
          </header>
          <main className="page-content">
            {loading ? (
              <div className="full-loader">
                <Spin />
                <span>正在打开工作空间…</span>
              </div>
            ) : (
              <div className="page-scene" key={location.pathname}>
                <ErrorBoundary>
                  {data.access?.read_only ? (
                    <ReadOnlyWorkspace />
                  ) : (
                    <Routes>
                      <Route path="/" element={<Dashboard />} />
                      <Route path="/products" element={<Products />} />
                      <Route path="/products/:id" element={<ProductDetail />} />
                      <Route path="/compare" element={<Compare />} />
                      <Route path="/alerts" element={<Alerts />} />
                      <Route path="/calendar" element={<CalendarPage />} />
                      <Route path="/collection" element={<Collection />} />
                      <Route path="/forecasts" element={<Forecasts />} />
                      <Route path="/audit" element={<Audit />} />
                      <Route path="/settings" element={<SettingsPage />} />
                      <Route
                        path="*"
                        element={
                          <div className="blank">
                            <h2>页面不存在</h2>
                            <Button onClick={() => navigate("/")}>
                              返回总览
                            </Button>
                          </div>
                        }
                      />
                    </Routes>
                  )}
                </ErrorBoundary>
              </div>
            )}
            <footer className="page-footer">
              <span>鬼打架 · 让每一次升级都有据可循</span>
              <span>
                <span className="tiny-dot" />{" "}
                {data.settings.price_source === "api"
                  ? "淘宝 / 京东官方 API"
                  : "淘宝 / 天猫纯爬虫"}
              </span>
            </footer>
          </main>
        </div>
        <Drawer
          open={notifications}
          onClose={() => setNotifications(false)}
          title="通知中心"
          width={430}
        >
          {items.filter((i) => i.channel === "inapp").length ? (
            items
              .filter((i) => i.channel === "inapp")
              .map((i) => (
                <div className="notification" key={i.id}>
                  <Bell size={17} />
                  <div>
                    <p>{i.message}</p>
                    <small>{date(i.created_at)}</small>
                    {i.product_id && (
                      <Button
                        size="small"
                        type="link"
                        onClick={() => {
                          navigate(`/products/${i.product_id}`);
                          setNotifications(false);
                        }}
                      >
                        查看商品
                      </Button>
                    )}
                  </div>
                </div>
              ))
          ) : (
            <div className="blank">
              <Bell size={30} />
              <h3>暂时没有通知</h3>
              <p>价格提醒和采集动态将在这里显示。</p>
            </div>
          )}
        </Drawer>
      </div>
    </ConfigProvider>
  );
}
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{ token: { fontFamily: interfaceFont, fontSize: 16 } }}
    >
      <App>
        <BrowserRouter>
          <Provider>
            <Shell />
          </Provider>
        </BrowserRouter>
      </App>
    </ConfigProvider>
  </React.StrictMode>,
);
