// SERM Cookie Sync Popup Logic
const PLATFORMS_MAP = {
  "vc.ru": { id: "vc", name: "VC.ru", icon: "🔴", domain: ".vc.ru" },
  "spark.ru": { id: "spark", name: "Spark.ru", icon: "⚡️", domain: ".spark.ru" },
  "dzen.ru": { id: "dzen", name: "Дзен (Dzen.ru)", icon: "🟠", domain: ".dzen.ru" },
  "e-xecutive.ru": { id: "executive", name: "E-xecutive.ru", icon: "🔵", domain: ".e-xecutive.ru" },
  "habr.com": { id: "habr", name: "Хабр (Habr)", icon: "🔷", domain: ".habr.com" },
  "rb.ru": { id: "rb", name: "RB.RU", icon: "🟢", domain: ".rb.ru" }
};

// Server address and token are set by the user in the settings below (no built-in server)
const DEFAULT_CONFIG = {
  serverUrl: "",
  adminToken: "",
  autoSync: false
};

let currentConfig = { ...DEFAULT_CONFIG };
let activePlatform = null;

// Initialize popup
document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  initSettingsUI();
  await checkServerStatus();
  await detectActiveTab();
  await checkLocalCookiesStatus();
  initActionHandlers();
});

// Load settings from chrome.storage
async function loadSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
      currentConfig = items;
      document.getElementById("cfgServerUrl").value = currentConfig.serverUrl;
      document.getElementById("cfgAdminToken").value = currentConfig.adminToken;
      document.getElementById("cfgAutoSync").checked = currentConfig.autoSync;
      resolve();
    });
  });
}

function initSettingsUI() {
  const toggle = document.getElementById("settingsToggle");
  const body = document.getElementById("settingsBody");
  toggle.addEventListener("click", () => {
    const isHidden = body.style.display === "none";
    body.style.display = isHidden ? "block" : "none";
    toggle.querySelector(".toggle-arrow").textContent = isHidden ? "▲" : "▼";
  });

  document.getElementById("btnSaveSettings").addEventListener("click", () => {
    const serverUrl = document.getElementById("cfgServerUrl").value.trim().replace(/\/+$/, "");
    const adminToken = document.getElementById("cfgAdminToken").value.trim();
    const autoSync = document.getElementById("cfgAutoSync").checked;

    let origin;
    try {
      origin = new URL(serverUrl).origin;
    } catch (e) {
      logMessage("❌ Укажите адрес сервера, например https://serm.example.com");
      return;
    }

    // Host access is granted only for the configured server (optional_host_permissions in manifest);
    // the request must run inside the click handler (user gesture)
    chrome.permissions.request({ origins: [`${origin}/*`] }, (granted) => {
      if (!granted) {
        logMessage(`⚠️ Нет доступа к ${origin}: без разрешения расширение не сможет отправить куки.`);
      }
      currentConfig = { serverUrl, adminToken, autoSync };
      chrome.storage.local.set(currentConfig, () => {
        logMessage("✅ Настройки успешно сохранены!");
        checkServerStatus();
      });
    });
  });
}

// Check server connection
async function checkServerStatus() {
  const statusEl = document.getElementById("serverStatus");
  if (!currentConfig.serverUrl) {
    statusEl.innerHTML = '<span class="dot dot-yellow"></span> <span class="status-text">Нет сервера</span>';
    document.getElementById("settingsBody").style.display = "block";
    return false;
  }
  statusEl.innerHTML = '<span class="dot dot-yellow"></span> <span class="status-text">Связь...</span>';

  try {
    const res = await fetch(`${currentConfig.serverUrl}/api/status`, {
      headers: { "X-Admin-Token": currentConfig.adminToken }
    });
    if (res.ok) {
      statusEl.innerHTML = '<span class="dot dot-green"></span> <span class="status-text">Онлайн</span>';
      return true;
    } else {
      statusEl.innerHTML = '<span class="dot dot-red"></span> <span class="status-text">Ошибка ' + res.status + '</span>';
      return false;
    }
  } catch (err) {
    statusEl.innerHTML = '<span class="dot dot-red"></span> <span class="status-text">Офлайн</span>';
    return false;
  }
}

// Detect if current active tab is a supported platform
async function detectActiveTab() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs || tabs.length === 0) return;
    const url = tabs[0].url || "";

    for (const [host, meta] of Object.entries(PLATFORMS_MAP)) {
      if (url.includes(host)) {
        activePlatform = meta;
        const card = document.getElementById("activeTabCard");
        card.style.display = "block";
        document.getElementById("activeIcon").textContent = meta.icon;
        document.getElementById("activeName").textContent = meta.name;
        document.getElementById("activeDomain").textContent = host;
        return;
      }
    }
  });
}

