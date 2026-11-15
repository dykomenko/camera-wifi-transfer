"use strict";

const G = {
  camera: "", wifi: "", card: "", download: "", forward: "",
  check: "", warning: "", error: "", sync: "", search: "",
};

const state = {
  strings: {},
  settings: {},
  tab: "wifi",
  wifi: { phase: "idle", status: "", storage: "", current: 0, total: 0, file: "", filePercent: 0, result: null, transferring: false, stopping: false },
  card: { state: "none", photos: 0, videos: 0, size: "", message: "", percent: 0, phase: "idle", result: null },
  logOpen: false,
};

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

function t(key, params) {
  let text = state.strings[key] ?? key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split("{" + name + "}").join(String(value));
    }
  }
  return text;
}

function applyStrings() {
  document.documentElement.lang = state.language || "en";
  for (const el of document.querySelectorAll("[data-t]")) {
    el.textContent = t(el.dataset.t);
  }
  $("brand-name").textContent = t("ui_app_name");
  $("open-folder").setAttribute("aria-label", t("open"));
  $("settings-close").setAttribute("aria-label", t("close"));
  document.title = t("ui_app_name");
  const auto = document.querySelector('#language option[value="auto"]');
  if (auto) auto.textContent = t("language_auto");
}

function setStatus(tone, glyph, title, sub, pill) {
  const icon = $("status-icon");
  icon.className = "status-icon " + tone;
  $("status-glyph").textContent = glyph;
  $("status-title").textContent = title;
  $("status-sub").textContent = sub || "";
  const pillEl = $("status-pill");
  pillEl.hidden = !pill;
  pillEl.textContent = pill || "";
}

function setProgress(title, numbers, percent, sub, mode) {
  $("progress-title").textContent = title || "";
  $("progress-numbers").textContent = numbers || "";
  $("progress-sub").textContent = sub || "";
  const bar = $("progress-bar");
  bar.classList.toggle("indeterminate", mode === "indeterminate");
  bar.classList.toggle("done", mode === "done");
  bar.style.width = (mode === "indeterminate" ? 30 : Math.max(0, Math.min(100, percent || 0))) + "%";
}

function busy() {
  return state.wifi.phase === "running" || state.card.phase === "moving";
}

function wifiBlocksCard() {
  return state.wifi.phase === "running" && state.wifi.transferring;
}

function render() {
  const tab = state.tab;
  for (const button of document.querySelectorAll(".tab")) {
    const selected = button.dataset.tab === tab;
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.tabIndex = selected ? 0 : -1;
  }
  $("card-dot").hidden = !(state.card.state === "ready" && tab !== "card");

  const ptp = (state.settings.mode || "ptpip") === "ptpip";
  for (const el of document.querySelectorAll(".wifi-only")) el.hidden = tab !== "wifi";
  for (const el of document.querySelectorAll(".ptp-only")) if (tab === "wifi") el.hidden = !ptp;
  $("options").hidden = tab === "wifi" && !ptp;
  for (const el of document.querySelectorAll(".ptp-only-setting")) el.hidden = !ptp;

  const s = state.settings;
  $("folder-text").textContent = s.destination || "";
  $("folder-path").title = s.destination || "";
  for (const el of document.querySelectorAll("input[type=checkbox][data-setting]")) {
    el.checked = !!s[el.dataset.setting];
  }
  for (const el of document.querySelectorAll("input[name=mode]")) {
    el.checked = el.value === (s.mode || "ptpip");
    el.disabled = busy();
  }
  $("mode-note").textContent = t(ptp ? "network_instruction" : "direct_instruction");
  if (document.activeElement !== $("camera-ip")) $("camera-ip").value = s.camera_ip || "";
  $("language").disabled = busy();
  $("choose-folder").disabled = busy();

  if (tab === "wifi") renderWifi(); else renderCard();
}

