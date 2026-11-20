"use strict";

const $ = (id) => document.getElementById(id);
const status = (msg, kind = "") => { $("status").textContent = msg; $("status").className = "status " + kind; };

(async () => {
  const cfg = await getConfig();
  $("server").value = cfg.server;
  $("token").value = cfg.token;
  $("openAfter").checked = cfg.openAfter;
  if (cfg.server && !(await hasServerPermission(cfg.server))) {
    status("Firefox hasn't been allowed to reach this server yet. Click Save to allow it.", "error");
  }
  try {
    const cmd = (await browser.commands.getAll()).find((c) => c.name === "send-to-reader");
    $("shortcut").textContent = cmd && cmd.shortcut ? cmd.shortcut : "not set";
  } catch { /* no commands on Android */ }
})();

function readForm() {
  return { server: normaliseServer($("server").value), token: $("token").value.trim(), openAfter: $("openAfter").checked };
}

async function testConnection(cfg) {
  status("Connecting…");
  const data = await api(cfg, "/api/languages");
  const names = data.configured.map((l) => l.name).join(", ");
  status(`Connected. Your languages: ${names}.`, "ok");
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let cfg;
  try {
    cfg = readForm();
  } catch (err) {
    status(err.message, "error");
    return;
  }
  // permissions.request must be the first thing done in response to the click.
  let granted;
  try {
    granted = await browser.permissions.request({ origins: [originPattern(cfg.server)] });
  } catch (err) {
    status(err.message, "error");
    return;
  }
  await browser.storage.local.set(cfg);
  $("server").value = cfg.server;
  if (!granted) {
    status("Saved, but Firefox wasn't allowed to reach the server, so sending won't work until you allow it.", "error");
    return;
  }
  try {
    await testConnection(cfg);
  } catch (err) {
    status("Saved, but the test failed: " + err.message, "error");
  }
});

$("test").addEventListener("click", async () => {
  let cfg;
  try {
    cfg = readForm();
  } catch (err) {
    status(err.message, "error");
    return;
  }
  if (!(await hasServerPermission(cfg.server))) {
    status("Click Save first so Firefox can ask to allow this address.", "error");
    return;
  }
  try {
    await testConnection(cfg);
  } catch (err) {
    status(err.message, "error");
  }
});
