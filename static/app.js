const sortState = {
  dirs: { key: "size_bytes", dir: -1 },
  files: { key: "size_bytes", dir: -1 },
  software: { key: "size_bytes", dir: -1 },
  recent: { key: "size_bytes", dir: -1 },
  caches: { key: "size_bytes", dir: -1 },
};

let state = null;
let lastRevision = -1;
let days = 30;
let checkTouched = false;
let checkedIds = new Set();
const checkedFiles = new Set();
const checkedRecent = new Set();
const checkedSoftware = new Set();
let modalAction = null;
let timer = null;
let ticking = false;
let pendingIds = [];

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function formatBytes(value, forceDigits) {
  if (value == null || value === "" || Number.isNaN(Number(value)) || Number(value) < 0) return "—";
  let number = Number(value);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (number >= 1024 && index < units.length - 1) {
    number /= 1024;
    index += 1;
  }
  if (index === 0) return `${Math.round(number)} B`;
  const digits = forceDigits == null ? (number >= 100 ? 0 : 1) : forceDigits;
  return `${number.toFixed(digits)} ${units[index]}`;
}

function percent(size, used) {
  if (!used || size == null) return "—";
  return `${(size / used * 100).toFixed(1)}%`;
}

function elapsed(iso) {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`;
}

function valueOf(row, key) {
  if (key === "paths") return (row.paths || []).join(" ");
  return row[key];
}

function compare(a, b, key, dir) {
  const left = valueOf(a, key);
  const right = valueOf(b, key);
  const leftEmpty = left == null || left === "";
  const rightEmpty = right == null || right === "";
  if (leftEmpty && rightEmpty) return 0;
  if (leftEmpty) return 1;
  if (rightEmpty) return -1;
  if (typeof left === "number" && typeof right === "number") return (left - right) * dir;
  return String(left).localeCompare(String(right), "zh") * dir;
}

function sortRows(rows, table) {
  const { key, dir } = sortState[table];
  return rows.slice().sort((a, b) => compare(a, b, key, dir));
}

function badge(text, kind) {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function th(table, key, label, numeric) {
  const current = sortState[table];
  const active = current.key === key;
  const arrow = active ? (current.dir < 0 ? " ↓" : " ↑") : "";
  const aria = active ? (current.dir < 0 ? "descending" : "ascending") : "none";
  return `<th class="${numeric ? "num" : ""}" data-table="${table}" data-key="${key}" aria-sort="${aria}">${esc(label)}${arrow}</th>`;
}

function grid(headers, body, cols, emptyText) {
  const row = body || `<tr><td class="empty" colspan="${cols}">${esc(emptyText || "还没有数据")}</td></tr>`;
  return `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${row}</tbody></table></div>`;
}

function queryOf(id) {
  const node = document.getElementById(id);
  return node ? node.value.trim() : "";
}

function matches(row, query, fields) {
  if (!query) return true;
  const haystack = fields.map((field) => {
    const value = valueOf(row, field);
    return Array.isArray(value) ? value.join(" ") : value || "";
  }).join(" ").toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function showNotice(text) {
  const box = document.getElementById("notice");
  box.hidden = !text;
  box.textContent = text || "";
}

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  return { status: response.status, data };
}

function shouldPoll(next) {
  return next.phase === "quick" || next.phase === "deep" || (next.clean && next.clean.running);
}

function selectableCaches() {
  const rows = state && Array.isArray(state.caches) ? state.caches : [];
  return rows.filter((row) => row.risk === "safe" && row.size_bytes > 0);
}

function activeIds() {
  const allowed = new Set(selectableCaches().map((row) => row.id));
  return [...checkedIds].filter((id) => allowed.has(id));
}

function selectedBytes() {
  const sizes = new Map(selectableCaches().map((row) => [row.id, row.size_bytes || 0]));
  return activeIds().reduce((sum, id) => sum + (sizes.get(id) || 0), 0);
}

function syncDefaultChecks() {
  if (checkTouched || !state) return;
  checkedIds = new Set((state.caches || []).filter((row) => row.recommended).map((row) => row.id));
}

function updateButtons() {
  if (!state) return;
  const scanning = state.phase === "quick" || state.phase === "deep";
  const measuring = state.phase === "quick";
  const cleaning = !!(state.clean && state.clean.running);
  const hasResult = !!(state.saved_at || (state.software || []).length || (state.top_level || []).length);
  const start = document.getElementById("start-btn");
  start.textContent = hasResult ? "重新分析" : "开始分析";
  start.disabled = scanning || cleaning;
  document.getElementById("stop-btn").disabled = !scanning;
  const button = document.getElementById("clean-btn");
  const ids = activeIds();
  const bytes = selectedBytes();
  if (!ids.length) button.textContent = "一键清空占大头缓存";
  else if (!checkTouched) button.textContent = `一键清空占大头缓存（${formatBytes(bytes)}）`;
  else button.textContent = `清空选中的缓存（${formatBytes(bytes)}）`;
  button.disabled = cleaning || measuring || !ids.length || bytes <= 0;
  const cleanCaches = document.getElementById("clean-caches");
  if (cleanCaches) {
    cleanCaches.disabled = button.disabled;
    cleanCaches.textContent = ids.length ? `清理选中的缓存（${ids.length}）` : "清理选中的缓存";
  }
  labelAction("delete-files", "删除选中的文件", checkedFiles.size);
  labelAction("delete-recent", "删除选中的文件", checkedRecent.size);
  labelAction("uninstall-software", "卸载选中的软件", checkedSoftware.size);
}

function labelAction(id, label, count) {
  const button = document.getElementById(id);
  if (!button) return;
  button.disabled = count === 0;
  button.textContent = count ? `${label}（${count}）` : label;
}

function rowKey(group, row) {
  if (group === "caches") return row.id;
  if (group === "software") return row.id || `${row.name}|${row.install_location || ""}`;
  return row.path;
}

function setFor(group) {
  if (group === "files") return checkedFiles;
  if (group === "recent") return checkedRecent;
  if (group === "software") return checkedSoftware;
  return checkedIds;
}

function canDeleteFile(row) {
  const path = String(row.path || "");
  const low = path.toLowerCase();
  const base = low.split("\\").pop();
  if (!path) return false;
  if (base === "pagefile.sys" || base === "hiberfil.sys" || base === "swapfile.sys") return false;
  if (low === "c:\\windows" || low.startsWith("c:\\windows\\")) return false;
  if ((row.note || "").includes("不删除")) return false;
  return true;
}

function canSelectSoftware(row) {
  if (!row.name) return false;
  if (row.can_uninstall === false) return false;
  return true;
}

function checkCell(cls, group, value, checked, disabled) {
  return `<td><input class="${cls}" data-group="${group}" type="checkbox" value="${esc(value)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}></td>`;
}