function renderWifi() {
  const w = state.wifi;
  const ptp = (state.settings.mode || "ptpip") === "ptpip";
  const instruction = t(ptp ? "network_instruction" : "direct_instruction");
  const main = $("main-button");
  $("main-glyph").textContent = G.download;
  $("main-label").textContent = t("download_new");
  main.disabled = w.phase === "running" || state.card.phase === "moving";
  $("stop-button").hidden = w.phase !== "running";
  $("stop-button").disabled = w.stopping;
  $("hint").textContent = w.stopping ? t("ui_stopping") : t("ui_hint_wifi");

  if (w.phase === "running" && w.stopping) {
    setStatus("busy", G.wifi, t("ui_stopping"), "");
    setProgress(t("ui_stopping"), "", 0, "", "indeterminate");
  } else if (w.phase === "running") {
    setStatus("accent busy", G.wifi, w.status || t("connecting"), w.storage || t("preparing_transfer"));
    if (w.total) {
      const overall = Math.round((w.current * 100) / w.total);
      setProgress(w.file ? `${w.file}  ${w.filePercent}%` : t("connecting"),
        t("ui_count_of", { current: Math.min(w.current + 1, w.total), total: w.total }) + ` · ${overall}%`,
        overall, w.storage, "");
    } else {
      setProgress(w.status || t("connecting"), "", 0, "", "indeterminate");
    }
  } else if (w.phase === "done" && w.result && w.result.cancelled) {
    setStatus("", G.camera, w.result.headline, w.result.coverage || instruction);
    setProgress(w.result.headline, "", 0, w.result.coverage, "");
  } else if (w.phase === "done" && w.result) {
    const r = w.result;
    const tone = r.success && r.complete ? "success" : "warning";
    setStatus(tone, r.success && r.complete ? G.check : G.warning, r.headline, r.coverage || "");
    setProgress(r.headline, w.total ? t("ui_count_of", { current: w.total, total: w.total }) : "",
      r.success ? 100 : 0, r.counts, r.success ? "done" : "");
  } else {
    setStatus("", G.camera, t("ui_wifi_idle"), instruction);
    setProgress(t("waiting_transfer"), "", 0, "", "");
  }
}

function renderCard() {
  const c = state.card;
  const main = $("main-button");
  $("main-glyph").textContent = G.forward;
  $("main-label").textContent = t("mover_move_all");
  main.disabled = !(c.state === "ready" && c.phase !== "moving" && !wifiBlocksCard());
  $("stop-button").hidden = c.phase !== "moving";
  $("stop-button").disabled = false;
  $("hint").textContent = wifiBlocksCard() ? t("ui_card_busy_wifi") : t("ui_hint_card");

  if (c.phase === "moving") {
    const updating = c.key === "updating_card";
    setStatus("accent busy", G.card, updating ? t("mover_updating") : t("mover_copying", { percent: c.percent }),
      t("ui_card_dont_remove"));
    setProgress(updating ? t("mover_updating") : t("mover_copying", { percent: c.percent }),
      `${c.percent}%`, c.percent, "", updating ? "indeterminate" : "");
    return;
  }
  if (c.phase === "done" && c.result) {
    setStatus("success", G.check, t("mover_done_title"), c.result.summary);
    setProgress(t("mover_done_title"), "", 100, c.result.summary, "done");
    return;
  }
  if (c.phase === "failed" && c.result) {
    setStatus("danger", G.error, t("needs_attention"), c.result.message);
    setProgress(t("needs_attention"), "", 0, "", "");
    return;
  }
  switch (c.state) {
    case "scanning":
      setStatus("accent busy", G.search, t("mover_searching"), "");
      setProgress(t("mover_searching"), "", 0, "", "indeterminate");
      break;
    case "ready":
      setStatus("success", G.card, t("ui_card_title"),
        t("mover_card_found", { photos: c.photos, videos: c.videos, size: c.size }) + "\n" + t("ui_card_checked"),
        c.size);
      setProgress(t("ui_card_ready"), t("ui_count_of", { current: 0, total: c.photos + c.videos }), 0,
        t("ui_hint_card"), "");
      break;
    case "empty":
      setStatus("", G.card, t("ui_card_title"), t("mover_card_empty"));
      setProgress(t("mover_card_empty"), "", 0, "", "");
      break;
    case "error":
      setStatus("danger", G.error, t("needs_attention"), c.message);
      setProgress(t("needs_attention"), "", 0, "", "");
      break;
    default:
      setStatus("", G.card, t("mover_no_card"), t("ui_card_none_sub"));
      setProgress(t("mover_no_card"), "", 0, "", "");
  }
}

