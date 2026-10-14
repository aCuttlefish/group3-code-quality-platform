"use strict";

const $ = (id) => document.getElementById(id);
const CODE = `def shipping_fee(amount):
    if amount < 0 or amount > 1000:
        raise ValueError("amount out of range")
    return 0 if amount > 99 else 10
`;
const SPEC = {
  S1: "R1：输入 amount 为 0 至 1000（含端点）范围内的整数。\nR2：amount >= 99 时运费为 0，否则运费为 10。\nR3：amount 小于 0 或大于 1000 时抛出 ValueError。\n本次不考虑非整数输入。",
  S2: "R1：输入 amount 为 0 至 1000（含端点）范围内的整数。\nR2：amount > 99 时运费为 0，否则运费为 10。\nR3：amount 小于 0 或大于 1000 时抛出 ValueError。\n本次不考虑非整数输入。",
};
const STATUS = {
  generating: "正在生成", needs_spec: "待补充说明", generation_failed: "生成失败",
  generation_timeout: "生成超时", validation_failed: "用例校验失败", ready: "待核对 · 尚未执行",
  running: "正在执行", completed: "执行完成", execution_timeout: "执行超时", environment_error: "执行环境失败",
};
const RESULT = {passed: "通过", assertion_failed: "断言失败", runtime_error: "运行异常", environment_error: "执行环境失败", execution_timeout: "执行超时", not_run: "未执行"};
const SCENARIO = {normal: "正常", boundary: "边界", exception: "异常", invalid: "异常"};
const IN_PROGRESS = new Set(["generating", "running"]);
const GENERATION_FAILURE = new Set(["needs_spec", "generation_failed", "generation_timeout", "validation_failed"]);
let activeTask = null;
let selectedScenario = "S1";
let pollTimer = null;
let operationBusy = false;
let config = null;
let historyTasks = [];
let requestEpoch = 0;
let sourceFilename = "";
let inspectionBusy = false;
let inspectionEpoch = 0;
let inspectionTimer = null;
let inspectedCode = null;
let commentsText = "";
let commentItems = [];
let inspectionError = "";