function selectHead(group) {
  return `<th class="nosort"><input class="select-all" data-group="${group}" type="checkbox" aria-label="全选"></th>`;
}

function paintSelectAll(group, rows, enabled) {
  const box = document.querySelector(`.select-all[data-group="${group}"]`);
  if (!box) return;
  const set = setFor(group);
  const keys = rows.filter(enabled).map((row) => rowKey(group, row));
  const chosen = keys.filter((key) => set.has(key));
  box.disabled = keys.length === 0;
  box.checked = keys.length > 0 && chosen.length === keys.length;
  box.indeterminate = chosen.length > 0 && chosen.length < keys.length;
  const buttons = {
    files: "select-all-files",
    recent: "select-all-recent",
    software: "select-all-software",
    caches: "select-all-caches",
  };
  const button = document.getElementById(buttons[group]);
  if (button) {
    button.disabled = keys.length === 0;
    button.textContent = keys.length && chosen.length === keys.length ? "取消全选" : "全选";
  }
}

function visibleRows(group) {
  if (group === "files") return visibleFiles();
  if (group === "recent") return visibleRecent();
  if (group === "software") return visibleSoftware();
  return (state && state.caches) || [];
}

function rowEnabled(group, row) {
  if (group === "files" || group === "recent") return canDeleteFile(row);
  if (group === "software") return canSelectSoftware(row);
  const measuring = state && state.phase === "quick";
  const cleaning = !!(state && state.clean && state.clean.running);
  return row.risk === "safe" && row.size_bytes > 0 && !measuring && !cleaning;
}

