import { $, state, start, command, action, form, selected, options, button, card, list, badge, errorText, message } from "/common.js";

function clearCamera() {
  $("camera-form").reset();
  $("camera-id").readOnly = false;
  $("camera-editor-title").textContent = "Add camera";
}

function editCamera(id) {
  const camera = state.settings.cameras[id];
  if (!camera) return;
  $("camera-id").value = id;
  $("camera-id").readOnly = true;
  $("camera-url").value = camera.url;
  $("camera-username").value = camera.username;
  $("camera-channel").value = camera.channel;
  $("camera-password").value = "";
  $("camera-editor-title").textContent = `Edit camera: ${id}`;
  $("camera-url").focus();
}

form("camera-form", async () => {
  const value = {
    op: "camera.save", id: $("camera-id").value,
    camera: { url: $("camera-url").value, username: $("camera-username").value, channel: Number($("camera-channel").value) },
  };
  if ($("camera-password").value) value.password = $("camera-password").value;
  $("camera-password").value = "";
  await command(value);
  clearCamera();
});
$("camera-clear").addEventListener("click", clearCamera);
form("tts-form", async () => {
  const result = await command({
    op: "tts.speak", text: $("tts-text").value, voice: $("tts-voice").value,
    cameras: selected("tts-cameras"),
  });
  message("app-notice", `Announcement queued: ${result.queued_job}`);
});
$("tts-stop").addEventListener("click", action(() => command({ op: "tts.stop" })));
$("show-audio-token").addEventListener("click", action(async () => {
  const result = await command({ op: "access.show" });
  $("audio-token").value = result.audio_access_token;
  $("audio-token-panel").hidden = false;
}));
$("copy-audio-token").addEventListener("click", action(async () => {
  const input = $("audio-token");
  if (!input.value) return;
  try {
    if (!navigator.clipboard) throw new Error("Clipboard requires a secure context.");
    await navigator.clipboard.writeText(input.value);
    message("app-notice", "Audio token copied. Share only with trusted desktops.");
  } catch {
    input.focus();
    input.select();
    message("app-notice", "Audio token selected. Press Ctrl+C or Command+C to copy.");
  }
}));
document.addEventListener("audio-signout", () => {
  $("audio-token-panel").hidden = true;
  clearCamera();
});

start((status) => {
  const cameras = Object.entries(status.settings.cameras);
  list("camera-list", cameras, (id) => card([
    button("Edit", () => editCamera(id)),
    button("Reconnect", () => command({ op: "reconnect", id })),
    button("Remove", async () => {
      if (confirm(`Remove camera '${id}' from the server? Active routes and speech jobs must be removed first.`)) {
        await command({ op: "camera.remove", id });
      }
    }, true),
  ]), (node, camera, id) => {
    const connection = status.cameras[id] || {};
    node.parts.title.textContent = id;
    node.parts.subtitle.textContent = `${camera.url} · ${camera.username} · channel ${camera.channel}`;
    node.parts.detail.textContent = `${connection.codec || "Codec not connected"} · sent ${connection.sent_bytes || 0} bytes · received ${connection.received_bytes || 0} bytes`;
    badge(node.parts.badge, connection.connection || "idle");
    errorText(node.parts.error, [connection.error, connection.cleanup_error && `Cleanup: ${connection.cleanup_error}`].filter(Boolean).join(" · "));
  }, "Add a camera to make it available to desktops and TTS.");
  options("tts-cameras", cameras.map(([id]) => [id, `${id} — ${status.cameras[id]?.connection || "idle"}`]));
  list("session-list", Object.entries(status.sessions), (id) => card([
    button("Disconnect", async () => {
      if (confirm("Disconnect this desktop's audio session? Other desktops remain connected.")) {
        await command({ op: "session.disconnect", id });
      }
    }, true),
  ]), (node, session, id) => {
    node.parts.title.textContent = session.name;
    node.parts.subtitle.textContent = id;
    node.parts.detail.textContent = Object.entries(session.routes)
      .map(([device, route]) => `${device} (${route.direction}) → ${route.cameras.join(", ") || "no cameras"}`).join("; ") || "Connected without active devices.";
    badge(node.parts.badge, "connected");
  }, "No desktops connected. TTS works independently of desktop sessions.");
  const speech = status.tts;
  badge($("tts-state"), speech.state);
  const jobs = [
    ...(speech.current ? [[speech.current.id, { ...speech.current, stage: speech.state }]] : []),
    ...speech.queue.map((job, index) => [job.id, { ...job, stage: `queued #${index + 1}` }]),
  ];
  list("tts-jobs", jobs, (id) => card([
    button("Cancel", () => command({ op: "tts.cancel", id }), true),
  ]), (node, job) => {
    node.parts.title.textContent = job.cameras.join(", ");
    node.parts.subtitle.textContent = `${job.id} · ${job.voice}`;
    node.parts.detail.textContent = "Announcement text is not included in status responses.";
    badge(node.parts.badge, job.stage);
  }, `No announcements queued. Up to ${speech.capacity} requests may wait in the queue.`);
  $("tts-last").textContent = speech.last
    ? `Last announcement: ${speech.last.id} — ${speech.last.state}${speech.last.error ? `: ${speech.last.error}` : ""}` : "";
});
