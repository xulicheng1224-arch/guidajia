import { exportCookies } from "./cookies.mjs";

const button = document.querySelector("#export");
const status = document.querySelector("#status");
button.addEventListener("click", async () => {
  button.disabled = true;
  status.dataset.error = "false";
  status.textContent = "正在读取本浏览器的淘宝 / 天猫 Cookie…";
  let url;
  try {
    const { state, skipped } = await exportCookies(chrome);
    url = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: "application/json" }));
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    await chrome.downloads.download({ url, filename: `taobao-cookie-${stamp}.json`, saveAs: true });
    const info = state.export_info;
    const warnings = [];
    if (!info.storage_origins.length) warnings.push("未读到页面本地存储，请保持已登录的淘宝及天猫商品页打开后重试导出。");
    if (info.storage_read_failures) warnings.push("部分页面存储读取失败，请刷新页面后重试。");
    if (info.missing_auth_storage_origins.length) warnings.push(`未读到 ${info.missing_auth_storage_origins.map((origin) => new URL(origin).hostname).join("、")} 的本地存储：相关页面或内嵌登录页可能已关闭，不代表一定未登录。`);
    if (!info.auth_markers["taobao.com"].length) warnings.push("淘宝域缺少常见登录标记，请核对当前浏览器用户配置。");
    status.textContent = `已发起文件保存：${state.cookies.length} 条 Cookie（含 ${info.partitioned_cookie_count} 条分区记录）、${info.storage_origins.length} 个本地存储来源，读取 ${info.storage_frames_read} 个页面 / 内嵌页。${skipped ? `忽略 ${skipped} 条过期或无效记录。` : ""}${warnings.join("")}保存完成后上传 JSON。该版本不导出 IndexedDB，无法读取已关闭页面的本地存储，会话有效性以实际采集为准。`;
  } catch (error) {
    status.dataset.error = "true";
    // No browser exception details or cookie values are displayed/logged.
    status.textContent = error instanceof Error && error.message.startsWith("未找到可用")
      ? error.message : "导出未完成。请确认扩展已获准访问淘宝 / 天猫，并重试保存文件。";
  } finally {
    if (url) setTimeout(() => URL.revokeObjectURL(url), 60000);
    button.disabled = false;
  }
});