function toggleVisible(group, force) {
  const rows = group === "caches"
    ? sortRows(((state && state.caches) || []).filter((row) => matches(row, queryOf("cache-filter"), ["name", "paths", "note", "risk_label"])), "caches")
    : visibleRows(group);
  const keys = rows.filter((row) => rowEnabled(group, row)).map((row) => rowKey(group, row));
  const set = setFor(group);
  const allOn = keys.length > 0 && keys.every((key) => set.has(key));
  const turnOn = force == null ? !allOn : force;
  keys.forEach((key) => {
    if (turnOn) set.add(key);
    else set.delete(key);
  });
  if (group === "caches") checkTouched = true;
  paint(group);
  updateButtons();
}

function renderDrive() {
  const drive = state.drive;
  document.body.classList.toggle("pressure", !!(drive && drive.percent >= 90));
  const warn = document.getElementById("disk-warn");
  if (!drive) {
    ["used", "free", "total", "percent"].forEach((id) => {
      document.getElementById(id).textContent = "—";
    });
    document.getElementById("disk-fill").style.width = "0";
    warn.hidden = true;
    return;
  }
  document.getElementById("used").textContent = formatBytes(drive.used, 1);
  document.getElementById("free").textContent = formatBytes(drive.free, 1);
  document.getElementById("total").textContent = formatBytes(drive.total, 1);
  document.getElementById("percent").textContent = `${drive.percent}%`;
  document.getElementById("disk-fill").style.width = `${Math.min(100, drive.percent)}%`;
  warn.hidden = drive.percent < 90;
  if (!warn.hidden) warn.textContent = `C 盘已用 ${drive.percent}%，剩余 ${formatBytes(drive.free, 1)}。`;
}

function renderStatus() {
  const names = { idle: "未开始", quick: "快速统计", deep: "全盘扫描", done: "已完成", cancelled: "已停止", error: "出错" };
  const progress = state.progress || {};
  const bits = [names[state.phase] || state.phase];
  if (state.message) bits.push(state.message);
  if (state.started_at && (state.phase === "quick" || state.phase === "deep")) bits.push(elapsed(state.started_at));
  document.getElementById("phase-line").textContent = bits.join(" · ");
  const ratio = state.phase === "done" ? 100 : (progress.rough_percent || 0);
  document.getElementById("scan-fill").style.width = `${Math.max(0, Math.min(100, ratio))}%`;
  const detail = [];
  if (progress.files) detail.push(`已读 ${Number(progress.files).toLocaleString("zh-CN")} 个文件`);
  if (progress.bytes) detail.push(`已统计 ${formatBytes(progress.bytes)}`);
  if (progress.inaccessible) detail.push(`${progress.inaccessible} 处无权限`);
  document.getElementById("scan-detail").textContent = detail.join(" · ");
  const path = document.getElementById("scan-path");
  path.textContent = progress.current_path || "";
  path.title = progress.current_path || "";
  const samples = progress.inaccessible_samples || [];
  document.getElementById("access-note").textContent = samples.length ? `读不到的例子：${samples.slice(0, 3).join("，")}` : "";
  const counted = document.getElementById("counted");
  counted.hidden = !state.counted_bytes;
  if (state.counted_bytes) counted.textContent = `目录合计 ${formatBytes(state.counted_bytes)}。这是文件大小相加，和系统“已用”不会完全相同。`;
  if (state.phase === "error") showNotice(state.error || state.message || "扫描失败");
}

function renderClean() {
  const clean = state.clean || {};
  const box = document.getElementById("clean-result");
  if (clean.running || (clean.finished_at && clean.message)) {
    let text = clean.message || "正在清理缓存";
    if (!clean.running && clean.errors && clean.errors.length) {
      text += "。例如：" + clean.errors.slice(0, 3).map((item) => item.path).join("，");
    }
    box.hidden = false;
    box.textContent = text;
    return;
  }
  box.hidden = true;
}

