import { $, state, start, command, action, form, selected, options, button, card, list, badge, errorText } from "/common.js";

let initialized = false;
function backendForm() {
  const backend = state?.settings.backend;
  $("backend-url").value = backend?.url || "";
  $("backend-name").value = backend?.name || "Desktop";
  $("backend-ca").value = backend?.ca_file || "";
  $("backend-insecure").checked = backend?.allow_insecure_lan || false;
  $("backend-token").value = "";
}
function clearGroup() {
  $("group-form").reset();
  $("group-id").readOnly = false;
  $("group-editor-title").textContent = "Add camera group";
}
function clearDevice() {
  $("device-form").reset();
  $("device-id").readOnly = false;
  $("device-editor-title").textContent = "Add audio device";
}
function cameraChoices() {
  return Object.entries(state.cameras).map(([id, camera]) => [id, `${id} — ${camera.connection}`]);
}
function groupChoices() {
  return Object.entries(state.settings.groups).map(([id, group]) => [id, `${group.name} (${id})`]);
}
function editGroup(id) {
  const group = state.settings.groups[id];
  if (!group) return;
  $("group-id").value = id;
  $("group-id").readOnly = true;
  $("group-name").value = group.name;
  options("group-cameras", cameraChoices(), group.cameras);
  $("group-editor-title").textContent = `Edit camera group: ${id}`;
  $("group-name").focus();
}
function editDevice(id) {
  const device = state.settings.devices[id];
  if (!device) return;
  $("device-id").value = id;
  $("device-id").readOnly = true;
  $("device-name").value = device.name;
  $("device-direction").value = device.direction;
  $("device-enabled").checked = device.enabled;
  options("device-groups", groupChoices(), device.groups);
  $("device-editor-title").textContent = `Edit audio device: ${id}`;
  $("device-name").focus();
}

form("backend-form", async () => {
  const value = {
    op: "backend.save",
    backend: {
      url: $("backend-url").value, name: $("backend-name").value,
      ca_file: $("backend-ca").value.trim() || null,
      allow_insecure_lan: $("backend-insecure").checked,
    },
  };
  if ($("backend-token").value) value.token = $("backend-token").value;
  $("backend-token").value = "";
  await command(value);
});
$("backend-reconnect").addEventListener("click", action(() => command({ op: "reconnect" })));
$("backend-disconnect").addEventListener("click", action(async () => {
  if (confirm("Disconnect this desktop from the backend? Local groups and devices will be retained.")) {
    await command({ op: "backend.remove" });
    backendForm();
  }
}));
$("backend-reset").addEventListener("click", backendForm);
form("group-form", async () => {
  await command({ op: "group.save", id: $("group-id").value,
    group: { name: $("group-name").value, cameras: selected("group-cameras") } });
  clearGroup();
});
form("device-form", async () => {
  await command({ op: "device.save", id: $("device-id").value,
    device: { name: $("device-name").value, direction: $("device-direction").value,
      groups: selected("device-groups"), enabled: $("device-enabled").checked } });
  clearDevice();
});
$("group-clear").addEventListener("click", clearGroup);
$("device-clear").addEventListener("click", clearDevice);
document.addEventListener("audio-signout", () => {
  initialized = false;
  clearGroup();
  clearDevice();
});

start((status) => {
  if (!initialized) { backendForm(); initialized = true; }
  badge($("backend-state"), status.backend.connection);
  errorText($("backend-error"), status.backend.error);
  $("backend-transport").textContent = status.settings.backend
    ? `${status.settings.backend.url} · ${status.settings.backend.url.startsWith("http:") ? "Unencrypted audio transport" : "TLS-protected audio transport"}. Camera errors must be reconnected from the server's management UI.`
    : "No backend configured. Add cameras in the server WebUI, then connect this desktop with its audio access token.";
  options("group-cameras", cameraChoices());
  options("device-groups", groupChoices());
  list("group-list", Object.entries(status.settings.groups), (id) => card([
    button("Edit", () => editGroup(id)),
    button("Remove", async () => {
      if (confirm(`Remove group '${id}'? Remove it from all local devices first.`)) {
        await command({ op: "group.remove", id });
      }
    }, true),
  ]), (node, group, id) => {
    node.parts.title.textContent = group.name;
    node.parts.subtitle.textContent = id;
    node.parts.detail.textContent = group.cameras.map((camera) => `${camera}: ${status.cameras[camera]?.connection || "unavailable"}`).join(" · ") || "No cameras selected.";
    badge(node.parts.badge, "idle", `${group.cameras.length} cameras`);
  }, "Create a group from cameras advertised by the backend.");
  list("device-list", Object.entries(status.settings.devices), (id) => card([
    button("Toggle", () => command({ op: "device.set", id, enabled: !state.settings.devices[id].enabled })),
    button("Edit", () => editDevice(id)),
    button("Remove", async () => {
      if (confirm(`Remove local audio device '${id}'? Other devices remain available.`)) {
        await command({ op: "device.remove", id });
      }
    }, true),
  ]), (node, device, id) => {
    const runtime = status.devices[id];
    const ready = device.enabled && runtime.present && status.backend.connection === "connected"
      && runtime.cameras.length > 0 && runtime.cameras.every((camera) => status.cameras[camera]?.connection === "connected");
    node.parts.title.textContent = device.name;
    node.parts.subtitle.textContent = `${device.direction} · ${runtime.name}`;
    node.parts.detail.textContent = `Groups: ${device.groups.join(", ") || "none"}. Cameras: ${runtime.cameras.join(", ") || "none"}.`;
    node.parts.tools.firstElementChild.textContent = device.enabled ? "Disable" : "Enable";
    node.classList.toggle("is-disabled", !device.enabled);
    badge(node.parts.badge, ready ? "connected" : "idle", ready ? "Ready" : device.enabled ? "Waiting for route" : "Disabled");
  }, "Create one or more speakers and microphones. Device names appear in desktop audio settings.");
});
