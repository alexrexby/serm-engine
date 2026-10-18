// SERM Cookie Sync Background Service Worker
const SUPPORTED_DOMAINS = [
  { id: "vc", domain: "vc.ru" },
  { id: "spark", domain: "spark.ru" },
  { id: "dzen", domain: "dzen.ru" },
  { id: "executive", domain: "e-xecutive.ru" }
];

// Server address and token are set by the user in the popup settings (no built-in server)
const DEFAULT_CONFIG = {
  serverUrl: "",
  adminToken: "",
  autoSync: false
};

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
    chrome.storage.local.set(items);
  });
});

// Update badge when user visits supported platform
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url) {
    const isSupported = SUPPORTED_DOMAINS.some(p => tab.url.includes(p.domain));
    if (isSupported) {
      chrome.action.setBadgeText({ tabId, text: "SYNC" });
      chrome.action.setBadgeBackgroundColor({ tabId, color: "#38bdf8" });
    } else {
      chrome.action.setBadgeText({ tabId, text: "" });
    }
  }
});

// Optional Auto-Sync listener on cookie changes
let autoSyncTimeout = null;
chrome.cookies.onChanged.addListener((changeInfo) => {
  chrome.storage.local.get(DEFAULT_CONFIG, (cfg) => {
    if (!cfg.autoSync || !cfg.serverUrl) return;

    const cookie = changeInfo.cookie;
    const match = SUPPORTED_DOMAINS.find(p => cookie.domain.includes(p.domain));
    if (!match) return;

    // Debounce sync so we don't spam on every single cookie update
    if (autoSyncTimeout) clearTimeout(autoSyncTimeout);
    autoSyncTimeout = setTimeout(async () => {
      syncPlatformInBackground(match.id, match.domain, cfg);
    }, 5000);
  });
});

async function syncPlatformInBackground(platformId, domain, cfg) {
  try {
    const cookies = await chrome.cookies.getAll({ domain });
    if (!cookies || cookies.length === 0) return;

    const formatted = cookies.map(c => ({
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      httpOnly: c.httpOnly,
      secure: c.secure,
      sameSite: c.sameSite === "no_restriction" ? "None" : (c.sameSite === "strict" ? "Strict" : "Lax")
    }));

    await fetch(`${cfg.serverUrl}/api/sessions/import`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Token": cfg.adminToken
      },
      body: JSON.stringify({
        platform: platformId,
        cookies: JSON.stringify(formatted)
      })
    });
    console.log(`[SERM Sync] Auto-synced ${formatted.length} cookies for ${platformId}`);
  } catch (e) {
    console.warn(`[SERM Sync] Auto-sync failed: ${e.message}`);
  }
}