function renderConclusion() {
  const blocks = (state.conclusion && state.conclusion.blocks) || [];
  const host = document.getElementById("conclusion");
  if (!blocks.length) {
    host.innerHTML = `<p class="muted">分析开始后，这里会说明最近的软件和文件是不是主因，以及真正占空间的目录。</p>`;
    return;
  }
  host.innerHTML = blocks.map((block) => `
    <article class="card ${esc(block.tone || "info")}">
      <h3>${esc(block.title)}</h3>
      <p>${esc(block.text)}</p>
    </article>`).join("");
}

function renderBars() {
  const rows = Array.isArray(state.top_level) ? state.top_level : [];
  const used = state.drive ? state.drive.used : 0;
  const host = document.getElementById("level-bars");
  if (!rows.length) {
    host.innerHTML = `<p class="muted">一级目录会在各自扫描完成后出现。</p>`;
  } else {
    host.innerHTML = rows.map((row) => {
      const width = used ? Math.min(100, row.size_bytes / used * 100) : 0;
      return `<div class="bar-row">
        <div class="path">${esc(row.path)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${width.toFixed(1)}%"></div></div>
        <div class="bar-size">${formatBytes(row.size_bytes)} · ${percent(row.size_bytes, used)}</div>
      </div>`;
    }).join("");
  }
  document.getElementById("level-note").textContent = state.top_level_partial && state.phase !== "done"
    ? "还有一级目录没统计完。"
    : "";
}

function renderDirs() {
  const used = state && state.drive ? state.drive.used : 0;
  const query = queryOf("hog-filter");
  const source = (state && state.top_dirs) || [];
  const rows = sortRows(source.filter((row) => matches(row, query, ["path", "note"])), "dirs");
  const headers = th("dirs", "path", "路径") + th("dirs", "size_bytes", "大小", true) + `<th class="nosort num">占已用</th>` + th("dirs", "note", "说明");
  const body = rows.map((row) => `<tr>
    <td class="path">${esc(row.path)}</td>
    <td class="num">${formatBytes(row.size_bytes)}</td>
    <td class="num">${percent(row.size_bytes, used)}</td>
    <td>${esc(row.note || "—")}</td>
  </tr>`).join("");
  document.getElementById("dir-table").innerHTML = grid(headers, body, 4, query ? "没有匹配的记录" : "还没有数据");
}

function visibleFiles() {
  const query = queryOf("hog-filter");
  const source = (state && state.top_files) || [];
  return sortRows(source.filter((row) => matches(row, query, ["path", "note"])), "files");
}

function renderFiles() {
  const used = state && state.drive ? state.drive.used : 0;
  const query = queryOf("hog-filter");
  const rows = visibleFiles();
  const headers = selectHead("files") + th("files", "path", "路径") + th("files", "size_bytes", "大小", true) + `<th class="nosort num">占已用</th>` + th("files", "created_ts", "创建时间") + th("files", "note", "说明");
  const body = rows.map((row) => {
    const disabled = !canDeleteFile(row);
    const checked = !disabled && checkedFiles.has(row.path);
    return `<tr>
    ${checkCell("file-check", "files", row.path, checked, disabled)}
    <td class="path">${esc(row.path)}</td>
    <td class="num">${formatBytes(row.size_bytes)}</td>
    <td class="num">${percent(row.size_bytes, used)}</td>
    <td class="num">${esc(row.created || "—")}</td>
    <td>${esc(row.note || disabled ? row.note || "不能删除" : "—")}</td>
  </tr>`;
  }).join("");
  document.getElementById("file-table").innerHTML = grid(headers, body, 6, query ? "没有匹配的记录" : "还没有数据");
  paintSelectAll("files", rows, canDeleteFile);
}

