const state = {
  config: null,
  valid: false,
  dirty: false,
  busy: false,
  status: null,
  logs: [],
  diagnostics: {
    streams: {},
    output: null,
    gpu: null,
  },
  toastTimer: null,
};

const $ = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", () => {
  $("saveButton").addEventListener("click", saveConfig);
  $("restartButton").addEventListener("click", restartPipeline);
  $("addCameraButton").addEventListener("click", addCamera);
  $("applyLayoutButton").addEventListener("click", () => applyLayout($("layoutSelect").value));
  $("refreshLogsButton").addEventListener("click", loadLogs);
  $("copyCommandButton").addEventListener("click", copyCommand);
  $("downloadConfigButton").addEventListener("click", downloadConfig);
  $("testOutputButton").addEventListener("click", testOutput);
  $("testGpuButton").addEventListener("click", testGpu);
  loadConfig();
  refreshStatus();
  setInterval(refreshStatus, 3000);
});

async function loadConfig() {
  try {
    const data = await requestJson("/api/config");
    state.config = data.config;
    state.valid = Boolean(data.valid);
    renderAll();
    updateConfigState(data.valid, data.error);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveConfig() {
  if (!state.config || state.busy) return;
  setBusy(true);
  try {
    const data = await requestJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ config: state.config }),
    });
    state.config = data.config;
    state.dirty = false;
    renderAll();
    updateConfigState(true, null);
    updateStatus(data.status);
    showToast("Saved and applied");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function restartPipeline() {
  if (state.busy) return;
  setBusy(true);
  try {
    const data = await requestJson("/api/restart", { method: "POST" });
    updateStatus(data.status);
    showToast("Restart requested");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function refreshStatus() {
  try {
    const data = await requestJson("/api/status");
    updateStatus(data.status);
    await loadLogs();
  } catch {
    updateFfmpegState("Status unavailable", "warn");
  }
}

async function loadLogs() {
  try {
    const data = await requestJson("/api/logs?limit=240");
    state.logs = data.logs || [];
    renderLogs();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function requestJson(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed with ${response.status}`);
  }
  return payload;
}

function renderAll() {
  renderOutput();
  renderCameras();
  renderPreview();
  renderStatus(state.status);
  renderDiagnosticsPanel();
  renderLogs();
}

function renderOutput() {
  const form = $("outputForm");
  form.replaceChildren();
  const output = ensureOutput();
  const ffmpeg = ensureFfmpeg();
  const workers = ensureWorkers();

  form.append(
    textField("RTSP output", output.url, (value) => setOutput("url", value), true),
    numberField("Width", output.width, (value) => setOutput("width", value)),
    numberField("Height", output.height, (value) => setOutput("height", value)),
    numberField("FPS", output.fps, (value) => setOutput("fps", value)),
    textField("Bitrate", output.bitrate, (value) => setOutput("bitrate", value)),
    selectField(
      "Encoder",
      output.encoder,
      [
        ["software", "software"],
        ["vaapi", "vaapi"],
        ["qsv", "qsv"],
      ],
      (value) => setOutput("encoder", value)
    ),
    selectField(
      "VAAPI RC",
      output.vaapi_rc_mode,
      [
        ["cqp", "cqp"],
        ["cbr", "cbr"],
        ["vbr", "vbr"],
        ["auto", "auto"],
      ],
      (value) => setOutput("vaapi_rc_mode", value)
    ),
    numberField("VAAPI QP", output.vaapi_qp, (value) => setOutput("vaapi_qp", value)),
    textField("VAAPI device", output.vaapi_device, (value) => setOutput("vaapi_device", value), true),
    textField("QSV device", output.qsv_device, (value) => setOutput("qsv_device", value), true),
    selectField(
      "Input RTSP",
      ffmpeg.input_rtsp_transport,
      [
        ["tcp", "tcp"],
        ["udp", "udp"],
      ],
      (value) => setFfmpeg("input_rtsp_transport", value)
    ),
    selectField(
      "Input decode",
      ffmpeg.input_hwaccel || "software",
      [
        ["software", "software"],
        ["vaapi", "vaapi"],
      ],
      (value) => setFfmpeg("input_hwaccel", value)
    ),
    textField(
      "Decode device",
      ffmpeg.hwaccel_device || "/dev/dri/renderD128",
      (value) => setFfmpeg("hwaccel_device", value),
      true
    ),
    selectField(
      "Log level",
      ffmpeg.log_level,
      [
        ["error", "error"],
        ["warning", "warning"],
        ["info", "info"],
        ["debug", "debug"],
      ],
      (value) => setFfmpeg("log_level", value)
    ),
    numberField("Input timeout", ffmpeg.input_timeout_seconds, (value) =>
      setFfmpeg("input_timeout_seconds", value)
    ),
    numberField("Restart delay", ffmpeg.restart_delay_seconds, (value) =>
      setFfmpeg("restart_delay_seconds", value)
    ),
    checkField("Remux workers", workers.enabled, (value) => setWorkers("enabled", value)),
    selectField(
      "Worker mode",
      workers.mode || "remux",
      [["remux", "remux"]],
      (value) => setWorkers("mode", value)
    ),
    selectField(
      "Slot transport",
      workers.slot_transport || "rtsp",
      [
        ["rtsp", "rtsp"],
        ["udp_mpegts", "udp mpeg-ts"],
      ],
      (value) => setWorkers("slot_transport", value)
    ),
    textField(
      "Worker template",
      workers.output_template || "",
      (value) => setWorkers("output_template", value),
      true
    ),
    textField(
      "Wall input template",
      workers.wall_input_template || "",
      (value) => setWorkers("wall_input_template", value),
      true
    ),
    numberField("UDP base port", workers.udp_base_port ?? 15000, (value) =>
      setWorkers("udp_base_port", value)
    ),
    selectField(
      "Worker RTSP",
      workers.rtsp_transport || "tcp",
      [
        ["tcp", "tcp"],
        ["udp", "udp"],
      ],
      (value) => setWorkers("rtsp_transport", value)
    ),
    checkField("Fallback stream", workers.fallback_enabled, (value) =>
      setWorkers("fallback_enabled", value)
    ),
    numberField("Worker grace", workers.start_grace_seconds ?? 2, (value) =>
      setWorkers("start_grace_seconds", value)
    ),
    numberField("Worker restart", workers.restart_delay_seconds ?? 5, (value) =>
      setWorkers("restart_delay_seconds", value)
    ),
    numberField("Live retry", workers.retry_live_seconds ?? 15, (value) =>
      setWorkers("retry_live_seconds", value)
    ),
    numberField("Probe timeout", workers.retry_probe_timeout_seconds ?? 3, (value) =>
      setWorkers("retry_probe_timeout_seconds", value)
    ),
    numberField("Stall timeout", workers.stall_timeout_seconds ?? 3, (value) =>
      setWorkers("stall_timeout_seconds", value)
    ),
    checkField("Preflight worker URLs", workers.wall_input_preflight, (value) =>
      setWorkers("wall_input_preflight", value)
    )
  );
}

function renderCameras() {
  const list = $("cameraList");
  list.replaceChildren();
  const inputs = ensureInputs();
  inputs.forEach((camera, index) => list.append(cameraCard(camera, index)));
}

function cameraCard(camera, index) {
  const card = element("article", "camera-card");
  const head = element("div", "camera-head");
  const headLeft = element("div", "camera-head-left");
  const enabled = checkbox(camera.enabled, (value) => setCamera(index, "enabled", value));
  const title = element("h3");
  title.textContent = camera.name || `camera-${index + 1}`;
  headLeft.append(enabled, title);

  const actions = element("div", "camera-actions");
  const test = button("Test", "secondary", () => testCamera(index));
  const remove = button("Remove", "danger", () => removeCamera(index));
  actions.append(test, remove);
  head.append(headLeft, actions);

  const grid = element("div", "camera-grid");
  const name = textField("Name", camera.name, (value) => {
    setCamera(index, "name", value);
    title.textContent = value || `camera-${index + 1}`;
  });
  const label = textField("Label", camera.label, (value) => setCamera(index, "label", value));
  const url = secretField("RTSP/HTTP URL", camera.url, (value) => setCamera(index, "url", value), true);
  const showLabel = checkField("Show label", camera.show_label, (value) =>
    setCamera(index, "show_label", value)
  );
  const preserve = checkField("Preserve aspect", camera.preserve_aspect, (value) =>
    setCamera(index, "preserve_aspect", value)
  );
  const padColor = textField("Pad color", camera.pad_color, (value) => setCamera(index, "pad_color", value));
  const x = numberField("X", camera.x, (value) => setCamera(index, "x", value));
  const y = numberField("Y", camera.y, (value) => setCamera(index, "y", value));
  const width = numberField("Width", camera.width, (value) => setCamera(index, "width", value));
  const height = numberField("Height", camera.height, (value) => setCamera(index, "height", value));

  grid.append(name, label, url, showLabel, preserve, padColor, x, y, width, height);
  card.append(head, grid);
  const result = state.diagnostics.streams[index];
  if (result) card.append(diagnosticResult(result));
  return card;
}

function renderPreview() {
  const preview = $("preview");
  preview.replaceChildren();
  if (!state.config) return;
  const output = ensureOutput();
  const width = Math.max(1, Number(output.width) || 1);
  const height = Math.max(1, Number(output.height) || 1);

  ensureInputs().forEach((camera, index) => {
    const health = inputHealthFor(index, camera);
    const healthClass = health ? ` ${healthStateClass(health.state)}` : "";
    const cell = element("div", `preview-cell${camera.enabled ? "" : " disabled"}${healthClass}`);
    cell.style.left = `${clampPercent(camera.x, width)}%`;
    cell.style.top = `${clampPercent(camera.y, height)}%`;
    cell.style.width = `${clampPercent(camera.width, width)}%`;
    cell.style.height = `${clampPercent(camera.height, height)}%`;
    const label = element("span");
    label.textContent = camera.label || camera.name || "Camera";
    cell.append(label);
    preview.append(cell);
  });
}

function applyLayout(mode) {
  if (!state.config) return;
  const indexes = enabledIndexes();
  if (!indexes.length) {
    showToast("Enable at least one camera", true);
    return;
  }
  if (mode === "three") {
    if (indexes.length !== 3) {
      showToast("3 wall needs exactly 3 enabled cameras", true);
      return;
    }
    assignCells(indexes, threeCells());
  } else if (mode === "two-by-two") {
    if (indexes.length !== 4) {
      showToast("2x2 needs exactly 4 enabled cameras", true);
      return;
    }
    assignCells(indexes, twoByTwoCells());
  } else if (mode === "five") {
    if (indexes.length !== 5) {
      showToast("5 wall needs exactly 5 enabled cameras", true);
      return;
    }
    assignCells(indexes, fiveCells());
  } else if (mode === "focus") {
    assignCells(indexes, focusCells(indexes.length));
  } else if (mode === "grid") {
    assignCells(indexes, gridCells(indexes.length));
  } else {
    assignCells(indexes, autoCells(indexes.length));
  }
  markDirty();
  renderCameras();
  renderPreview();
}

function autoCells(count) {
  if (count === 1) return [{ x: 0, y: 0, width: outputWidth(), height: outputHeight() }];
  if (count === 2) {
    const half = Math.floor(outputWidth() / 2);
    return [
      { x: 0, y: 0, width: half, height: outputHeight() },
      { x: half, y: 0, width: outputWidth() - half, height: outputHeight() },
    ];
  }
  if (count === 3) return threeCells();
  if (count === 4) return twoByTwoCells();
  if (count === 5) return fiveCells();
  return gridCells(count);
}

function threeCells() {
  const width = outputWidth();
  const height = outputHeight();
  const halfW = Math.floor(width / 2);
  const halfH = Math.floor(height / 2);
  return [
    { x: 0, y: 0, width: halfW, height: halfH },
    { x: halfW, y: 0, width: width - halfW, height: halfH },
    { x: 0, y: halfH, width, height: height - halfH },
  ];
}

function gridCells(count) {
  const width = outputWidth();
  const height = outputHeight();
  const cols = Math.ceil(Math.sqrt(count));
  const rows = Math.ceil(count / cols);
  const cellW = Math.floor(width / cols);
  const cellH = Math.floor(height / rows);
  return Array.from({ length: count }, (_, index) => {
    const col = index % cols;
    const row = Math.floor(index / cols);
    const x = col * cellW;
    const y = row * cellH;
    return {
      x,
      y,
      width: col === cols - 1 ? width - x : cellW,
      height: row === rows - 1 ? height - y : cellH,
    };
  });
}

function twoByTwoCells() {
  return gridCells(4);
}

function fiveCells() {
  const width = outputWidth();
  const height = outputHeight();
  const topH = Math.floor(height / 2);
  const bottomH = height - topH;
  const topW = Math.floor(width / 3);
  const bottomW = Math.floor(width / 2);
  return [
    { x: 0, y: 0, width: topW, height: topH },
    { x: topW, y: 0, width: topW, height: topH },
    { x: topW * 2, y: 0, width: width - topW * 2, height: topH },
    { x: 0, y: topH, width: bottomW, height: bottomH },
    { x: bottomW, y: topH, width: width - bottomW, height: bottomH },
  ];
}

function focusCells(count) {
  if (count < 2) return autoCells(count);
  const width = outputWidth();
  const height = outputHeight();
  const mainW = Math.floor(width * 0.66);
  const sideW = width - mainW;
  const sideCount = count - 1;
  const sideH = Math.floor(height / sideCount);
  const cells = [{ x: 0, y: 0, width: mainW, height }];
  for (let index = 0; index < sideCount; index += 1) {
    const y = index * sideH;
    cells.push({
      x: mainW,
      y,
      width: sideW,
      height: index === sideCount - 1 ? height - y : sideH,
    });
  }
  return cells;
}

function assignCells(indexes, cells) {
  indexes.forEach((cameraIndex, cellIndex) => {
    Object.assign(state.config.inputs[cameraIndex], cells[cellIndex]);
  });
}

function addCamera() {
  if (!state.config) return;
  const next = ensureInputs().length + 1;
  state.config.inputs.push({
    name: `camera-${next}`,
    enabled: true,
    url: "",
    label: `Camera ${next}`,
    show_label: true,
    x: 0,
    y: 0,
    width: outputWidth(),
    height: outputHeight(),
    preserve_aspect: true,
    pad_color: "black",
  });
  applyLayout("auto");
}

function removeCamera(index) {
  state.config.inputs.splice(index, 1);
  markDirty();
  renderCameras();
  renderPreview();
}

function setOutput(key, value) {
  ensureOutput()[key] = value;
  markDirty();
  if (["width", "height"].includes(key)) renderPreview();
}

function setFfmpeg(key, value) {
  ensureFfmpeg()[key] = value;
  markDirty();
}

function setWorkers(key, value) {
  ensureWorkers()[key] = value;
  markDirty();
}

function setCamera(index, key, value) {
  state.config.inputs[index][key] = value;
  markDirty();
  renderPreview();
}

function ensureOutput() {
  state.config.output ||= {};
  return state.config.output;
}

function ensureFfmpeg() {
  state.config.ffmpeg ||= {};
  return state.config.ffmpeg;
}

function ensureWorkers() {
  state.config.workers ||= {
    enabled: false,
    mode: "remux",
    slot_transport: "rtsp",
    output_template: "",
    wall_input_template: "",
    udp_base_port: 15000,
    rtsp_transport: "tcp",
    fallback_enabled: true,
    restart_delay_seconds: 5,
    start_grace_seconds: 2,
    retry_live_seconds: 15,
    retry_probe_timeout_seconds: 3,
    stall_timeout_seconds: 3,
    wall_input_preflight: false,
  };
  return state.config.workers;
}

function ensureInputs() {
  state.config.inputs ||= [];
  return state.config.inputs;
}

function enabledIndexes() {
  return ensureInputs()
    .map((camera, index) => (camera.enabled ? index : null))
    .filter((index) => index !== null);
}

function outputWidth() {
  return Math.max(1, Number(ensureOutput().width) || 1920);
}

function outputHeight() {
  return Math.max(1, Number(ensureOutput().height) || 1080);
}

function textField(labelText, value, onInput, wide = false) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value ?? "";
  input.addEventListener("input", () => onInput(input.value));
  return field(labelText, input, wide);
}

function secretField(labelText, value, onInput, wide = false) {
  const wrap = element("div", "url-row");
  const input = document.createElement("input");
  input.type = "password";
  input.value = value ?? "";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.addEventListener("input", () => onInput(input.value));
  const toggle = button("Show", "secondary", () => {
    input.type = input.type === "password" ? "text" : "password";
    toggle.textContent = input.type === "password" ? "Show" : "Hide";
  });
  wrap.append(input, toggle);
  return field(labelText, wrap, wide);
}

function numberField(labelText, value, onInput) {
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = "1";
  input.value = value ?? 0;
  input.addEventListener("input", () => onInput(Number(input.value)));
  return field(labelText, input);
}

function selectField(labelText, value, options, onInput) {
  const select = document.createElement("select");
  options.forEach(([optionValue, text]) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = text;
    select.append(option);
  });
  select.value = value;
  select.addEventListener("change", () => onInput(select.value));
  return field(labelText, select);
}

function checkField(labelText, value, onInput) {
  const row = element("label", "check-row");
  const input = checkbox(value, onInput);
  const text = document.createElement("span");
  text.textContent = labelText;
  row.append(input, text);
  return row;
}

function checkbox(value, onInput) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(value);
  input.addEventListener("change", () => onInput(input.checked));
  return input;
}

function field(labelText, control, wide = false) {
  const label = element("label", wide ? "wide" : "");
  const text = document.createElement("span");
  text.textContent = labelText;
  label.append(text, control);
  return label;
}

function button(text, className, onClick) {
  const item = document.createElement("button");
  item.type = "button";
  item.textContent = text;
  if (className) item.className = className;
  item.addEventListener("click", onClick);
  return item;
}

function element(tag, className = "") {
  const item = document.createElement(tag);
  if (className) item.className = className;
  return item;
}

function clampPercent(value, total) {
  return Math.max(0, Math.min(100, (Number(value || 0) / total) * 100));
}

function markDirty() {
  state.dirty = true;
  updateConfigState(state.valid, null);
}

function updateConfigState(valid, error) {
  const pill = $("configState");
  pill.className = "state-pill";
  if (state.dirty) {
    pill.textContent = "Unsaved";
    pill.classList.add("warn");
    return;
  }
  if (valid) {
    pill.textContent = "Saved";
    pill.classList.add("ok");
  } else {
    pill.textContent = "Config error";
    pill.classList.add("bad");
    if (error) showToast(error, true);
  }
}

function updateStatus(status) {
  if (!status) return;
  state.status = status;
  updateVersionState(status.version);
  if (status.ffmpeg_running) {
    updateFfmpegState(`Running ${status.pid}`, "ok");
  } else if (status.last_error) {
    updateFfmpegState("Config error", "bad");
  } else if (status.restart_requested) {
    updateFfmpegState("Restarting", "warn");
  } else {
    updateFfmpegState("Stopped", "warn");
  }
  renderStatus(status);
}

function updateVersionState(version) {
  const pill = $("versionState");
  if (!pill) return;
  pill.textContent = version ? `v${version}` : "Version unknown";
  pill.className = "state-pill";
}

function updateFfmpegState(text, className) {
  const pill = $("ffmpegState");
  pill.textContent = text;
  pill.className = `state-pill ${className}`;
}

function setBusy(value) {
  state.busy = value;
  $("saveButton").disabled = value;
  $("restartButton").disabled = value;
  $("refreshLogsButton").disabled = value;
  $("testOutputButton").disabled = value;
  $("testGpuButton").disabled = value;
}

function renderStatus(status) {
  const grid = $("statusGrid");
  const command = $("commandBox");
  grid.replaceChildren();
  if (!status) {
    command.textContent = "No command yet";
    return;
  }
  const runtime = status.runtime || {};
  const gpu = status.gpu || {};
  const items = [
    ["Version", status.version || "-"],
    ["State", status.ffmpeg_running ? "running" : status.last_error ? "config error" : "stopped"],
    ["PID", status.pid || "-"],
    ["Restarts", status.restart_count ?? 0],
    ["Exit", status.last_exit_code ?? "-"],
    ["Started", status.last_started_at || "-"],
    ["Encoder", runtime.encoder || "-"],
    ["Input decode", runtime.input_hwaccel || "-"],
    ["Output", runtime.output_url || "-"],
    ["Canvas", runtime.resolution || "-"],
    ["FPS", runtime.fps || "-"],
    ["Bitrate", runtime.bitrate || "-"],
    ["Inputs", runtime.enabled_inputs ?? "-"],
    ["Active inputs", runtime.active_inputs ?? "-"],
    ["Offline inputs", runtime.offline_inputs ?? "-"],
    ["Workers", runtime.workers || "off"],
  ];
  if (runtime.workers && runtime.workers !== "off") {
    items.push(["Worker transport", runtime.worker_transport || "-"]);
    items.push(["Worker fallback", runtime.worker_fallback ? "on" : "off"]);
    items.push(["Worker inputs", runtime.worker_inputs ?? "-"]);
    items.push(["Worker preflight", runtime.worker_wall_preflight ? "on" : "off"]);
  }
  if (gpu.enabled !== false) {
    items.push(["GPU load", gpu.available ? formatPercent(gpu.load_percent) : "unavailable"]);
    items.push(["GPU video", formatPercent(gpu.video_percent)]);
    items.push(["GPU render", formatPercent(gpu.render_percent)]);
    if (gpu.frequency_mhz !== null && gpu.frequency_mhz !== undefined) {
      items.push(["GPU freq", `${gpu.frequency_mhz} MHz`]);
    }
    if (gpu.source) items.push(["GPU source", gpu.source]);
  }
  if (status.restart_reason) items.push(["Restart reason", status.restart_reason]);
  if (status.last_error) items.push(["Last error", status.last_error]);
  if (gpu.error) items.push(["GPU note", gpu.error]);
  items.forEach(([label, value]) => grid.append(statusCard(label, value)));
  renderInputHealth(status.input_health || []);
  renderWorkerHealth(status.workers || []);
  renderDiagnosticsPanel();
  command.textContent = status.last_command || "No command yet";
}

function statusCard(label, value) {
  const card = element("div", "status-card");
  const title = document.createElement("span");
  const text = document.createElement("strong");
  title.textContent = label;
  text.textContent = String(value);
  card.append(title, text);
  return card;
}

function renderLogs() {
  const panel = $("logPanel");
  if (!state.logs.length) {
    panel.textContent = "No logs yet";
    return;
  }
  panel.textContent = state.logs
    .map((item) => `${item.time} ${item.level.padEnd(7)} ${item.message}`)
    .join("\n");
  panel.scrollTop = panel.scrollHeight;
}

function renderInputHealth(items) {
  const panel = $("inputHealthPanel");
  panel.replaceChildren();
  if (!items.length) {
    panel.textContent = "No input health yet";
    return;
  }
  items.forEach((item) => {
    const row = element("div", `input-health-row ${healthStateClass(item.state)}`);
    const main = element("div", "input-health-main");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    title.textContent = item.label || item.name || `camera-${Number(item.index || 0) + 1}`;
    detail.textContent = item.last_error || item.url || "-";
    main.append(title, detail);

    const side = element("div", "input-health-side");
    const pill = element("span", `input-health-pill ${healthStateClass(item.state)}`);
    pill.textContent = item.state || "unknown";
    const seen = document.createElement("span");
    seen.textContent = item.last_seen_at || "-";
    side.append(pill, seen);

    row.append(main, side);
    panel.append(row);
  });
}

function renderWorkerHealth(items) {
  const panel = $("workerPanel");
  panel.replaceChildren();
  if (!items.length) return;
  items.forEach((item) => {
    const row = element("div", `input-health-row ${workerStateClass(item.state)}`);
    const main = element("div", "input-health-main");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    title.textContent = `${item.name || "worker"} worker`;
    detail.textContent = item.last_error || item.output_url || "-";
    main.append(title, detail);

    const side = element("div", "input-health-side");
    const pill = element("span", `input-health-pill ${workerStateClass(item.state)}`);
    pill.textContent = item.mode ? `${item.state || "unknown"}:${item.mode}` : item.state || "unknown";
    const meta = document.createElement("span");
    const pid = item.pid ? `pid ${item.pid}` : "no pid";
    meta.textContent = `${pid}, restarts ${item.restarts ?? 0}`;
    side.append(pill, meta);

    row.append(main, side);
    panel.append(row);
  });
}

async function testCamera(index) {
  const camera = ensureInputs()[index];
  if (!camera?.url) {
    showToast("Camera URL is empty", true);
    return;
  }
  state.diagnostics.streams[index] = {
    pending: true,
    type: "stream",
    message: "Testing stream...",
    name: camera.name || `camera-${index + 1}`,
  };
  renderCameras();
  try {
    const data = await requestJson("/api/diagnostics/stream", {
      method: "POST",
      body: JSON.stringify({
        url: camera.url,
        name: camera.name || `camera-${index + 1}`,
        rtsp_transport: ensureFfmpeg().input_rtsp_transport || "tcp",
        timeout_seconds: 8,
      }),
    });
    state.diagnostics.streams[index] = data.result;
    renderCameras();
    showToast(data.result.ok ? "Stream OK" : data.result.message, !data.result.ok);
  } catch (error) {
    state.diagnostics.streams[index] = {
      ok: false,
      type: "stream",
      message: error.message,
      name: camera.name || `camera-${index + 1}`,
    };
    renderCameras();
    showToast(error.message, true);
  }
}

async function testOutput() {
  const url = ensureOutput().url;
  if (!url) {
    showToast("Output URL is empty", true);
    return;
  }
  state.diagnostics.output = { pending: true, type: "output", message: "Testing output target..." };
  renderDiagnosticsPanel();
  try {
    const data = await requestJson("/api/diagnostics/output", {
      method: "POST",
      body: JSON.stringify({ url, timeout_seconds: 5 }),
    });
    state.diagnostics.output = data.result;
    renderDiagnosticsPanel();
    showToast(data.result.ok ? "Output target reachable" : data.result.message, !data.result.ok);
  } catch (error) {
    state.diagnostics.output = { ok: false, type: "output", message: error.message };
    renderDiagnosticsPanel();
    showToast(error.message, true);
  }
}

async function testGpu() {
  const output = ensureOutput();
  const ffmpeg = ensureFfmpeg();
  const device = output.vaapi_device || ffmpeg.hwaccel_device || "/dev/dri/renderD128";
  state.diagnostics.gpu = { pending: true, type: "gpu", message: "Testing GPU metrics..." };
  renderDiagnosticsPanel();
  try {
    const data = await requestJson("/api/diagnostics/gpu", {
      method: "POST",
      body: JSON.stringify({ device, sample_ms: 1000 }),
    });
    state.diagnostics.gpu = data.result;
    renderDiagnosticsPanel();
    showToast(data.result.ok ? "GPU metrics OK" : data.result.message, !data.result.ok);
  } catch (error) {
    state.diagnostics.gpu = { ok: false, type: "gpu", message: error.message };
    renderDiagnosticsPanel();
    showToast(error.message, true);
  }
}

function renderDiagnosticsPanel() {
  const panel = $("diagnosticsPanel");
  panel.replaceChildren();
  const results = [state.diagnostics.output, state.diagnostics.gpu].filter(Boolean);
  if (!results.length) return;
  results.forEach((result) => panel.append(diagnosticResult(result)));
}

function diagnosticResult(result) {
  const item = element("div", `diagnostic-result ${diagnosticClass(result)}`);
  const head = element("div", "diagnostic-head");
  const title = document.createElement("strong");
  const pill = element("span", `input-health-pill ${diagnosticClass(result)}`);
  title.textContent = diagnosticTitle(result);
  pill.textContent = result.pending ? "running" : result.ok ? "ok" : "failed";
  head.append(title, pill);

  const body = element("div", "diagnostic-body");
  body.append(diagnosticLine(result.message || "-"));
  diagnosticDetails(result).forEach((line) => body.append(diagnosticLine(line)));
  item.append(head, body);
  return item;
}

function diagnosticTitle(result) {
  if (result.type === "stream") return result.name || "Stream";
  if (result.type === "output") return "Output target";
  if (result.type === "gpu") return "GPU diagnostics";
  return "Diagnostics";
}

function diagnosticDetails(result) {
  if (result.pending) return [];
  if (result.type === "stream") {
    const video = result.video || {};
    const details = [];
    if (video.codec) details.push(`Video: ${video.codec} ${video.width || "?"}x${video.height || "?"}`);
    if (video.fps) details.push(`FPS: ${video.fps}`);
    if (video.pix_fmt) details.push(`Pixel format: ${video.pix_fmt}`);
    details.push(`Audio: ${result.audio_present ? result.audio_codec || "present" : "none"}`);
    if (result.error) details.push(result.error);
    return details;
  }
  if (result.type === "output") {
    const details = [];
    if (result.host && result.port) details.push(`${result.host}:${result.port}`);
    if (result.error) details.push(result.error);
    return details;
  }
  if (result.type === "gpu") {
    const stats = result.stats || {};
    const details = [];
    if (stats.source) details.push(`Source: ${stats.source}`);
    if (stats.load_percent !== null && stats.load_percent !== undefined) {
      details.push(`Load: ${formatPercent(stats.load_percent)}`);
    }
    (result.checks || []).forEach((check) => {
      details.push(`${check.ok ? "OK" : "FAIL"} ${check.name}: ${check.detail}`);
    });
    return details;
  }
  return [];
}

function diagnosticLine(text) {
  const line = document.createElement("span");
  line.textContent = text;
  return line;
}

function diagnosticClass(result) {
  if (result.pending) return "warn";
  return result.ok ? "ok" : "bad";
}

function inputHealthFor(index, camera) {
  const items = state.status?.input_health || [];
  return items.find((item) => item.index === index || item.name === camera.name);
}

function healthStateClass(value) {
  if (value === "active") return "ok";
  if (value === "connecting" || value === "restarting" || value === "offline") return "warn";
  if (value === "failed" || value === "stopped") return "bad";
  if (value === "disabled") return "disabled";
  return "unknown";
}

function workerStateClass(value) {
  if (value === "running") return "ok";
  if (value === "starting" || value === "stopped") return "warn";
  if (value === "failed") return "bad";
  return "unknown";
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(1)}%`;
}

async function copyCommand() {
  const command = state.status?.last_command;
  if (!command) {
    showToast("No command yet", true);
    return;
  }
  try {
    await copyText(command);
    showToast("Command copied");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const ok = document.execCommand("copy");
  textarea.remove();
  if (!ok) throw new Error("Copy failed");
}

async function downloadConfig() {
  try {
    const response = await fetch("/api/config.yaml", { headers: { Accept: "application/x-yaml" } });
    if (!response.ok) throw new Error(`Download failed with ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "camera-wall-config.yaml";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    showToast(error.message, true);
  }
}

function showToast(message, bad = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast visible${bad ? " bad" : ""}`;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    toast.className = "toast";
  }, bad ? 6000 : 2600);
}
