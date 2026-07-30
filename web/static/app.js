const API = "";
let selectedDataFile = null;
let selectedSymbol = null;
let selectedTimeframe = null;
let historicalDataFiles = [];
let selectedStrategyFile = null;
let strategyUploadTarget = "backtest";
let selectedStrategySymbol = null;
let selectedBacktestRunId = null;
let backtestRuns = [];
let chart = null;
let chartSymbol = null;
let pollTimer = null;
let realtimePollTimer = null;
let clientErrors = [];
let debugMode = false;
let lastDebugViewContent = "";

// 分页与回测状态
let currentPage = "train";
let btActive = false;
let btBuster = "";      // 图表缓存刷新键（用 job 时间戳）
let btRunsSig = "";
let btPortfolioSig = ""; // 绩效卡签名：变化时才重建 + 播放数字动画，避免每次轮询重播
let lastEquityData = null; // 最近一次资金曲线数据，供绩效卡 sparkline 复用
let lastTrainingActive = false;
let btLastAlertKey = "";
let lastErrorPopupText = "";
let lastErrorPopupAt = 0;

const $ = (id) => document.getElementById(id);

const CPU_TRAINING_NOTE = `暂无报错

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【为什么用 CPU 训练，不用 GPU？】

你可以把 GPU 想象成一辆超大的货车，CPU 想象成一辆灵活的小电瓶车。

我们这个项目的训练，就像要做很多很多道「小题」：
每道题只算一点点数字，算完马上换下一道。
货车虽然一次能装很多，但每装卸一次都要准备很久才能再出发；
电瓶车一次装的少，但说走就走，一道接一道做，反而更快。

再打个比方：
GPU 像很多厨师一起做大锅饭，适合一次炒一大锅；
我们这个训练更像一道道菜分开炒，而且每道菜份量很小。
大锅饭团队每次开火、洗锅、集合都要时间，小菜一碟反而耽误在「准备」上。

所以具体原因是：
1. 每次要算的数据不多，GPU「启动一次计算」的等待，有时比真正算数还久。
2. 训练是一步接一步、一条公式接一条公式地指挥，GPU 经常闲着等下一道题，没法一直满负荷。
3. 数据还要在 CPU 和 GPU 之间来回搬运，也要花时间。

我们实测过（同样训练 50 步）：GPU 大约 4.5 秒一步，CPU 大约 1.9 秒一步。
这不是显卡坏了，也不是没装驱动，而是这个项目的做题方式，更适合 CPU。

说白了就是这个项目用CPU训练的速度比用GPU训练的速度更快`;

function emptyDebugMessage() {
  return debugMode ? "暂无日志" : CPU_TRAINING_NOTE;
}

function formatApiError(data, status, path) {
  const d = data?.detail;
  let detail = "";
  if (Array.isArray(d)) {
    detail = d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  } else if (typeof d === "string") {
    detail = d;
  } else if (d) {
    detail = JSON.stringify(d);
  }
  if (data?.traceback) {
    detail += `\n\n${data.traceback}`;
  }
  return detail || `HTTP ${status} ${path}`;
}

async function logClientError(message, context = {}) {
  const entry = `[${new Date().toLocaleString()}] ${message}`;
  clientErrors.push(entry);
  if (clientErrors.length > 80) clientErrors = clientErrors.slice(-80);
  renderDebugView();
  const silent = !!context.silent;
  if (!silent) {
    const detail = context.detail ? `${message}\n\n${context.detail}` : message;
    showErrorPopup("出错了", detail);
  }
  try {
    await fetch(API + "/api/debug/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: "error", message, context }),
    });
  } catch (_) {
    /* server may be down */
  }
}

function showErrorPopup(title, detail) {
  const modal = $("errorModal");
  const titleEl = $("errorModalTitle");
  const detailEl = $("errorModalDetail");
  if (!modal || !detailEl) {
    window.alert(`${title}\n\n${detail}`);
    return;
  }
  const text = String(detail || "").trim() || "未知错误";
  const now = Date.now();
  if (text === lastErrorPopupText && now - lastErrorPopupAt < 2500) return;
  lastErrorPopupText = text;
  lastErrorPopupAt = now;
  if (titleEl) titleEl.textContent = title || "出错了";
  detailEl.textContent = text;
  modal.hidden = false;
}

function closeErrorPopup() {
  const modal = $("errorModal");
  if (modal) modal.hidden = true;
}

async function copyErrorPopupDetail() {
  const text = $("errorModalDetail")?.textContent || "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

function isViewAtBottom(el, threshold = 40) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
}

function renderDebugView(serverLines = [], errorLines = []) {
  const parts = [];
  if (clientErrors.length) {
    parts.push("=== 前端报错 ===", ...clientErrors);
  }
  if (errorLines.length) {
    parts.push("\n=== 服务端错误日志 (logs/web_errors.log) ===", ...errorLines);
  }
  if (debugMode && serverLines.length) {
    parts.push("\n=== 服务端运行日志 (logs/web_server.log) ===", ...serverLines);
  }
  const el = $("debugView");
  const atBottom = isViewAtBottom(el);
  const next = parts.length ? parts.join("\n") : emptyDebugMessage();
  const changed = next !== lastDebugViewContent;
  el.textContent = next;
  if (changed && atBottom && lastDebugViewContent) {
    el.scrollTop = el.scrollHeight;
  }
  lastDebugViewContent = next;
}

async function setDebugMode(enabled) {
  debugMode = !!enabled;
  $("debugModeCheck").checked = debugMode;
  try {
    await fetchJSON("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ debug_mode: debugMode }),
    });
  } catch (e) {
    await logClientError("切换调试模式失败: " + e.message);
  }
  if (!debugMode) {
    renderDebugView([], []);
  } else {
    await refreshDebugLogs();
  }
}

async function refreshDebugLogs() {
  try {
    const data = await fetch(API + "/api/debug/logs?lines=120").then(async (res) => {
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(formatApiError(json, res.status, "/api/debug/logs"));
      return json;
    });
    $("debugLogPaths").textContent = `本地: ${data.error_log}`;
    renderDebugView(data.server_tail || [], data.error_tail || []);
  } catch (e) {
    renderDebugView();
  }
}

async function fetchJSON(path, opts = {}) {
  const silent = !!opts.silent;
  const maxRetries = opts.retries != null ? Number(opts.retries) : 5;
  const retryDelayMs = opts.retryDelayMs != null ? Number(opts.retryDelayMs) : 2000;
  const fetchOpts = { ...opts };
  delete fetchOpts.silent;
  delete fetchOpts.retries;
  delete fetchOpts.retryDelayMs;

  let lastNetworkMsg = null;
  for (let attempt = 1; attempt <= Math.max(1, maxRetries); attempt++) {
    let res;
    try {
      res = await fetch(API + path, fetchOpts);
    } catch (e) {
      lastNetworkMsg = `网络错误 ${path}: ${e.message}`;
      if (attempt < maxRetries) {
        await new Promise((r) => setTimeout(r, retryDelayMs));
        continue;
      }
      await logClientError(lastNetworkMsg, { path, silent, attempts: attempt });
      throw new Error(lastNetworkMsg);
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 401) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.replace(`/login?next=${next}`);
        throw new Error("登录已失效，正在跳转登录页");
      }
      const msg = formatApiError(data, res.status, path);
      await logClientError(`${path} -> ${msg}`, { path, status: res.status, silent });
      if (!silent) await refreshDebugLogs();
      throw new Error(msg);
    }
    return data;
  }
  throw new Error(lastNetworkMsg || `网络错误 ${path}`);
}

function formatScore(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toFixed(4);
}

function renderDataFileCard(info) {
  const card = $("dataFileCard");
  const startBtn = $("startBtn");

  if (!info || !info.data_file) {
    card.className = "data-file-card";
    card.innerHTML = '<div class="data-file-empty">尚未选择数据文件</div>';
    selectedDataFile = null;
    selectedSymbol = null;
    selectedTimeframe = null;
    startBtn.disabled = true;
    if ($("retrainBtn")) $("retrainBtn").disabled = true;
    if ($("exportBtn")) $("exportBtn").disabled = true;
    if ($("exportTrainingBtn")) $("exportTrainingBtn").disabled = true;
    if ($("importTrainingBtn")) $("importTrainingBtn").disabled = true;
    return;
  }

  selectedDataFile = info.data_file;
  selectedSymbol = info.symbol || null;
  selectedTimeframe = info.timeframe || null;

  if (info.valid === false) {
    card.className = "data-file-card invalid";
    card.innerHTML = `
      <div class="data-file-error">${info.message || "文件无效"}</div>
      <div class="data-file-path">${info.data_file}</div>
    `;
    startBtn.disabled = true;
    if ($("retrainBtn")) $("retrainBtn").disabled = true;
    if ($("exportTrainingBtn")) $("exportTrainingBtn").disabled = true;
    if ($("importTrainingBtn")) $("importTrainingBtn").disabled = true;
    return;
  }

  card.className = "data-file-card valid";
  const yearsText = info.years_h1 != null ? `${info.years_h1} 年` : "—";
  card.innerHTML = `
    <div class="data-file-row">
      <div class="item"><span class="label">品种</span><span class="value sym">${info.symbol}</span></div>
      <div class="item"><span class="label">周期</span><span class="value">${info.timeframe}</span></div>
      <div class="item"><span class="label">K线</span><span class="value">${info.bars?.toLocaleString()}</span></div>
      <div class="item"><span class="label">数据年限</span><span class="value">${yearsText}</span></div>
      <div class="item"><span class="label">进度</span><span class="value" id="fileProgressPct">—</span></div>
      <div class="item"><span class="label">本次训练时长</span><span class="value" id="fileElapsedTime">—</span></div>
      <div class="item"><span class="label">历史训练总时长</span><span class="value" id="fileHistoryElapsedTime">—</span></div>
      <div class="item"><span class="label">最优分数</span><span class="value score-best" id="fileBestScore">—</span></div>
      <div class="item"><span class="label">验证分数</span><span class="value score-val" id="fileValScore">—</span></div>
    </div>
    <div class="path" title="${info.data_file}">${info.filename || info.data_file}</div>
  `;
  startBtn.disabled = false;
  if ($("retrainBtn")) $("retrainBtn").disabled = false;
}

function formatFileSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / (1024 * 1024)).toFixed(value >= 100 * 1024 * 1024 ? 0 : 1)} MB`;
}

function updateHistoryDataSelect(active = false) {
  const select = $("historyDataSelect");
  if (!select) return;
  select.disabled = active || select.options.length <= 1;
  const deleteBtn = $("deleteHistoryDataBtn");
  if (deleteBtn) {
    const row = historicalDataFiles.find(
      (item) => normalizeFilePath(item.data_file) === normalizeFilePath(select.value)
    );
    deleteBtn.disabled = active || !row;
    deleteBtn.title = row
      ? "永久删除历史记录及对应的源 Parquet 文件"
      : "请先选择一条历史数据";
  }
}

async function refreshDataFileHistory(preferredPath = selectedDataFile) {
  const select = $("historyDataSelect");
  if (!select) return;

  const preferredKey = String(preferredPath || "").replaceAll("\\", "/").toLowerCase();
  try {
    const response = await fetchJSON("/api/data-files/history", { silent: true });
    const rows = response.data_files || [];
    historicalDataFiles = rows;
    select.replaceChildren();

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = rows.length ? "选择已保存的数据…" : "暂无历史数据";
    select.appendChild(placeholder);

    for (const row of rows) {
      const option = document.createElement("option");
      option.value = row.data_file;
      const size = formatFileSize(row.size_bytes);
      option.textContent = `${row.symbol} · ${row.timeframe} · ${row.filename}${size ? ` · ${size}` : ""}`;
      option.title = row.data_file;
      if (row.data_file.replaceAll("\\", "/").toLowerCase() === preferredKey) {
        option.selected = true;
      }
      select.appendChild(option);
    }
    updateHistoryDataSelect(false);
  } catch (_) {
    historicalDataFiles = [];
    select.replaceChildren(new Option("历史数据加载失败", ""));
    select.disabled = true;
  }
}

async function selectHistoricalDataFile(event) {
  const select = event.target;
  const dataFile = select.value;
  if (!dataFile) return;

  updateHistoryDataSelect(true);
  try {
    const res = await fetchJSON("/api/data-file/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_file: dataFile }),
    });
    renderDataFileCard(res);
    selectedSymbol = res.symbol || null;
    selectedTimeframe = res.timeframe || null;
    await refreshDataFileHistory(res.data_file);
    await refreshOverview();
  } catch (e) {
    select.value = selectedDataFile || "";
    $("debugView")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } finally {
    updateHistoryDataSelect(false);
  }
}

function updateBtStartBtn() {
  const startBtn = $("btStartBtn");
  if (!startBtn) return;
  startBtn.disabled = btActive || !selectedStrategyFile;
  const strategySelect = $("btStrategySelect");
  if (strategySelect) strategySelect.disabled = btActive || strategySelect.options.length <= 1;
  ["btCommissionInput", "btSlippageInput"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = btActive;
  });
}

function renderStrategyFileCard(info) {
  const card = $("btStrategyCard");
  if (!card) return;

  if (!info || !info.strategy_file) {
    card.className = "data-file-card";
    card.innerHTML = '<div class="data-file-empty">尚未选择策略文件</div>';
    selectedStrategyFile = null;
    selectedStrategySymbol = null;
    updateBtStartBtn();
    return;
  }

  if (info.valid === false) {
    card.className = "data-file-card invalid";
    card.innerHTML = `
      <div class="data-file-error">${info.message || "文件无效"}</div>
      <div class="data-file-path">${info.strategy_file}</div>
    `;
    selectedStrategyFile = null;
    selectedStrategySymbol = null;
    updateBtStartBtn();
    return;
  }

  selectedStrategyFile = info.strategy_file;
  selectedStrategySymbol = info.symbol || null;
  const strategySelect = $("btStrategySelect");
  if (strategySelect) {
    const selectedKey = normalizeFilePath(info.strategy_file);
    const matching = Array.from(strategySelect.options).find(
      (option) => normalizeFilePath(option.value) === selectedKey
    );
    strategySelect.value = matching?.value || "";
  }
  card.className = "data-file-card valid";
  const timeframeItem = info.timeframe
    ? `<div class="item"><span class="label">周期</span><span class="value">${info.timeframe}</span></div>`
    : "";
  const dataPath = info.data_file || "";
  const dataOk = info.data_file_exists;
  const dataHint = dataPath
    ? (dataOk ? dataPath : `（文件不存在）${dataPath}`)
    : "未记录数据路径 — 回测前请先在训练页选择同品种 Parquet";
  card.innerHTML = `
    <div class="data-file-row">
      <div class="item"><span class="label">品种</span><span class="value sym">${info.symbol || "—"}</span></div>
      ${timeframeItem}
      <div class="item"><span class="label">最优分数</span><span class="value score-best">${formatScore(info.best_score)}</span></div>
      <div class="item"><span class="label">词表版本</span><span class="value">${info.vocab_version || "—"}</span></div>
      <div class="item"><span class="label">公式长度</span><span class="value">${info.formula_decoded ? info.formula_decoded.split("→").length : "—"}</span></div>
    </div>
    <div class="path" title="${info.strategy_file}">策略: ${info.filename || info.strategy_file}</div>
    <div class="path ${dataPath && dataOk ? "" : "data-file-missing"}" title="${dataPath || ""}">数据: ${dataHint}</div>
  `;
  updateBtStartBtn();
}

function normalizeFilePath(value) {
  return String(value || "").replaceAll("\\", "/").toLowerCase();
}

function renderBacktestStrategyOptions(rows) {
  const select = $("btStrategySelect");
  if (!select) return;
  const selectedKey = normalizeFilePath(selectedStrategyFile);
  const signature = (rows || [])
    .map((row) => [row.strategy_file, row.symbol, row.timeframe, row.best_score].join("|"))
    .join(";");
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.replaceChildren();

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = rows?.length ? "选择已保存策略…" : "暂无已保存策略";
  select.appendChild(placeholder);

  for (const row of rows || []) {
    const option = document.createElement("option");
    option.value = row.strategy_file || "";
    const score = row.best_score == null ? "—" : formatScore(row.best_score);
    option.textContent = `${row.symbol || "—"} · ${row.timeframe || "—"} · 分数 ${score}`;
    option.title = row.strategy_file || row.file || "";
    if (normalizeFilePath(option.value) === selectedKey) option.selected = true;
    select.appendChild(option);
  }
  select.disabled = !rows?.length || btActive;
}

async function selectSavedBacktestStrategy(event) {
  const select = event.target;
  const strategyFile = select.value;
  if (!strategyFile) return;
  select.disabled = true;
  try {
    const info = await fetchJSON("/api/strategy-file/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy_file: strategyFile }),
    });
    renderStrategyFileCard(info);
    const matchedRun = backtestRuns.find(
      (run) => run.available && normalizeFilePath(run.strategy_file) === normalizeFilePath(strategyFile)
    );
    if (matchedRun) {
      selectedBacktestRunId = matchedRun.run_id;
      if ($("btRunSelect")) $("btRunSelect").value = matchedRun.run_id;
      btBuster = matchedRun.run_id;
      btPortfolioSig = "";
      await refreshBacktestReport();
    }
  } catch (_) {
    select.value = Array.from(select.options).find(
      (option) => normalizeFilePath(option.value) === normalizeFilePath(selectedStrategyFile)
    )?.value || "";
  } finally {
    select.disabled = btActive || select.options.length <= 1;
  }
}

async function deleteHistoricalData() {
  const select = $("historyDataSelect");
  const row = historicalDataFiles.find(
    (item) => normalizeFilePath(item.data_file) === normalizeFilePath(select?.value)
  );
  if (!row) return;

  const warning =
    `将永久删除以下历史数据及源 Parquet 文件：\n\n${row.data_file}\n\n删除后无法恢复，是否继续？`;
  if (!window.confirm(warning)) return;

  updateHistoryDataSelect(true);
  try {
    const response = await fetchJSON("/api/data-file/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_file: row.data_file }),
    });
    if (normalizeFilePath(selectedDataFile) === normalizeFilePath(row.data_file)) {
      renderDataFileCard(null);
    }
    await refreshDataFileHistory(response.current_data_file || null);
    await refreshOverview();
    window.alert(response.message || "历史数据已删除");
  } catch (error) {
    showErrorPopup("删除历史数据失败", error.message);
  } finally {
    updateHistoryDataSelect(false);
  }
}

function formatElapsed(startedAtIso, endAtIso) {
  if (!startedAtIso) return "—";
  const started = new Date(startedAtIso).getTime();
  if (Number.isNaN(started)) return "—";
  const end = endAtIso ? new Date(endAtIso).getTime() : Date.now();
  if (Number.isNaN(end)) return "—";
  return formatDurationSeconds(Math.max(0, Math.floor((end - started) / 1000)));
}

function formatDurationSeconds(secs) {
  if (secs == null || secs < 0) return "—";
  const total = Math.floor(secs);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h > 0) return `${h}小时${m}分`;
  if (m > 0) return `${m}分钟`;
  return `${total}秒`;
}

function updateTrainingTimeFields(progress, training) {
  const sessionEl = $("fileElapsedTime");
  const historyEl = $("fileHistoryElapsedTime");
  if (!sessionEl && !historyEl) return;

  if (historyEl) {
    const hist = progress?.history_total_seconds;
    historyEl.textContent = hist != null ? formatDurationSeconds(hist) : "—";
  }

  const job = training?.job;
  const active = !!training?.active;
  if (!sessionEl) return;

  if (!job || job.state === "idle") {
    sessionEl.textContent = "—";
    return;
  }

  const elapsed = formatElapsed(job.started_at, active ? null : job.finished_at);
  sessionEl.textContent = active || elapsed === "—" ? elapsed : `${elapsed}（已停）`;
}

function updateFileProgress(progress) {
  const el = document.getElementById("fileProgressPct");
  if (el && progress) {
    el.textContent = `${progress.current_step} / ${progress.train_steps} (${progress.progress_pct}%)`;
  }
  const bestEl = document.getElementById("fileBestScore");
  if (bestEl) {
    bestEl.textContent = progress ? formatScore(progress.best_score) : "—";
  }
  const valEl = document.getElementById("fileValScore");
  if (valEl) {
    let val = progress?.val_score;
    if (val == null && progress?.history?.val_score?.length) {
      val = progress.history.val_score[progress.history.val_score.length - 1];
    }
    valEl.textContent = progress ? formatScore(val) : "—";
  }
}

const CHART_SERIES = [
  { key: "best_score", label: "最优分数", borderColor: "#34f5c8", fillRGB: "52, 245, 200", yAxisID: "y" },
  { key: "val_score", label: "验证分数", borderColor: "#38bdf8", fillRGB: "56, 189, 248", yAxisID: "y" },
];

// 让曲线在填充区形成竖向渐变
function makeGradient(ctx, area, rgb) {
  if (!area) return `rgba(${rgb}, 0.08)`;
  const g = ctx.createLinearGradient(0, area.top, 0, area.bottom);
  g.addColorStop(0, `rgba(${rgb}, 0.28)`);
  g.addColorStop(0.6, `rgba(${rgb}, 0.06)`);
  g.addColorStop(1, `rgba(${rgb}, 0)`);
  return g;
}

// 发光效果：在每条数据线绘制前设置对应颜色的柔和阴影
const glowPlugin = {
  id: "neonGlow",
  beforeDatasetDraw(chart, args) {
    const color = args?.meta?.dataset?.options?.borderColor;
    const ctx = chart.ctx;
    ctx.save();
    if (typeof color === "string") {
      ctx.shadowColor = color;
      ctx.shadowBlur = 10;
    }
  },
  afterDatasetDraw(chart) {
    chart.ctx.restore();
  },
};
if (window.Chart) Chart.register(glowPlugin);

const CHART_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: "index", intersect: false },
  animation: { duration: 450, easing: "easeOutQuart" },
  transitions: {
    active: { animation: { duration: 450, easing: "easeOutQuart" } },
  },
  plugins: {
    legend: {
      labels: {
        color: "#a9bccf",
        usePointStyle: true,
        pointStyle: "circle",
        boxWidth: 8,
        boxHeight: 8,
        padding: 16,
        font: { family: "'DM Sans'", size: 12, weight: "600" },
      },
    },
    tooltip: {
      backgroundColor: "rgba(8, 12, 20, 0.92)",
      borderColor: "rgba(94, 234, 212, 0.35)",
      borderWidth: 1,
      titleColor: "#e8edf4",
      bodyColor: "#a9bccf",
      titleFont: { family: "'JetBrains Mono'", size: 11 },
      bodyFont: { family: "'JetBrains Mono'", size: 11 },
      padding: 10,
      cornerRadius: 8,
      displayColors: true,
      usePointStyle: true,
    },
  },
  scales: {
    x: {
      ticks: { color: "#6b7d92", maxTicksLimit: 8, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: "rgba(120,190,235,0.05)" },
      border: { color: "rgba(120,190,235,0.12)" },
    },
    y: {
      title: { display: true, text: "分数（最优 / 验证）", color: "#7dd3fc", font: { family: "'DM Sans'", size: 10, weight: "600" } },
      ticks: { color: "#6b7d92", font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: "rgba(120,190,235,0.05)" },
      border: { color: "rgba(120,190,235,0.12)" },
    },
  },
};

function buildChartDatasets(history) {
  return CHART_SERIES.filter((s) => history?.[s.key]?.length).map((s) => ({
    label: s.label,
    data: history[s.key],
    borderColor: s.borderColor,
    borderWidth: 2,
    tension: 0.35,
    pointRadius: 0,
    pointHoverRadius: 4,
    pointHoverBackgroundColor: s.borderColor,
    pointHoverBorderColor: "#05070d",
    fill: true,
    backgroundColor: (context) => {
      const { ctx, chartArea } = context.chart;
      return makeGradient(ctx, chartArea, s.fillRGB);
    },
    yAxisID: s.yAxisID,
  }));
}

function destroyChart() {
  if (chart) {
    chart.destroy();
    chart = null;
  }
  chartSymbol = null;
}

function createChart(ctx, steps, history) {
  return new Chart(ctx, {
    type: "line",
    data: { labels: steps, datasets: buildChartDatasets(history) },
    options: CHART_OPTIONS,
  });
}

function updateChartInPlace(steps, history) {
  const prevLen = chart.data.labels.length;
  chart.data.labels = steps;

  const next = buildChartDatasets(history);
  for (const ds of next) {
    const existing = chart.data.datasets.find((d) => d.label === ds.label);
    if (existing) {
      existing.data = ds.data;
    } else {
      chart.data.datasets.push(ds);
    }
  }

  const nextLabels = new Set(next.map((d) => d.label));
  chart.data.datasets = chart.data.datasets.filter((d) => nextLabels.has(d.label));

  const grew = steps.length > prevLen;
  chart.update(grew ? "active" : "none");
}

function renderChart(history, label, progress) {
  const ctx = $("mainChart").getContext("2d");
  const steps = history?.step || [];
  if (!steps.length) {
    destroyChart();
    if (progress?.current_step > 0) {
      $("chartHint").textContent = `训练中 第 ${progress.current_step}/${progress.train_steps} 步，曲线每步更新`;
    } else {
      $("chartHint").textContent = "暂无历史数据（首步约需 15–30 秒）";
    }
    return;
  }

  const sameSymbol = chart && chartSymbol === label;
  if (sameSymbol) {
    updateChartInPlace(steps, history);
  } else {
    destroyChart();
    chart = createChart(ctx, steps, history);
    chartSymbol = label;
  }

  $("chartTitle").textContent = `${label} 训练曲线`;
  $("chartHint").textContent = `${steps.length} 个记录点`;
}

async function loadSymbolChart(symbol, timeframe, progress) {
  if (!symbol) return;
  try {
    const query = timeframe ? `?timeframe=${encodeURIComponent(timeframe)}` : "";
    const data = await fetchJSON(`/api/symbols/${encodeURIComponent(symbol)}${query}`);
    const label = timeframe ? `${symbol} ${timeframe}` : symbol;
    renderChart(data.history, label, progress || data);
    $("formulaText").textContent = data.formula_decoded || "—";
  } catch (e) {
    $("formulaText").textContent = "—";
  }
}

function renderStrategies(rows) {
  const tbody = $("strategiesBody");
  if (!rows.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="4">暂无已保存策略</td></tr>';
    return;
  }
  tbody.innerHTML = rows
    .map(
      (r) => `
    <tr>
      <td>${r.symbol}</td>
      <td>${r.timeframe || "—"}</td>
      <td>${formatScore(r.best_score)}</td>
      <td><code>${r.formula_decoded || "—"}</code></td>
    </tr>`
    )
    .join("");
}

function updateTrainingUI(training, progress) {
  const job = training?.job;
  const active = training?.active;
  const pill = $("jobPill");
  const startBtn = $("startBtn");
  const retrainBtn = $("retrainBtn");
  const stopBtn = $("stopBtn");

  if (!job || job.state === "idle") {
    pill.innerHTML = '<i class="pill-dot"></i>空闲';
    pill.className = "pill";
    startBtn.disabled = !selectedDataFile;
    if (retrainBtn) retrainBtn.disabled = !selectedDataFile;
    stopBtn.disabled = true;
    updateHistoryDataSelect(false);
    $("logHint").textContent = "—";
    updateTrainingTimeFields(progress, training);
    return;
  }

  const stateLabel = {
    running: "训练中",
    completed: "已完成",
    failed: "失败",
    stopped: "已停止",
  };
  const label = job.symbol ? `${job.symbol} ${job.timeframe || ""}`.trim() : "训练";
  const stateText = stateLabel[job.state] || job.state;
  pill.innerHTML = `<i class="pill-dot"></i>${stateText} · ${label}`;
  pill.className = "pill " + (job.state === "running" ? "running" : job.state);

  startBtn.disabled = active;
  if (retrainBtn) retrainBtn.disabled = active;
  stopBtn.disabled = !active;
  updateHistoryDataSelect(active);
  $("logHint").textContent = job.log_path || "—";
  updateTrainingTimeFields(progress, training);

  const logView = $("logView");
  const atBottom = isViewAtBottom(logView);
  logView.textContent = (training.log_tail || []).join("\n") || "等待输出…";
  if (atBottom) logView.scrollTop = logView.scrollHeight;
}

async function refreshOverview() {
  let overview = { data_file: null, progress: null };
  let strategies = { strategies: [] };
  let training = { active: false, job: null, log_tail: [] };

  try {
    overview = await fetchJSON("/api/overview", { silent: true });
  } catch (_) {}

  try {
    strategies = await fetchJSON("/api/strategies", { silent: true });
  } catch (_) {}

  try {
    training = await fetchJSON("/api/training/status", { silent: true });
  } catch (_) {}

  if (overview.data_file) renderDataFileCard(overview.data_file);
  updateFileProgress(overview.progress);
  updateExportBtn(overview.progress, strategies.strategies);
  updateTrainingBtns(overview.progress, training);
  updateTrainingUI(training, overview.progress);
  renderStrategies(strategies.strategies);
  renderBacktestStrategyOptions(strategies.strategies);

  const sym = overview.progress?.symbol || selectedSymbol || training?.job?.symbol;
  const timeframe =
    overview.progress?.timeframe ||
    selectedTimeframe ||
    training?.job?.timeframe;
  const trainingActive = !!training?.active;
  if (lastTrainingActive && !trainingActive && sym) {
    await applyBestStrategyForBacktest(sym, null);
  }
  lastTrainingActive = trainingActive;

  if (sym && (training?.active || overview.progress)) {
    await loadSymbolChart(sym, timeframe, overview.progress);
  }

  await refreshDebugLogs();
  refreshAiProviderStatus();
}

async function loadConfig() {
  const health = await fetch(API + "/api/health").then((r) => r.json()).catch(() => ({}));
  if (!health.version) {
    await logClientError(
      "后端版本过旧或未启动新版服务。请关闭旧进程后重新运行: python run_web.py",
      { health }
    );
  }

  const cfg = await fetchJSON("/api/config");
  debugMode = !!cfg.debug_mode;
  $("debugModeCheck").checked = debugMode;
  $("deviceMeta").textContent = `${cfg.train_steps} steps · batch ${cfg.batch_size} · ${cfg.device}`;
  if (cfg.error_log) {
    $("debugLogPaths").textContent = `本地: ${cfg.error_log}`;
  }
  if (cfg.data_file) renderDataFileCard(cfg.data_file);
  await refreshDataFileHistory(cfg.data_file?.data_file || cfg.last_data_file);
  if (cfg.strategy_file) renderStrategyFileCard(cfg.strategy_file);
  applyBacktestCostDefaults(cfg);
  await initAiPanel(cfg);
}

function applyBacktestCostDefaults(cfg) {
  const cIn = $("btCommissionInput");
  const sIn = $("btSlippageInput");
  if (cIn && cfg.bt_commission_pct != null) cIn.value = Number(cfg.bt_commission_pct);
  if (sIn && cfg.bt_slippage_pct != null) sIn.value = Number(cfg.bt_slippage_pct);
  updateBtCostHint();
}

function readBacktestCosts() {
  const cRaw = Number($("btCommissionInput")?.value);
  const sRaw = Number($("btSlippageInput")?.value);
  const commission = Number.isFinite(cRaw) && cRaw >= 0 ? cRaw : 0.02;
  const slippage = Number.isFinite(sRaw) && sRaw >= 0 ? sRaw : 0.01;
  return { commission_pct: commission, slippage_pct: slippage };
}

function updateBtCostHint() {
  const hint = $("btCostSumHint");
  if (!hint) return;
  const { commission_pct, slippage_pct } = readBacktestCosts();
  const fee = Number((commission_pct + slippage_pct).toFixed(4));
  hint.textContent = `单边成本 ${fee}%`;
}

async function refreshAiProviderStatus() {
  try {
    const status = await fetchJSON("/api/ai/providers", { silent: true });
    window.__aiProviderStatus = status;
  } catch (_) {
    /* keep previous snapshot */
  }
  updateAiChannelHint();
}

async function initAiPanel(cfg) {
  const keyInput = $("aiApiKeyInput");
  if (!keyInput) return;

  if (cfg?.ai_api_key) keyInput.value = cfg.ai_api_key;
  else if (cfg?.ai_provider === "openclaw" || cfg?.ai_provider === "openclaw_wb") {
    keyInput.value = cfg.ai_provider;
  }

  await refreshAiProviderStatus();
  if (!keyInput.dataset.aiStatusBound) {
    keyInput.dataset.aiStatusBound = "1";
    keyInput.addEventListener("input", () => {
      updateAiChannelHint();
      refreshAiProviderStatus();
    });
  }
}

function resolveAiFromKey(raw) {
  const v = (raw || "").trim().toLowerCase();
  // openclaw_wb 必须先于 openclaw，避免前缀误匹配
  if (v === "openclaw_wb" || v.startsWith("openclaw_wb/")) {
    return { provider: "openclaw_wb", apiKey: raw.trim(), isAlias: true };
  }
  if (v === "openclaw" || v.startsWith("openclaw/")) {
    return { provider: "openclaw", apiKey: raw.trim(), isAlias: true };
  }
  return { provider: "deepseek", apiKey: (raw || "").trim(), isAlias: false };
}

function updateAiChannelHint() {
  const hint = $("aiChannelHint");
  const headHint = $("aiProviderHint");
  const keyInput = $("aiApiKeyInput");
  if (!hint || !keyInput) return;

  const resolved = resolveAiFromKey(keyInput.value);
  const status = window.__aiProviderStatus;
  const row = (status?.providers || []).find((p) => p.id === resolved.provider);

  if (resolved.provider === "deepseek") {
    if (headHint) headHint.textContent = "DeepSeek · deepseek-v4-flash";
    hint.textContent = "当前：DeepSeek（deepseek-v4-flash · https://api.deepseek.com）。";
  } else if (resolved.provider === "openclaw") {
    if (headHint) {
      headHint.textContent = row?.available ? "openclaw (QClaw) · 已匹配" : "openclaw (QClaw) · 未就绪";
    }
    hint.textContent = row?.hint || "已匹配 openclaw：将自动使用本地 QClaw token。";
  } else {
    if (headHint) headHint.textContent = row?.available ? "openclaw_wb · 已匹配" : "openclaw_wb · 未就绪";
    hint.textContent = row?.hint || "已匹配 openclaw_wb：将自动使用 WorkBuddy token。";
  }
}

function openUnlimitedModal() {
  const modal = $("aiUnlimitedModal");
  if (modal) modal.hidden = false;
}

function closeUnlimitedModal() {
  const modal = $("aiUnlimitedModal");
  if (modal) modal.hidden = true;
}

async function runAiAnalyze() {
  const btn = $("aiAnalyzeBtn");
  const view = $("aiAnswerView");
  const rawKey = $("aiApiKeyInput")?.value || "";
  const resolved = resolveAiFromKey(rawKey);
  if (!view) return;

  if (resolved.provider === "deepseek" && !resolved.apiKey) {
    view.className = "ai-answer error";
    view.textContent = "请填写 DeepSeek API Key";
    return;
  }

  await refreshAiProviderStatus();

  if (btn) btn.disabled = true;
  view.className = "ai-answer loading";
  view.textContent = `正在通过 ${resolved.provider} 连接并流式分析…`;

  let header = "";
  let answer = "";

  try {
    const res = await fetch(API + "/api/ai/analyze-training", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: resolved.provider,
        api_key: resolved.apiKey,
        symbol: selectedSymbol || null,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(formatApiError(data, res.status, "/api/ai/analyze-training"));
    }
    if (!res.body) throw new Error("浏览器不支持流式响应");

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    view.className = "ai-answer streaming";
    view.textContent = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const block of chunks) {
        const line = block
          .split("\n")
          .map((l) => l.trim())
          .find((l) => l.startsWith("data:"));
        if (!line) continue;
        let event;
        try {
          event = JSON.parse(line.slice(5).trim());
        } catch (_) {
          continue;
        }
        if (event.type === "meta") {
          header =
            `[${event.label || event.provider || resolved.provider} · ${event.model || ""} · ${event.symbol || ""}${event.timeframe ? " " + event.timeframe : ""}]` +
            (event.prior_count
              ? ` · 已带入前 ${event.prior_count} 次同品种同周期分析`
              : " · 首次分析") +
            `\n\n`;
          view.textContent = header;
          view.scrollTop = view.scrollHeight;
        } else if (event.type === "delta") {
          answer += event.text || "";
          view.textContent = header + answer;
          view.scrollTop = view.scrollHeight;
        } else if (event.type === "error") {
          throw new Error(event.message || "分析失败");
        } else if (event.type === "done") {
          answer = event.answer || answer;
          view.className = "ai-answer";
          view.textContent = header + (answer || "（无内容）");
        }
      }
    }
    if (!answer && view.className.includes("streaming")) {
      throw new Error("流式分析中断，未收到完整回复");
    }
    view.className = "ai-answer";
  } catch (e) {
    view.className = "ai-answer error";
    view.textContent = `分析失败: ${e.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function browseStrategyFile() {
  strategyUploadTarget = "backtest";
  $("strategyFileInput")?.click();
}

async function handleStrategyFileUpload(event) {
  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;
  try {
    const body = new FormData();
    body.append("file", file);
    const res = await fetchJSON("/api/strategy-file/upload", {
      method: "POST",
      body,
    });
    if (strategyUploadTarget === "realtime") {
      const name = res.filename || res.strategy_file;
      rtImportedStrategy = {
        path: res.strategy_file,
        name,
        symbol: (res.symbol || "").trim() || rtParseSymbolFromFilename(name),
      };
      await loadRtStrategies();
      rtApplySymbolFromStrategy(rtImportedStrategy.symbol);
    } else {
      renderStrategyFileCard(res);
    }
  } catch (e) {
    $("debugView")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } finally {
    input.value = "";
  }
}

async function applyBestStrategyForBacktest(symbol, strategyFile) {
  if (strategyFile) {
    renderStrategyFileCard(strategyFile);
    return;
  }
  if (!symbol) return;
  try {
    const res = await fetchJSON(
      `/api/strategy-file/sync-best?symbol=${encodeURIComponent(symbol)}` +
        (selectedTimeframe ? `&timeframe=${encodeURIComponent(selectedTimeframe)}` : ""),
      { method: "POST" }
    );
    renderStrategyFileCard(res);
  } catch (_) {
    await loadBacktestStrategyContext();
  }
}

async function loadBacktestStrategyContext() {
  const sym = selectedStrategySymbol || selectedSymbol;
  if (sym) {
    await applyBestStrategyForBacktest(sym, null);
    return;
  }
  try {
    const cfg = await fetchJSON("/api/config");
    if (cfg.strategy_file) renderStrategyFileCard(cfg.strategy_file);
  } catch (_) {
    /* ignore */
  }
}

function browseDataFile() {
  $("dataFileInput")?.click();
}

async function handleDataFileUpload(event) {
  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;
  try {
    const body = new FormData();
    body.append("file", file);
    const res = await fetchJSON("/api/data-file/upload", {
      method: "POST",
      body,
    });
    renderDataFileCard(res);
    selectedSymbol = res.symbol;
    selectedTimeframe = res.timeframe || null;
    await refreshDataFileHistory(res.data_file);
    await loadSymbolChart(res.symbol, res.timeframe);
  } catch (e) {
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } finally {
    input.value = "";
  }
}

async function startTraining() {
  if (!selectedDataFile) {
    await logClientError("请先选择数据文件");
    return;
  }
  try {
    const res = await fetchJSON("/api/training/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_file: selectedDataFile, from_scratch: false }),
    });
    selectedSymbol = res.data_file?.symbol || res.job?.symbol;
    selectedTimeframe = res.data_file?.timeframe || res.job?.timeframe || null;
    renderDataFileCard(res.data_file);
    await refreshOverview();
  } catch (e) {
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

async function retrainFromScratch() {
  if (!selectedDataFile) {
    await logClientError("请先选择数据文件");
    return;
  }
  const ok = window.confirm(
    "重新训练会清除该品种、该周期的检查点，从第 0 步重新搜索。\n" +
      "已有的更优策略会保留，只有挖到更高分才会覆盖。\n\n" +
      "确定要重新训练吗？"
  );
  if (!ok) return;
  try {
    const res = await fetchJSON("/api/training/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_file: selectedDataFile, from_scratch: true }),
    });
    selectedSymbol = res.data_file?.symbol || res.job?.symbol;
    selectedTimeframe = res.data_file?.timeframe || res.job?.timeframe || null;
    renderDataFileCard(res.data_file);
    await refreshOverview();
  } catch (e) {
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function updateExportBtn(progress, strategies) {
  const sym = progress?.symbol || selectedSymbol;
  const timeframe = progress?.timeframe || selectedTimeframe;
  const hasStrategy =
    progress?.has_strategy ||
    (strategies || []).some(
      (s) =>
        s.symbol === sym &&
        (!timeframe || String(s.timeframe || "").toUpperCase() === String(timeframe).toUpperCase())
    );
  const btn = $("exportBtn");
  if (btn) btn.disabled = !sym || !hasStrategy;
}

function updateTrainingBtns(progress, training) {
  const sym = progress?.symbol || selectedSymbol;
  const active = training?.active;
  const hasCheckpoint = Boolean(progress?.has_checkpoint);
  const exportBtn = $("exportTrainingBtn");
  const importBtn = $("importTrainingBtn");

  let exportTitle = "打包 checkpoint、训练曲线与策略为 zip";
  if (!sym) {
    exportTitle = "请先选择数据文件";
  } else if (active) {
    exportTitle = "训练进行中，请停止后再导出";
  } else if (!hasCheckpoint) {
    exportTitle = "该品种、该周期尚无检查点：至少训练满 20 步后才会生成（每 20 步保存一次）";
  }

  if (exportBtn) {
    exportBtn.disabled = !sym || !hasCheckpoint || !!active;
    exportBtn.title = exportTitle;
  }
  if (importBtn) {
    importBtn.disabled = !sym || !!active;
    importBtn.title = active ? "训练进行中，请停止后再导入" : "上传 .zip 或 .pt，下次训练断点续训";
  }
}

async function exportTraining() {
  const sym = selectedSymbol;
  if (!sym) {
    await logClientError("请先选择数据文件");
    return;
  }
  const query = selectedTimeframe
    ? `?timeframe=${encodeURIComponent(selectedTimeframe)}`
    : "";
  const path = `/api/training/${encodeURIComponent(sym)}/export${query}`;
  try {
    const res = await fetch(API + path);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(formatApiError(data, res.status, path));
    }
    const blob = await res.blob();
    const disp = res.headers.get("Content-Disposition") || "";
    const m = /filename="([^"]+)"/.exec(disp);
    const identity = selectedTimeframe ? `${sym}_${selectedTimeframe}` : sym;
    const filename = m ? m[1] : `training_${identity.replace(/\./g, "_")}.zip`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    await logClientError(`导出训练失败: ${e.message}`);
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function triggerImportTraining() {
  const input = $("importTrainingFile");
  if (input) {
    input.value = "";
    input.click();
  }
}

async function handleImportTrainingFile(event) {
  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;

  const sym = selectedSymbol;
  if (!sym) {
    await logClientError("请先选择数据文件");
    return;
  }

  const form = new FormData();
  form.append("file", file);

  try {
    const query =
      `symbol=${encodeURIComponent(sym)}` +
      (selectedTimeframe ? `&timeframe=${encodeURIComponent(selectedTimeframe)}` : "");
    const res = await fetch(`${API}/api/training/import?${query}`, {
      method: "POST",
      body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(formatApiError(data, res.status, "/api/training/import"));
    }
    if (data.symbol && data.symbol !== sym) {
      selectedSymbol = data.symbol;
    }
    if (data.timeframe) selectedTimeframe = data.timeframe;
    clientErrors.push(`[${new Date().toLocaleString()}] ${data.message || "训练文件导入成功"}`);
    if (clientErrors.length > 80) clientErrors = clientErrors.slice(-80);
    renderDebugView();
    await refreshOverview();
  } catch (e) {
    await logClientError(`导入训练失败: ${e.message}`);
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } finally {
    input.value = "";
  }
}

function parseContentDispositionFilename(header) {
  if (!header) return null;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8) return decodeURIComponent(utf8[1]);
  const plain = /filename="([^"]+)"/i.exec(header) || /filename=([^;]+)/i.exec(header);
  return plain ? plain[1].trim() : null;
}