function log(line) {
  if (!line) return;
  const el = $("log");
  const row = document.createElement("div");
  row.textContent = line;
  el.appendChild(row);
  while (el.childElementCount > 2000) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function showDialog(title, text, buttons) {
  $("dialog-title").textContent = title;
  $("dialog-text").textContent = text;
  const holder = $("dialog-buttons");
  holder.innerHTML = "";
  return new Promise((resolve) => {
    buttons.forEach((b, i) => {
      const el = document.createElement("button");
      el.type = "button";
      el.textContent = b.label;
      if (b.primary) el.className = "primary";
      el.addEventListener("click", () => { $("dialog").hidden = true; resolve(b.value); });
      holder.appendChild(el);
      if (b.primary || i === buttons.length - 1) setTimeout(() => el.focus(), 0);
    });
    $("dialog").hidden = false;
  });
}

const alertDialog = (title, text) => showDialog(title, text, [{ label: t("close"), value: true, primary: true }]);

async function confirmDialog(title, text, yes) {
  return showDialog(title, text, [
    { label: t("ui_cancel"), value: false },
    { label: yes, value: true, primary: true },
  ]);
}

function selectTab(tab) {
  if (state.tab === tab) return;
  state.tab = tab;
  render();
}

async function updateSetting(key, value) {
  state.settings = await api().set_setting(key, value);
  render();
}

function wireUi() {
  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        selectTab(state.tab === "wifi" ? "card" : "wifi");
        $("tab-" + state.tab).focus();
      }
    });
  }
  for (const el of document.querySelectorAll("input[type=checkbox][data-setting]")) {
    el.addEventListener("change", () => updateSetting(el.dataset.setting, el.checked));
  }
  for (const el of document.querySelectorAll("input[name=mode]")) {
    el.addEventListener("change", () => el.checked && updateSetting("mode", el.value));
  }
  $("camera-ip").addEventListener("change", (e) => updateSetting("camera_ip", e.target.value.trim()));
  $("language").addEventListener("change", async (e) => {
    const data = await api().set_language(e.target.value);
    state.strings = data.strings;
    state.language = data.language;
    state.settings = data.settings;
    applyStrings();
    render();
  });
  $("choose-folder").addEventListener("click", async () => {
    const path = await api().choose_folder();
    if (path) await updateSetting("destination", path);
  });
  $("open-folder").addEventListener("click", () => api().open_folder());
  $("details-toggle").addEventListener("click", () => {
    state.logOpen = !state.logOpen;
    $("log").hidden = !state.logOpen;
    $("details-toggle").classList.toggle("open", state.logOpen);
    $("progress-card").classList.toggle("open", state.logOpen);
  });
  const openSettings = (open) => {
    $("settings").hidden = !open;
    $("settings-scrim").hidden = !open;
  };
  $("settings-button").addEventListener("click", () => openSettings(true));
  $("settings-close").addEventListener("click", () => openSettings(false));
  $("settings-scrim").addEventListener("click", () => openSettings(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("settings").hidden && $("dialog").hidden) openSettings(false);
  });
  $("card-autostart").addEventListener("change", async (e) => {
    e.target.checked = await api().set_card_autostart(e.target.checked);
  });
  $("pair-button").addEventListener("click", async () => {
    const go = await confirmDialog(t("confirm_pairing"),
      [t("pairing_intro"), t("pairing_steps"), t("pairing_warning")].join("\n\n"), t("pair_camera"));
    if (!go) return;
    $("pair-button").disabled = true;
    try {
      const result = await api().pair_usb();
      await alertDialog(result.success ? t("pairing_success_title") : t("pairing_failed_title"), result.message);
    } finally {
      $("pair-button").disabled = false;
    }
  });
  $("main-button").addEventListener("click", onMain);
  $("stop-button").addEventListener("click", onStop);
}

