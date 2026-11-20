/* Right-click menu and keyboard shortcut. The toolbar popup is in popup.js. */
"use strict";

const MENU_SELECTION = "send-selection";
const MENU_PAGE = "send-page";

browser.runtime.onInstalled.addListener(async ({ reason }) => {
  // Firefox for Android has no context menus.
  if (browser.menus) {
    await browser.menus.removeAll();
    browser.menus.create({ id: MENU_SELECTION, title: "Send selection to Language Reader", contexts: ["selection"] });
    browser.menus.create({ id: MENU_PAGE, title: "Send article to Language Reader", contexts: ["page"] });
  }
  if (reason === "install" && !(await getConfig()).server) browser.runtime.openOptionsPage();
});

if (browser.menus) {
  browser.menus.onClicked.addListener((info, tab) => {
    if (info.menuItemId === MENU_SELECTION) send(tab, "selection", info.selectionText);
    else if (info.menuItemId === MENU_PAGE) send(tab, "article");
  });
}

if (browser.commands) {
  browser.commands.onCommand.addListener(async (command) => {
    if (command !== "send-to-reader") return;
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    if (tab) send(tab, "auto");
  });
}

function notify(title, message) {
  browser.notifications.create({ type: "basic", iconUrl: "icons/icon.svg", title, message });
}

async function badge(tabId, text, color = "#2b5f8f") {
  try {
    await browser.action.setBadgeBackgroundColor({ color, tabId });
    await browser.action.setBadgeText({ text, tabId });
    if (text) setTimeout(() => browser.action.setBadgeText({ text: "", tabId }).catch(() => {}), 4000);
  } catch { /* tab closed */ }
}

/**
 * mode: "selection", "article", or "auto" (the selection if there is one,
 * else the article). menuText is the menu's copy of the selection, used when
 * the page can't be scripted; it has lost its line breaks.
 */
async function send(tab, mode, menuText = "") {
  const cfg = await getConfig();
  if (!cfg.server || !(await hasServerPermission(cfg.server))) {
    notify("Set up Language Reader", "Enter your server's address in the add-on's settings first.");
    browser.runtime.openOptionsPage();
    return;
  }
  badge(tab.id, "...");
  try {
    let page;
    if (mode === "article") {
      page = await grabFromTab(tab.id, "article");
    } else {
      try {
        page = await grabFromTab(tab.id, "selection");
      } catch (e) {
        if (!menuText) throw e;
        page = { text: menuText, title: "" };
      }
      if (!page.text.trim() && mode === "auto") page = await grabFromTab(tab.id, "article");
    }
    if (!page.text.trim()) {
      throw new ReaderError(mode === "article" ? "Couldn't find the article text on this page." : "Nothing is selected.");
    }
    const r = await sendText(cfg, { body: page.text, title: page.title || "", url: tab.url || "" });
    badge(tab.id, "OK", "#2e7d4f");
    if (cfg.openAfter) {
      browser.tabs.create({ url: readerUrl(cfg, r.id), index: tab.index + 1, openerTabId: tab.id });
    } else {
      notify("Sent to Language Reader", r.existing ? "It was already in your library." : "Added to your library.");
    }
  } catch (e) {
    badge(tab.id, "!", "#b3261e");
    const hint = e.status === 400 ? " To choose the language yourself, use the Language Reader toolbar button." : "";
    notify("Couldn't send to Language Reader", e.message + hint);
  }
}
