/* Shared by the background script, the popup and the settings page. */
"use strict";

const DEFAULT_CONFIG = { server: "", token: "", openAfter: true };

async function getConfig() {
  const saved = await browser.storage.local.get(Object.keys(DEFAULT_CONFIG));
  return { ...DEFAULT_CONFIG, ...saved };
}

/** "homelab:8413/" -> "http://homelab:8413"; throws on anything that isn't a web address. */
function normaliseServer(raw) {
  let s = (raw || "").trim();
  if (!s) throw new Error("Enter the address of your Language Reader server.");
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s) && !/^https?:\/\//i.test(s)) {
    throw new Error("The address must start with http:// or https://");
  }
  if (!/^https?:\/\//i.test(s)) s = "http://" + s;
  let u;
  try {
    u = new URL(s);
  } catch {
    throw new Error("That isn't a web address. Use something like http://homelab:8413");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("The address must start with http:// or https://");
  return (u.origin + u.pathname).replace(/\/+$/, "");
}

/** Host permission pattern for the server. Match patterns can't carry a port. */
function originPattern(server) {
  const u = new URL(server);
  return `${u.protocol}//${u.hostname}/*`;
}

async function hasServerPermission(server) {
  try {
    return await browser.permissions.contains({ origins: [originPattern(server)] });
  } catch {
    return false;
  }
}

class ReaderError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

async function api(cfg, path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  let r;
  try {
    r = await fetch(cfg.server + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ReaderError(`Couldn't reach ${cfg.server}. Check that the server is running and reachable from this browser.`);
  }
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    if (r.status === 401) throw new ReaderError("The server rejected the access token. Check it in the add-on's settings.", 401);
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new ReaderError(detail || `The server answered ${r.status}.`, r.status);
  }
  return data;
}

function sendText(cfg, { body, title = "", url = "", lang = "auto" }) {
  return api(cfg, "/api/texts", { method: "POST", body: { lang, title, source_url: url, body } });
}

const readerUrl = (cfg, id) => `${cfg.server}/#/text/${id}`;

/* ------------------------------------------------------------------------ */
/* Page extraction. These run inside the page via scripting.executeScript,  */
/* so each must be self-contained.                                           */
/* ------------------------------------------------------------------------ */

function pageSelection() {
  const a = document.activeElement;
  if (a && (a.tagName === "TEXTAREA" || (a.tagName === "INPUT" && a.type === "text")) && a.selectionStart !== a.selectionEnd) {
    return a.value.slice(a.selectionStart, a.selectionEnd);
  }
  return String(window.getSelection() || "");
}

/** Needs vendor/Readability.js injected first. Keeps paragraph breaks. */
function pageArticle() {
  const fallbackTitle = document.title || "";
  let article = null;
  try {
    article = new Readability(document.cloneNode(true)).parse();
  } catch {
    article = null;
  }
  const root = article
    ? new DOMParser().parseFromString(article.content, "text/html").body
    : document.querySelector("article, main, [role=main]") || document.body;
  const BLOCK = /^(P|DIV|SECTION|ARTICLE|MAIN|HEADER|FOOTER|ASIDE|H[1-6]|UL|OL|LI|DL|DT|DD|BLOCKQUOTE|PRE|TABLE|TR|FIGURE|FIGCAPTION|HR)$/;
  const SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|SVG|BUTTON|NAV)$/;
  let out = "";
  const walk = (node) => {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) {
        out += n.data.replace(/\s+/g, " ");
      } else if (n.nodeType === 1 && !SKIP.test(n.tagName)) {
        if (n.tagName === "BR") { out += "\n"; continue; }
        const block = BLOCK.test(n.tagName);
        if (block) out += "\n\n";
        walk(n);
        if (block) out += "\n\n";
      }
    }
  };
  walk(root);
  const text = out.replace(/[ \t]*\n[ \t]*/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  return { title: (article && article.title) || fallbackTitle, text, pageLang: document.documentElement.lang || "" };
}

/** Text from a tab: mode "selection" or "article". */
async function grabFromTab(tabId, mode) {
  try {
    if (mode === "selection") {
      const [res] = await browser.scripting.executeScript({ target: { tabId }, func: pageSelection });
      return { text: (res && res.result) || "", title: "" };
    }
    await browser.scripting.executeScript({ target: { tabId }, files: ["vendor/Readability.js"] });
    const [res] = await browser.scripting.executeScript({ target: { tabId }, func: pageArticle });
    return (res && res.result) || { text: "", title: "" };
  } catch {
    throw new ReaderError("Firefox doesn't let add-ons read this page (built-in pages, the add-ons site and PDFs are off limits).");
  }
}