function setText(id, value) { $(id).textContent = value ?? ""; }
function show(id, visible) { $(id).hidden = !visible; }
function json(value) { try { return JSON.stringify(value); } catch { return String(value); } }
function date(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("zh-CN", {hour12: false});
}
function shortDate(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false});
}
function sourceLabel(source, compact = false) {
  if (source === "fault_injection") return compact ? "故障模拟" : "故障注入（未调用 AI）";
  if (source === "demo") return compact ? "演示" : "固定样例演示 · 未调用 AI";
  return compact ? "AI" : "DeepSeek · 真实模型生成任务";
}
function inputSnapshot() {
  return {code: $("code").value, spec: $("spec").value, function_name: $("function-name").value.trim(), target_count: Number($("target-count").value), spec_source: specSource(), filename: sourceFilename};
}
function inputMatchesTask() {
  if (!activeTask) return true;
  const current = inputSnapshot();
  return current.code === activeTask.code && current.spec === (activeTask.manual_spec ?? activeTask.spec ?? "") && current.function_name === activeTask.function_name && current.target_count === Number(activeTask.target_count) && current.spec_source === (activeTask.spec_source || "manual") && current.filename === (activeTask.filename || "");
}
function unwrapTask(value) { return value.task || value; }
function clearError() { show("global-alert", false); }
function displayError(error) { setText("global-alert", error.message || String(error)); show("global-alert", true); }
async function api(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options, headers:{"Content-Type":"application/json", ...options.headers}});
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了无效响应（HTTP ${response.status}）。请检查后端日志。`); }
  if (!response.ok) {
    const detail = data.error?.message || (typeof data.detail === "string" ? data.detail : null);
    throw new Error(detail || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}
function updateInputCounts() {
  setText("line-count", `${$("code").value.split(/\r?\n/).length - ($( "code").value.endsWith("\n") ? 1 : 0)} 行`);
  setText("char-count", `${$("spec").value.length} / 2000`);
}
function specSource() { return $("spec-mode-comments").checked ? "comments" : "manual"; }
function setSpecSource(source) {
  if (source !== "comments") { clearTimeout(inspectionTimer); inspectionTimer = null; }
  $("spec-mode-comments").checked = source === "comments";
  $("spec-mode-manual").checked = source !== "comments";
  show("manual-spec-panel", source !== "comments");
  show("comments-spec-panel", source === "comments");
  setText("spec-source-label", source === "comments" ? "代码注释" : "手写说明");
}
function updateFilename() {
  setText("source-filename", sourceFilename || "未上传文件 · 可直接编辑代码");
  $("source-filename").title = sourceFilename;
}
function renderCommentPreview() {
  $("comments-state").classList.toggle("comments-warning", Boolean(inspectionError) || (inspectedCode === $("code").value && !commentsText.trim()));
  if (inspectionBusy) {
    setText("comments-state", "正在解析当前代码…");
    setText("comments-preview", "正在提取注释和文档字符串，请稍候。");
  } else if (inspectionError) {
    setText("comments-state", "注释提取失败");
    setText("comments-preview", `${inspectionError}\n\n请修正代码后重新提取，或切换为手写说明。`);
  } else if (inspectedCode !== $("code").value) {
    setText("comments-state", "代码已更新 · 需要重新提取");
    setText("comments-preview", "尚未提取当前代码的注释。点击“重新提取”，或切换为手写说明。");
  } else if (!commentsText.trim()) {
    setText("comments-state", "当前代码未发现可用注释");
    setText("comments-preview", "没有提取到注释或文档字符串。请为函数添加功能说明后重新提取，或选择“手写说明”。");
  } else {
    setText("comments-state", commentItems.length ? `已提取 ${commentItems.length} 处注释 / 文档字符串` : "已提取当前代码注释");
    setText("comments-preview", commentsText);
  }
}
function invalidateComments() {
  clearTimeout(inspectionTimer);
  inspectionTimer = null;
  inspectionEpoch++;
  inspectedCode = null;
  commentsText = "";
  commentItems = [];
  inspectionError = "";
  renderCommentPreview();
}
async function inspectCurrentCode() {
  if (operationBusy || inspectionBusy) return;
  clearTimeout(inspectionTimer);
  inspectionTimer = null;
  const code = $("code").value;
  const epoch = ++inspectionEpoch;
  inspectionBusy = true;
  inspectionError = "";
  renderCommentPreview();
  updateControls();
  try {
    const result = await api("/api/source/inspect", {method:"POST", body:JSON.stringify({code})});
    if (epoch !== inspectionEpoch || code !== $("code").value) return;
    inspectedCode = code;
    commentsText = result.comments_text || "";
    commentItems = Array.isArray(result.comment_items) ? result.comment_items : [];
    if (result.function_name) $("function-name").value = result.function_name;
  } catch (error) {
    if (epoch !== inspectionEpoch) return;
    inspectedCode = null;
    commentsText = "";
    commentItems = [];
    inspectionError = error.message;
  } finally {
    if (epoch === inspectionEpoch) inspectionBusy = false;
    renderCommentPreview();
    updateControls();
  }
}
async function uploadSource(file) {
  if (!file || operationBusy || inspectionBusy) return;
  clearError();
  if (!/\.py$/i.test(file.name)) { displayError(new Error("请选择 .py 格式的 Python 文件。")); $("source-file").value = ""; return; }
  if (file.size > 80000) { displayError(new Error("文件超过 80 KB（80000 字节），请上传较小的单函数 Python 文件。")); $("source-file").value = ""; return; }
  invalidateComments();
  const epoch = ++inspectionEpoch;
  inspectionBusy = true;
  setText("upload-note", "正在读取并检查 Python 文件…");
  renderCommentPreview();
  updateControls();
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    const result = await api("/api/source/inspect", {method:"POST", body:JSON.stringify({filename:file.name, content_base64:btoa(binary)})});
    if (epoch !== inspectionEpoch) return;
    if (Object.values(SPEC).includes($("spec").value)) $("spec").value = "";
    $("code").value = result.code;
    $("function-name").value = result.function_name || "";
    sourceFilename = result.filename || file.name;
    inspectedCode = result.code;
    commentsText = result.comments_text || "";
    commentItems = Array.isArray(result.comment_items) ? result.comment_items : [];
    setSpecSource("manual");
    updateFilename();
    setText("upload-note", "文件已载入。可选择代码注释，或核对并填写手写说明。");
    ["sample-s1", "sample-s2"].forEach((id) => { $(id).classList.remove("selected"); $(id).setAttribute("aria-pressed", "false"); });
    $("confirm").checked = false;
    updateInputCounts();
  } catch (error) {
    if (epoch !== inspectionEpoch) return;
    displayError(error);
    setText("upload-note", "文件未载入，请检查文件内容后重试。当前代码保持原样。");
  } finally {
    if (epoch === inspectionEpoch) inspectionBusy = false;
    $("source-file").value = "";
    renderCommentPreview();
    updateControls();
  }
}
function markScenario(scenario) {
  selectedScenario = scenario;
  ["S1", "S2"].forEach((name) => {
    const button = $("sample-" + name.toLowerCase());
    button.classList.toggle("selected", name === scenario);
    button.setAttribute("aria-pressed", String(name === scenario));
  });
}
function inputChanged(event) {
  if (event?.target?.id === "code") {
    invalidateComments();
    if (specSource() === "comments") inspectionTimer = setTimeout(inspectCurrentCode, 700);
  }
  updateInputCounts();
  $("confirm").checked = false;
  updateControls();
}
function loadScenario(scenario, resetCode = false) {
  markScenario(scenario);
  setSpecSource("manual");
  $("spec").value = SPEC[scenario];
  if (resetCode) {
    $("code").value = CODE;
    $("function-name").value = "shipping_fee";
    $("target-count").value = 7;
    sourceFilename = "";
    updateFilename();
    invalidateComments();
    setText("upload-note", "仅支持 .py · 最大 80 KB · UTF-8 或带编码声明，载入后统一为 UTF-8");
  }
  inputChanged();
}
function restoreTaskInputs(task) {
  clearTimeout(inspectionTimer);
  inspectionTimer = null;
  inspectionEpoch++;
  $("code").value = task.code || "";
  $("spec").value = task.manual_spec ?? task.spec ?? "";
  $("function-name").value = task.function_name || "shipping_fee";
  $("target-count").value = task.target_count || 7;
  sourceFilename = task.filename || "";
  updateFilename();
  setSpecSource(task.spec_source || "manual");
  inspectedCode = task.comments_text !== undefined ? task.code : null;
  commentsText = task.comments_text || "";
  commentItems = Array.isArray(task.comment_items) ? task.comment_items : [];
  inspectionError = "";
  renderCommentPreview();
  if (!task.filename && task.code === CODE) {
    if ((task.spec || "").includes("严格大于 99") || ((task.spec || "").includes("> 99") && !(task.spec || "").includes(">= 99"))) markScenario("S2");
    else markScenario("S1");
  } else {
    ["sample-s1", "sample-s2"].forEach((id) => { $(id).classList.remove("selected"); $(id).setAttribute("aria-pressed", "false"); });
  }
  updateInputCounts();
}
function updateControls() {
  const busy = operationBusy || inspectionBusy || (activeTask && IN_PROGRESS.has(activeTask.status));
  const missingComments = specSource() === "comments" && (inspectedCode !== $("code").value || !commentsText.trim());
  $("generate").disabled = Boolean(busy) || missingComments || (!config?.configured && !$("fault-timeout").checked);
  $("generate").title = missingComments ? "请先提取当前代码的注释，或切换为手写说明。" : "";
  $("generate").textContent = $("fault-timeout").checked ? "运行生成超时测试 →" : "✦ DeepSeek 生成用例 →";
  $("demo").disabled = Boolean(busy);
  $("sample-s1").disabled = Boolean(busy);
  $("sample-s2").disabled = Boolean(busy);
  $("retry").disabled = Boolean(busy) || (!config?.configured && !$("fault-timeout").checked);
  ["upload-source", "source-file", "refresh-comments", "spec-mode-manual", "spec-mode-comments", "fault-timeout"].forEach((id) => { $(id).disabled = Boolean(busy); });
  ["code", "spec", "function-name", "target-count"].forEach((id) => { $(id).readOnly = inspectionBusy || operationBusy; });
  const changed = activeTask && !inputMatchesTask();
  show("input-changed", Boolean(changed));
  const canConfirm = activeTask?.status === "ready" && !changed && !operationBusy && !inspectionBusy;
  $("confirm").disabled = !canConfirm;
  $("run").disabled = !canConfirm || !$("confirm").checked;
  if (!canConfirm) $("confirm").checked = false;
  $("run").textContent = activeTask?.run ? "此任务已执行" : "确认并运行 ▶";
}
async function refreshConfig() {
  try {
    config = await api("/api/config");
    setText("config-state", config.configured ? `DeepSeek 已配置 · ${config.model || "默认模型"}` : "DeepSeek 尚未配置 · 可先体验演示流程");
    setText("config-detail", config.configured ? "密钥已由服务端读取。每次生成都会创建独立任务与输入快照。" : "设置用户环境变量 DEEPSEEK_API_KEY 或填写项目 .env，然后点击“刷新配置”。");
    setText("connection-text", "本地服务已连接");
    $("connection-dot").className = "connection-dot good";
    updateControls();
  } catch (error) {
    setText("config-state", "无法连接本地服务");
    setText("config-detail", error.message);
    setText("connection-text", "本地服务未连接");
    $("connection-dot").className = "connection-dot bad";
    config = null;
    updateControls();
  }
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function expectedLabel(expected) {
  if (!expected) return "—";
  if (expected.kind === "exception") return `抛出 ${expected.type || "异常"}`;
  return `返回 ${json(expected.value)}`;
}
function actualLabel(actual) {
  if (actual === null || actual === undefined) return "未获得实际结果";
  if (actual.kind === "exception") return `${actual.type || "异常"}${actual.message ? `: ${actual.message}` : ""}`;
  if (actual.kind === "return") return `返回 ${json(actual.value)}`;
  return json(actual);
}
function inputLabel(testCase) {
  if (!testCase) return "—";
  return `args: ${json(testCase.args || [])}${Object.keys(testCase.kwargs || {}).length ? `\nkwargs: ${json(testCase.kwargs)}` : ""}`;
}
function renderCases(cases) {
  const body = $("case-rows");
  body.replaceChildren();
  cases.forEach((testCase) => {
    const row = node("tr");
    const idCell = node("td");
    idCell.append(node("span", "case-id", testCase.id), node("span", "scenario-tag", SCENARIO[testCase.scenario] || testCase.scenario || "用例"));
    const inputCell = node("td");
    inputCell.append(node("pre", "value-code", inputLabel(testCase)));
    const expectedCell = node("td");
    expectedCell.append(node("pre", "value-code", expectedLabel(testCase.expected)));
    const ruleCell = node("td");
    ruleCell.append(node("span", "rule-label", (testCase.rule_ids || []).join(" · ")), node("div", "reason", testCase.reason || ""));
    row.append(idCell, inputCell, expectedCell, ruleCell);
    body.append(row);
  });
}
function renderResults(task) {
  const run = task.run;
  show("results-section", Boolean(run));
  if (!run) return;
  const results = run.results || [];
  const passed = results.filter((item) => item.status === "passed").length;
  const failed = results.filter((item) => item.status === "assertion_failed").length;
  const other = results.filter((item) => !["passed", "assertion_failed"].includes(item.status)).length;
  const summary = $("run-summary");
  summary.replaceChildren();
  [["", results.length, "记录数"], ["pass", passed, "实际通过"], [failed ? "fail" : "", failed, "断言失败"], [other ? "fail" : "", other, "异常 / 未执行"]].forEach(([kind, count, label]) => {
    const metric = node("div", `metric ${kind}`);
    metric.append(node("strong", "", count), node("span", "", label));
    summary.append(metric);
  });
  setText("run-duration", Number.isFinite(run.duration_ms) ? `耗时 ${(run.duration_ms / 1000).toFixed(2)} 秒` : "");
  const body = $("result-rows");
  body.replaceChildren();
  results.forEach((result) => {
    const row = node("tr");
    const testCase = (task.cases || []).find((item) => item.id === result.id);
    const idCell = node("td", "case-id", result.id);
    idCell.dataset.label = "用例";
    row.append(idCell);
    const inputCell = node("td");
    inputCell.dataset.label = "输入";
    inputCell.append(node("pre", "value-code", inputLabel(testCase)));
    row.append(inputCell);
    const statusCell = node("td");
    statusCell.dataset.label = "结果";
    statusCell.append(node("span", `result-label ${result.status}`, RESULT[result.status] || result.status));
    row.append(statusCell);
    [expectedLabel(result.expected || testCase?.expected), actualLabel(result.actual)].forEach((value, index) => {
      const cell = node("td");
      cell.dataset.label = index === 0 ? "预期" : "实际";
      cell.append(node("pre", "value-code", value));
      row.append(cell);
    });
    const messageCell = node("td", "result-message", result.message || "—");
    messageCell.dataset.label = "说明";
    row.append(messageCell);
    body.append(row);
  });
  if (!results.length) {
    const row = node("tr");
    const cell = node("td", "", task.status === "running" ? "执行中，等待工具返回实际结果…" : "执行工具尚未返回逐条结果，请查看状态与日志。");
    cell.dataset.label = "状态";
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
  setText("execution-log", `[stdout]\n${run.stdout || "（无输出）"}\n\n[stderr]\n${run.stderr || "（无输出）"}`);
  const confirmation = task.confirmation;
  const integrityNote = run.source_unchanged === true ? " · 源码校验：运行前后保持一致 ✓" : run.source_unchanged === false ? " · 源码校验：发现变化" : "";
  setText("confirmation-note", confirmation ? `人工确认时间：${date(confirmation.timestamp || confirmation.confirmed_at)} · 用例版本：${task.case_version || "—"}${integrityNote}` : "尚未记录人工确认。");
}
function renderTask(task) {
  activeTask = task;
  show("empty-state", false);
  show("task-section", true);
  const cases = Array.isArray(task.cases) ? task.cases : [];
  const hasCases = cases.length > 0;
  let statusText = STATUS[task.status] || task.status;
  const failed = task.run?.results?.some((item) => item.status !== "passed");
  if (task.status === "completed") statusText = failed ? "测试执行完成，存在不符合当前说明的结果" : task.run?.results?.length ? "执行完成 · 全部通过" : "执行完成 · 请查看报告";
  setText("task-status", statusText);
  $("task-status").className = `status-badge ${GENERATION_FAILURE.has(task.status) || task.status === "execution_timeout" ? "warning" : task.status === "environment_error" || (task.status === "completed" && failed) ? "bad" : task.status === "completed" ? "good" : ""}`;
  setText("task-source", sourceLabel(task.generation_source));
  $("task-source").className = `source-label ${task.generation_source !== "deepseek" ? "demo" : ""}`;
  setText("task-id", `任务 ${task.id}`);
  setText("task-created", date(task.created_at));
  const specSourceText = task.spec_source === "comments" ? "代码注释" : "手写说明";
  setText("task-spec-source", `说明来源：${specSourceText}${task.filename ? ` · ${task.filename}` : ""}`);
  setText("effective-spec-source", specSourceText);
  setText("effective-spec", task.spec || "尚未获得有效功能说明。");
  show("demo-notice", task.generation_source === "demo");
  show("task-progress", IN_PROGRESS.has(task.status));
  if (task.status === "generating") {
    setText("progress-title", task.generation_source === "fault_injection" ? "正在模拟生成超时（未调用 AI）…" : "DeepSeek 正在根据说明生成用例…");
    setText("progress-detail", task.generation_source === "fault_injection" ? "此任务用于验证超时处理；会等待 60 秒预算耗尽并保留失败记录。恢复真实生成前请取消“模拟生成超时”。" : "正在等待模型并校验返回结果。生成预算为 60 秒；失败后会保留本次输入与记录。");
  } else if (task.status === "running") {
    setText("progress-title", "已记录确认，正在执行测试…");
    setText("progress-detail", "执行预算为 10 秒。逐条实际结果由执行工具返回，生成阶段不会判定通过或失败。");
  }
  show("task-error", Boolean(task.error));
  setText("task-error", task.error ? task.error.message || "处理失败，请查看下载报告中的详细记录。" : "");
  const questions = Array.isArray(task.questions) ? task.questions : task.questions ? [task.questions] : [];
  show("task-questions", questions.length > 0);
  const questionBox = $("task-questions");
  questionBox.replaceChildren();
  if (questions.length) {
    questionBox.append(node("strong", "", "请补充以下说明后重新生成："));
    const list = node("ul");
    questions.forEach((question) => list.append(node("li", "", typeof question === "string" ? question : json(question))));
    questionBox.append(list);
  }
  show("case-preview", hasCases);
  renderCases(cases);
  setText("test-file", task.test_file || "测试文件尚未生成。");
  show("retry", GENERATION_FAILURE.has(task.status));
  show("download-test", Boolean(task.test_file));
  $("download-test").href = `/api/tasks/${encodeURIComponent(task.id)}/test-file`;
  $("download-report").href = `/api/tasks/${encodeURIComponent(task.id)}/report`;
  $("step-input").className = `step ${hasCases ? "done" : "current"}`;
  $("step-review").className = `step ${task.run ? "done" : hasCases ? "current" : ""}`;
  $("step-run").className = `step ${task.run || task.status === "running" ? "current" : ""}`;
  renderResults(task);
  updateControls();
  renderHistory();
}
function renderHistory() {
  const history = $("history");
  history.replaceChildren();
  if (!historyTasks.length) {
    history.append(node("p", "sidebar-empty", "生成后，任务会保存在这里。"));
    return;
  }
  historyTasks.forEach((task) => {
    const item = node("button", `history-item ${activeTask?.id === task.id ? "active" : ""}`);
    item.type = "button";
    item.title = `${task.function_name || "任务"} · ${STATUS[task.status] || task.status} · ${task.id}`;
    item.append(node("span", "history-item-title", task.function_name || "测试任务"));
    const bottom = node("span", "history-item-bottom");
    bottom.append(node("span", "", shortDate(task.created_at)), node("span", "history-source", sourceLabel(task.generation_source, true)));
    item.append(bottom);
    const label = node("span", "history-item-bottom", STATUS[task.status] || task.status);
    item.append(label);
    item.addEventListener("click", () => openTask(task.id));
    history.append(item);
  });
}
async function refreshHistory() {
  try {
    const data = await api("/api/tasks");
    historyTasks = Array.isArray(data) ? data : data.tasks || [];
    renderHistory();
  } catch (error) {
    $("history").replaceChildren(node("p", "sidebar-empty", `记录暂时不可用：${error.message}`));
  }
}
function stopPolling() { clearTimeout(pollTimer); pollTimer = null; }
function startPolling(id, epoch) {
  stopPolling();
  pollTimer = setTimeout(async () => {
    if (epoch !== requestEpoch) return;
    try {
      const task = unwrapTask(await api(`/api/tasks/${encodeURIComponent(id)}`));
      if (epoch !== requestEpoch) return;
      renderTask(task);
      if (IN_PROGRESS.has(task.status)) startPolling(id, epoch);
      else refreshHistory();
    } catch (error) {
      if (epoch !== requestEpoch) return;
      displayError(new Error(`读取任务状态失败：${error.message}。正在重试。`));
      startPolling(id, epoch);
    }
  }, 800);
}
async function openTask(id) {
  if (operationBusy || inspectionBusy) return;
  operationBusy = true;
  clearTimeout(inspectionTimer);
  inspectionTimer = null;
  updateControls();
  const epoch = ++requestEpoch;
  stopPolling();
  clearError();
  try {
    const task = unwrapTask(await api(`/api/tasks/${encodeURIComponent(id)}`));
    if (epoch !== requestEpoch) return;
    restoreTaskInputs(task);
    $("confirm").checked = false;
    renderTask(task);
    if (IN_PROGRESS.has(task.status)) startPolling(id, epoch);
    setHistoryOpen(false);
    $("task-section").scrollIntoView({behavior:"smooth", block:"start"});
  } catch (error) { displayError(error); }
  finally { operationBusy = false; updateControls(); }
}
function validateInput(snapshot) {
  if (!snapshot.code.trim()) throw new Error("请先填写被测 Python 代码。");
  if (snapshot.spec_source !== "comments" && !snapshot.spec.trim()) throw new Error("请先填写功能说明，或选择使用代码注释。");
  if (!snapshot.function_name.trim()) throw new Error("请填写目标函数名。");
  if (!Number.isInteger(snapshot.target_count) || snapshot.target_count < 1 || snapshot.target_count > 10) throw new Error("请将用例数量设置为 1 至 10 的整数。");
}
async function generate(demo = false, snapshot = null) {
  if (operationBusy || inspectionBusy) return;
  clearError();
  let payload;
  try {
    if (demo) {
      loadScenario(selectedScenario, true);
      payload = {scenario:selectedScenario};
    } else {
      payload = snapshot || inputSnapshot();
      validateInput(payload);
      payload = {...payload, fault_timeout:$("fault-timeout").checked};
    }
  } catch (error) { displayError(error); return; }
  operationBusy = true;
  updateControls();
  const epoch = ++requestEpoch;
  stopPolling();
  try {
    const task = unwrapTask(await api(demo ? "/api/demo-generations" : "/api/generations", {method:"POST", body:JSON.stringify(payload)}));
    if (epoch !== requestEpoch) return;
    restoreTaskInputs(task);
    $("confirm").checked = false;
    renderTask(task);
    refreshHistory();
    if (IN_PROGRESS.has(task.status)) startPolling(task.id, epoch);
    $("task-section").scrollIntoView({behavior:"smooth", block:"start"});
  } catch (error) { displayError(error); }
  finally { operationBusy = false; updateControls(); }
}
async function runTask() {
  if (!activeTask || activeTask.status !== "ready" || !$("confirm").checked || !inputMatchesTask() || operationBusy || inspectionBusy) return;
  clearError();
  operationBusy = true;
  const id = activeTask.id;
  const caseVersion = activeTask.case_version;
  const epoch = ++requestEpoch;
  stopPolling();
  updateControls();
  try {
    const task = unwrapTask(await api(`/api/tasks/${encodeURIComponent(id)}/run`, {method:"POST", body:JSON.stringify({confirmed:true, case_version:caseVersion})}));
    if (epoch !== requestEpoch) return;
    renderTask(task);
    refreshHistory();
    if (IN_PROGRESS.has(task.status)) startPolling(id, epoch);
  } catch (error) { displayError(error); }
  finally { operationBusy = false; updateControls(); }
}

$("sample-s1").addEventListener("click", () => loadScenario("S1"));
$("sample-s2").addEventListener("click", () => loadScenario("S2"));
["spec-mode-manual", "spec-mode-comments"].forEach((id) => $(id).addEventListener("change", async () => {
  const source = specSource();
  setSpecSource(source);
  inputChanged();
  if (source === "comments") await inspectCurrentCode();
}));
$("upload-source").addEventListener("click", () => $("source-file").click());
$("source-file").addEventListener("change", () => uploadSource($("source-file").files?.[0]));
$("refresh-comments").addEventListener("click", () => inspectCurrentCode());
["code", "spec", "function-name", "target-count"].forEach((id) => $(id).addEventListener("input", inputChanged));
$("confirm").addEventListener("change", updateControls);
$("fault-timeout").addEventListener("change", updateControls);
$("generate").addEventListener("click", () => generate(false));
$("demo").addEventListener("click", () => generate(true));
$("run").addEventListener("click", runTask);
$("retry").addEventListener("click", () => {
  if (activeTask) generate(false, {code:activeTask.code, spec:activeTask.manual_spec ?? activeTask.spec ?? "", function_name:activeTask.function_name, target_count:activeTask.target_count, spec_source:activeTask.spec_source || "manual", filename:activeTask.filename || "", retry_of:activeTask.id});
});
$("refresh-config").addEventListener("click", refreshConfig);
$("refresh-history").addEventListener("click", refreshHistory);
function setHistoryOpen(open) {
  document.querySelector(".sidebar").classList.toggle("history-open", open);
  $("toggle-history").setAttribute("aria-expanded", String(open));
  $("toggle-history").textContent = open ? "收起记录 ▴" : "任务记录 ▾";
}
$("toggle-history").addEventListener("click", () => {
  const open = $("toggle-history").getAttribute("aria-expanded") !== "true";
  setHistoryOpen(open);
  if (open) refreshHistory();
});
window.addEventListener("focus", refreshConfig);
$("code").addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || event.currentTarget.readOnly) return;
  event.preventDefault();
  const editor = event.currentTarget;
  const start = editor.selectionStart;
  editor.setRangeText("    ", start, editor.selectionEnd, "end");
  inputChanged({target:editor});
});
loadScenario("S1", true);
Promise.allSettled([refreshConfig(), refreshHistory()]);
