const $ = (id) => document.getElementById(id);
let editing = null;
let accountCache = [];
let selectedAccounts = new Set();
let batchBusy = false;
let lastNetworkCheck = null;
let proxyPasswordPresent = false;
let browserInfoTimer = null;
let browserInfoRequest = 0;
let options = {
  platforms: [], releaseChannels: [], defaultPlatform: "windows", hostPlatformLabel: "当前系统",
  timezones: [], locales: [], sep: "\\"
};

async function api(path, init = {}) {
  const response = await fetch(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    let detail = body.detail || body;
    if (Array.isArray(detail)) {
      detail = detail.map((item) => {
        const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
        const message = String(item.msg || "参数无效").replace(/^Value error,\s*/, "");
        return field ? `${field}：${message}` : message;
      }).join("；");
    } else if (typeof detail !== "string") {
      detail = JSON.stringify(detail);
    }
    throw new Error(detail);
  }
  return body;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
}

function fingerprintInfo(account) {
  const result = {...(account.fingerprint || {})};
  const runtime = account.runtime?.fingerprint || null;
  if (runtime) {
    for (const [key, value] of Object.entries(runtime)) {
      if (value !== null && value !== undefined && value !== "") result[key] = value;
    }
  }
  return result;
}

function renderFingerprint(account) {
  const fp = fingerprintInfo(account);
  const sourceLabels = {
    runtime: account.runtime?.status === "running" ? "运行身份" : "上次运行",
    lastCheck: "检测记录",
    configured: "手动配置",
    notChecked: "未检测",
    disabled: "地区匹配关闭",
  };
  const source = sourceLabels[fp.source] || "未检测";
  const userAgent = fp.userAgent || "启动后获取";
  const uaType = fp.userAgentSource === "projected" ? "预估" : "实测";
  return `<div class="fingerprint-cell">
    <div class="fingerprint-tags"><span>${escapeHtml(source)}</span>${fp.stale ? '<span class="stale">已过期</span>' : ""}</div>
    <div class="fingerprint-row"><b>WebRTC</b><code>${escapeHtml(fp.webrtcIp || "未检测")}</code></div>
    <div class="fingerprint-row"><b>地区</b><span>${escapeHtml(fp.region || "未检测")}</span></div>
    <div class="fingerprint-row split"><span><b>语言</b>${escapeHtml(fp.locale || "未检测")}</span><span><b>时区</b>${escapeHtml(fp.timezone || "未检测")}</span></div>
    <div class="fingerprint-row"><b>浏览器</b><code>${escapeHtml(fp.browserVersion || "未知")}</code></div>
    <div class="fingerprint-row fingerprint-ua" title="${escapeHtml(userAgent)}"><b>UA · ${escapeHtml(uaType)}</b><code>${escapeHtml(userAgent)}</code></div>
  </div>`;
}

async function load() {
  const [settings, accounts, opts] = await Promise.all([
    api("/api/settings"), api("/api/accounts"), api("/api/cloak/options")
  ]);
  options = opts;
  $("globalBrowserPath").value = settings.cloakBrowserPath || "";
  $("globalUserBase").value = settings.cloakUserDataBase || "";
  $("globalExtensionPaths").value = (settings.extensionPaths || []).join("\n");
  accountCache = accounts.accounts;
  renderAccounts();
  fillSelect("fpPlatformOverride", options.platforms, false);
  fillSelect("releaseChannel", options.releaseChannels, false);
  fillDatalist("timezoneOptions", options.timezones);
  fillDatalist("localeOptions", options.locales);
}

async function refreshStatuses() {
  const button = $("statusRefresh");
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    const response = await api("/api/browser/status");
    const statuses = new Map(response.accounts.map((runtime) => [runtime.acc, runtime]));
    accountCache = accountCache.map((account) => ({
      ...account,
      runtime: statuses.get(account.acc) || account.runtime,
    }));
    renderAccounts();
    button.textContent = "已刷新";
  } catch (error) {
    button.textContent = "刷新失败";
    $("batchMsg").textContent = `刷新浏览器状态失败：${error.message}`;
    $("batchMsg").classList.add("error");
  } finally {
    window.setTimeout(() => {
      button.textContent = "刷新";
      button.disabled = false;
    }, 800);
  }
}