function softwareMarks(row) {
  const marks = [];
  if (row.bulky) marks.push(badge("近期且体积大", "bad"));
  else if (row.recent) marks.push(badge("近 90 天", "info"));
  if (row.partial) marks.push(badge("未测完", "warn"));
  return marks.join(" ") || "—";
}

function visibleSoftware() {
  const onlyRecent = document.getElementById("only-recent").checked;
  const query = queryOf("software-filter");
  let rows = ((state && state.software) || []).filter((row) => !onlyRecent || row.recent);
  rows = rows.filter((row) => matches(row, query, ["name", "publisher", "install_location"]));
  return sortRows(rows, "software");
}

function renderSoftware() {
  const used = state && state.drive ? state.drive.used : 0;
  const all = (state && state.software) || [];
  const recent = all.filter((row) => row.recent);
  const recentBytes = recent.reduce((sum, row) => sum + (row.size_bytes || 0), 0);
  const ratio = used ? (recentBytes / used * 100).toFixed(1) : "0.0";
  document.getElementById("software-summary").textContent =
    `近 90 天 ${recent.length} 个，能统计到的体积 ${formatBytes(recentBytes)}，占已用 ${ratio}%。`;
  const onlyRecent = document.getElementById("only-recent").checked;
  const query = queryOf("software-filter");
  const rows = visibleSoftware();
  const headers = [
    selectHead("software"),
    th("software", "name", "名称"),
    th("software", "publisher", "发布者"),
    th("software", "install_ts", "安装日期"),
    th("software", "size_bytes", "大小", true),
    `<th class="nosort num">占已用</th>`,
    th("software", "size_source", "大小来源"),
    `<th class="nosort">标记</th>`,
    th("software", "install_location", "安装路径"),
  ].join("");
  const body = rows.map((row) => {
    const key = rowKey("software", row);
    const disabled = !canSelectSoftware(row);
    const checked = !disabled && checkedSoftware.has(key);
    return `<tr>
    ${checkCell("software-check", "software", key, checked, disabled)}
    <td>${esc(row.name)}</td>
    <td>${esc(row.publisher || "—")}</td>
    <td class="num">${esc(row.install_date || "—")}</td>
    <td class="num">${formatBytes(row.size_bytes)}</td>
    <td class="num">${percent(row.size_bytes, used)}</td>
    <td>${esc(row.size_source || "—")}</td>
    <td>${softwareMarks(row)}</td>
    <td class="path">${esc(row.install_location || "—")}</td>
  </tr>`;
  }).join("");
  document.getElementById("software-table").innerHTML = grid(headers, body, 9, query || onlyRecent ? "没有匹配的记录" : "还没有数据");
  paintSelectAll("software", rows, canSelectSoftware);
}

function visibleRecent() {
  const query = queryOf("recent-filter");
  const source = ((state && state.recent_files) || {})[String(days)] || [];
  return sortRows(source.filter((row) => matches(row, query, ["path", "note"])), "recent");
}

function renderRecent() {
  const summary = ((state && state.recent_summary) || {})[String(days)] || { bytes: 0, count: 0 };
  document.getElementById("recent-summary").textContent =
    `近 ${days} 天、大于 50 MB 的新文件合计 ${formatBytes(summary.bytes || 0)}，${summary.count || 0} 个。表格列出其中最大的一批。`;
  const query = queryOf("recent-filter");
  const rows = visibleRecent();
  const headers = selectHead("recent") + th("recent", "path", "路径") + th("recent", "size_bytes", "大小", true) + th("recent", "created_ts", "创建时间") + th("recent", "note", "说明");
  const body = rows.map((row) => {
    const disabled = !canDeleteFile(row);
    const checked = !disabled && checkedRecent.has(row.path);
    return `<tr>
    ${checkCell("file-check", "recent", row.path, checked, disabled)}
    <td class="path">${esc(row.path)}</td>
    <td class="num">${formatBytes(row.size_bytes)}</td>
    <td class="num">${esc(row.created || "—")}</td>
    <td>${esc(row.note || "—")}</td>
  </tr>`;
  }).join("");
  document.getElementById("recent-table").innerHTML = grid(headers, body, 5, query ? "没有匹配的记录" : "还没有数据");
  paintSelectAll("recent", rows, canDeleteFile);
}