async function exportStrategy() {
  const sym = selectedSymbol;
  if (!sym) {
    await logClientError("请先选择数据文件");
    return;
  }
  const query = selectedTimeframe
    ? `?timeframe=${encodeURIComponent(selectedTimeframe)}`
    : "";
  const path = `/api/strategies/${encodeURIComponent(sym)}/export${query}`;
  try {
    const res = await fetch(API + path);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(formatApiError(data, res.status, path));
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      parseContentDispositionFilename(res.headers.get("Content-Disposition")) ||
      `strategy_${(
        selectedTimeframe ? `${sym}_${selectedTimeframe}` : sym
      ).replace(/\./g, "_")}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    await logClientError(`导出策略失败: ${e.message}`);
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

async function stopTraining() {
  try {
    const res = await fetchJSON("/api/training/stop", { method: "POST" });
    await refreshOverview();
    const sym = res.training?.job?.symbol || selectedSymbol;
    await applyBestStrategyForBacktest(sym, res.strategy_file);
  } catch (e) {
    $("debugView").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

// ═══════════════════════════════════════════════════════════════════
// 分页切换
// ═══════════════════════════════════════════════════════════════════
function switchPage(page) {
  if (page !== "train" && page !== "backtest" && page !== "realtime") return;
  currentPage = page;
  document.querySelectorAll(".stepper .step").forEach((s) => {
    s.classList.toggle("active", s.dataset.page === page);
  });
  document.querySelectorAll(".page").forEach((p) => {
    p.classList.toggle("active", p.id === `page-${page}`);
  });
  if (page === "backtest") {
    loadBacktestStrategyContext();
    refreshBacktest();
  } else if (page === "realtime") {
    initRealtimeOnce();
    refreshRealtime();
  }
}

// ═══════════════════════════════════════════════════════════════════
// 回测：格式化辅助
// ═══════════════════════════════════════════════════════════════════
function fmtPct(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
}
function fmtSigned(v, digits = 3) {
  if (v == null || Number.isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + Number(v).toFixed(digits);
}

function formatBacktestRunLabel(run) {
  const started = run.started_at ? new Date(run.started_at) : null;
  const when = started && !Number.isNaN(started.getTime())
    ? started.toLocaleString("zh-CN", { hour12: false })
    : "时间未知";
  const state = BT_STATE_LABEL[run.state] || run.state || "未知";
  return `${run.symbol || "—"} · ${run.timeframe || "—"} · ${run.strategy_name || "策略"} · ${when} · ${state}`;
}

async function refreshBacktestRuns(preferredRunId = selectedBacktestRunId) {
  const select = $("btRunSelect");
  if (!select) return;
  let rows;
  try {
    const response = await fetchJSON("/api/backtest/runs", { silent: true });
    rows = response.runs || [];
  } catch (_) {
    return;
  }
  backtestRuns = rows;
  const nextRunId = rows.some(
    (row) => row.run_id === preferredRunId && (row.available || row.state === "running")
  )
    ? preferredRunId
    : (rows.find((row) => row.available)?.run_id || rows[0]?.run_id || null);
  const signature = rows
    .map((row) => [row.run_id, row.state, row.available, row.finished_at].join("|"))
    .join(";");
  if (signature !== btRunsSig) {
    btRunsSig = signature;
    select.replaceChildren();
    if (!rows.length) {
      select.appendChild(new Option("暂无历史回测", ""));
    } else {
      for (const row of rows) {
        const option = document.createElement("option");
        option.value = row.run_id;
        option.textContent = formatBacktestRunLabel(row);
        option.title = row.output_dir || row.run_id;
        option.disabled = !row.available && row.state !== "running";
        select.appendChild(option);
      }
    }
  }
  const changed = selectedBacktestRunId !== nextRunId;
  selectedBacktestRunId = nextRunId;
  select.value = nextRunId || "";
  select.disabled = !rows.length;
  if (changed) {
    btBuster = nextRunId || "";
    btPortfolioSig = "";
  }
}

async function selectBacktestRun(event) {
  selectedBacktestRunId = event.target.value || null;
  btBuster = selectedBacktestRunId || "";
  btPortfolioSig = "";
  lastEquityData = null;
  await refreshBacktestReport();
}

function backtestResultUrl(path) {
  const params = new URLSearchParams();
  if (selectedBacktestRunId) {
    params.set("run_id", selectedBacktestRunId);
  } else {
    const sym = selectedStrategySymbol || selectedSymbol;
    if (sym) params.set("symbol", sym);
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

// ═══════════════════════════════════════════════════════════════════
// 回测：状态轮询 + UI 更新
// ═══════════════════════════════════════════════════════════════════
async function refreshBacktest() {
  let st;
  try {
    st = await fetchJSON("/api/backtest/status", { silent: true });
  } catch (_) {
    return;
  }
  btActive = !!st.active;
  const job = st.job;
  const state = job?.state || "idle";
  await refreshBacktestRuns(selectedBacktestRunId || job?.run_id || null);

  // 按钮
  const stopBtn = $("btStopBtn");
  updateBtStartBtn();
  if (stopBtn) stopBtn.disabled = !btActive;

  // 缓存刷新键：用最近一次任务的结束/开始时间
  btBuster = job?.finished_at || job?.started_at || btBuster;

  // 日志
  const logView = $("btLogView");
  const logText = (st.log_tail || []).join("\n") || "等待任务…";
  if (logView) {
    const atBottom = isViewAtBottom(logView);
    logView.textContent = logText;
    if (atBottom) logView.scrollTop = logView.scrollHeight;
  }
  if ($("btLogHint")) $("btLogHint").textContent = job?.log_path || "—";

  // 阶段进度条
  updateBacktestPhase(st, state);

  if (state === "failed") {
    const alertKey = `${job?.log_path || ""}|${job?.finished_at || ""}|${job?.exit_code ?? ""}`;
    if (alertKey && alertKey !== btLastAlertKey) {
      btLastAlertKey = alertKey;
      const errLine = job?.error ? `\n错误: ${job.error}` : "";
      showErrorPopup(
        "回测失败",
        `退出码: ${job?.exit_code ?? "?"}${errLine}\n日志: ${job?.log_path || "—"}\n\n${logText}`
      );
    }
  }

  // 结果报告（非运行态时刷新，运行态保留上次结果）
  if (!btActive) {
    await refreshBacktestReport();
  }
}

const BT_STATE_LABEL = {
  running: "回测中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  idle: "待机",
};

function updateBacktestPhase(st, state) {
  const fill = $("btPhaseFill");
  const label = $("btPhaseLabel");
  if (!fill || !label) return;

  const total = st.phase_total || 7;
  const idx = st.phase_index || 0;

  let pct;
  if (btActive) {
    pct = Math.min(96, Math.round(((idx + 1) / total) * 100));
    label.textContent = `${st.phase_label || "回测中"}…`;
    fill.classList.add("animate");
  } else if (state === "completed") {
    pct = 100;
    label.textContent = "完成";
    fill.classList.remove("animate");
  } else if (state === "failed" || state === "stopped") {
    pct = Math.min(96, Math.round(((idx + 1) / total) * 100));
    label.textContent = BT_STATE_LABEL[state];
    fill.classList.remove("animate");
  } else {
    pct = 0;
    label.textContent = "待机";
    fill.classList.remove("animate");
  }
  fill.style.width = pct + "%";
}

async function refreshBacktestReport() {
  let data;
  const url = backtestResultUrl("/api/backtest/report");
  try {
    data = await fetchJSON(url, { silent: true });
  } catch (_) {
    return;
  }
  if (!data.available || !data.report) {
    if ($("btPortfolioHint")) $("btPortfolioHint").textContent = "尚未运行回测";
    lastEquityData = null;
    btPortfolioSig = "";
    renderEquity(null);
    renderKellyBacktestTable({});
    return;
  }
  // 先取资金曲线（写入 lastEquityData），再渲染绩效卡，让 sparkline 用上真实数据
  await refreshEquityCurve();
  renderPortfolio(data.report);
  renderBacktestTable(data.report.symbols || {});
  renderKellyBacktestTable(data.report.symbols || {});
}

async function refreshEquityCurve() {
  const url = backtestResultUrl("/api/backtest/equity");
  try {
    const data = await fetchJSON(url, { silent: true });
    lastEquityData = data?.available ? data.data : null;
    renderEquity(data);
  } catch (_) {
    lastEquityData = null;
    renderEquity(null);
  }
}

// ═══════════════════════════════════════════════════════════════════
// 迷你 sparkline + 数字滚动动画（终端仪表盘质感）
// ═══════════════════════════════════════════════════════════════════
const METRIC_FMT = {
  pct: (v) => (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "%",
  signed: (v) => (v >= 0 ? "+" : "") + v.toFixed(3),
  ratio: (v) => v.toFixed(3),
  int: (v) => Math.round(v).toLocaleString(),
  winrate: (v) => (v * 100).toFixed(1) + "%",
  strength: (v) => Math.round(v * 100) + "%",
};

function prefersReducedMotion() {
  return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

// 短促 count-up（≈420ms, easeOutCubic），克制不浮夸
function animateCount(el, to, fmt) {
  const fn = METRIC_FMT[fmt] || ((v) => String(v));
  if (!Number.isFinite(to)) {
    el.textContent = "—";
    return;
  }
  if (prefersReducedMotion()) {
    el.textContent = fn(to);
    return;
  }
  const dur = 420;
  const t0 = performance.now();
  function frame(now) {
    const p = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3); // easeOutCubic
    el.textContent = fn(to * e);
    if (p < 1) requestAnimationFrame(frame);
    else el.textContent = fn(to);
  }
  requestAnimationFrame(frame);
}

function runCountUp(root) {
  if (!root) return;
  root.querySelectorAll("[data-count]").forEach((el) => {
    animateCount(el, parseFloat(el.dataset.count), el.dataset.fmt || "");
  });
}

// 均匀降采样为 <= target 个有限点
function downsampleSeries(arr, target) {
  const clean = (arr || [])
    .map(Number)
    .filter((v) => Number.isFinite(v));
  if (clean.length <= target) return clean;
  const out = [];
  const step = (clean.length - 1) / (target - 1);
  for (let i = 0; i < target; i++) out.push(clean[Math.round(i * step)]);
  return out;
}

// 生成极小趋势微线（内联 SVG，轻量、清晰）
function sparklineSVG(values, { color = "#5eead4", fillRGB = null, w = 74, h = 22 } = {}) {
  const v = downsampleSeries(values, 56);
  if (v.length < 2) return "";
  const min = Math.min(...v);
  const max = Math.max(...v);
  const range = max - min || 1;
  const n = v.length;
  const x = (i) => (i / (n - 1)) * w;
  const y = (val) => h - 2 - ((val - min) / range) * (h - 4);
  const line = "M" + v.map((val, i) => `${x(i).toFixed(1)} ${y(val).toFixed(1)}`).join(" L ");
  const area = fillRGB
    ? `<path d="${line} L ${w} ${h} L 0 ${h} Z" fill="rgba(${fillRGB},0.14)" stroke="none"/>`
    : "";
  const dot = `<circle cx="${x(n - 1).toFixed(1)}" cy="${y(v[n - 1]).toFixed(1)}" r="1.6" fill="${color}"/>`;
  return `<svg class="spark-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${area}<path d="${line}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>${dot}</svg>`;
}

// 取当前主资金曲线序列（组合优先，否则第一个品种）
function mainEquitySeries() {
  const d = lastEquityData;
  if (!d) return null;
  if (d.portfolio) return d.portfolio;
  const syms = d.symbols || {};
  const names = Object.keys(syms);
  return names.length ? syms[names[0]] : null;
}

function renderPortfolio(report) {
  const grid = $("btPortfolioGrid");
  if (!grid) return;
  const p = report.portfolio || {};
  const focus = report.focus_symbol || Object.keys(report.symbols || {})[0] || "";
  const symData = focus ? (report.symbols || {})[focus] : null;

  if (!Object.keys(p).length) {
    grid.innerHTML = '<div class="metric-empty">回测结果无绩效数据</div>';
    btPortfolioSig = "";
    return;
  }

  const plNum = Number(symData?.profit_loss_ratio ?? p.profit_loss_ratio);
  const nTrades = symData?.n_trades ?? p.n_trades;
  const winRate = symData?.win_rate;

  // sparkline 数据源：主资金曲线 + 滚动夏普
  const eq = mainEquitySeries();
  const posColor = p.total_return >= 0 ? "#4ade80" : "#f87171";
  const posRGB = p.total_return >= 0 ? "74, 222, 128" : "248, 113, 113";
  const equitySpark = eq ? sparklineSVG(eq.equity, { color: posColor, fillRGB: posRGB }) : "";
  const rollSpark = eq ? sparklineSVG(eq.rolling_sharpe, { color: "#5eead4", fillRGB: "94, 234, 212" }) : "";

  const cards = [
    { label: "总收益", raw: p.total_return, fmt: "pct", cls: p.total_return >= 0 ? "pos" : "neg", spark: equitySpark },
    { label: "Sharpe", raw: p.sharpe, fmt: "signed", cls: "accent", spark: rollSpark },
    { label: "Sortino", raw: p.sortino, fmt: "signed", cls: "accent", spark: rollSpark },
    { label: "盈亏比", raw: Number.isFinite(plNum) ? plNum : null, fmt: "ratio", cls: Number.isFinite(plNum) ? "accent" : "" },
    { label: "交易数", raw: Number.isFinite(Number(nTrades)) ? Number(nTrades) : null, fmt: "int", cls: "" },
    { label: "胜率", raw: winRate != null ? Number(winRate) : null, fmt: "winrate", cls: "" },
  ];

  // 签名守卫：数值/焦点/资金曲线未变则不重建，避免每次轮询重播动画
  const sig = [focus, btEquitySig, ...cards.map((c) => c.raw)].join("|");
  if (sig === btPortfolioSig) {
    if ($("btPortfolioHint")) $("btPortfolioHint").textContent = focus ? `${focus} 回测绩效` : "回测绩效";
    return;
  }
  btPortfolioSig = sig;

  grid.innerHTML = cards
    .map((c) => {
      const cardCls = c.cls === "pos" || c.cls === "neg" ? c.cls : "";
      const finite = c.raw != null && Number.isFinite(c.raw);
      const finalText = finite ? METRIC_FMT[c.fmt](c.raw) : "—";
      const countAttr = finite ? ` data-count="${c.raw}" data-fmt="${c.fmt}"` : "";
      const spark = c.spark ? `<div class="metric-spark">${c.spark}</div>` : "";
      return `
    <div class="metric-card ${cardCls}">
      <div class="metric-label">${c.label}</div>
      <div class="metric-value ${c.cls}"${countAttr}>${finalText}</div>
      ${spark}
    </div>`;
    })
    .join("");

  runCountUp(grid);

  if ($("btPortfolioHint")) {
    $("btPortfolioHint").textContent = focus ? `${focus} 回测绩效` : "回测绩效";
  }
}

function renderBacktestTable(symbols) {
  const tbody = $("btTableBody");
  if (!tbody) return;
  const rows = Object.entries(symbols);
  if (!rows.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">暂无回测结果</td></tr>';
    if ($("btTableHint")) $("btTableHint").textContent = "—";
    return;
  }
  if ($("btTableHint")) $("btTableHint").textContent = rows.length === 1 ? rows[0][0] : `${rows.length} 个品种`;
  tbody.innerHTML = rows
    .map(([sym, d]) => {
      const retCls = (d.total_return || 0) >= 0 ? "pos" : "neg";
      const shCls = (d.sharpe || 0) >= 0 ? "pos" : "neg";
      return `
      <tr>
        <td class="sym-cell">${sym}</td>
        <td class="${retCls}">${fmtPct(d.total_return)}</td>
        <td class="${shCls}">${fmtSigned(d.sharpe)}</td>
        <td>${fmtSigned(d.sortino)}</td>
        <td>${Number.isFinite(Number(d.profit_loss_ratio)) ? Number(d.profit_loss_ratio).toFixed(3) : "—"}</td>
        <td>${d.n_trades ?? "—"}</td>
        <td>${d.win_rate != null ? (d.win_rate * 100).toFixed(1) + "%" : "—"}</td>
      </tr>`;
    })
    .join("");
}

function renderKellyBacktestTable(symbols) {
  const tbody = $("btKellyTableBody");
  if (!tbody) return;
  const rows = Object.entries(symbols).filter(([, d]) => d?.kelly);
  if (!rows.length) {
    tbody.innerHTML =
      '<tr class="empty-row"><td colspan="8">运行新版回测后显示凯利仓位对照结果</td></tr>';
    if ($("btKellyHint")) $("btKellyHint").textContent = "等待回测";
    return;
  }
  if ($("btKellyHint")) {
    $("btKellyHint").textContent =
      rows.length === 1 ? `${rows[0][0]} · 完整凯利` : `${rows.length} 个品种 · 完整凯利`;
  }
  tbody.innerHTML = rows
    .map(([sym, d]) => {
      const k = d.kelly || {};
      const retCls = (k.total_return || 0) >= 0 ? "pos" : "neg";
      const shCls = (k.sharpe || 0) >= 0 ? "pos" : "neg";
      const fraction = Number(k.fraction);
      return `
      <tr>
        <td class="sym-cell">${escHtml(sym)}</td>
        <td>${Number.isFinite(fraction) ? (fraction * 100).toFixed(1) + "%" : "—"}</td>
        <td class="${retCls}">${fmtPct(k.total_return)}</td>
        <td class="${shCls}">${fmtSigned(k.sharpe)}</td>
        <td>${fmtSigned(k.sortino)}</td>
        <td>${Number.isFinite(Number(k.profit_loss_ratio)) ? Number(k.profit_loss_ratio).toFixed(3) : "—"}</td>
        <td>${k.n_trades ?? "—"}</td>
        <td>${k.win_rate != null ? (k.win_rate * 100).toFixed(1) + "%" : "—"}</td>
      </tr>`;
    })
    .join("");
}

// ═══════════════════════════════════════════════════════════════════
// 交互式资金曲线（HTML / Chart.js）
// ═══════════════════════════════════════════════════════════════════
let equityChart = null;
let rollingChart = null;
let btEquitySig = "";

const EQUITY_COLORS = [
  { hex: "#5eead4", rgb: "94, 234, 212" },
  { hex: "#38bdf8", rgb: "56, 189, 248" },
  { hex: "#818cf8", rgb: "129, 140, 248" },
  { hex: "#fbbf24", rgb: "251, 191, 36" },
  { hex: "#f472b6", rgb: "244, 114, 182" },
  { hex: "#a3e635", rgb: "163, 230, 53" },
];

function verticalGradient(chart, rgb, topAlpha, bottomAlpha) {
  const { ctx, chartArea } = chart;
  if (!chartArea) return `rgba(${rgb}, ${topAlpha})`;
  const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
  g.addColorStop(0, `rgba(${rgb}, ${topAlpha})`);
  g.addColorStop(0.62, `rgba(${rgb}, ${(topAlpha + bottomAlpha) / 4})`);
  g.addColorStop(1, `rgba(${rgb}, ${bottomAlpha})`);
  return g;
}

const EQUITY_TOOLTIP = {
  backgroundColor: "rgba(8, 12, 20, 0.94)",
  borderColor: "rgba(94, 234, 212, 0.35)",
  borderWidth: 1,
  titleColor: "#e8edf4",
  bodyColor: "#a9bccf",
  titleFont: { family: "'JetBrains Mono'", size: 11 },
  bodyFont: { family: "'JetBrains Mono'", size: 11 },
  padding: 10,
  cornerRadius: 8,
  usePointStyle: true,
};

const EQUITY_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: "index", intersect: false },
  animation: { duration: 500, easing: "easeOutQuart" },
  plugins: {
    legend: {
      display: true,
      labels: {
        color: "#a9bccf",
        usePointStyle: true,
        pointStyle: "circle",
        boxWidth: 8,
        boxHeight: 8,
        padding: 14,
        font: { family: "'DM Sans'", size: 12, weight: "600" },
      },
    },
    tooltip: {
      ...EQUITY_TOOLTIP,
      callbacks: {
        label: (c) => ` ${c.dataset.label}: ${Number(c.parsed.y).toFixed(4)}`,
      },
    },
  },
  scales: {
    x: {
      ticks: { color: "#6b7d92", maxTicksLimit: 8, maxRotation: 0, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: "rgba(120,190,235,0.05)" },
      border: { color: "rgba(120,190,235,0.12)" },
    },
    y: {
      ticks: { color: "#6b7d92", font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: "rgba(120,190,235,0.05)" },
      border: { color: "rgba(120,190,235,0.12)" },
    },
  },
};

const ROLLING_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: "index", intersect: false },
  animation: { duration: 500, easing: "easeOutQuart" },
  spanGaps: false,
  plugins: {
    legend: { display: false },
    tooltip: {
      ...EQUITY_TOOLTIP,
      borderColor: "rgba(251, 191, 36, 0.4)",
      callbacks: {
        label: (c) => {
          const v = c.parsed.y;
          if (v == null || Number.isNaN(v)) return " 滚动夏普: —";
          return ` 滚动夏普: ${Number(v).toFixed(3)}`;
        },
      },
    },
  },
  scales: {
    x: {
      ticks: { color: "#6b7d92", maxTicksLimit: 8, maxRotation: 0, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: "rgba(120,190,235,0.05)" },
      border: { color: "rgba(120,190,235,0.12)" },
    },
    y: {
      ticks: { color: "#6b7d92", font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: "rgba(251,191,36,0.06)" },
      border: { color: "rgba(251,191,36,0.18)" },
    },
  },
};

function destroyEquityCharts() {
  if (equityChart) { equityChart.destroy(); equityChart = null; }
  if (rollingChart) { rollingChart.destroy(); rollingChart = null; }
}

function renderEquityStats(name, series) {
  const el = $("btEquityStats");
  if (!el) return;
  const pl = series.profit_loss_ratio;
  const plText = Number.isFinite(Number(pl)) ? Number(pl).toFixed(3) : "—";
  const roll = series.rolling_sharpe || [];
  let lastRoll = null;
  for (let i = roll.length - 1; i >= 0; i--) {
    const v = Number(roll[i]);
    if (Number.isFinite(v)) {
      lastRoll = v;
      break;
    }
  }
  const plNum = Number(pl);
  const cards = [
    { label: "总收益", raw: series.total_return, fmt: "pct", cls: series.total_return >= 0 ? "pos" : "neg" },
    { label: "夏普", raw: series.sharpe, fmt: "signed", cls: "accent" },
    { label: "索提诺", raw: series.sortino, fmt: "signed", cls: "accent" },
    { label: "盈亏比", raw: Number.isFinite(plNum) ? plNum : null, fmt: "ratio", cls: "accent" },
    {
      label: "最新滚动夏普",
      raw: lastRoll,
      fmt: "signed",
      cls: lastRoll == null ? "" : lastRoll >= 0 ? "accent" : "neg",
    },
  ];
  el.innerHTML =
    `<span class="equity-stat-name">${name}</span>` +
    cards
      .map((c) => {
        const finite = c.raw != null && Number.isFinite(c.raw);
        const finalText = finite ? METRIC_FMT[c.fmt](c.raw) : "—";
        const countAttr = finite ? ` data-count="${c.raw}" data-fmt="${c.fmt}"` : "";
        return `
      <div class="equity-stat">
        <span class="equity-stat-label">${c.label}</span>
        <span class="equity-stat-value ${c.cls}"${countAttr}>${finalText}</span>
      </div>`;
      })
      .join("");
  runCountUp(el);
}

function buildEquityChart(labels, symbols, portfolio) {
  const canvas = $("btEquityChart");
  if (!canvas) return;
  const symNames = Object.keys(symbols);
  const multi = symNames.length > 1;
  const datasets = symNames.map((s, i) => {
    const col = EQUITY_COLORS[i % EQUITY_COLORS.length];
    return {
      label: s,
      data: symbols[s].equity,
      borderColor: col.hex,
      borderWidth: multi ? 1.5 : 2.2,
      tension: 0.25,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHoverBackgroundColor: col.hex,
      pointHoverBorderColor: "#05070d",
      fill: !multi,
      backgroundColor: (ctx) => verticalGradient(ctx.chart, col.rgb, 0.3, 0),
    };
  });
  symNames.forEach((s, i) => {
    const kellyEquity = symbols[s].kelly_equity || [];
    if (!kellyEquity.length) return;
    const col = EQUITY_COLORS[i % EQUITY_COLORS.length];
    datasets.push({
      label: `${s} · 凯利仓位`,
      data: kellyEquity,
      borderColor: col.hex,
      borderWidth: 1.8,
      borderDash: [7, 5],
      tension: 0.25,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHoverBackgroundColor: col.hex,
      pointHoverBorderColor: "#05070d",
      fill: false,
    });
  });
  if (portfolio) {
    datasets.push({
      label: "等权组合",
      data: portfolio.equity,
      borderColor: "#e8edf4",
      borderWidth: 2.4,
      tension: 0.25,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHoverBackgroundColor: "#e8edf4",
      pointHoverBorderColor: "#05070d",
      fill: true,
      backgroundColor: (ctx) => verticalGradient(ctx.chart, "232, 237, 244", 0.16, 0),
    });
    if ((portfolio.kelly_equity || []).length) {
      datasets.push({
        label: "等权组合 · 凯利仓位",
        data: portfolio.kelly_equity,
        borderColor: "#a78bfa",
        borderWidth: 2,
        borderDash: [7, 5],
        tension: 0.25,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: "#a78bfa",
        pointHoverBorderColor: "#05070d",
        fill: false,
      });
    }
  }
  if (equityChart) equityChart.destroy();
  equityChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels, datasets },
    options: EQUITY_OPTIONS,
  });
}

function buildRollingChart(labels, series, windowBars) {
  const canvas = $("btRollingChart");
  if (!canvas) return;
  if (rollingChart) rollingChart.destroy();
  const data = series.rolling_sharpe || [];
  const datasets = [
    {
      label: "滚动夏普",
      data,
      borderColor: "#fbbf24",
      borderWidth: 1.5,
      tension: 0.2,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHoverBackgroundColor: "#fbbf24",
      pointHoverBorderColor: "#05070d",
      spanGaps: false,
      fill: {
        target: "origin",
        above: "rgba(251, 191, 36, 0.16)",
        below: "rgba(248, 113, 113, 0.16)",
      },
    },
  ];
  if ((series.kelly_rolling_sharpe || []).length) {
    datasets.push({
      label: "凯利仓位 · 滚动夏普",
      data: series.kelly_rolling_sharpe,
      borderColor: "#a78bfa",
      borderWidth: 1.5,
      borderDash: [7, 5],
      tension: 0.2,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHoverBackgroundColor: "#a78bfa",
      pointHoverBorderColor: "#05070d",
      spanGaps: false,
      fill: false,
    });
  }
  const labelEl = $("btRollingLabel");
  if (labelEl) {
    labelEl.textContent = windowBars
      ? `滚动夏普 · ${windowBars} bars`
      : "滚动夏普 · Rolling Sharpe";
  }
  rollingChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets,
    },
    options: ROLLING_OPTIONS,
  });
}

function renderEquity(resp) {
  const live = $("btEquityLive");
  const empty = $("btEquityEmpty");
  const data = resp?.data;
  const symbols = data?.symbols || {};
  const symNames = Object.keys(symbols);

  if (!resp?.available || !symNames.length) {
    if (live) live.hidden = true;
    if (empty) empty.hidden = false;
    destroyEquityCharts();
    btEquitySig = "";
    return;
  }

  const focus = resp.focus_symbol;
  const sig = [focus, data.total_bars, data.n_points, data.rolling_window, symNames.join(",")].join("|") + "|" + btBuster;
  if (sig === btEquitySig && equityChart) return; // 无变化，避免重建闪烁
  btEquitySig = sig;

  if (live) live.hidden = false;
  if (empty) empty.hidden = true;

  const portfolio = data.portfolio || null;
  let mainName, mainSeries;
  if (portfolio) {
    mainName = "等权组合";
    mainSeries = portfolio;
  } else {
    const key = focus && symbols[focus] ? focus : symNames[0];
    mainName = key;
    mainSeries = symbols[key];
  }

  renderEquityStats(mainName, mainSeries);
  buildEquityChart(data.labels, symbols, portfolio);
  buildRollingChart(data.labels, mainSeries, data.rolling_window);

  if ($("btChartsHint")) {
    $("btChartsHint").textContent = `${mainName} · 实线原仓位 / 虚线凯利仓位 · 悬停查看数值`;
  }
}

async function startBacktest() {
  if (!selectedStrategyFile) {
    await logClientError("请先选择策略文件");
    return;
  }
  const startBtn = $("btStartBtn");
  if (startBtn) startBtn.disabled = true;
  try {
    const costs = readBacktestCosts();
    const res = await fetchJSON("/api/backtest/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        strategy_file: selectedStrategyFile,
        commission_pct: costs.commission_pct,
        slippage_pct: costs.slippage_pct,
      }),
    });
    if (res.strategy_file) renderStrategyFileCard(res.strategy_file);
    selectedBacktestRunId = res.job?.run_id || selectedBacktestRunId;
    await refreshBacktestRuns(selectedBacktestRunId);
    await refreshBacktest();
  } catch (e) {
    if ($("btLogHint")) $("btLogHint").textContent = e.message;
    updateBtStartBtn();
  }
}

async function stopBacktest() {
  try {
    await fetchJSON("/api/backtest/stop", { method: "POST" });
    await refreshBacktest();
  } catch (e) {
    if ($("btLogHint")) $("btLogHint").textContent = e.message;
  }
}

// ═══════════════════════════════════════════════════════════════════
// 实时行情分析（信号雷达）
// ═══════════════════════════════════════════════════════════════════
let rtInited = false;
let rtEngineRunning = false;
let rtSources = [];
let rtSourceById = {};
let rtImportedStrategy = null; // {path, name}
let rtGridSig = "";
let rtDataSig = "";
let rtLastWatches = [];
let rtServerSkew = 0; // server_time - local_now（秒）
let rtCountdownTimer = null;
let rtTvBlockedShownAt = 0;
let rtTvWikiUrl = "https://my.feishu.cn/wiki/FuqnwkPwdiCLhQkPloKc7r1lntg";
let rtMinRefreshSeconds = 1;
let rtMaxRefreshSeconds = 30 * 24 * 60 * 60;
const RT_CAPITAL_STORAGE_KEY = "alphamaster.realtime.totalCapital";
const RT_REFRESH_UNIT_SECONDS = Object.freeze({
  second: 1,
  minute: 60,
  hour: 60 * 60,
});
const RT_TV_BLOCKED_MSG =
  "当前设备无法连接 TradingView 数据服务，将无法获取以下 K 线数据：\n" +
  "  · A 股（上证 SSE、深证 SZSE）\n" +
  "  · 港股（HKEX）\n" +
  "  · 美股及指数（NYSE、NASDAQ、SP）\n" +
  "  · 外汇、贵金属、商品期货\n\n" +
  "解决方案：\n" +
  "  · 把你的VPN工具设成全局，并开启TUN(虚拟网卡)模式，如果还不行：\n" +
  "  · 使用云服务器部署本程序（推荐）—— 云服务器可正常连接 TradingView\n" +
  "  · 或切换回 MT5 数据源，仅使用 MT5 提供的品种数据";
const RT_TV_BLOCKED_CODE = "TV_CONNECTIVITY_BLOCKED";

const RT_DIR = {
  LONG: { label: "↑ 预期上涨", cls: "rt-long", color: "#4ade80" },
  SHORT: { label: "↓ 预期下跌", cls: "rt-short", color: "#f87171" },
  FLAT: { label: "— 先观望", cls: "rt-flat", color: "#7a8a9e" },
};
const RT_STATE_LABEL = {
  pending: "等待首次计算",
  ok: "运行中",
  insufficient: "历史不足",
  error: "错误",
};

/** 把 0~1 强度翻成「把握」白话 */
function rtSizePlain(strength, direction) {
  if (direction === "FLAT" || direction == null) {
    return { size: "没把握" };
  }
  const s = Math.max(0, Math.min(1, Number(strength) || 0));
  let size;
  if (s < 0.2) size = "一点把握";
  else if (s < 0.4) size = "把握不大";
  else if (s < 0.6) size = "一半把握";
  else if (s < 0.8) size = "比较有把握";
  else size = "很有把握";
  return { size };
}

function escHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

function rtClock(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}

function rtDateTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function rtNumber(value, kind = "price") {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const num = Number(value);
  if (kind === "volume") {
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(num);
  }
  const abs = Math.abs(num);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return num.toLocaleString("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function rtReadTotalCapital() {
  const capital = Number($("rtTotalCapital")?.value);
  return Number.isFinite(capital) && capital > 0 ? capital : null;
}

function rtFormatCapital(value) {
  if (!Number.isFinite(Number(value))) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function rtKellyPosition(w) {
  if (w.win_rate == null || w.profit_loss_ratio == null) return null;
  const p = Number(w.win_rate);
  const b = Number(w.profit_loss_ratio);
  if (!Number.isFinite(p) || p < 0 || p > 1 || !Number.isFinite(b) || b <= 0) {
    return null;
  }
  // 完整凯利：f* = p - q / b；不使用杠杆，负期望和超过本金部分分别限制为 0/100%。
  const rawFraction = p - (1 - p) / b;
  const formulaFraction = Math.max(0, Math.min(1, rawFraction));
  const hasTradableSignal =
    w.state === "ok" && (w.direction === "LONG" || w.direction === "SHORT");
  const fraction = hasTradableSignal ? formulaFraction : 0;
  const capital = rtReadTotalCapital();
  return {
    fraction,
    amount: capital == null ? null : capital * fraction,
    hasTradableSignal,
  };
}

function rtInitCapitalInput() {
  const input = $("rtTotalCapital");
  if (!input) return;
  try {
    const saved = localStorage.getItem(RT_CAPITAL_STORAGE_KEY);
    if (saved && Number(saved) > 0) input.value = saved;
  } catch (_) {}
  const update = () => {
    const capital = rtReadTotalCapital();
    try {
      if (capital == null) localStorage.removeItem(RT_CAPITAL_STORAGE_KEY);
      else localStorage.setItem(RT_CAPITAL_STORAGE_KEY, String(capital));
    } catch (_) {}
    rtGridSig = "";
    renderRealtimeGrid(rtLastWatches);
  };
  input.addEventListener("input", update);
  input.addEventListener("change", update);
}

function rtSyncRefreshRange() {
  const input = $("rtRefreshValue");
  const unit = $("rtRefreshUnit")?.value || "second";
  if (!input) return;
  const multiplier = RT_REFRESH_UNIT_SECONDS[unit] || 1;
  input.min = "1";
  input.max = String(Math.max(1, Math.floor(rtMaxRefreshSeconds / multiplier)));
  if (Number(input.value) > Number(input.max)) input.value = input.max;
}

function rtReadRefreshInterval() {
  const value = Number($("rtRefreshValue")?.value || 5);
  const unit = $("rtRefreshUnit")?.value || "second";
  const multiplier = RT_REFRESH_UNIT_SECONDS[unit] || 1;
  return { value, unit, seconds: value * multiplier };
}

function rtFormatRefreshInterval(seconds) {
  const value = Math.max(1, Math.round(Number(seconds) || 5));
  if (value % 3600 === 0) return `${value / 3600} 小时`;
  if (value % 60 === 0) return `${value / 60} 分钟`;
  return `${value} 秒`;
}

function rtNowSec() {
  return Date.now() / 1000 + rtServerSkew;
}

function rtFmtCountdown(sec) {
  const s = Math.max(0, Math.ceil(sec));
  if (s < 60) return `${s}秒`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}分${String(rs).padStart(2, "0")}秒`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 48) return `${h}小时${rm}分`;
  const d = Math.floor(h / 24);
  return `${d}天${h % 24}小时`;
}

