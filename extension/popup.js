"use strict";

const $ = (id) => document.getElementById(id);
const status = (msg, kind = "") => { $("status").textContent = msg; $("status").className = "status " + kind; };

let cfg;
let tab;
const grabbed = { selection: null, article: null };
let mode = "selection";
let detectTimer;
let edited = false; // the text was changed by hand; don't overwrite it when switching mode

$("settings").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); window.close(); });
$("open-settings").addEventListener("click", () => { browser.runtime.openOptionsPage(); window.close(); });

(async () => {
  cfg = await getConfig();
  if (!cfg.server || !(await hasServerPermission(cfg.server))) {
    $("setup").style.display = "block";
    return;
  }
  $("main").style.display = "block";
  $("openAfter").checked = cfg.openAfter;
  [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  $("source").textContent = tab && tab.url ? new URL(tab.url).hostname : "";

  loadLanguages();
  try {
    grabbed.selection = await grabFromTab(tab.id, "selection");
  } catch (e) {
    grabbed.selection = { text: "", title: "" };
    grabbed.error = e.message;
  }
  const hasSelection = grabbed.selection.text.trim().length > 0;
  document.querySelector('[data-mode="selection"]').disabled = !hasSelection && !grabbed.error;
  await setMode(hasSelection ? "selection" : "article");
})();

async function loadLanguages() {
  try {
    const data = await api(cfg, "/api/languages");
    for (const l of data.configured) $("lang").append(new Option(l.name, l.code));
  } catch (e) {
    status(e.message, "error");
  }
}

async function setMode(m) {
  mode = m;
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  if (grabbed[m] === null) {
    status("Finding the article…");
    try {
      grabbed[m] = await grabFromTab(tab.id, m);
      status("");
    } catch (e) {
      grabbed[m] = { text: "", title: "" };
      status(e.message, "error");
    }
  } else if (grabbed.error) {
    status(grabbed.error + " You can paste the text instead.", "error");
  }
  if (!edited) $("body").value = grabbed[m].text;
  $("title").value = m === "article" ? grabbed[m].title || (tab && tab.title) || "" : "";
  textChanged();
}

document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
$("body").addEventListener("input", () => { edited = true; textChanged(); });
$("lang").addEventListener("change", textChanged);

function textChanged() {
  const n = $("body").value.trim().length;
  $("count").textContent = n ? `${n.toLocaleString()} characters` : "";
  $("send").disabled = !n;
  clearTimeout(detectTimer);
  detectTimer = setTimeout(detect, 250);
}

async function detect() {
  const hint = $("detect");
  hint.className = "hint";
  const text = $("body").value.trim();
  if ($("lang").value !== "auto" || !text) { hint.textContent = ""; return; }
  try {
    const d = await api(cfg, "/api/detect", { method: "POST", body: { text: text.slice(0, 4000) } });
    if (d.lang) hint.textContent = `Detected: ${d.name}`;
    else {
      hint.className = "hint warn";
      hint.textContent = d.guess_name
        ? `Looks like ${d.guess_name}, which isn't one of your languages. Choose one, or add it in the reader's Settings.`
        : "Couldn't tell the language. Choose one.";
    }
  } catch {
    hint.textContent = "";
  }
}

$("main").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = $("body").value;
  if (!body.trim()) return;
  $("send").disabled = true;
  status("Sending…");
  const openAfter = $("openAfter").checked;
  if (openAfter !== cfg.openAfter) browser.storage.local.set({ openAfter });
  try {
    const r = await sendText(cfg, { body, title: $("title").value.trim(), url: (tab && tab.url) || "", lang: $("lang").value });
    if (openAfter) {
      await browser.tabs.create({ url: readerUrl(cfg, r.id), index: tab ? tab.index + 1 : undefined });
      window.close();
      return;
    }
    $("status").className = "status ok";
    $("status").replaceChildren(
      r.existing ? "Already in your library. " : "Added to your library. ",
      Object.assign(document.createElement("a"), {
        href: readerUrl(cfg, r.id), textContent: "Open it", target: "_blank",
      }),
    );
  } catch (err) {
    status(err.message, "error");
    $("send").disabled = false;
  }
});