function renderCaches() {
  const query = queryOf("cache-filter");
  const measuring = state.phase === "quick";
  const cleaning = !!(state.clean && state.clean.running);
  const source = (state.caches || []).filter((row) => matches(row, query, ["name", "paths", "note", "risk_label"]));
  const rows = sortRows(source, "caches");
  const headers = selectHead("caches") + th("caches", "name", "名称") + th("caches", "size_bytes", "大小", true) + th("caches", "file_count", "文件数", true) + th("caches", "risk", "风险") + th("caches", "paths", "路径") + th("caches", "note", "说明");
  const body = rows.map((row) => {
    const disabled = row.risk !== "safe" || !(row.size_bytes > 0) || measuring || cleaning;
    const checked = !disabled && checkedIds.has(row.id);
    return `<tr class="${row.recommended ? "picked" : ""}">
      ${checkCell("cache-check", "caches", row.id, checked, disabled)}
      <td>${esc(row.name)}</td>
      <td class="num">${formatBytes(row.size_bytes)}</td>
      <td class="num">${row.file_count ?? "—"}</td>
      <td>${badge(row.risk_label || row.risk, row.risk === "safe" ? "ok" : "warn")}</td>
      <td class="path">${(row.paths || []).map(esc).join("<br>")}</td>
      <td>${esc(row.note || "—")}</td>
    </tr>`;
  }).join("");
  document.getElementById("cache-table").innerHTML = grid(headers, body, 7, query ? "没有匹配的记录" : "还没有数据");
  paintSelectAll("caches", rows, (row) => row.risk === "safe" && row.size_bytes > 0 && !measuring && !cleaning);
}

function renderTables() {
  syncDefaultChecks();
  renderConclusion();
  renderBars();
  renderDirs();
  renderFiles();
  renderSoftware();
  renderRecent();
  renderCaches();
}

function render(next) {
  state = next;
  renderDrive();
  renderStatus();
  renderClean();
  updateButtons();
  if (next.revision !== lastRevision) {
    renderTables();
    lastRevision = next.revision;
    updateButtons();
  }
}

function paint(table) {
  if (!state) return;
  if (table === "dirs") renderDirs();
  if (table === "files") renderFiles();
  if (table === "software") renderSoftware();
  if (table === "recent") renderRecent();
  if (table === "caches") renderCaches();
}

function startPolling() {
  if (timer) return;
  timer = setInterval(() => { loop(); }, 1000);
}

function stopPolling() {
  if (!timer) return;
  clearInterval(timer);
  timer = null;
}

async function tick() {
  const { data } = await api("/api/state", { cache: "no-store" });
  if (!data || !data.phase) return;
  render(data);
}

async function loop() {
  if (ticking) return;
  ticking = true;
  try {
    await tick();
    if (state && !shouldPoll(state)) stopPolling();
  } catch (error) {
    showNotice("页面暂时连不上分析服务");
  } finally {
    ticking = false;
  }
}

async function startScan() {
  showNotice("");
  const { data } = await api("/api/scan/start", { method: "POST" });
  if (data && data.ok === false && !(data.message || "").includes("已在进行")) {
    showNotice(data.message || "无法开始扫描");
  }
  startPolling();
  await tick();
}

async function stopScan() {
  await api("/api/scan/stop", { method: "POST" });
  startPolling();
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  pendingIds = [];
  modalAction = null;
}

function openChoiceModal({ title, text, warn, okLabel, items, enabled, action }) {
  modalAction = action;
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-text").textContent = text;
  document.getElementById("modal-warn").textContent = warn;
  document.getElementById("modal-list").innerHTML = (items || []).map((item) => `
    <li>
      <strong>${esc(item.name)}</strong>
      <span>${item.size_bytes == null ? "" : formatBytes(item.size_bytes)}</span>
      <div class="path">${(item.paths || []).map(esc).join("<br>") || "—"}</div>
    </li>`).join("");
  const ok = document.getElementById("modal-ok");
  ok.textContent = okLabel;
  ok.disabled = !enabled;
  document.getElementById("modal").hidden = false;
  document.getElementById("modal-cancel").focus();
}