function ensureRtCountdownTimer() {
  if (rtCountdownTimer) return;
  rtCountdownTimer = setInterval(tickRtCountdowns, 1000);
}

function tickRtCountdowns() {
  document.querySelectorAll(".rt-countdown").forEach((el) => {
    if (el.dataset.evaluating === "true") {
      el.textContent = "正在刷新数据并判断…";
      return;
    }
    const nxt = Number(el.dataset.nextEvaluation);
    if (!Number.isFinite(nxt) || nxt <= 0) {
      el.textContent = "距离下次判断 —";
      return;
    }
    const left = nxt - rtNowSec();
    el.textContent = left <= 0 ? "即将重新判断…" : `距离下次判断 ${rtFmtCountdown(left)}`;
  });
  const hintCd = $("rtNextHint");
  if (hintCd) {
    if (hintCd.dataset.evaluating === "true") {
      hintCd.textContent = "正在刷新数据并判断…";
      return;
    }
    if (hintCd.dataset.nextEvaluation) {
      const nxt = Number(hintCd.dataset.nextEvaluation);
      if (Number.isFinite(nxt) && nxt > 0) {
        const left = nxt - rtNowSec();
        hintCd.textContent =
          left <= 0 ? "即将重新判断" : `距离下次判断 ${rtFmtCountdown(left)}`;
      }
    }
  }
}