function renderAccounts() {
  const accountIds = new Set(accountCache.map((account) => account.acc));
  selectedAccounts = new Set([...selectedAccounts].filter((acc) => accountIds.has(acc)));
  $("accounts").innerHTML = accountCache.map((account) => {
    const rt = account.runtime;
    return `<tr>
      <td class="select-column"><input type="checkbox" class="account-select" data-id="${escapeHtml(account.acc)}" aria-label="选择 ${escapeHtml(account.name || account.acc)}" ${selectedAccounts.has(account.acc) ? "checked" : ""}></td>
      <td><strong>${escapeHtml(account.name || account.acc)}</strong><small>${escapeHtml(account.acc)}<br>${escapeHtml(account.userDataDir)}</small></td>
      <td><span class="status ${rt.status}">${escapeHtml(rt.status)}</span></td>
      <td>${renderFingerprint(account)}</td>
      <td>${escapeHtml(account.proxyDisplay || account.network?.proxy?.server || "—")}</td>
      <td>${rt.processCount} 个</td>
      <td class="row-actions">
        <button data-action="open" data-id="${account.acc}">打开</button>
        <button data-action="close" data-id="${account.acc}">关闭</button>
        <button data-action="restart" data-id="${account.acc}">重启</button>
        <button data-action="edit" data-id="${account.acc}">编辑</button>
        <button data-action="delete" data-id="${account.acc}" class="danger">删除</button>
      </td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="empty">暂无账户</td></tr>';
  updateSelectionControls();
}

function updateSelectionControls() {
  const visibleIds = accountCache.map((account) => account.acc);
  const selectedCount = visibleIds.filter((acc) => selectedAccounts.has(acc)).length;
  $("selectAll").checked = visibleIds.length > 0 && selectedCount === visibleIds.length;
  $("selectAll").indeterminate = selectedCount > 0 && selectedCount < visibleIds.length;
  $("selectAll").disabled = batchBusy || visibleIds.length === 0;
  $("batchOpen").disabled = batchBusy || selectedCount === 0;
  $("batchClose").disabled = batchBusy || selectedCount === 0;
  $("batchRestart").disabled = batchBusy || selectedCount === 0;
  $("selectionCount").textContent = `已选择 ${selectedCount} 个`;
}

function fillSelect(id, values, includeDefault = true) {
  const first = includeDefault ? '<option value="">（默认）</option>' : "";
  $(id).innerHTML = first + (values || []).map((item) => {
    const value = typeof item === "string" ? item : item.value;
    const label = typeof item === "string" ? item : item.label;
    return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
  }).join("");
}

function fillDatalist(id, values) {
  $(id).innerHTML = (values || []).map((item) =>
    `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`
  ).join("");
}

function platformLabel(value) {
  return options.platforms.find((item) => item.value === value)?.label || value;
}

function updateFingerprintSummary(account = null) {
  const override = $("fpPlatformOverride").value || "auto";
  const automatic = override === "auto";
  $("fingerprintPlatformSummary").textContent = automatic
    ? `自动匹配当前系统（${options.hostPlatformLabel}）`
    : `高级覆盖：${platformLabel(override)}`;
  $("fingerprintHostSummary").textContent = automatic
    ? "由 CloakBrowser 使用宿主系统的官方默认策略。"
    : "已启用手动平台 persona，请确认与宿主环境一致。";
  $("fingerprintSeedSummary").textContent = account?.acc
    ? `账户 ${account.acc} 的 ID 会稳定生成同一个 seed。`
    : "保存后由账户 ID 自动生成固定 seed。";
  $("platformOverrideWarning").classList.toggle(
    "hidden", !(options.defaultPlatform === "macos" && override === "windows")
  );
}

function updateCustomBrowserPathHint() {
  $("customBrowserPathHint").classList.toggle("hidden", !$("browserPath").value.trim());
}

function renderBrowserInfo(info) {
  const target = $("browserInfo");
  if (info.error) {
    target.className = "browser-info error";
    target.textContent = info.error;
    return;
  }
  target.className = "browser-info";
  const status = info.installed ? "已安装" : "尚未安装，首次启动时将下载";
  target.innerHTML = `<strong>Chromium ${escapeHtml(info.version || "未知版本")} · ${escapeHtml(info.platform || "未知平台")} · ${escapeHtml(info.tier || "未知授权")}</strong>
    ${escapeHtml(status)}${info.binaryPath ? `<br><small>${escapeHtml(info.binaryPath)}</small>` : ""}`;
}

async function loadBrowserInfo() {
  const requestId = ++browserInfoRequest;
  const target = $("browserInfo");
  target.className = "browser-info loading";
  target.textContent = "正在解析实际二进制版本…";
  const channel = $("releaseChannel").value || "stable";
  const version = $("browserVersion").value.trim();
  const query = new URLSearchParams({releaseChannel: channel});
  if (version) query.set("browserVersion", version);
  try {
    const info = await api(`/api/cloak/browser-info?${query}`);
    if (requestId === browserInfoRequest) renderBrowserInfo(info);
  } catch (error) {
    if (requestId === browserInfoRequest) renderBrowserInfo({error: error.message});
  }
}

function scheduleBrowserInfo() {
  window.clearTimeout(browserInfoTimer);
  browserInfoTimer = window.setTimeout(loadBrowserInfo, 250);
}

function proposedDir(name) {
  const base = ($("globalUserBase").value || options.cloakUserDataBase || "").replace(/[\\/]+$/, "");
  const safe = (name || "account").replace(/[\\/:*?"<>|]+/g, "_");
  return base ? `${base}${options.sep || "\\"}${safe}` : "";
}

function renderNetworkPreview(result = lastNetworkCheck) {
  const preview = $("identityPreview");
  if (!result) {
    preview.className = "identity-preview empty-preview";
    preview.textContent = "尚未检测";
    return;
  }
  if (result.error) {
    preview.className = "identity-preview error";
    preview.textContent = result.error;
    return;
  }
  const stale = result.stale === true;
  preview.className = `identity-preview${stale ? " warning" : ""}`;
  const checkedAt = result.checkedAt ? new Date(result.checkedAt).toLocaleString() : "—";
  preview.innerHTML = `<dl>
    <div><dt>检测状态</dt><dd>${stale ? "配置已变化，请重新检测" : "正常"}</dd></div>
    <div><dt>出口 IP / WebRTC</dt><dd>${escapeHtml(result.exitIp || result.webrtcIp || "—")}</dd></div>
    <div><dt>延迟</dt><dd>${result.latencyMs != null ? `${escapeHtml(result.latencyMs)} ms` : "—"}</dd></div>
    <div><dt>检测时区</dt><dd>${escapeHtml(result.detectedTimezone || result.timezone || "—")}</dd></div>
    <div><dt>最终时区</dt><dd>${escapeHtml(result.appliedTimezone || result.timezone || "—")} ${result.timezoneSource === "custom" ? "（自定义）" : "（自动）"}</dd></div>
    <div><dt>检测语言 / 最终语言</dt><dd>${escapeHtml(result.detectedLocale || result.locale || "—")} / ${escapeHtml(result.appliedLocale || result.locale || "—")} ${result.localeSource === "custom" ? "（自定义）" : "（自动）"}</dd></div>
    <div><dt>检测时间</dt><dd>${escapeHtml(checkedAt)}</dd></div>
  </dl>`;
}

function invalidateNetworkCheck() {
  if (!lastNetworkCheck || lastNetworkCheck.stale) return;
  lastNetworkCheck = {...lastNetworkCheck, stale: true};
  renderNetworkPreview();
}

function updateNetworkControls() {
  const useProxy = $("useProxy").checked;
  for (const id of ["proxyServer", "proxyUsername", "proxyPassword", "clearProxyPassword"]) {
    $(id).disabled = !useProxy;
  }
  $("strictProxy").disabled = !useProxy;
  $("proxyServer").required = useProxy;
  $("proxyFields").classList.toggle("disabled", !useProxy);

  const mode = $("regionMode").value;
  const disabled = mode === "disabled";
  if (mode === "manual") {
    $("timezoneOverrideEnabled").checked = true;
    $("localeOverrideEnabled").checked = true;
  }
  for (const id of ["timezoneOverrideEnabled", "localeOverrideEnabled"]) $(id).disabled = disabled || mode === "manual";
  $("timezoneOverride").disabled = disabled || !$("timezoneOverrideEnabled").checked;
  $("localeOverride").disabled = disabled || !$("localeOverrideEnabled").checked;

  const geoEnabled = $("geolocationEnabled").checked;
  for (const id of ["geolocationLatitude", "geolocationLongitude", "geolocationAccuracy"]) $(id).disabled = !geoEnabled;
}

function openForm(account = null) {
  editing = account && account.acc;
  $("formTitle").textContent = account ? `编辑 ${account.name || account.acc}` : "新增账户";
  $("name").value = account?.name || "";
  $("userDataDir").value = account?.userDataDir || proposedDir("account");
  $("browserPath").value = account?.browserPath || "";
  const network = account?.network || {};
  const proxy = network.proxy || null;
  $("useProxy").checked = Boolean(proxy);
  $("proxyServer").value = proxy?.server || "";
  $("proxyUsername").value = proxy?.username || "";
  $("proxyPassword").value = "";
  proxyPasswordPresent = proxy?.hasPassword === true;
  $("clearProxyPassword").checked = false;
  $("clearPasswordLabel").classList.toggle("hidden", !proxyPasswordPresent);
  $("proxyPasswordHint").textContent = proxyPasswordPresent ? "已安全保存密码；留空表示保持不变。" : "";
  $("regionMode").value = network.regionMode || "auto";
  $("strictProxy").checked = network.strictProxy !== false;
  $("timezoneOverrideEnabled").checked = Boolean(network.timezoneOverride);
  $("localeOverrideEnabled").checked = Boolean(network.localeOverride);
  $("timezoneOverride").value = network.timezoneOverride || "";
  $("localeOverride").value = network.localeOverride || "";
  lastNetworkCheck = network.lastCheck || null;
  const geolocation = account?.geolocation || {};
  $("geolocationEnabled").checked = geolocation.enabled === true;
  $("geolocationLatitude").value = geolocation.latitude ?? "";
  $("geolocationLongitude").value = geolocation.longitude ?? "";
  $("geolocationAccuracy").value = geolocation.accuracy ?? 5000;
  $("fpPlatformOverride").value = account?.fpPlatform || "auto";
  $("platformVersion").value = account?.platformVersion || "";
  $("brandVersion").value = account?.brandVersion || "";
  $("releaseChannel").value = account?.releaseChannel || "stable";
  $("browserVersion").value = account?.browserVersion || "";
  $("advancedFingerprint").open = Boolean(
    account?.fpPlatform && account.fpPlatform !== "auto" || account?.platformVersion || account?.brandVersion
  );
  $("humanPreset").value = account?.humanPreset || "careful";
  $("cloakArgs").value = (account?.cloakArgs || []).join("\n");
  $("humanize").checked = account?.humanize !== false;
  $("headless").checked = account?.headless === true;
  $("formMsg").textContent = "";
  updateNetworkControls();
  updateFingerprintSummary(account);
  updateCustomBrowserPathHint();
  renderNetworkPreview();
  $("accountDialog").showModal();
  loadBrowserInfo();
}

function networkPayload() {
  let proxy = null;
  if ($("useProxy").checked) {
    const server = $("proxyServer").value.trim();
    if (!server) {
      throw new Error("已启用代理，请填写代理服务器，例如 socks5://host:port");
    }
    proxy = {
      server,
      username: $("proxyUsername").value.trim() || null,
    };
    const password = $("proxyPassword").value;
    if (password) proxy.password = password;
    else if ($("clearProxyPassword").checked) proxy.password = "";
  }
  const regionMode = $("regionMode").value;
  return {
    proxy,
    regionMode,
    timezoneOverride: regionMode !== "disabled" && $("timezoneOverrideEnabled").checked ? $("timezoneOverride").value.trim() || null : null,
    localeOverride: regionMode !== "disabled" && $("localeOverrideEnabled").checked ? $("localeOverride").value.trim() || null : null,
    strictProxy: $("strictProxy").checked,
    lastCheck: lastNetworkCheck,
  };
}

function formPayload() {
  const geolocationEnabled = $("geolocationEnabled").checked;
  return {
    name: $("name").value.trim(), userDataDir: $("userDataDir").value.trim(),
    browserPath: $("browserPath").value.trim() || null, network: networkPayload(),
    geolocation: {
      enabled: geolocationEnabled,
      latitude: geolocationEnabled && $("geolocationLatitude").value !== "" ? Number($("geolocationLatitude").value) : null,
      longitude: geolocationEnabled && $("geolocationLongitude").value !== "" ? Number($("geolocationLongitude").value) : null,
      accuracy: Number($("geolocationAccuracy").value || 5000),
    },
    fpPlatform: $("fpPlatformOverride").value || "auto",
    platformVersion: $("platformVersion").value || null, brandVersion: $("brandVersion").value || null,
    releaseChannel: $("releaseChannel").value || "stable",
    browserVersion: $("browserVersion").value.trim() || null,
    cloakArgs: $("cloakArgs").value.split("\n").map((value) => value.trim()).filter(Boolean),
    humanPreset: $("humanPreset").value, humanize: $("humanize").checked,
    headless: $("headless").checked,
  };
}

$("refresh").onclick = load;
$("statusRefresh").onclick = refreshStatuses;
$("addAccount").onclick = () => openForm();
$("closeDialog").onclick = () => $("accountDialog").close();
$("fpPlatformOverride").addEventListener("change", () => updateFingerprintSummary(
  editing ? accountCache.find((account) => account.acc === editing) : null
));
$("releaseChannel").addEventListener("change", loadBrowserInfo);
$("browserVersion").addEventListener("input", scheduleBrowserInfo);
$("browserPath").addEventListener("input", updateCustomBrowserPathHint);
$("name").addEventListener("input", () => { if (!editing && $("globalUserBase").value) $("userDataDir").value = proposedDir($("name").value); });
$("useProxy").addEventListener("change", () => { invalidateNetworkCheck(); updateNetworkControls(); });
$("regionMode").addEventListener("change", () => { invalidateNetworkCheck(); updateNetworkControls(); });
$("timezoneOverrideEnabled").addEventListener("change", () => { invalidateNetworkCheck(); updateNetworkControls(); });
$("localeOverrideEnabled").addEventListener("change", () => { invalidateNetworkCheck(); updateNetworkControls(); });
$("geolocationEnabled").addEventListener("change", updateNetworkControls);
for (const id of ["proxyServer", "proxyUsername", "proxyPassword", "clearProxyPassword", "timezoneOverride", "localeOverride"]) {
  $(id).addEventListener("input", invalidateNetworkCheck);
  $(id).addEventListener("change", invalidateNetworkCheck);
}

$("saveSettings").onclick = async () => {
  try {
    await api("/api/settings", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
      cloakBrowserPath: $("globalBrowserPath").value.trim(), cloakUserDataBase: $("globalUserBase").value.trim(),
      extensionPaths: $("globalExtensionPaths").value.split("\n").map((value) => value.trim()).filter(Boolean)
    })});
    $("settingsMsg").textContent = "已保存；插件将在账户下次打开或批量重启后生效";
  } catch (error) { $("settingsMsg").textContent = error.message; }
};

$("accounts").onclick = async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const account = accountCache.find((item) => item.acc === button.dataset.id);
  if (button.dataset.action === "edit") return openForm(account);
  if (button.dataset.action === "delete") {
    if (!confirm(`删除账户 ${account.name || account.acc}？用户数据目录不会被删除。`)) return;
    await api(`/api/accounts/${encodeURIComponent(account.acc)}`, {method:"DELETE"});
  } else {
    button.disabled = true;
    try { await api(`/api/browser/${encodeURIComponent(account.acc)}/${button.dataset.action}`, {method:"POST"}); }
    catch (error) { alert(error.message); }
  }
  await load();
};

$("accounts").addEventListener("change", (event) => {
  const checkbox = event.target.closest("input.account-select");
  if (!checkbox) return;
  if (checkbox.checked) selectedAccounts.add(checkbox.dataset.id);
  else selectedAccounts.delete(checkbox.dataset.id);
  updateSelectionControls();
});

$("selectAll").addEventListener("change", () => {
  for (const account of accountCache) {
    if ($("selectAll").checked) selectedAccounts.add(account.acc);
    else selectedAccounts.delete(account.acc);
  }
  renderAccounts();
});

async function runBatch(action) {
  const accounts = accountCache.map((account) => account.acc).filter((acc) => selectedAccounts.has(acc));
  if (!accounts.length || batchBusy) return;
  const actionLabel = {open: "打开", close: "关闭", restart: "重启"}[action];
  batchBusy = true;
  $("batchMsg").classList.remove("error");
  $("batchMsg").textContent = `正在批量${actionLabel} ${accounts.length} 个账户…`;
  updateSelectionControls();
  try {
    const response = await api("/api/browser/batch", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action, accounts})
    });
    const failures = response.results.filter((item) => !item.ok);
    const succeeded = response.results.length - failures.length;
    $("batchMsg").textContent = failures.length
      ? `批量${actionLabel}完成：成功 ${succeeded} 个，失败 ${failures.length} 个（${failures.map((item) => `${item.acc}: ${item.error}`).join("；")}）`
      : `批量${actionLabel}完成：成功 ${succeeded} 个`;
    $("batchMsg").classList.toggle("error", failures.length > 0);
  } catch (error) {
    $("batchMsg").textContent = `批量${actionLabel}失败：${error.message}`;
    $("batchMsg").classList.add("error");
  } finally {
    batchBusy = false;
    await load();
  }
}

$("batchOpen").onclick = () => runBatch("open");
$("batchClose").onclick = () => runBatch("close");
$("batchRestart").onclick = () => runBatch("restart");

$("accountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const path = editing ? `/api/accounts/${encodeURIComponent(editing)}` : "/api/accounts";
    await api(path, {method:editing ? "PUT" : "POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(formPayload())});
    $("accountDialog").close();
    await load();
  } catch (error) { $("formMsg").textContent = error.message; }
});

$("testNetwork").onclick = async () => {
  const button = $("testNetwork");
  button.disabled = true;
  button.textContent = "检测中…";
  $("formMsg").textContent = "正在使用 CloakBrowser 官方 GeoIP 检测，首次运行可能需要下载数据库…";
  try {
    const result = await api("/api/cloak/network-test", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({acc: editing, network: networkPayload()})
    });
    lastNetworkCheck = {
      exitIp: result.exitIp, timezone: result.detectedTimezone, locale: result.detectedLocale,
      checkedAt: result.checkedAt, proxySignature: result.proxySignature,
      latencyMs: result.latencyMs, stale: false,
      detectedTimezone: result.detectedTimezone, detectedLocale: result.detectedLocale,
      appliedTimezone: result.appliedTimezone, appliedLocale: result.appliedLocale,
      timezoneSource: result.timezoneSource, localeSource: result.localeSource,
      webrtcIp: result.webrtcIp,
    };
    renderNetworkPreview();
    $("formMsg").textContent = `检测正常，出口 IP：${result.exitIp}`;
  } catch (error) {
    lastNetworkCheck = {error: error.message, stale: true};
    renderNetworkPreview();
    $("formMsg").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "检测网络并预览身份";
  }
};

load().catch((error) => alert(error.message));