async function onMain() {
  if (state.tab === "wifi") {
    const result = await api().start_wifi();
    if (!result.ok) { await alertDialog(result.title, result.message); return; }
    state.wifi = { phase: "running", status: t("connecting"), storage: "", current: 0, total: 0, file: "", filePercent: 0, result: null, transferring: false, stopping: false };
    log("\n" + t("new_run"));
    render();
    return;
  }
  const c = state.card;
  const ok = await confirmDialog(t("mover_confirm_title"),
    t("mover_confirm", { photos: c.photos, videos: c.videos, size: c.size, folder: state.settings.destination }),
    t("mover_move_all"));
  if (!ok) return;
  const result = await api().move_card();
  if (!result.ok) { await alertDialog(t("needs_attention"), result.message); return; }
  state.card.phase = "moving";
  state.card.percent = 0;
  state.card.key = "copying";
  log("\n" + t("new_run"));
  render();
}

async function onStop() {
  if (state.tab === "card" && state.card.phase === "moving") {
    api().stop_card();
    return;
  }
  if (state.wifi.phase !== "running" || state.wifi.stopping) return;
  if (!(await confirmDialog(t("stop_title"), t("stop_question"), t("stop")))) return;
  state.wifi.stopping = true;
  render();
  api().stop_wifi();
}

function onEvent(event) {
  const [type, data] = event;
  const w = state.wifi;
  const c = state.card;
  switch (type) {
    case "log": log(data); break;
    case "status": w.status = data; break;
    case "storage": w.storage = data; break;
    case "overall": w.current = data[0]; w.total = data[1]; break;
    case "file_progress": w.file = data[0]; w.filePercent = data[1]; w.total = data[3] || w.total; break;
    case "wifi_transferring": w.transferring = true; break;
    case "wifi_started":
      state.wifi = { phase: "running", status: t("connecting"), storage: "", current: 0, total: 0, file: "", filePercent: 0, result: null, transferring: false, stopping: false };
      log("\n" + t("new_run"));
      break;
    case "wifi_done":
      w.phase = "done";
      w.stopping = false;
      w.transferring = false;
      w.result = data;
      if (data.success && data.complete) w.current = w.total;
      if (data.dialog) {
        showDialog(data.headline, data.dialog, [
          { label: t("close"), value: false },
          { label: t("ui_open_folder"), value: true, primary: true },
        ]).then((open) => open && api().open_folder());
      }
      break;
    case "card_state":
      Object.assign(c, data);
      if (c.phase === "done" || c.phase === "failed") {
        if (data.state === "none" || data.state === "ready") c.phase = "idle";
      }
      break;
    case "card_progress": c.phase = "moving"; c.key = data.key; c.percent = data.percent; break;
    case "card_done":
      c.phase = "done";
      c.result = data;
      showDialog(t("mover_done_title"), data.dialog, [
        { label: t("close"), value: false },
        { label: t("ui_open_folder"), value: true, primary: true },
      ]).then((open) => open && api().open_folder());
      break;
    case "card_failed":
      c.phase = "failed";
      c.result = data;
      alertDialog(t("needs_attention"), data.message);
      break;
    case "switch_tab": selectTab(data); break;
    case "alert": alertDialog(data.title, data.message); break;
    case "settings": state.settings = data; break;
  }
}

window.app = {
  onEvents(events) {
    for (const event of events) {
      try { onEvent(event); } catch (error) { console.error(error); }
    }
    render();
  },
};

async function start() {
  const data = await api().init();
  state.strings = data.strings;
  state.language = data.language;
  state.settings = data.settings;
  state.tab = data.tab;
  Object.assign(state.card, data.card);
  const select = $("language");
  for (const [code, name] of data.languages) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = code === "auto" ? t("language_auto") : name;
    select.appendChild(option);
  }
  select.value = data.settings.language || "auto";
  $("card-autostart").checked = data.cardAutostart;
  $("version").textContent = data.version;
  applyStrings();
  wireUi();
  render();
  document.getElementById("app").classList.remove("loading");
  api().ready();
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start);