// Check cookies availability in browser
async function checkLocalCookiesStatus() {
  for (const [key, meta] of Object.entries(PLATFORMS_MAP)) {
    const badge = document.getElementById(`badge-${meta.id}`);
    if (!badge) continue;

    chrome.cookies.getAll({ domain: meta.domain.replace(/^\./, "") }, (cookies) => {
      if (cookies && cookies.length > 0) {
        badge.textContent = `${cookies.length} кук`;
        badge.className = "plat-badge badge-ok";
      } else {
        badge.textContent = "Не авторизован";
        badge.className = "plat-badge badge-empty";
      }
    });
  }
}

// Sync cookies for a specific platform to the backend
async function syncPlatformCookies(platformId, domain) {
  if (!currentConfig.serverUrl) {
    logMessage("⚠️ Сначала укажите адрес сервера в настройках.");
    return { success: false, message: "No server configured" };
  }
  logMessage(`🔄 Извлечение кук для ${platformId} (${domain})...`);
  
  return new Promise((resolve) => {
    chrome.cookies.getAll({ domain: domain.replace(/^\./, "") }, async (cookies) => {
      if (!cookies || cookies.length === 0) {
        logMessage(`⚠️ Нет кук для ${platformId}. Войдите в аккаунт в браузере.`);
        resolve({ success: false, message: "No cookies found" });
        return;
      }

      const formattedCookies = cookies.map(c => ({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path,
        httpOnly: c.httpOnly,
        secure: c.secure,
        sameSite: c.sameSite === "no_restriction" ? "None" : (c.sameSite === "strict" ? "Strict" : "Lax")
      }));

      logMessage(`📤 Отправка ${formattedCookies.length} кук на сервер SERM Engine...`);

      try {
        const res = await fetch(`${currentConfig.serverUrl}/api/sessions/import`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Admin-Token": currentConfig.adminToken
          },
          body: JSON.stringify({
            platform: platformId,
            cookies: JSON.stringify(formattedCookies)
          })
        });

        const data = await res.json();
        if (data.authorized) {
          logMessage(`✅ [${platformId.toUpperCase()}] Сессия подтверждена! Авторизован как: ${data.user || 'OK'}`);
          const badge = document.getElementById(`badge-${platformId}`);
          if (badge) {
            badge.textContent = `✅ Активна`;
            badge.className = "plat-badge badge-ok";
          }
          resolve({ success: true, authorized: true });
        } else {
          logMessage(`⚠️ [${platformId.toUpperCase()}] Куки сохранены (${formattedCookies.length} шт), статус: ${data.message || 'Проверяется'}`);
          resolve({ success: true, authorized: false });
        }
      } catch (err) {
        logMessage(`❌ Ошибка отправки на сервер: ${err.message}`);
        resolve({ success: false, error: err.message });
      }
    });
  });
}

function initActionHandlers() {
  // Sync active tab
  const btnActive = document.getElementById("btnSyncActive");
  if (btnActive) {
    btnActive.addEventListener("click", async () => {
      if (!activePlatform) return;
      btnActive.disabled = true;
      btnActive.textContent = "⏳ Синхронизация...";
      await syncPlatformCookies(activePlatform.id, activePlatform.domain);
      btnActive.disabled = false;
      btnActive.textContent = "⚡️ Синхронизировать эту вкладку";
    });
  }

  // Sync single platform buttons
  document.querySelectorAll(".btn-sync-single").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const platId = btn.getAttribute("data-platform");
      const row = btn.closest(".platform-row");
      const domain = row.getAttribute("data-domain");

      btn.disabled = true;
      btn.textContent = "⏳...";
      await syncPlatformCookies(platId, domain);
      btn.disabled = false;
      btn.textContent = "Синхр.";
    });
  });

  // Sync ALL platforms button
  const btnSyncAll = document.getElementById("btnSyncAll");
  btnSyncAll.addEventListener("click", async () => {
    btnSyncAll.disabled = true;
    btnSyncAll.textContent = "⏳ Синхронизация всех...";
    logMessage("🚀 Старт синхронизации всех площадок...");

    const rows = document.querySelectorAll(".platform-row");
    for (const row of rows) {
      const platId = row.getAttribute("data-platform");
      const domain = row.getAttribute("data-domain");
      await syncPlatformCookies(platId, domain);
    }

    logMessage("🏁 Синхронизация всех площадок завершена!");
    btnSyncAll.disabled = false;
    btnSyncAll.textContent = "🚀 Синхронизировать ВСЕ сессии в 1 клик";
  });
}

function logMessage(msg) {
  const consoleEl = document.getElementById("logConsole");
  consoleEl.style.display = "block";
  const line = document.createElement("div");
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  consoleEl.appendChild(line);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}