async function initRealtimeOnce() {
  if (rtInited) return;
  rtInited = true;
  try {
    const data = await fetchJSON("/api/realtime/sources");
    rtSources = data.sources || [];
    rtSourceById = {};
    rtSources.forEach((s) => (rtSourceById[s.id] = s));
    const sel = $("rtSourceSelect");
    if (sel) {
      sel.innerHTML = rtSources
        .map((s) => `<option value="${s.id}">${escHtml(s.label)}${s.available ? "" : " · 未就绪"}</option>`)
        .join("");
      // 默认选第一个可用数据源
      if (rtSources.length) sel.value = rtSources[0].id;
    }
    if (data.min_exposure != null && $("rtThresholdHint")) {
      $("rtThresholdHint").textContent = `无信号阈值 |tanh(因子)| < ${data.min_exposure}`;
    }
    const refreshInput = $("rtRefreshValue");
    if (refreshInput) {
      rtMinRefreshSeconds = Number(data.min_refresh_seconds) || 1;
      rtMaxRefreshSeconds = Number(data.max_refresh_seconds) || 30 * 24 * 60 * 60;
      refreshInput.value = String(data.default_refresh_seconds || 5);
      if ($("rtRefreshUnit")) $("rtRefreshUnit").value = "second";
      rtSyncRefreshRange();
    }
    onRtSourceChange();
  } catch (e) {
    await logClientError("加载数据源失败: " + e.message);
  }
  await loadRtStrategies();
  await loadRtFeishuSettings();
}

