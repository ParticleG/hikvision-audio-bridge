(() => {
  "use strict";

  const TOKEN_KEY = "hikvision-audio-bridge.access-token";
  const DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural";
  const POLL_INTERVAL_MS = 5000;
  const REQUEST_TIMEOUT_MS = 20000;
  const LOGIN_TIMEOUT_MS = 15000;
  const NOTICE_TIMEOUT_MS = 7000;
  const XML_CONTROL_CHARACTERS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/;
  const CONNECTION_LABELS = {
    idle: "Idle",
    connecting: "Connecting",
    connected: "Connected",
    error: "Error",
  };
  const CONNECTION_CLASSES = ["idle", "connecting", "connected", "error"];
  const TTS_STATE_LABELS = {
    idle: "Idle",
    synthesizing: "Synthesizing",
    playing: "Playing",
    error: "Error",
  };
  const TTS_STATE_CLASSES = ["idle", "synthesizing", "playing", "error"];

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  const byId = (id) => {
    const element = document.getElementById(id);
    if (!element) {
      throw new Error(`Missing required element: ${id}`);
    }
    return element;
  };

  const el = {
    loginPanel: byId("login-panel"),
    loginForm: byId("login-form"),
    loginToken: byId("login-token"),
    loginSubmit: byId("login-submit"),
    loginStatus: byId("login-status"),
    loginError: byId("login-error"),
    app: byId("app"),
    sessionTools: byId("session-tools"),
    signOut: byId("sign-out"),
    appError: byId("app-error"),
    appNotice: byId("app-notice"),
    liveWarning: byId("live-warning"),
    camerasHeading: byId("cameras-heading"),
    cameraSummary: byId("camera-summary"),
    cameraNew: byId("camera-new"),
    cameraList: byId("camera-list"),
    cameraForm: byId("camera-form"),
    cameraEditorHeading: byId("camera-editor-heading"),
    cameraEditId: byId("camera-edit-id"),
    cameraId: byId("camera-id"),
    cameraUrl: byId("camera-url"),
    cameraUsername: byId("camera-username"),
    cameraChannel: byId("camera-channel"),
    cameraPassword: byId("camera-password"),
    cameraPasswordHint: byId("camera-password-hint"),
    cameraSubmit: byId("camera-submit"),
    cameraCancel: byId("camera-cancel"),
    cameraClear: byId("camera-clear"),
    deviceSummary: byId("device-summary"),
    deviceNew: byId("device-new"),
    deviceList: byId("device-list"),
    deviceForm: byId("device-form"),
    deviceEditorHeading: byId("device-editor-heading"),
    deviceEditId: byId("device-edit-id"),
    deviceId: byId("device-id"),
    deviceName: byId("device-name"),
    deviceDirection: byId("device-direction"),
    deviceCameras: byId("device-cameras"),
    deviceEnabled: byId("device-enabled"),
    deviceSubmit: byId("device-submit"),
    deviceCancel: byId("device-cancel"),
    deviceClear: byId("device-clear"),
    ttsForm: byId("tts-form"),
    ttsText: byId("tts-text"),
    ttsCount: byId("tts-count"),
    ttsVoice: byId("tts-voice"),
    ttsTargetsSelect: byId("tts-targets-select"),
    ttsState: byId("tts-state"),
    ttsError: byId("tts-error"),
    ttsActiveTargets: byId("tts-active-targets"),
    ttsSpeak: byId("tts-speak"),
    ttsStop: byId("tts-stop"),
  };

  const state = {
    token: null,
    authenticated: false,
    status: null,
    version: 0,
    pollTimer: null,
    quickPollTimer: null,
    pollInFlight: false,
    loginPending: false,
    cameraSaving: false,
    deviceSaving: false,
    cameraEditingId: null,
    deviceEditingId: null,
    ttsPending: false,
    requests: new Set(),
    pendingDeviceToggles: new Set(),
    signatures: {
      cameraList: "",
      deviceList: "",
      cameraOptions: "",
      ttsTargets: "",
    },
    noticeTimer: null,
  };

  const tokenStorage = {
    read() {
      try {
        return window.sessionStorage.getItem(TOKEN_KEY);
      } catch {
        return null;
      }
    },
    write(value) {
      try {
        window.sessionStorage.setItem(TOKEN_KEY, value);
      } catch {
        // The token remains available in this page's memory.
      }
    },
    clear() {
      try {
        window.sessionStorage.removeItem(TOKEN_KEY);
      } catch {
        // Nothing else can be cleared here.
      }
    },
  };

  function create(tagName, className, text) {
    const item = document.createElement(tagName);
    if (className) {
      item.className = className;
    }
    if (text !== undefined && text !== null) {
      item.textContent = String(text);
    }
    return item;
  }

  function setText(element, value) {
    const next = value === undefined || value === null ? "" : String(value);
    if (element.textContent !== next) {
      element.textContent = next;
    }
  }

  function setVisible(element, visible) {
    element.hidden = !visible;
  }

  function setRoleText(root, role, value) {
    const target = root.querySelector(`[data-role="${role}"]`);
    if (target) {
      setText(target, value);
    }
  }

  function asRecord(value) {
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  }

  function displayValue(value, fallback = "—") {
    if (typeof value === "string" && value !== "") {
      return value;
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      return String(value);
    }
    return fallback;
  }

  function currentSettings() {
    return asRecord(state.status && state.status.settings);
  }

  function currentCameras() {
    return asRecord(currentSettings().cameras);
  }

  function currentDevices() {
    return asRecord(currentSettings().devices);
  }

  function currentTts() {
    return asRecord(state.status && state.status.tts);
  }

  function compareText(left, right) {
    return String(left).localeCompare(String(right), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  }

  function deviceDisplayName(id) {
    const device = asRecord(currentDevices()[id]);
    return typeof device.name === "string" && device.name !== ""
      ? device.name
      : id;
  }

  function cameraRouteLabel(id) {
    const camera = asRecord(currentCameras()[id]);
    const url = displayValue(camera.url, "unknown URL");
    return `${id} (${url})`;
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 B";
    }
    const units = ["B", "KiB", "MiB", "GiB"];
    let size = bytes;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    const rounded = unit === 0 ? Math.round(size) : Math.round(size * 10) / 10;
    return `${rounded} ${units[unit]}`;
  }

  function connectionValue(runtime) {
    const connection = asRecord(runtime).connection;
    return typeof connection === "string" && CONNECTION_LABELS[connection]
      ? connection
      : "idle";
  }

  function ttsStateValue() {
    const value = currentTts().state;
    return typeof value === "string" && TTS_STATE_LABELS[value] ? value : "idle";
  }

  function ttsBusy() {
    const value = ttsStateValue();
    return value === "synthesizing" || value === "playing";
  }

  function setLoginStatus(message) {
    setText(el.loginStatus, message || "");
    setVisible(el.loginStatus, Boolean(message));
  }

  function setLoginError(message) {
    setText(el.loginError, message || "");
    setVisible(el.loginError, Boolean(message));
  }

  function clearAppMessages() {
    window.clearTimeout(state.noticeTimer);
    state.noticeTimer = null;
    setText(el.appError, "");
    setVisible(el.appError, false);
    setText(el.appNotice, "");
    setVisible(el.appNotice, false);
  }

  function showError(message) {
    if (!state.authenticated) {
      return;
    }
    window.clearTimeout(state.noticeTimer);
    state.noticeTimer = null;
    setVisible(el.appNotice, false);
    setText(el.appError, message);
    setVisible(el.appError, true);
  }

  function showNotice(message) {
    if (!state.authenticated) {
      return;
    }
    window.clearTimeout(state.noticeTimer);
    setText(el.appError, "");
    setVisible(el.appError, false);
    setText(el.appNotice, message);
    setVisible(el.appNotice, true);
    state.noticeTimer = window.setTimeout(() => {
      setVisible(el.appNotice, false);
      state.noticeTimer = null;
    }, NOTICE_TIMEOUT_MS);
  }

  function showLiveWarning(message) {
    setText(el.liveWarning, message);
    setVisible(el.liveWarning, true);
  }

  function hideLiveWarning() {
    setText(el.liveWarning, "");
    setVisible(el.liveWarning, false);
  }

  function errorMessage(error) {
    if (error instanceof ApiError) {
      return error.message;
    }
    return "The request could not be completed.";
  }

  function safely(action) {
    return (...args) => {
      try {
        const result = action(...args);
        if (result && typeof result.then === "function") {
          result.catch(handleUnexpected);
        }
      } catch (error) {
        handleUnexpected(error);
      }
    };
  }

  function handleUnexpected() {
    if (state.authenticated) {
      showError("An unexpected browser error occurred. Reload the page if the problem continues.");
    } else {
      setLoginError("An unexpected browser error occurred. Reload the page and try again.");
    }
  }

  async function request(path, options = {}) {
    const {
      method = "GET",
      body,
      tokenValue = state.token,
      timeoutMs = REQUEST_TIMEOUT_MS,
    } = options;
    if (!tokenValue) {
      throw new ApiError("Sign in to continue.", 401);
    }
    const encodedBody = body === undefined ? undefined : JSON.stringify(body);
    const headers = {
      Accept: "application/json",
      Authorization: `Bearer ${tokenValue}`,
    };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const controller = new AbortController();
    state.requests.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    const init = {
      method,
      headers,
      mode: "same-origin",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    };
    if (encodedBody !== undefined) {
      init.body = encodedBody;
    }
    let statusCode = 0;
    let payload = null;
    try {
      const response = await fetch(path, init);
      statusCode = response.status;
      const raw = await response.text();
      if (raw) {
        try {
          payload = JSON.parse(raw);
        } catch {
          payload = null;
        }
      }
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new ApiError("The bridge did not respond in time.", 0);
      }
      throw new ApiError(
        "Could not reach the bridge. Check that the local service is running.",
        0
      );
    } finally {
      window.clearTimeout(timeout);
      state.requests.delete(controller);
    }
    if (statusCode === 401) {
      throw new ApiError("The access token was rejected.", 401);
    }
    if (
      statusCode < 200 ||
      statusCode >= 300 ||
      !payload ||
      payload.ok !== true ||
      !payload.status ||
      typeof payload.status !== "object"
    ) {
      const message =
        payload && typeof payload.error === "string" && payload.error
          ? payload.error
          : `The bridge rejected the request (HTTP ${statusCode}).`;
      throw new ApiError(message, statusCode);
    }
    return payload.status;
  }

  async function api(path, options = {}) {
    const tokenAtStart = state.token;
    try {
      const status = await request(path, options);
      if (!state.authenticated || state.token !== tokenAtStart) {
        throw new ApiError("Your session ended.", 401);
      }
      return status;
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 401 &&
        state.authenticated &&
        state.token === tokenAtStart
      ) {
        signOut("Your access token was rejected. Sign in again.");
      }
      throw error;
    }
  }

  async function executeCommand(command) {
    const status = await api("/api/command", { method: "POST", body: command });
    state.version += 1;
    applyStatus(status);
    hideLiveWarning();
    return status;
  }

  function applyStatus(status) {
    if (!status || typeof status !== "object") {
      return;
    }
    state.status = status;
    renderCameras();
    syncCameraOptions();
    renderDevices();
    renderTts();
    syncTtsTargets();
    updateTtsControls();
  }

  function createDetail(label, value, role, wide = false) {
    const wrapper = create("div", wide ? "detail detail-wide" : "detail");
    const term = create("dt", null, label);
    const description = create("dd", null, value);
    if (role) {
      description.dataset.role = role;
    }
    wrapper.append(term, description);
    return wrapper;
  }

  function createErrorDetail(label, wrapperRole, valueRole) {
    const wrapper = createDetail(label, "", valueRole, true);
    wrapper.dataset.role = wrapperRole;
    wrapper.hidden = true;
    const description = wrapper.querySelector("dd");
    if (description) {
      description.classList.add("error-text");
    }
    return wrapper;
  }

  function updateErrorDetail(row, role, value) {
    const message = typeof value === "string" ? value : "";
    setRoleText(row, role, message);
    row.hidden = !message;
  }

  function actionButton(label, className, onClick, accessibleLabel) {
    const button = create("button", `button ${className}`, label);
    button.type = "button";
    if (accessibleLabel) {
      button.setAttribute("aria-label", accessibleLabel);
    }
    button.addEventListener("click", safely(onClick));
    return button;
  }

  function buildCameraRow(id, camera, runtime) {
    const article = create("article", "entity");
    article.dataset.cameraId = id;

    const header = create("div", "entity-header");
    const titleWrap = create("div", "entity-title-wrap");
    titleWrap.append(
      create("h3", "entity-title", id),
      create("p", "entity-subtitle", displayValue(camera.url, "No camera URL"))
    );
    const statusBadge = create("span", "badge badge-idle", "Idle");
    statusBadge.dataset.role = "connection";
    header.append(titleWrap, statusBadge);

    const details = create("dl", "detail-grid");
    details.append(
      createDetail("Username", displayValue(camera.username)),
      createDetail("Channel", displayValue(camera.channel)),
      createDetail("Codec", "—", "codec"),
      createDetail("Sent", "0 B", "sent"),
      createDetail("Received", "0 B", "received"),
      createErrorDetail("Last error", "error-row", "error"),
      createErrorDetail("Cleanup error", "cleanup-row", "cleanup-error")
    );

    const actions = create("div", "entity-actions");
    const reconnectButton = actionButton(
      "Reconnect",
      "button-secondary",
      () => reconnectCamera(id, reconnectButton),
      `Reconnect camera ${id}`
    );
    actions.append(
      actionButton(
        "Edit",
        "button-secondary",
        () => beginCameraEdit(id),
        `Edit camera ${id}`
      ),
      reconnectButton,
      actionButton(
        "Delete",
        "button-danger",
        () => removeCamera(id),
        `Delete camera ${id}`
      )
    );

    article.append(header, details, actions);
    updateCameraRow(article, runtime);
    return article;
  }

  function updateCameraRow(row, runtime) {
    const current = asRecord(runtime);
    const connection = connectionValue(current);
    const badge = row.querySelector('[data-role="connection"]');
    if (badge) {
      badge.className = `badge badge-${CONNECTION_CLASSES.includes(connection) ? connection : "unknown"}`;
      setText(badge, CONNECTION_LABELS[connection]);
    }
    setRoleText(row, "codec", displayValue(current.codec));
    setRoleText(row, "sent", formatBytes(current.sent_bytes));
    setRoleText(row, "received", formatBytes(current.received_bytes));
    const errorRow = row.querySelector('[data-role="error-row"]');
    if (errorRow) {
      updateErrorDetail(errorRow, "error", current.error);
    }
    const cleanupRow = row.querySelector('[data-role="cleanup-row"]');
    if (cleanupRow) {
      updateErrorDetail(cleanupRow, "cleanup-error", current.cleanup_error);
    }
  }

  function renderCameras() {
    const cameras = currentCameras();
    const runtimes = asRecord(state.status && state.status.cameras);
    const ids = Object.keys(cameras).sort(compareText);
    const signature = JSON.stringify(
      ids.map((id) => {
        const camera = asRecord(cameras[id]);
        return [
          id,
          displayValue(camera.url, ""),
          displayValue(camera.username, ""),
          displayValue(camera.channel, ""),
        ];
      })
    );

    if (signature !== state.signatures.cameraList) {
      state.signatures.cameraList = signature;
      if (!ids.length) {
        el.cameraList.replaceChildren(
          create("p", "empty-state", "No cameras configured yet.")
        );
      } else {
        const fragment = document.createDocumentFragment();
        for (const id of ids) {
          fragment.append(
            buildCameraRow(id, asRecord(cameras[id]), asRecord(runtimes[id]))
          );
        }
        el.cameraList.replaceChildren(fragment);
      }
    }

    const rows = new Map();
    for (const row of el.cameraList.querySelectorAll("[data-camera-id]")) {
      rows.set(row.dataset.cameraId, row);
    }
    let connected = 0;
    let connecting = 0;
    let failed = 0;
    for (const id of ids) {
      const runtime = asRecord(runtimes[id]);
      const row = rows.get(id);
      if (row) {
        updateCameraRow(row, runtime);
      }
      const connection = connectionValue(runtime);
      if (connection === "connected") {
        connected += 1;
      } else if (connection === "connecting") {
        connecting += 1;
      } else if (connection === "error") {
        failed += 1;
      }
    }

    if (!ids.length) {
      setText(el.cameraSummary, "No cameras configured.");
    } else {
      const parts = [
        `${ids.length} camera${ids.length === 1 ? "" : "s"}`,
        `${connected} connected`,
      ];
      if (connecting) {
        parts.push(`${connecting} connecting`);
      }
      if (failed) {
        parts.push(`${failed} in error`);
      }
      setText(el.cameraSummary, parts.join(" · "));
    }
  }

  function buildDeviceRow(id, device, runtime) {
    const article = create("article", "entity");
    article.dataset.deviceId = id;
    const direction = device.direction === "microphone" ? "microphone" : "speaker";
    const directionLabel = direction === "microphone" ? "Microphone" : "Speaker";

    const header = create("div", "entity-header");
    const titleWrap = create("div", "entity-title-wrap");
    const subtitle = create("p", "entity-subtitle");
    subtitle.append(
      create("code", null, id),
      document.createTextNode(" · "),
      create("span", null, directionLabel)
    );
    titleWrap.append(
      create("h3", "entity-title", displayValue(device.name, id)),
      subtitle
    );
    header.append(titleWrap, create("span", `badge badge-${direction}`, directionLabel));

    const details = create("dl", "detail-grid");
    const routes = Array.isArray(device.cameras) ? device.cameras : [];
    details.append(
      createDetail(
        "Technical name",
        displayValue(asRecord(runtime).name),
        "technical"
      ),
      createDetail(
        "Camera routes",
        routes.length
          ? routes.map((cameraId) => cameraRouteLabel(cameraId)).join(", ")
          : "No camera routes",
        null,
        true
      )
    );

    const actions = create("div", "entity-actions");
    const switchLabel = create("label", "switch");
    const toggle = create("input");
    toggle.type = "checkbox";
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-label", `Enable ${displayValue(device.name, id)}`);
    toggle.dataset.role = "enabled";
    toggle.addEventListener(
      "change",
      safely(() => toggleDevice(id, toggle))
    );
    switchLabel.append(toggle, create("span", null, "Enabled"));
    actions.append(
      switchLabel,
      actionButton(
        "Edit",
        "button-secondary",
        () => beginDeviceEdit(id),
        `Edit device ${displayValue(device.name, id)}`
      ),
      actionButton(
        "Remove",
        "button-danger",
        () => removeDevice(id),
        `Remove device ${displayValue(device.name, id)}`
      )
    );

    article.append(header, details, actions);
    updateDeviceRow(article, currentDevices()[id], runtime);
    return article;
  }

  function updateDeviceRow(row, device, runtime) {
    const configured = asRecord(device);
    const enabled = configured.enabled !== false;
    const pending = state.pendingDeviceToggles.has(row.dataset.deviceId);
    const toggle = row.querySelector('[data-role="enabled"]');
    if (toggle) {
      if (!pending) {
        toggle.checked = enabled;
      }
      toggle.disabled = pending;
    }
    row.classList.toggle("is-disabled", !enabled);
    setRoleText(row, "technical", displayValue(asRecord(runtime).name));
  }

  function renderDevices() {
    const devices = currentDevices();
    const runtimes = asRecord(state.status && state.status.devices);
    const ids = Object.keys(devices).sort(compareText);
    const signature = JSON.stringify(
      ids.map((id) => {
        const device = asRecord(devices[id]);
        const routes = Array.isArray(device.cameras)
          ? device.cameras.map((cameraId) => String(cameraId))
          : [];
        return [
          id,
          displayValue(device.name, ""),
          displayValue(device.direction, ""),
          routes,
        ];
      })
    );

    if (signature !== state.signatures.deviceList) {
      state.signatures.deviceList = signature;
      if (!ids.length) {
        el.deviceList.replaceChildren(
          create("p", "empty-state", "No virtual devices configured yet.")
        );
      } else {
        const fragment = document.createDocumentFragment();
        for (const id of ids) {
          fragment.append(
            buildDeviceRow(id, asRecord(devices[id]), asRecord(runtimes[id]))
          );
        }
        el.deviceList.replaceChildren(fragment);
      }
    }

    const rows = new Map();
    for (const row of el.deviceList.querySelectorAll("[data-device-id]")) {
      rows.set(row.dataset.deviceId, row);
    }
    let enabled = 0;
    for (const id of ids) {
      const runtime = asRecord(runtimes[id]);
      const device = asRecord(devices[id]);
      const row = rows.get(id);
      if (row) {
        updateDeviceRow(row, device, runtime);
      }
      if (device.enabled !== false) {
        enabled += 1;
      }
    }

    if (!ids.length) {
      setText(el.deviceSummary, "No devices configured.");
    } else {
      setText(
        el.deviceSummary,
        `${ids.length} device${ids.length === 1 ? "" : "s"} · ${enabled} enabled`
      );
    }
  }

  function renderTts() {
    const tts = currentTts();
    const value = ttsStateValue();
    const badgeClass = TTS_STATE_CLASSES.includes(value) ? value : "idle";
    el.ttsState.className = `badge badge-${badgeClass}`;
    setText(el.ttsState, TTS_STATE_LABELS[value] || "Idle");

    const error = typeof tts.error === "string" ? tts.error : "";
    setText(el.ttsError, error);
    setVisible(el.ttsError, Boolean(error));

    const targets = Array.isArray(tts.devices)
      ? tts.devices.filter((id) => typeof id === "string")
      : [];
    if (targets.length) {
      setText(
        el.ttsActiveTargets,
        `Active targets: ${targets.map((id) => deviceDisplayName(id)).join(", ")}`
      );
    } else {
      setText(el.ttsActiveTargets, "Active targets: none");
    }
  }

  function syncSelectOptions(select, desired, emptyLabel) {
    const selected = new Set(
      Array.from(select.selectedOptions)
        .map((option) => option.value)
        .filter((value) => value !== "")
    );
    const desiredValues = new Set(desired.map((item) => item.value));
    const optionsByValue = new Map();
    for (const option of Array.from(select.options)) {
      if (desiredValues.has(option.value)) {
        optionsByValue.set(option.value, option);
      } else {
        option.remove();
      }
    }
    for (const item of desired) {
      let option = optionsByValue.get(item.value);
      if (!option) {
        option = create("option");
        option.value = item.value;
        optionsByValue.set(item.value, option);
      }
      if (option.textContent !== item.label) {
        option.textContent = item.label;
      }
      option.disabled = false;
      select.append(option);
    }
    for (const option of select.options) {
      option.selected = selected.has(option.value);
    }
    if (!desired.length) {
      for (const option of Array.from(select.options)) {
        option.remove();
      }
      const placeholder = create("option", null, emptyLabel);
      placeholder.value = "";
      placeholder.disabled = true;
      placeholder.selected = true;
      select.append(placeholder);
    }
    select.disabled = false;
  }

  function syncCameraOptions() {
    const cameras = currentCameras();
    const desired = Object.keys(cameras)
      .sort(compareText)
      .map((id) => ({
        value: id,
        label: `${id} — ${displayValue(asRecord(cameras[id]).url, "unknown URL")}`,
      }));
    const signature = JSON.stringify(desired);
    if (signature === state.signatures.cameraOptions) {
      return;
    }
    state.signatures.cameraOptions = signature;
    syncSelectOptions(el.deviceCameras, desired, "No cameras configured");
  }

  function syncTtsTargets() {
    const devices = currentDevices();
    const desired = Object.keys(devices)
      .filter((id) => {
        const device = asRecord(devices[id]);
        return device.direction === "speaker" && device.enabled === true;
      })
      .sort((left, right) => {
        const byName = compareText(
          deviceDisplayName(left),
          deviceDisplayName(right)
        );
        return byName || compareText(left, right);
      })
      .map((id) => ({
        value: id,
        label: `${deviceDisplayName(id)} (${id})`,
      }));
    const signature = JSON.stringify(desired);
    if (signature === state.signatures.ttsTargets) {
      return;
    }
    state.signatures.ttsTargets = signature;
    syncSelectOptions(
      el.ttsTargetsSelect,
      desired,
      "No enabled speaker devices"
    );
  }

  function updateTtsControls() {
    const hasTargets = Array.from(el.ttsTargetsSelect.options).some(
      (option) => !option.disabled && option.value !== ""
    );
    el.ttsSpeak.disabled =
      !state.authenticated || state.ttsPending || ttsBusy() || !hasTargets;
    el.ttsStop.disabled = !state.authenticated;
  }

  function updateCharCount() {
    setText(el.ttsCount, el.ttsText.value.length);
  }

  function clearValidity(input) {
    input.setCustomValidity("");
  }

  function cameraUrlProblem(value) {
    let parsed;
    try {
      parsed = new URL(value);
    } catch {
      return "Enter a complete HTTP or HTTPS camera URL.";
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "Camera URL must use HTTP or HTTPS.";
    }
    if (parsed.username || parsed.password) {
      return "Do not embed credentials in the camera URL.";
    }
    if (parsed.pathname && parsed.pathname !== "/") {
      return "Camera URL must not include a path.";
    }
    if (parsed.search || parsed.hash) {
      return "Camera URL must not include a query or fragment.";
    }
    if (!parsed.hostname) {
      return "Enter a camera host name or address.";
    }
    if (parsed.port !== "") {
      const port = Number(parsed.port);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        return "Camera URL port must be from 1 to 65535.";
      }
    }
    return "";
  }

  function readCameraForm() {
    const idInput = state.cameraEditingId || el.cameraId.value.trim();
    const url = el.cameraUrl.value.trim();
    const username = el.cameraUsername.value.trim();
    const channel = Number(el.cameraChannel.value);

    clearValidity(el.cameraUrl);
    clearValidity(el.cameraUsername);
    clearValidity(el.cameraChannel);

    const urlProblem = cameraUrlProblem(url);
    if (urlProblem) {
      el.cameraUrl.setCustomValidity(urlProblem);
    }
    if (!username) {
      el.cameraUsername.setCustomValidity("Enter the camera username.");
    } else if (/[\u0000-\u001F]/.test(username)) {
      el.cameraUsername.setCustomValidity(
        "Username must not contain control characters."
      );
    }
    if (!Number.isInteger(channel) || channel < 1 || channel > 65535) {
      el.cameraChannel.setCustomValidity(
        "Channel must be a whole number from 1 to 65535."
      );
    }

    if (!el.cameraForm.reportValidity()) {
      return null;
    }
    if (!idInput) {
      showError("Enter a camera ID.");
      el.cameraId.focus();
      return null;
    }
    return {
      id: idInput,
      camera: { url, username, channel },
      password: el.cameraPassword.value,
    };
  }

  function readDeviceForm() {
    const idInput = state.deviceEditingId || el.deviceId.value.trim();
    const name = el.deviceName.value.trim();
    const direction = el.deviceDirection.value;
    const routes = Array.from(el.deviceCameras.selectedOptions)
      .filter((option) => !option.disabled && option.value !== "")
      .map((option) => option.value);

    clearValidity(el.deviceName);
    clearValidity(el.deviceCameras);

    if (!name) {
      el.deviceName.setCustomValidity("Enter a device display name.");
    } else if (/[\u0000-\u001F]/.test(name)) {
      el.deviceName.setCustomValidity(
        "Device names must not contain control characters."
      );
    }

    if (!el.deviceForm.reportValidity()) {
      return null;
    }
    if (!idInput) {
      showError("Enter a device ID.");
      el.deviceId.focus();
      return null;
    }
    if (direction !== "speaker" && direction !== "microphone") {
      showError("Choose a valid device direction.");
      el.deviceDirection.focus();
      return null;
    }
    return {
      id: idInput,
      device: {
        name,
        direction,
        cameras: routes,
        enabled: el.deviceEnabled.checked,
      },
    };
  }

  function readTtsForm() {
    const text = el.ttsText.value;
    const voice = el.ttsVoice.value.trim();
    const targets = Array.from(el.ttsTargetsSelect.selectedOptions)
      .filter((option) => !option.disabled && option.value !== "")
      .map((option) => option.value);

    clearValidity(el.ttsText);
    clearValidity(el.ttsVoice);
    clearValidity(el.ttsTargetsSelect);

    if (!text.trim()) {
      el.ttsText.setCustomValidity("Enter the text to speak.");
    } else if (text.length > 2000) {
      el.ttsText.setCustomValidity("Speech text must be 2,000 characters or fewer.");
    } else if (XML_CONTROL_CHARACTERS.test(text)) {
      el.ttsText.setCustomValidity(
        "Remove unsupported control characters from the speech text."
      );
    }
    if (!voice) {
      el.ttsVoice.setCustomValidity("Enter a voice short name.");
    } else if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,99}$/.test(voice)) {
      el.ttsVoice.setCustomValidity(
        "Use a valid Edge voice short name, such as zh-CN-XiaoxiaoNeural."
      );
    }
    if (!targets.length) {
      el.ttsTargetsSelect.setCustomValidity(
        "Select at least one enabled speaker device."
      );
    }

    if (!el.ttsForm.reportValidity()) {
      return null;
    }
    return { text, voice, devices: targets };
  }

  function resetCameraForm(focus = false) {
    state.cameraEditingId = null;
    el.cameraForm.reset();
    el.cameraEditId.value = "";
    el.cameraId.disabled = false;
    clearValidity(el.cameraId);
    clearValidity(el.cameraUrl);
    clearValidity(el.cameraUsername);
    clearValidity(el.cameraChannel);
    setText(el.cameraEditorHeading, "Add camera");
    setText(el.cameraSubmit, "Save camera");
    el.cameraCancel.hidden = true;
    el.cameraPassword.value = "";
    setText(
      el.cameraPasswordHint,
      "Stored in the system Secret Service; never displayed or returned. When editing, leave blank to keep the existing password."
    );
    if (focus) {
      el.cameraId.focus();
    }
  }

  function beginCameraEdit(id) {
    const camera = asRecord(currentCameras()[id]);
    if (!Object.keys(camera).length) {
      return;
    }
    state.cameraEditingId = id;
    el.cameraEditId.value = id;
    el.cameraId.value = id;
    el.cameraId.disabled = true;
    el.cameraUrl.value = displayValue(camera.url, "");
    el.cameraUsername.value = displayValue(camera.username, "");
    el.cameraChannel.value = displayValue(camera.channel, "1");
    el.cameraPassword.value = "";
    el.cameraCancel.hidden = false;
    setText(el.cameraEditorHeading, `Edit camera ${id}`);
    setText(el.cameraSubmit, "Save changes");
    setText(
      el.cameraPasswordHint,
      "Leave blank to keep the existing password, or enter a new password to replace it. Passwords are never displayed or returned."
    );
    clearValidity(el.cameraId);
    clearValidity(el.cameraUrl);
    clearValidity(el.cameraUsername);
    clearValidity(el.cameraChannel);
    el.cameraUrl.focus();
  }

  async function onCameraSubmit(event) {
    event.preventDefault();
    if (!state.authenticated || state.cameraSaving || state.loginPending) {
      return;
    }
    clearAppMessages();
    const form = readCameraForm();
    if (!form) {
      return;
    }
    state.cameraSaving = true;
    el.cameraSubmit.disabled = true;
    try {
      const command = {
        op: "camera.save",
        id: form.id,
        camera: form.camera,
      };
      if (form.password !== "") {
        command.password = form.password;
      }
      await executeCommand(command);
      if (!state.authenticated) {
        return;
      }
      showNotice(`Camera ${form.id} saved; its connection will be refreshed.`);
      resetCameraForm();
      scheduleQuickPoll();
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not save camera: ${errorMessage(error)}`);
      }
    } finally {
      state.cameraSaving = false;
      el.cameraSubmit.disabled = false;
    }
  }

  async function removeCamera(id) {
    if (!state.authenticated) {
      return;
    }
    const devices = currentDevices();
    const references = Object.keys(devices).filter((deviceId) => {
      const routes = asRecord(devices[deviceId]).cameras;
      return Array.isArray(routes) && routes.includes(id);
    });
    let prompt = `Delete camera "${id}"? Its connection will be removed.`;
    if (references.length) {
      const names = references
        .map((deviceId) => `${deviceDisplayName(deviceId)} (${deviceId})`)
        .join(", ");
      prompt = `Camera "${id}" is used by ${names}. Remove those camera routes before deleting it. Continue?`;
    }
    if (!window.confirm(prompt)) {
      return;
    }
    clearAppMessages();
    try {
      await executeCommand({ op: "camera.remove", id });
      if (!state.authenticated) {
        return;
      }
      showNotice(`Camera ${id} removed.`);
      if (state.cameraEditingId === id) {
        resetCameraForm();
      }
      scheduleQuickPoll();
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not remove camera ${id}: ${errorMessage(error)}`);
      }
    }
  }

  async function reconnectCamera(id, button) {
    if (!state.authenticated) {
      return;
    }
    clearAppMessages();
    const previousLabel = button.textContent;
    button.disabled = true;
    setText(button, "Reconnecting...");
    try {
      await executeCommand({ op: "reconnect", id });
      if (!state.authenticated) {
        return;
      }
      showNotice(`Reconnect requested for camera ${id}.`);
      scheduleQuickPoll();
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not reconnect camera ${id}: ${errorMessage(error)}`);
      }
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        setText(button, previousLabel);
      }
    }
  }

  function resetDeviceForm(focus = false) {
    state.deviceEditingId = null;
    el.deviceForm.reset();
    el.deviceEditId.value = "";
    el.deviceId.disabled = false;
    clearValidity(el.deviceId);
    clearValidity(el.deviceName);
    clearValidity(el.deviceCameras);
    for (const option of el.deviceCameras.options) {
      option.selected = false;
    }
    setText(el.deviceEditorHeading, "Add device");
    setText(el.deviceSubmit, "Save device");
    el.deviceCancel.hidden = true;
    if (focus) {
      el.deviceId.focus();
    }
  }

  function beginDeviceEdit(id) {
    const device = asRecord(currentDevices()[id]);
    if (!Object.keys(device).length) {
      return;
    }
    syncCameraOptions();
    state.deviceEditingId = id;
    el.deviceEditId.value = id;
    el.deviceId.value = id;
    el.deviceId.disabled = true;
    el.deviceName.value = displayValue(device.name, "");
    el.deviceDirection.value =
      device.direction === "microphone" ? "microphone" : "speaker";
    const routes = new Set(
      Array.isArray(device.cameras) ? device.cameras : []
    );
    for (const option of el.deviceCameras.options) {
      option.selected = !option.disabled && routes.has(option.value);
    }
    el.deviceEnabled.checked = device.enabled !== false;
    el.deviceCancel.hidden = false;
    setText(el.deviceEditorHeading, `Edit device ${displayValue(device.name, id)}`);
    setText(el.deviceSubmit, "Save changes");
    clearValidity(el.deviceId);
    clearValidity(el.deviceName);
    clearValidity(el.deviceCameras);
    el.deviceName.focus();
  }

  async function onDeviceSubmit(event) {
    event.preventDefault();
    if (!state.authenticated || state.deviceSaving || state.loginPending) {
      return;
    }
    clearAppMessages();
    const form = readDeviceForm();
    if (!form) {
      return;
    }
    state.deviceSaving = true;
    el.deviceSubmit.disabled = true;
    try {
      await executeCommand({
        op: "device.save",
        id: form.id,
        device: form.device,
      });
      if (!state.authenticated) {
        return;
      }
      showNotice(`Device ${form.id} saved.`);
      resetDeviceForm();
      scheduleQuickPoll();
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not save device: ${errorMessage(error)}`);
      }
    } finally {
      state.deviceSaving = false;
      el.deviceSubmit.disabled = false;
    }
  }

  async function toggleDevice(id, toggle) {
    if (!state.authenticated || state.pendingDeviceToggles.has(id)) {
      return;
    }
    const enabled = toggle.checked;
    state.pendingDeviceToggles.add(id);
    toggle.disabled = true;
    clearAppMessages();
    try {
      await executeCommand({ op: "device.set", id, enabled });
      if (!state.authenticated) {
        return;
      }
      showNotice(`${deviceDisplayName(id)} ${enabled ? "enabled" : "disabled"}.`);
      scheduleQuickPoll();
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not update device ${id}: ${errorMessage(error)}`);
      }
    } finally {
      state.pendingDeviceToggles.delete(id);
      const device = asRecord(currentDevices()[id]);
      const row = Array.from(
        el.deviceList.querySelectorAll("[data-device-id]")
      ).find((candidate) => candidate.dataset.deviceId === id);
      if (row) {
        updateDeviceRow(row, device, asRecord((state.status?.devices || {})[id]));
      }
      if (toggle.isConnected) {
        toggle.disabled = false;
      }
    }
  }

  async function removeDevice(id) {
    if (!state.authenticated) {
      return;
    }
    const name = deviceDisplayName(id);
    if (
      !window.confirm(
        `Remove device "${name}" (${id})? Its audio routing will stop.`
      )
    ) {
      return;
    }
    clearAppMessages();
    try {
      await executeCommand({ op: "device.remove", id });
      if (!state.authenticated) {
        return;
      }
      showNotice(`Device ${name} removed.`);
      if (state.deviceEditingId === id) {
        resetDeviceForm();
      }
      scheduleQuickPoll();
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not remove device: ${errorMessage(error)}`);
      }
    }
  }

  async function onTtsSubmit(event) {
    event.preventDefault();
    if (!state.authenticated || state.ttsPending) {
      return;
    }
    if (ttsBusy()) {
      showError("Speech is already in progress. Stop it before starting another message.");
      return;
    }
    clearAppMessages();
    const form = readTtsForm();
    if (!form) {
      return;
    }
    state.ttsPending = true;
    updateTtsControls();
    try {
      await executeCommand({
        op: "tts.speak",
        text: form.text,
        devices: form.devices,
        voice: form.voice,
      });
      if (!state.authenticated) {
        return;
      }
      showNotice("Speech synthesis started.");
      scheduleQuickPoll(300);
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not start speech: ${errorMessage(error)}`);
      }
    } finally {
      state.ttsPending = false;
      updateTtsControls();
    }
  }

  async function stopSpeech() {
    if (!state.authenticated) {
      return;
    }
    clearAppMessages();
    try {
      await executeCommand({ op: "tts.stop" });
      if (!state.authenticated) {
        return;
      }
      showNotice("Speech stopped.");
      scheduleQuickPoll(300);
    } catch (error) {
      if (state.authenticated) {
        showError(`Could not stop speech: ${errorMessage(error)}`);
      }
    }
  }

  async function pollStatus() {
    if (
      !state.authenticated ||
      !state.token ||
      state.pollInFlight ||
      document.hidden
    ) {
      return;
    }
    state.pollInFlight = true;
    const tokenAtStart = state.token;
    const versionAtStart = state.version;
    try {
      const status = await api("/api/status");
      if (
        !state.authenticated ||
        state.token !== tokenAtStart ||
        versionAtStart !== state.version
      ) {
        return;
      }
      applyStatus(status);
      hideLiveWarning();
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 401
      ) {
        return;
      }
      if (!state.authenticated || state.token !== tokenAtStart) {
        return;
      }
      showLiveWarning(
        "Live status is temporarily unavailable. Showing the last known state."
      );
    } finally {
      state.pollInFlight = false;
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = window.setInterval(() => {
      if (!document.hidden) {
        pollStatus().catch(handleUnexpected);
      }
    }, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (state.quickPollTimer !== null) {
      window.clearTimeout(state.quickPollTimer);
      state.quickPollTimer = null;
    }
  }

  function scheduleQuickPoll(delay = 500) {
    if (!state.authenticated) {
      return;
    }
    if (state.quickPollTimer !== null) {
      window.clearTimeout(state.quickPollTimer);
    }
    state.quickPollTimer = window.setTimeout(() => {
      state.quickPollTimer = null;
      if (!document.hidden) {
        pollStatus().catch(handleUnexpected);
      }
    }, delay);
  }

  function showLoginPanel() {
    el.loginPanel.hidden = false;
    el.app.hidden = true;
    el.sessionTools.hidden = true;
  }

  function enterApp() {
    el.loginPanel.hidden = true;
    el.app.hidden = false;
    el.sessionTools.hidden = false;
    setLoginError("");
    setLoginStatus("");
    el.loginToken.value = "";
    el.camerasHeading.focus();
  }

  function loginErrorMessage(error, source) {
    if (error instanceof ApiError && error.status === 401) {
      if (source === "launch") {
        return "The access token from the launch link was rejected. Paste the current token to sign in.";
      }
      if (source === "saved") {
        return "The saved session token was rejected. Paste the current token to sign in.";
      }
      return "The access token was rejected. Check the token and try again.";
    }
    if (error instanceof ApiError) {
      return error.message;
    }
    return "Could not sign in. Reload the page and try again.";
  }

  function setLoginBusy(busy) {
    state.loginPending = busy;
    el.loginSubmit.disabled = busy;
    el.loginToken.disabled = busy;
    setText(el.loginSubmit, busy ? "Signing in..." : "Sign in");
  }

  async function attemptLogin(candidate, source) {
    if (state.loginPending || !candidate) {
      return;
    }
    setLoginBusy(true);
    setLoginError("");
    const checking =
      source === "launch"
        ? "Checking the launch access token..."
        : source === "saved"
          ? "Checking the saved access token..."
          : "Checking the access token...";
    setLoginStatus(checking);
    try {
      const status = await request("/api/status", {
        tokenValue: candidate,
        timeoutMs: LOGIN_TIMEOUT_MS,
      });
      state.token = candidate;
      state.authenticated = true;
      state.version += 1;
      tokenStorage.write(candidate);
      el.loginToken.value = "";
      applyStatus(status);
      hideLiveWarning();
      enterApp();
      showNotice("Connected to the audio bridge.");
      startPolling();
    } catch (error) {
      state.token = null;
      state.authenticated = false;
      tokenStorage.clear();
      showLoginPanel();
      setLoginError(loginErrorMessage(error, source));
    } finally {
      setLoginBusy(false);
      setLoginStatus("");
      if (!state.authenticated) {
        el.loginToken.focus();
      }
    }
  }

  async function onLoginSubmit(event) {
    event.preventDefault();
    if (state.loginPending) {
      return;
    }
    const candidate = el.loginToken.value.trim();
    if (!candidate) {
      setLoginError("Enter the access token.");
      el.loginToken.focus();
      return;
    }
    setLoginError("");
    await attemptLogin(candidate, "manual");
  }

  function signOut(message, options = {}) {
    stopPolling();
    state.authenticated = false;
    state.token = null;
    tokenStorage.clear();
    state.status = null;
    state.version += 1;
    state.pendingDeviceToggles.clear();
    state.cameraSaving = false;
    state.deviceSaving = false;
    state.ttsPending = false;
    for (const controller of state.requests) {
      controller.abort();
    }
    state.requests.clear();
    el.cameraSubmit.disabled = false;
    el.deviceSubmit.disabled = false;
    setLoginBusy(false);
    clearAppMessages();
    hideLiveWarning();
    resetCameraForm();
    resetDeviceForm();
    state.signatures.cameraList = "";
    state.signatures.deviceList = "";
    state.signatures.cameraOptions = "";
    state.signatures.ttsTargets = "";
    el.cameraList.replaceChildren(
      create("p", "empty-state", "No cameras configured yet.")
    );
    el.deviceList.replaceChildren(
      create("p", "empty-state", "No virtual devices configured yet.")
    );
    setText(el.cameraSummary, "No cameras configured.");
    setText(el.deviceSummary, "No devices configured.");
    el.deviceCameras.replaceChildren();
    el.ttsTargetsSelect.replaceChildren();
    el.ttsText.value = "";
    el.ttsText.setCustomValidity("");
    updateCharCount();
    el.ttsVoice.value = DEFAULT_VOICE;
    el.ttsVoice.setCustomValidity("");
    el.ttsTargetsSelect.setCustomValidity("");
    setText(el.ttsError, "");
    setVisible(el.ttsError, false);
    setText(el.ttsActiveTargets, "Active targets: none");
    el.ttsState.className = "badge badge-idle";
    setText(el.ttsState, "Idle");
    updateTtsControls();
    el.loginToken.value = "";
    showLoginPanel();
    setLoginError("");
    setLoginStatus("");
    if (message) {
      if (options.error === false) {
        setLoginStatus(message);
      } else {
        setLoginError(message);
      }
    } else {
      setLoginStatus("Enter the access token to continue.");
    }
    el.loginToken.focus();
  }

  function consumeFragmentToken() {
    if (!window.location.hash) {
      return "";
    }
    const fragment = window.location.hash.slice(1);
    try {
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${window.location.search}`
      );
    } catch {
      // A fragment is never sent to the server even when it cannot be rewritten.
    }
    try {
      const params = new URLSearchParams(fragment);
      return params.get("token") || "";
    } catch {
      return "";
    }
  }

  async function bootstrap() {
    const fragmentToken = consumeFragmentToken();
    const savedToken = tokenStorage.read();
    const candidate = fragmentToken || savedToken;
    if (!candidate) {
      showLoginPanel();
      setLoginStatus("Enter the access token to continue.");
      el.loginToken.focus();
      return;
    }
    showLoginPanel();
    await attemptLogin(
      candidate,
      fragmentToken ? "launch" : "saved"
    );
  }

  function bindEvents() {
    el.loginForm.addEventListener("submit", safely(onLoginSubmit));
    el.signOut.addEventListener(
      "click",
      safely(() => signOut("Signed out.", { error: false }))
    );
    el.cameraNew.addEventListener("click", safely(() => resetCameraForm(true)));
    el.cameraCancel.addEventListener(
      "click",
      safely(() => resetCameraForm(true))
    );
    el.cameraClear.addEventListener(
      "click",
      safely(() => resetCameraForm(true))
    );
    el.cameraForm.addEventListener("submit", safely(onCameraSubmit));
    el.deviceNew.addEventListener("click", safely(() => resetDeviceForm(true)));
    el.deviceCancel.addEventListener(
      "click",
      safely(() => resetDeviceForm(true))
    );
    el.deviceClear.addEventListener(
      "click",
      safely(() => resetDeviceForm(true))
    );
    el.deviceForm.addEventListener("submit", safely(onDeviceSubmit));
    el.ttsForm.addEventListener("submit", safely(onTtsSubmit));
    el.ttsStop.addEventListener("click", safely(stopSpeech));
    el.loginToken.addEventListener("input", () => {
      if (!el.loginError.hidden) {
        setLoginError("");
      }
    });
    el.ttsText.addEventListener("input", () => {
      clearValidity(el.ttsText);
      updateCharCount();
    });
    el.ttsVoice.addEventListener("input", () => clearValidity(el.ttsVoice));
    el.ttsTargetsSelect.addEventListener("change", () =>
      clearValidity(el.ttsTargetsSelect)
    );
    el.cameraId.addEventListener("input", () => clearValidity(el.cameraId));
    el.cameraUrl.addEventListener("input", () => clearValidity(el.cameraUrl));
    el.cameraUsername.addEventListener("input", () =>
      clearValidity(el.cameraUsername)
    );
    el.cameraChannel.addEventListener("input", () =>
      clearValidity(el.cameraChannel)
    );
    el.deviceId.addEventListener("input", () => clearValidity(el.deviceId));
    el.deviceName.addEventListener("input", () =>
      clearValidity(el.deviceName)
    );
    el.deviceCameras.addEventListener("change", () =>
      clearValidity(el.deviceCameras)
    );
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.authenticated) {
        pollStatus().catch(handleUnexpected);
      }
    });
  }

  function init() {
    bindEvents();
    showLoginPanel();
    resetCameraForm();
    resetDeviceForm();
    el.loginToken.value = "";
    el.ttsForm.reset();
    el.ttsText.setCustomValidity("");
    el.ttsVoice.setCustomValidity("");
    el.ttsTargetsSelect.setCustomValidity("");
    updateCharCount();
    updateTtsControls();
    safely(bootstrap)();
  }

  init();
})();