function openModal(preview, ids) {
  pendingIds = ids.slice();
  const items = preview.items || [];
  const hasPaths = items.some((item) => item.paths && item.paths.length);
  openChoiceModal({
    title: checkTouched ? "确认清理选中的缓存" : "确认清理占空间较大的安全缓存",
    text: `预计释放 ${formatBytes(preview.total_bytes)}。大小来自最近一次统计，确认后会清空下面这些位置里的文件。`,
    warn: "只会删除这些缓存里的文件，缓存文件夹本身会留下。正在使用的文件会被跳过。",
    okLabel: "确认清理",
    items,
    enabled: hasPaths,
    action: confirmClean,
  });
}

async function requestClean() {
  const ids = activeIds();
  if (!ids.length) return;
  showNotice("");
  const { data } = await api("/api/caches/clean", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, confirm: false }),
  });
  if (!data.ok) {
    showNotice(data.message || data.reason || "无法清理");
    return;
  }
  openModal(data, ids);
}

async function confirmClean() {
  const button = document.getElementById("modal-ok");
  button.disabled = true;
  const ids = pendingIds.slice();
  const { data } = await api("/api/caches/clean", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, confirm: true }),
  });
  closeModal();
  if (!data.ok) {
    showNotice(data.message || data.reason || "没有开始清理");
    return;
  }
  startPolling();
  await tick();
}

async function requestDelete(group) {
  const paths = [...setFor(group)];
  if (!paths.length) return;
  showNotice("");
  const { data } = await api("/api/files/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, confirm: false }),
  });
  if (!data.ok) {
    showNotice(data.message || "没有可以删除的文件");
    return;
  }
  const program = (data.items || []).some((item) => /\\program files/i.test(item.path || (item.paths || [])[0] || ""));
  openChoiceModal({
    title: "确认删除选中的文件",
    text: `将删除 ${data.items.length} 个文件，预计释放 ${formatBytes(data.total_bytes)}。${(data.rejected || []).length ? `另有 ${data.rejected.length} 个不能删，已跳过。` : ""}`,
    warn: program
      ? "其中包含程序目录里的文件，删除后对应软件可能打不开。删除后进不了回收站。"
      : "删除后文件进不了回收站。系统文件和 Windows 目录不会删。",
    okLabel: "确认删除",
    items: data.items,
    enabled: data.items.length > 0,
    action: async () => {
      const chosen = (data.items || []).map((item) => item.path);
      const result = await api("/api/files/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: chosen, confirm: true }),
      });
      chosen.forEach((path) => {
        checkedFiles.delete(path);
        checkedRecent.delete(path);
      });
      closeModal();
      showNotice((result.data && result.data.message) || "删除已结束");
      await tick();
    },
  });
}

async function requestUninstall() {
  const selected = ((state && state.software) || []).filter((row) => checkedSoftware.has(rowKey("software", row)));
  if (!selected.length) return;
  const items = selected.slice(0, 20).map((row) => ({
    id: row.id || "",
    name: row.name,
    install_location: row.install_location || "",
  }));
  showNotice("");
  const { data } = await api("/api/software/uninstall", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items, confirm: false }),
  });
  if (!data.ok) {
    showNotice(data.message || "没有可以卸载的软件");
    return;
  }
  openChoiceModal({
    title: "确认卸载选中的软件",
    text: `将打开 ${data.items.length} 个软件自带的卸载程序。请在弹出的窗口里自己完成卸载。${(data.rejected || []).length ? `另有 ${data.rejected.length} 个没有卸载程序，已跳过。` : ""}`,
    warn: "这里不会静默卸载，也不会额外加上静默参数。卸载完成后需要重新分析，列表才会更新。",
    okLabel: "打开卸载程序",
    items: data.items,
    enabled: data.items.length > 0,
    action: async () => {
      const result = await api("/api/software/uninstall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, confirm: true }),
      });
      closeModal();
      showNotice((result.data && result.data.message) || "已处理卸载请求");
      await tick();
    },
  });
}