async function loadRtFeishuSettings() {
  try {
    const data = await fetchJSON("/api/realtime/feishu");
    const en = $("rtFeishuEnabled");
    const wh = $("rtFeishuWebhook");
    const sec = $("rtFeishuSecret");
    if (en) en.checked = !!data.enabled;
    if (wh) wh.value = data.webhook_url || "";
    if (sec) sec.value = data.secret || "";
  } catch (e) {
    const hint = $("rtFeishuHint");
    if (hint) {
      hint.textContent = "加载飞书设置失败: " + e.message;
      hint.classList.add("bad");
    }
  }
}

async function saveRtFeishuSettings() {
  const hint = $("rtFeishuHint");
  const btn = $("rtFeishuSaveBtn");
  if (btn) btn.disabled = true;
  try {
    await fetchJSON("/api/realtime/feishu", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: !!$("rtFeishuEnabled")?.checked,
        webhook_url: $("rtFeishuWebhook")?.value || "",
        secret: $("rtFeishuSecret")?.value || "",
      }),
    });
    if (hint) {
      hint.textContent = "✓ 已保存，训练状态与方向转折会推送到飞书群。";
      hint.classList.remove("bad", "invalid");
      hint.classList.add("valid");
    }
    if (btn) {
      const old = btn.textContent;
      btn.textContent = "已保存";
      setTimeout(() => {
        if (btn.textContent === "已保存") btn.textContent = old || "保存";
      }, 1600);
    }
  } catch (e) {
    if (hint) {
      hint.textContent = "保存失败: " + e.message;
      hint.classList.remove("valid");
      hint.classList.add("bad", "invalid");
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function testRtFeishu() {
  const hint = $("rtFeishuHint");
  const btn = $("rtFeishuTestBtn");
  if (btn) btn.disabled = true;
  try {
    await fetchJSON("/api/realtime/feishu/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        webhook_url: $("rtFeishuWebhook")?.value || "",
        secret: $("rtFeishuSecret")?.value || "",
      }),
    });
    if (hint) {
      hint.textContent = "✓ 测试消息已发送，请到飞书群查收。";
      hint.classList.remove("bad", "invalid");
      hint.classList.add("valid");
    }
  } catch (e) {
    if (hint) {
      hint.textContent = "测试失败: " + e.message;
      hint.classList.remove("valid");
      hint.classList.add("bad", "invalid");
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openRtFeishuHelpModal() {
  const modal = $("rtFeishuHelpModal");
  if (modal) modal.hidden = false;
}

function closeRtFeishuHelpModal() {
  const modal = $("rtFeishuHelpModal");
  if (modal) modal.hidden = true;
}

async function loadRtStrategies() {
  const sel = $("rtStrategySelect");
  if (!sel) return;
  let rows = [];
  try {
    const data = await fetchJSON("/api/realtime/strategies");
    rows = data.strategies || [];
  } catch (_) {}
  const opts = ['<option value="">— 选择已保存策略 —</option>'];
  if (rtImportedStrategy) {
    const isym = escHtml(rtImportedStrategy.symbol || "");
    opts.push(
      `<option value="${escHtml(rtImportedStrategy.path)}" data-symbol="${isym}">导入: ${escHtml(rtImportedStrategy.name)}</option>`
    );
  }
  rows.forEach((r) => {
    const score = r.best_score != null ? Number(r.best_score).toFixed(3) : "—";
    const tf = r.timeframe ? ` ${r.timeframe}` : "";
    opts.push(
      `<option value="${escHtml(r.strategy_file)}" data-symbol="${escHtml(r.symbol || "")}">${escHtml(r.symbol)}${tf} · 分数 ${score}</option>`
    );
  });
  const prev = sel.value;
  sel.innerHTML = opts.join("");
  if (rtImportedStrategy) sel.value = rtImportedStrategy.path;
  else if (prev) sel.value = prev;
  onRtStrategyChange();
}

function onRtSourceChange() {
  const src = rtSourceById[$("rtSourceSelect")?.value];
  const tfSel = $("rtTimeframeSelect");
  const presets = $("rtSymbolPresets");
  const hint = $("rtSourceHint");
  if (!src) return;
  if (tfSel) {
    const cur = tfSel.value;
    tfSel.innerHTML = (src.timeframes || []).map((t) => `<option value="${t}">${t}</option>`).join("");
    if (src.timeframes && src.timeframes.includes(cur)) tfSel.value = cur;
    else if (src.timeframes && src.timeframes.includes("1h")) tfSel.value = "1h";
  }
  // 品种输入/下拉切换：presets 较多时（如国内期货 60 个品种）用下拉框
  const symbolInput = $("rtSymbolInput");
  const symbolSelect = $("rtSymbolSelect");
  const useSelect = src.id === "domestic_futures" || (src.presets && src.presets.length > 20);
  if (symbolInput && symbolSelect) {
    if (useSelect) {
      symbolInput.hidden = true;
      symbolSelect.hidden = false;
      symbolSelect.innerHTML = (src.presets || [])
        .map((s) => `<option value="${escHtml(s)}">${escHtml(s)}</option>`)
        .join("");
      symbolSelect.onchange = () => { symbolInput.value = symbolSelect.value; };
      if (symbolSelect.value) symbolInput.value = symbolSelect.value;
    } else {
      symbolInput.hidden = false;
      symbolSelect.hidden = true;
      if (presets) {
        presets.innerHTML = (src.presets || [])
          .map((s) => `<option value="${escHtml(s)}"></option>`)
          .join("");
      }
    }
  } else if (presets) {
    presets.innerHTML = (src.presets || [])
      .map((s) => `<option value="${escHtml(s)}"></option>`)
      .join("");
  }
  if (hint) {
    hint.textContent = `${src.label}：${src.hint || ""}`;
    hint.classList.toggle("bad", !src.available);
  }
}

function rtParseSymbolFromFilename(pathOrName) {
  const name = String(pathOrName || "").split(/[/\\]/).pop() || "";
  let m = name.match(/^best_(.+)\.json$/i);
  if (m) return m[1];
  m = name.match(/^strategy_(.+)_step\d+/i);
  if (m) return m[1];
  return "";
}

function rtApplySymbolFromStrategy(sym) {
  const s = String(sym || "").trim();
  if (!s) return;
  const input = $("rtSymbolInput");
  if (input) input.value = s;
  const select = $("rtSymbolSelect");
  if (select && !select.hidden) select.value = s;
}

function onRtStrategyChange() {
  const sel = $("rtStrategySelect");
  const picked = $("rtStrategyPicked");
  if (!sel || !picked) return;
  const opt = sel.options[sel.selectedIndex];
  picked.textContent = sel.value
    ? `因子来源：${opt ? opt.textContent : sel.value}。信号取最后已收盘 bar。`
    : "因子来源：从已保存策略下拉选择，或「导入策略」选本地 JSON。信号取最后已收盘 bar。";
  if (!sel.value) return;
  const fromOpt = (opt && opt.dataset.symbol) || "";
  const fromImport =
    rtImportedStrategy && sel.value === rtImportedStrategy.path
      ? rtImportedStrategy.symbol || ""
      : "";
  const sym = fromOpt || fromImport || rtParseSymbolFromFilename(sel.value);
  rtApplySymbolFromStrategy(sym);
}

function rtBrowseStrategy() {
  strategyUploadTarget = "realtime";
  $("strategyFileInput")?.click();
}

async function rtAddWatch() {
  const source = $("rtSourceSelect")?.value;
  const symbol = ($("rtSymbolInput")?.value || "").trim();
  const timeframe = $("rtTimeframeSelect")?.value;
  const strategy_file = $("rtStrategySelect")?.value;
  const refreshInterval = rtReadRefreshInterval();
  const refresh_seconds = refreshInterval.seconds;
  const picked = $("rtStrategyPicked");
  if (!symbol) {
    if (picked) { picked.textContent = "请填写品种"; picked.classList.add("bad"); }
    return;
  }
  if (!strategy_file) {
    if (picked) { picked.textContent = "请选择或导入策略因子"; picked.classList.add("bad"); }
    return;
  }
  if (
    !Number.isInteger(refreshInterval.value) ||
    refreshInterval.value < 1 ||
    refresh_seconds < rtMinRefreshSeconds ||
    refresh_seconds > rtMaxRefreshSeconds
  ) {
    if (picked) {
      picked.textContent = `刷新间隔必须是正整数，且不能超过 ${rtFormatRefreshInterval(rtMaxRefreshSeconds)}`;
      picked.classList.add("bad");
    }
    return;
  }
  const btn = $("rtAddBtn");
  if (btn) btn.disabled = true;
  try {
    if (source === "tradingview") {
      const reachable = await rtEnsureTradingViewReachable();
      if (!reachable) return;
    }
    await fetchJSON("/api/realtime/watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source,
        symbol,
        timeframe,
        strategy_file,
        refresh_seconds,
      }),
    });
    if (picked) picked.classList.remove("bad");
    rtEngineRunning = true;
    rtGridSig = "";
    await refreshRealtime();
  } catch (e) {
    if (picked) { picked.textContent = "添加失败: " + e.message; picked.classList.add("bad"); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function rtEnsureTradingViewReachable() {
  const picked = $("rtStrategyPicked");
  if (picked) {
    picked.textContent = "正在检测 TradingView 连通性…";
    picked.classList.remove("bad");
  }
  try {
    const res = await fetchJSON("/api/realtime/tradingview/probe", {
      method: "POST",
      silent: true,
    });
    if (res && res.ok) {
      if (picked) picked.textContent = "";
      return true;
    }
    if (res?.wiki_url) rtTvWikiUrl = res.wiki_url;
    await showTvBlockedDialog(res?.message || RT_TV_BLOCKED_MSG);
    if (picked) {
      picked.textContent = "TradingView 不可用，请开 VPN 或换云服务器";
      picked.classList.add("bad");
    }
    return false;
  } catch (e) {
    await showTvBlockedDialog(RT_TV_BLOCKED_MSG);
    if (picked) {
      picked.textContent = "TradingView 检测失败: " + (e.message || e);
      picked.classList.add("bad");
    }
    return false;
  }
}

function showTvBlockedDialog(message) {
  const now = Date.now();
  // 避免轮询反复弹出
  if (now - rtTvBlockedShownAt < 60_000) {
    return Promise.resolve("dedupe");
  }
  rtTvBlockedShownAt = now;
  const modal = $("tvBlockedModal");
  const body = $("tvBlockedBody");
  if (!modal || !body) {
    window.alert(message || RT_TV_BLOCKED_MSG);
    return Promise.resolve("alert");
  }
  body.textContent = message || RT_TV_BLOCKED_MSG;
  modal.hidden = false;
  return new Promise((resolve) => {
    modal._tvResolve = resolve;
  });
}

function closeTvBlockedDialog(choice) {
  const modal = $("tvBlockedModal");
  if (modal) modal.hidden = true;
  const resolve = modal && modal._tvResolve;
  if (resolve) {
    modal._tvResolve = null;
    resolve(choice || "cancel");
  }
}

async function onTvBlockedSwitchMt5() {
  const sel = $("rtSourceSelect");
  if (sel) {
    const hasMt5 = [...sel.options].some((o) => o.value === "mt5");
    if (hasMt5) {
      sel.value = "mt5";
      onRtSourceChange();
    }
  }
  closeTvBlockedDialog("mt5");
}

function onTvBlockedOpenCloud() {
  try {
    window.open(rtTvWikiUrl, "_blank", "noopener,noreferrer");
  } catch (_) {}
  closeTvBlockedDialog("cloud");
}

function maybeShowTvBlockedFromWatches(watches) {
  const hit = (watches || []).find(
    (w) =>
      w.source === "tradingview" &&
      (w.tv_blocked || w.message === RT_TV_BLOCKED_CODE)
  );
  if (!hit) return;
  showTvBlockedDialog(RT_TV_BLOCKED_MSG);
}

async function rtRemoveWatch(id) {
  try {
    await fetchJSON("/api/realtime/unwatch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    rtGridSig = "";
    await refreshRealtime();
  } catch (e) {
    await logClientError("移除监控失败: " + e.message);
  }
}

async function refreshRealtime() {
  let st;
  try {
    st = await fetchJSON("/api/realtime/status", { silent: true });
  } catch (_) {
    return;
  }
  // 有监控项却未在跑时自动拉起（不再提供手动开关）
  if (st.count > 0 && !st.running) {
    try {
      st = await fetchJSON("/api/realtime/start", { method: "POST", silent: true });
    } catch (_) {}
  }
  rtEngineRunning = !!st.running;
  rtLastWatches = st.watches || [];
  if (typeof st.server_time === "number") {
    rtServerSkew = st.server_time - Date.now() / 1000;
  }

  let nearestEvaluation = null;
  let anyEvaluating = false;
  for (const w of st.watches || []) {
    if (w.evaluating) anyEvaluating = true;
    if (w.next_evaluation_at != null) {
      if (nearestEvaluation == null || w.next_evaluation_at < nearestEvaluation) {
        nearestEvaluation = w.next_evaluation_at;
      }
    }
  }

  const hint = $("rtStatusHint");
  if (hint) {
    const base = st.count
      ? `${rtEngineRunning ? "运行中" : "已暂停"} · ${st.count} 项`
      : "暂无监控项";
    if (anyEvaluating) {
      hint.innerHTML = `${base} · <span id="rtNextHint" data-evaluating="true">正在刷新数据并判断…</span>`;
    } else if (nearestEvaluation) {
      hint.innerHTML = `${base} · <span id="rtNextHint" data-next-evaluation="${nearestEvaluation}"></span>`;
    } else {
      hint.textContent = base;
    }
  }
  renderRealtimeGrid(rtLastWatches);
  renderRealtimeData(rtLastWatches);
  maybeShowTvBlockedFromWatches(rtLastWatches);
  ensureRtCountdownTimer();
  tickRtCountdowns();
}

// 半环表盘（180° 上半环，值弧按强度填充）
const RT_ARC_LEN = 150.8; // π * 48
function halfRingGauge(strength, colorHex) {
  const s = Math.max(0, Math.min(1, strength || 0));
  const off = RT_ARC_LEN * (1 - s);
  return `<svg class="rt-gauge-svg" viewBox="0 0 120 74" aria-hidden="true">
    <path class="rt-gauge-track" d="M12 62 A 48 48 0 0 1 108 62" />
    <path class="rt-gauge-val" d="M12 62 A 48 48 0 0 1 108 62"
      style="stroke:${colorHex};stroke-dasharray:${RT_ARC_LEN};stroke-dashoffset:${off.toFixed(1)};" />
  </svg>`;
}

function renderRealtimeGrid(watches) {
  const grid = $("rtGrid");
  if (!grid) return;
  if (!watches.length) {
    grid.innerHTML =
      '<div class="metric-empty">尚无监控项。添加「数据源 + 品种 + 周期 + 因子」后开始实时分析。</div>';
    rtGridSig = "";
    return;
  }

  // 签名：只在信号相关字段变化时重建（避免每次轮询重播动画）
  const sig = watches
    .map((w) =>
      [
        w.id,
        w.state,
        w.direction,
        w.strength,
        w.warn,
        w.message,
        w.last_bar_ts,
        w.updated_at,
        w.evaluating ? 1 : 0,
        w.next_evaluation_at || "",
        w.refresh_seconds || 5,
        w.win_rate ?? "",
        w.profit_loss_ratio ?? "",
        w.n_trades ?? "",
      ].join("~")
    )
    .join("|") + `|capital=${rtReadTotalCapital() ?? ""}`;
  // 签名未变时仍同步刷新/判断倒计时锚点
  if (sig === rtGridSig) {
    watches.forEach((w) => {
      const el = grid.querySelector(`.rt-card[data-id="${CSS.escape(w.id)}"] .rt-countdown`);
      if (!el) return;
      if (w.evaluating) {
        el.dataset.evaluating = "true";
        el.dataset.nextEvaluation = "";
      } else if (w.next_evaluation_at) {
        el.dataset.evaluating = "";
        el.dataset.nextEvaluation = String(w.next_evaluation_at);
      } else {
        el.dataset.evaluating = "";
        el.dataset.nextEvaluation = "";
      }
    });
    return;
  }
  rtGridSig = sig;

  grid.innerHTML = watches
    .map((w) => {
      const dir = w.state === "ok" ? RT_DIR[w.direction] || RT_DIR.FLAT : null;
      const color = dir ? dir.color : "#7a8a9e";
      const strength = w.state === "ok" ? w.strength || 0 : 0;
      const dirKey = w.state === "ok" ? w.direction : null;
      const plain = w.state === "ok" ? rtSizePlain(strength, dirKey) : null;
      const dirLabel = dir ? dir.label : RT_STATE_LABEL[w.state] || w.state;
      const dirCls = dir ? dir.cls : "rt-flat";
      const srcLabel = (rtSourceById[w.source] || {}).label || w.source;
      const factorText = w.factor_value != null ? Number(w.factor_value).toFixed(4) : "—";
      const warn = w.warn ? `<div class="rt-warn" title="${escHtml(w.warn)}">⚠ ${escHtml(w.warn)}</div>` : "";
      const displayMsg =
        w.message === RT_TV_BLOCKED_CODE || w.tv_blocked
          ? "无法连接 TradingView：请开启全局 VPN（TUN）或使用云服务器"
          : w.message;
      const msg =
        w.state !== "ok" && displayMsg
          ? `<div class="rt-msg">${escHtml(displayMsg)}</div>`
          : "";
      const sizeText = plain ? plain.size : "—";
      const winRate = Number(w.win_rate);
      const winRateValid =
        w.win_rate != null && Number.isFinite(winRate) && winRate >= 0 && winRate <= 1;
      const plRatio = Number(w.profit_loss_ratio);
      const plRatioValid =
        w.profit_loss_ratio != null && Number.isFinite(plRatio) && plRatio > 0;
      const tradesText =
        w.n_trades != null && Number.isFinite(Number(w.n_trades))
          ? ` · ${Math.max(0, Number(w.n_trades)).toLocaleString("zh-CN")}笔`
          : "";
      const winRateText = winRateValid ? `${(winRate * 100).toFixed(1)}%${tradesText}` : "无匹配回测";
      const kelly = rtKellyPosition(w);
      let kellyText = "等待胜率与盈亏比";
      let kellyClass = "is-empty";
      if (kelly) {
        const pct = `${(kelly.fraction * 100).toFixed(1)}%`;
        if (!kelly.hasTradableSignal) {
          kellyText = `${pct} · 当前观望`;
          kellyClass = "is-zero";
        } else if (kelly.amount == null) {
          kellyText = `${pct} · 请输入总资金`;
          kellyClass = kelly.fraction > 0 ? "is-ready" : "is-zero";
        } else {
          kellyText = `${pct} · ${rtFormatCapital(kelly.amount)}`;
          kellyClass = kelly.fraction > 0 ? "is-ready" : "is-zero";
        }
      }
      const performanceTitle = winRateValid
        ? `最近一次同品种、同公式回测；盈亏比 ${plRatioValid ? plRatio.toFixed(2) : "—"}`
        : "请先使用该策略完成一次回测";
      return `
    <div class="rt-card ${dirCls}" data-id="${escHtml(w.id)}">
      <button class="rt-remove" data-remove="${escHtml(w.id)}" title="移除监控">×</button>
      <div class="rt-card-head">
        <span class="rt-sym">${escHtml(w.symbol)}</span>
        <span class="rt-tf">${escHtml(w.timeframe)}</span>
        <span class="rt-src">${escHtml(srcLabel)}</span>
      </div>
      <div class="rt-gauge">
        ${halfRingGauge(strength, color)}
        <div class="rt-gauge-center">
          <div class="rt-strength">${escHtml(sizeText)}</div>
          <div class="rt-dir ${dirCls}">${dirLabel}</div>
        </div>
      </div>
      <div class="rt-meta">
        <span class="rt-meta-item">因子 <b>${factorText}</b></span>
        <span class="rt-meta-item">${escHtml(w.strategy_name)}</span>
        <span class="rt-meta-item">刷新 ${escHtml(rtFormatRefreshInterval(w.refresh_seconds))}</span>
      </div>
      <div class="rt-kelly-row">
        <span class="rt-kelly-metric${winRateValid ? "" : " is-empty"}" title="${escHtml(performanceTitle)}">
          回测胜率
          <b>${escHtml(winRateText)}</b>
        </span>
        <span class="rt-kelly-metric ${kellyClass}" title="完整凯利仓位；负值归零，最高限制为 100%，观望信号仓位为 0">
          凯利仓位
          <b>${escHtml(kellyText)}</b>
        </span>
      </div>
      <div class="rt-foot">
        <span class="rt-state ${w.state}">${RT_STATE_LABEL[w.state] || w.state}</span>
        <span class="rt-time">更新 ${rtClock(w.updated_at)}</span>
        <span class="rt-countdown"${
          w.evaluating
            ? ` data-evaluating="true"`
            : w.next_evaluation_at
              ? ` data-next-evaluation="${w.next_evaluation_at}"`
              : ""
        }>${
          w.evaluating
            ? "正在刷新数据并判断…"
            : w.next_evaluation_at
              ? "距离下次判断 …"
              : "距离下次判断 —"
        }</span>
      </div>
      ${warn}
      ${msg}
    </div>`;
    })
    .join("");

  runCountUp(grid);
}

function renderRealtimeData(watches) {
  const body = $("rtDataBody");
  const hint = $("rtDataHint");
  if (!body) return;

  const snapshots = watches.filter((w) => w.data_snapshot);
  if (hint) {
    hint.textContent = watches.length
      ? `已读取 ${snapshots.length}/${watches.length} 项`
      : "等待监控读取";
  }

  const sig = watches
    .map((w) => {
      const d = w.data_snapshot || {};
      const b = d.latest_bar || {};
      return [
        w.id,
        w.state,
        w.message,
        d.bar_count,
        d.first_bar_ts,
        d.last_bar_ts,
        d.read_at,
        b.open,
        b.high,
        b.low,
        b.close,
        b.volume,
      ].join("~");
    })
    .join("|");
  if (sig === rtDataSig) return;
  rtDataSig = sig;

  if (!watches.length) {
    body.innerHTML =
      '<tr class="rt-data-empty-row"><td colspan="11">尚无监控项，添加监控后将在这里显示实际读取的数据。</td></tr>';
    return;
  }

  body.innerHTML = watches
    .map((w) => {
      const srcLabel = (rtSourceById[w.source] || {}).label || w.source;
      const d = w.data_snapshot;
      if (!d) {
        const waiting = w.message || (w.state === "pending" ? "等待首次读取" : "暂未读取到数据");
        return `<tr>
          <td><div class="rt-data-symbol">${escHtml(w.symbol)} <span>${escHtml(w.timeframe)}</span></div></td>
          <td>${escHtml(srcLabel)}</td>
          <td colspan="9"><span class="rt-data-wait">${escHtml(waiting)}</span></td>
        </tr>`;
      }

      const bar = d.latest_bar || {};
      const range = `${rtDateTime(d.first_bar_ts)} → ${rtDateTime(d.last_bar_ts)}`;
      return `<tr>
        <td><div class="rt-data-symbol">${escHtml(w.symbol)} <span>${escHtml(w.timeframe)}</span></div></td>
        <td><span class="rt-data-source">${escHtml(srcLabel)}</span></td>
        <td class="rt-data-number">${escHtml(rtNumber(d.bar_count, "volume"))}</td>
        <td class="rt-data-range">${escHtml(range)}</td>
        <td class="rt-data-time">${escHtml(rtDateTime(bar.ts))}</td>
        <td class="rt-data-number">${escHtml(rtNumber(bar.open))}</td>
        <td class="rt-data-number rt-data-high">${escHtml(rtNumber(bar.high))}</td>
        <td class="rt-data-number rt-data-low">${escHtml(rtNumber(bar.low))}</td>
        <td class="rt-data-number rt-data-close">${escHtml(rtNumber(bar.close))}</td>
        <td class="rt-data-number">${escHtml(rtNumber(bar.volume, "volume"))}</td>
        <td class="rt-data-time">${escHtml(rtDateTime(d.read_at))}</td>
      </tr>`;
    })
    .join("");
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  if (realtimePollTimer) clearInterval(realtimePollTimer);
  pollTimer = setInterval(() => {
    refreshOverview();
    if (currentPage === "backtest" || btActive) refreshBacktest();
  }, 4000);
  realtimePollTimer = setInterval(() => {
    if (currentPage === "realtime" || rtEngineRunning) refreshRealtime();
  }, 1000);
}

async function logout() {
  const button = $("logoutButton");
  if (button) button.disabled = true;
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } finally {
    window.location.replace("/login");
  }
}

async function init() {
  try {
    await loadConfig();
    await refreshOverview();
  } catch (e) {
    await logClientError("初始化失败: " + e.message);
  }
  $("browseBtn").addEventListener("click", browseDataFile);
  $("dataFileInput").addEventListener("change", handleDataFileUpload);
  $("historyDataSelect")?.addEventListener("change", selectHistoricalDataFile);
  $("deleteHistoryDataBtn")?.addEventListener("click", deleteHistoricalData);
  $("startBtn").addEventListener("click", startTraining);
  if ($("retrainBtn")) $("retrainBtn").addEventListener("click", retrainFromScratch);
  $("stopBtn").addEventListener("click", stopTraining);
  $("exportBtn").addEventListener("click", exportStrategy);
  $("exportTrainingBtn").addEventListener("click", exportTraining);
  $("importTrainingBtn").addEventListener("click", triggerImportTraining);
  $("importTrainingFile").addEventListener("change", handleImportTrainingFile);
  $("debugModeCheck").addEventListener("change", (e) => setDebugMode(e.target.checked));
  $("logoutButton")?.addEventListener("click", logout);
  if ($("aiApiKeyInput")) {
    $("aiApiKeyInput").addEventListener("input", updateAiChannelHint);
    $("aiApiKeyInput").addEventListener("change", updateAiChannelHint);
  }
  if ($("aiAnalyzeBtn")) $("aiAnalyzeBtn").addEventListener("click", runAiAnalyze);
  if ($("aiUnlimitedBtn")) $("aiUnlimitedBtn").addEventListener("click", openUnlimitedModal);
  document.querySelectorAll("[data-close-unlimited]").forEach((el) => {
    el.addEventListener("click", closeUnlimitedModal);
  });
  document.querySelectorAll("[data-close-error]").forEach((el) => {
    el.addEventListener("click", closeErrorPopup);
  });
  if ($("errorModalCopyBtn")) {
    $("errorModalCopyBtn").addEventListener("click", copyErrorPopupDetail);
  }

  // 步骤导航
  document.querySelectorAll(".stepper .step").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.page));
  });

  // 回测控制
  if ($("btBrowseStrategyBtn")) $("btBrowseStrategyBtn").addEventListener("click", browseStrategyFile);
  if ($("strategyFileInput")) $("strategyFileInput").addEventListener("change", handleStrategyFileUpload);
  if ($("btStrategySelect")) $("btStrategySelect").addEventListener("change", selectSavedBacktestStrategy);
  if ($("btRunSelect")) $("btRunSelect").addEventListener("change", selectBacktestRun);
  if ($("btStartBtn")) $("btStartBtn").addEventListener("click", startBacktest);
  if ($("btStopBtn")) $("btStopBtn").addEventListener("click", stopBacktest);
  ["btCommissionInput", "btSlippageInput"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", updateBtCostHint);
    el.addEventListener("change", updateBtCostHint);
  });

  // 实时分析控制
  rtInitCapitalInput();
  if ($("rtSourceSelect")) $("rtSourceSelect").addEventListener("change", onRtSourceChange);
  if ($("rtStrategySelect")) $("rtStrategySelect").addEventListener("change", onRtStrategyChange);
  if ($("rtRefreshUnit")) $("rtRefreshUnit").addEventListener("change", rtSyncRefreshRange);
  if ($("rtBrowseStrategyBtn")) $("rtBrowseStrategyBtn").addEventListener("click", rtBrowseStrategy);
  if ($("rtAddBtn")) $("rtAddBtn").addEventListener("click", rtAddWatch);
  if ($("tvBlockedMt5Btn")) $("tvBlockedMt5Btn").addEventListener("click", onTvBlockedSwitchMt5);
  if ($("tvBlockedCloudBtn")) $("tvBlockedCloudBtn").addEventListener("click", onTvBlockedOpenCloud);
  document.querySelectorAll("[data-close-tv-blocked]").forEach((el) => {
    el.addEventListener("click", () => closeTvBlockedDialog("cancel"));
  });
  if ($("rtFeishuSaveBtn")) $("rtFeishuSaveBtn").addEventListener("click", saveRtFeishuSettings);
  if ($("rtFeishuTestBtn")) $("rtFeishuTestBtn").addEventListener("click", testRtFeishu);
  if ($("rtFeishuHelpBtn")) $("rtFeishuHelpBtn").addEventListener("click", openRtFeishuHelpModal);
  document.querySelectorAll("[data-close-feishu-help]").forEach((el) => {
    el.addEventListener("click", closeRtFeishuHelpModal);
  });
  if ($("rtGrid")) {
    $("rtGrid").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-remove]");
      if (btn) rtRemoveWatch(btn.dataset.remove);
    });
  }

  startPolling();
}

init();