function bind() {
  document.getElementById("start-btn").addEventListener("click", () => { startScan(); });
  document.getElementById("stop-btn").addEventListener("click", () => { stopScan(); });
  document.getElementById("clean-btn").addEventListener("click", () => { requestClean(); });
  document.getElementById("clean-caches").addEventListener("click", () => { requestClean(); });
  document.getElementById("delete-files").addEventListener("click", () => { requestDelete("files"); });
  document.getElementById("delete-recent").addEventListener("click", () => { requestDelete("recent"); });
  document.getElementById("uninstall-software").addEventListener("click", () => { requestUninstall(); });
  document.getElementById("select-all-files").addEventListener("click", () => { toggleVisible("files"); });
  document.getElementById("select-all-recent").addEventListener("click", () => { toggleVisible("recent"); });
  document.getElementById("select-all-software").addEventListener("click", () => { toggleVisible("software"); });
  document.getElementById("select-all-caches").addEventListener("click", () => { toggleVisible("caches"); });
  document.getElementById("modal-cancel").addEventListener("click", closeModal);
  document.getElementById("modal-ok").addEventListener("click", async () => {
    const action = modalAction;
    if (!action) return;
    document.getElementById("modal-ok").disabled = true;
    await action();
  });
  document.getElementById("modal").addEventListener("click", (event) => {
    if (event.target.id === "modal") closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
  document.addEventListener("click", (event) => {
    const header = event.target.closest("th[data-key]");
    if (!header) return;
    const table = header.dataset.table;
    const key = header.dataset.key;
    const current = sortState[table];
    if (!current) return;
    if (current.key === key) current.dir *= -1;
    else {
      current.key = key;
      current.dir = ["name", "path", "publisher", "paths", "note", "size_source", "install_location", "risk"].includes(key) ? 1 : -1;
    }
    paint(table);
  });
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.classList && target.classList.contains("select-all")) {
      toggleVisible(target.dataset.group, target.checked);
      return;
    }
    if (target.classList && target.classList.contains("cache-check")) {
      checkTouched = true;
      if (target.checked) checkedIds.add(target.value);
      else checkedIds.delete(target.value);
      paintSelectAll("caches", sortRows(((state && state.caches) || []).filter((row) => matches(row, queryOf("cache-filter"), ["name", "paths", "note", "risk_label"])), "caches"), (row) => rowEnabled("caches", row));
      updateButtons();
    }
    if (target.classList && target.classList.contains("file-check")) {
      const set = setFor(target.dataset.group);
      if (target.checked) set.add(target.value);
      else set.delete(target.value);
      paint(target.dataset.group);
      updateButtons();
    }
    if (target.classList && target.classList.contains("software-check")) {
      if (target.checked) checkedSoftware.add(target.value);
      else checkedSoftware.delete(target.value);
      paint("software");
      updateButtons();
    }
    if (target.id === "only-recent") renderSoftware();
  });
  ["hog-filter", "software-filter", "recent-filter", "cache-filter"].forEach((id) => {
    document.getElementById(id).addEventListener("input", () => {
      if (id === "hog-filter") { renderDirs(); renderFiles(); }
      if (id === "software-filter") renderSoftware();
      if (id === "recent-filter") renderRecent();
      if (id === "cache-filter") renderCaches();
    });
  });
  document.querySelectorAll(".day-btn").forEach((button) => {
    button.addEventListener("click", () => {
      days = Number(button.dataset.days);
      document.querySelectorAll(".day-btn").forEach((item) => item.classList.toggle("active", item === button));
      renderRecent();
    });
  });
}

async function boot() {
  bind();
  try {
    await tick();
  } catch (error) {
    showNotice("页面暂时连不上分析服务");
    return;
  }
  if (state && state.phase === "idle" && !state.saved_at) await startScan();
  else if (state && shouldPoll(state)) startPolling();
}

boot();
