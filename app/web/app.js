    (function () {
      // --- Auth state ---
      let currentUser = null;

      function isAuthenticated() { return currentUser !== null; }
      function isAdmin() { return currentUser && currentUser.role === 'admin'; }

      async function apiFetch(url, opts) {
        opts = opts || {};
        try {
          var res = await fetch(url, opts);
          if (res.status === 401) {
            var refreshed = await tryRefresh();
            if (refreshed) {
              res = await fetch(url, opts);
              return res;
            }
            showLogin();
            throw new Error('Unauthorized');
          }
          return res;
        } catch (e) {
          if (e.message === 'Unauthorized') throw e;
          throw e;
        }
      }

      // Single-flight guard: the refresh token is one-time-use and rotates on the
      // server, so if several requests 401 at once (which happens every ~15 min
      // when the access token expires and multiple polls fire together) each
      // firing its own /refresh would revoke the token for all but the first,
      // logging everyone out. Coalesce concurrent callers onto one in-flight call.
      var _refreshInFlight = null;
      function tryRefresh() {
        if (_refreshInFlight) return _refreshInFlight;
        var p = (async function () {
          try {
            var res = await fetch('/api/auth/refresh', { method: 'POST' });
            if (res.ok) {
              var data = await res.json();
              currentUser = data.user;
              updateAuthUI();
              return true;
            }
          } catch (e) {}
          return false;
        })();
        p.finally(function () { if (_refreshInFlight === p) _refreshInFlight = null; });
        _refreshInFlight = p;
        return p;
      }

      function showLogin() {
        // Логин теперь отдельная страница: аноним получает её от сервера по '/',
        // само приложение (HTML/JS/CSS) без валидной сессии не отдаётся вовсе.
        currentUser = null;
        window.location.replace('/');
      }

      function updateAuthUI() {
        var adminTab = document.getElementById('adminTab');
        if (adminTab) adminTab.style.display = isAdmin() ? '' : 'none';
        // Серверные секции вкладки «Настройки» — только админам (бэкенд всё
        // равно вернёт 403, это чисто про видимость).
        document.querySelectorAll('.settings-admin').forEach(function (el) {
          el.style.display = isAdmin() ? '' : 'none';
        });
        // Если вкладка настроек восстановилась из localStorage раньше, чем
        // пришёл /api/auth/me, данные ещё не загружены — догружаем теперь.
        if (isAdmin()) {
          var panel = document.querySelector('.view-panel[data-panel="settings"]');
          if (panel && panel.classList.contains('active')) {
            Promise.resolve().then(function () { loadServerSettings(); });
          }
        }
      }

      // Check auth on load
      (function initAuth() {
        fetch('/api/auth/me').then(function(res) {
          if (res.ok) return res.json();
          return null;
        }).then(function(data) {
          if (data) {
            currentUser = data.user;
            updateAuthUI();
            syncShowErrorsControl();
          } else {
            tryRefresh().then(function(refreshed) {
              if (!refreshed) showLogin();
              else syncShowErrorsControl();
            });
          }
        }).catch(function() {
          tryRefresh().then(function(refreshed) {
            if (!refreshed) showLogin();
            else syncShowErrorsControl();
          });
        });
      })();

      // ---- End auth ----

      const grid = document.getElementById("printerGrid");
      const sortBtn = document.getElementById("sortBtn");
      const sortDropdown = document.getElementById("sortDropdown");
      const sortBtnLabel = document.getElementById("sortBtnLabel");
      const alertsTopToggle = document.getElementById("alertsTopToggle");
      const SORT_STORAGE_KEY = "forge-ops-sort";
      const ALERTS_TOP_KEY = "forge-ops-alerts-top";
      let currentSort = "name";
      let alertsTop = true;
      const filtersEl = document.getElementById("filters");
      const kpiTotal = document.getElementById("kpiTotal");
      const kpiOnline = document.getElementById("kpiOnline");
      const kpiPrinting = document.getElementById("kpiPrinting");
      const kpiPaused = document.getElementById("kpiPaused");
      const kpiError = document.getElementById("kpiError");
      const kpiOffline = document.getElementById("kpiOffline");
      const metaUtil = document.getElementById("metaUtil");
      const metaNextFinish = document.getElementById("metaNextFinish");
      const metaFarmIdle = document.getElementById("metaFarmIdle");
      const metaNozzle = document.getElementById("metaNozzle");
      const metaBed = document.getElementById("metaBed");
      const statsList = document.getElementById("statsList");
      const eventLogEl = document.getElementById("eventLog");
      const eventLogEnd = document.getElementById("eventLogEnd");
      const eventLogLoader = document.getElementById("eventLogLoader");
      const viewTabs = Array.from(document.querySelectorAll(".view-tab"));
      const viewPanels = Array.from(document.querySelectorAll(".view-panel"));
      const lastUpdate = document.getElementById("lastUpdate");
      const filtersBtn = document.getElementById("filtersBtn");
      const filtersDropdown = document.getElementById("filtersDropdown");
      const filtersBtnBadge = document.getElementById("filtersBtnBadge");
      const settingsList = document.getElementById("settingsList");
      const notificationsEnabledToggle = document.getElementById("notificationsEnabledToggle");
      const notificationsFinishToggle = document.getElementById("notificationsFinishToggle");
      const notificationsErrorToggle = document.getElementById("notificationsErrorToggle");
      const notificationsPausedToggle = document.getElementById("notificationsPausedToggle");
      const showErrorsToggle = document.getElementById("showErrorsToggle");

      const stateFilter = new Set();
      let cachedAllPrinters = [];
      let lastFilteredPrinters = null;
      const FILTERS_STORAGE_KEY = "forge-ops-state-filters";
      const NOTIFICATION_SETTINGS_KEY = "forge-ops-notification-settings";
      const THEME_STORAGE_KEY = "forge-ops-theme";
      const SHOW_ERRORS_KEY = "forge-ops-show-errors";
      const DEFAULT_NOTIFICATION_SETTINGS = {
        enabled: false,
        finish: true,
        error: true,
        paused: true
      };
      function applyTheme(value) {
        if (value) {
          document.documentElement.setAttribute("data-theme", value);
        } else {
          document.documentElement.removeAttribute("data-theme");
        }
        var btns = document.querySelectorAll(".theme-seg-btn");
        btns.forEach(function (btn) {
          var themeVal = btn.getAttribute("data-theme");
          if ((themeVal || "") === (value || "")) {
            btn.classList.add("active");
          } else {
            btn.classList.remove("active");
          }
        });
        // Redraw timeline canvas if visible
        STATE_COLORS = getStateColors();
        var canvas = document.getElementById("timeline-canvas");
        if (canvas && timelineData.length > 0) {
          drawTimeline();
        }
      }
      function initTheme() {
        var stored = null;
        try { stored = localStorage.getItem(THEME_STORAGE_KEY); } catch (e) {}
        if (stored !== null) {
          applyTheme(stored);
          return;
        }
        if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
          applyTheme("");
        } else {
          applyTheme("light");
        }
      }
      function setTheme(value) {
        applyTheme(value);
        try { localStorage.setItem(THEME_STORAGE_KEY, value || ""); } catch (e) {}
      }
      const prevPrinterStates = {};
      let lastEventLogLoad = 0;
      const uiStatusFilters = [
        { id: "online", label: "Онлайн" },
        { id: "offline", label: "Офлайн" },
        { id: "printing", label: "В печати" },
        { id: "idle", label: "Простаивают" },
        { id: "error", label: "Ошибка" },
        { id: "paused", label: "Пауза" }
      ];
      
      var STATE_COLORS = getStateColors();
      
      let timelineData = [];
      let timelineRange = { from: null, to: null };
      var tickStep = 3600; // всегда пересчитывается в drawTimeline от видимого окна
      let timelinePrinter = null;
      let timelineOffset = 0;
      // Бекенд прореживает таймлайн (~800 точек на принтер на окно + все смены
      // состояний), так что сутки всей фермы — ~13k строк и влезают в одну
      // страницу: время ответа доминирует скан БД, а не передача, поэтому один
      // запрос на 20000 быстрее трёх по 5000. Бекенд принимает до 50000 —
      // границы согласованы, 422 не будет. Догрузка страницами остаётся
      // запасным путём на случай окна с аномально частыми сменами состояний.
      // Раньше один запрос на 50000 сырых строк весил ~4МБ и держал вкладку
      // 8-12 секунд.
      let timelineLimit = 20000;
      let timelineTotal = 0;
      let timelineHasMore = false;
      let timelineLoadGen = 0;      // диапазон/фильтр сменили — старые ответы в мусор
      let timelineLoadingMore = false;

      const imageMap = {
        "bambu-x1": "/static/img/bambu-x1c.png",
        "bambu-x1c": "/static/img/bambu-x1c.png",
        "bambu-x1e": "/static/img/bambu-x1c.png",
        "bambu-p1s": "/static/img/bambu-p2s.png",
        "bambu-p2s": "/static/img/bambu-p2s.png",
        "bambu-a1": "/static/img/bambu-a1.png",
        "bambu-a1mini": "/static/img/bambu-a1mini.png",
        "bambu-h2d": "/static/img/bambu-h2s.png",
        "bambu-h2s": "/static/img/bambu-h2s.png",
        "creality-k1max": "/static/img/creality-k1max.png",
        "creality-k1c": "/static/img/creality-k1c.png",
        "creality-ender5max": "/static/img/creality-ender5max.png",
        "klipper": "/static/img/generic-klipper.svg",
        "klipper-flyingbear_ghost6": "/static/img/flyingbear-ghost6-cutout.png",
        "mks-reborn2": "/static/img/flyingbear-reborn2-cutout.png",
        "mks-robin": "/static/img/flyingbear-reborn2-cutout.png"
      };
      function imgFor(printer) {
        const kind = (printer.kind || "").toLowerCase();
        const deviceType = (printer.device_type || "").toLowerCase();
        const key = kind + "-" + deviceType;
        return imageMap[key] || imageMap[kind] || imageMap["klipper"];
      }
      function loadShowErrors() {
        try {
          return localStorage.getItem(SHOW_ERRORS_KEY) === "true";
        } catch (e) {
          return false;
        }
      }
      function saveShowErrors(value) {
        try { localStorage.setItem(SHOW_ERRORS_KEY, value ? "true" : "false"); } catch (e) {}
      }
      function syncShowErrorsControl() {
        if (showErrorsToggle) showErrorsToggle.checked = loadShowErrors();
      }
      function loadNotificationSettings() {
        try {
          const raw = localStorage.getItem(NOTIFICATION_SETTINGS_KEY);
          if (!raw) return Object.assign({}, DEFAULT_NOTIFICATION_SETTINGS);
          return Object.assign({}, DEFAULT_NOTIFICATION_SETTINGS, JSON.parse(raw));
        } catch (error) {
          return Object.assign({}, DEFAULT_NOTIFICATION_SETTINGS);
        }
      }
      function saveNotificationSettings(settings) {
        try {
          localStorage.setItem(NOTIFICATION_SETTINGS_KEY, JSON.stringify(settings));
        } catch (error) {}
      }
      function syncNotificationControls() {
        var settings = loadNotificationSettings();
        notificationsEnabledToggle.checked = !!settings.enabled;
        notificationsFinishToggle.checked = !!settings.finish;
        notificationsErrorToggle.checked = !!settings.error;
        notificationsPausedToggle.checked = !!settings.paused;
        settingsList.classList.toggle("master-off", !settings.enabled);
      }
      function showBrowserNotification(title, body) {
        if (!("Notification" in window) || Notification.permission !== "granted") return;
        try {
          new Notification(title, {
            body: body,
            icon: "/static/img/bambu-x1c.png"
          });
        } catch (error) {}
      }
      function closeSortPopup() {
        if (sortDropdown) sortDropdown.classList.remove("open");
      }
      function openFiltersPopup() {
        if (!filtersDropdown) return;
        closeSortPopup();
        filtersDropdown.classList.add("open");
        if (filtersBtn) filtersBtn.setAttribute("aria-expanded", "true");
      }
      function closeFiltersPopup() {
        if (!filtersDropdown) return;
        filtersDropdown.classList.remove("open");
        if (filtersBtn) filtersBtn.setAttribute("aria-expanded", "false");
      }
      function toggleFiltersPopup() {
        if (!filtersDropdown) return;
        if (filtersDropdown.classList.contains("open")) closeFiltersPopup();
        else openFiltersPopup();
      }
      async function updateNotificationSetting(key, value) {
        var settings = loadNotificationSettings();
        if (key === "enabled") {
          if (value) {
            if (!("Notification" in window)) {
              settings.enabled = false;
              saveNotificationSettings(settings);
              syncNotificationControls();
              showToast("Браузер не поддерживает уведомления", false);
              return;
            }
            if (Notification.permission === "default") {
              try {
                const permission = await Notification.requestPermission();
                if (permission !== "granted") {
                  settings.enabled = false;
                  saveNotificationSettings(settings);
      syncNotificationControls();
      syncShowErrorsControl();
                  showToast(permission === "denied" ? "Доступ к уведомлениям запрещён" : "Разрешение не выдано", false);
                  return;
                }
              } catch (error) {
                settings.enabled = false;
                saveNotificationSettings(settings);
                syncNotificationControls();
                showToast("Не удалось запросить разрешение", false);
                return;
              }
            } else if (Notification.permission === "denied") {
              settings.enabled = false;
              saveNotificationSettings(settings);
              syncNotificationControls();
              var httpsUrl = "https://" + window.location.hostname;
              showToast("Уведомления не работают через HTTP. Откройте " + httpsUrl, false);
              return;
            }
          }
          settings.enabled = value && ("Notification" in window) && Notification.permission === "granted";
        } else {
          settings[key] = value;
        }
        saveNotificationSettings(settings);
        syncNotificationControls();
      }
      function hasTelemetry(p) {
        return p.nozzle_temp != null
          || p.bed_temp != null
          || p.progress_pct != null
          || p.job_name
          || p.current_layer != null
          || p.total_layers != null;
      }
      function isOffline(p) {
        return !p.online || p.state === "offline" || (p.state === "unknown" && !hasTelemetry(p));
      }
      function computeUiStatus(p) {
        if (isOffline(p)) return "offline";
        if (p.state === "printing") return "printing";
        if (p.state === "error") return "error";
        if (p.state === "paused") return "paused";
        if (p.state === "finished") return "finished";
        if (p.state === "idle") return "idle";
        return "unknown";
      }
      function statusLabel(status) {
        const labels = {
          printing: "В ПЕЧАТИ",
          idle: "ПРОСТОЙ",
          error: "ОШИБКА",
          paused: "ПАУЗА",
          finished: "ЗАВЕРШЕНО",
          unknown: "НЕИЗВЕСТНО",
          offline: "ОФЛАЙН"
        };
        return labels[status] || "—";
      }
      // Перевод этапа печати (stage) на русский. Ключи — значения pybambu
      // CURRENT_STAGE_IDS (Bambu) в нижнем регистре. Возвращает null, если
      // этап показывать не нужно (дублирует основной статус или пустой).
      const STAGE_LABELS = {
        auto_bed_leveling: "Калибровка стола",
        heatbed_preheating: "Нагрев стола",
        sweeping_xy_mech_mode: "Калибровка осей XY",
        changing_filament: "Смена филамента",
        m400_pause: "Пауза (M400)",
        paused_filament_runout: "Пауза: закончился филамент",
        heating_hotend: "Нагрев сопла",
        calibrating_extrusion: "Калибровка экструзии",
        scanning_bed_surface: "Сканирование стола",
        inspecting_first_layer: "Проверка первого слоя",
        identifying_build_plate_type: "Определение типа стола",
        calibrating_micro_lidar: "Калибровка лидара",
        homing_toolhead: "Парковка головы",
        cleaning_nozzle_tip: "Очистка сопла",
        checking_extruder_temperature: "Проверка температуры сопла",
        paused_user: "Пауза (пользователь)",
        paused_front_cover_falling: "Пауза: открыта крышка",
        calibrating_extrusion_flow: "Калибровка потока",
        paused_nozzle_temperature_malfunction: "Пауза: ошибка темп. сопла",
        paused_heat_bed_temperature_malfunction: "Пауза: ошибка темп. стола",
        filament_unloading: "Выгрузка филамента",
        paused_skipped_step: "Пауза: пропуск шагов",
        filament_loading: "Загрузка филамента",
        calibrating_motor_noise: "Калибровка шума моторов",
        paused_ams_lost: "Пауза: потеря AMS",
        paused_low_fan_speed_heat_break: "Пауза: низкие обороты вентилятора",
        paused_chamber_temperature_control_error: "Пауза: ошибка темп. камеры",
        cooling_chamber: "Охлаждение камеры",
        paused_user_gcode: "Пауза (G-code)",
        motor_noise_showoff: "Калибровка моторов",
        paused_nozzle_filament_covered_detected: "Пауза: сопло в филаменте",
        paused_cutter_error: "Пауза: ошибка ножа",
        paused_first_layer_error: "Пауза: ошибка первого слоя",
        paused_nozzle_clog: "Пауза: засор сопла"
      };
      function stageLabel(stage) {
        if (stage == null) return null;
        const raw = String(stage).trim();
        if (!raw) return null;
        const key = raw.toLowerCase();
        // Этапы, дублирующие основной статус карточки, не показываем.
        if (/^(printing|paused|idle|standby|complete|completed|error|finished|finish|cancelled|canceled|ready|unknown)$/.test(key)) {
          return null;
        }
        if (STAGE_LABELS[key]) return STAGE_LABELS[key];
        // Неизвестный этап: если это техническая строка вида some_stage_name —
        // приводим к читаемому виду, иначе показываем как есть (напр. сообщение Klipper).
        if (/^[a-z0-9_]+$/.test(key)) {
          return raw.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
        }
        return raw;
      }
      function timeFmt(seconds) {
        if (seconds == null || !isFinite(seconds)) return "—";
        const sec = Math.max(0, Math.floor(seconds));
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const parts = [];
        if (h) parts.push(h + "ч");
        if (m) parts.push(m + "м");
        return parts.join(" ") || "—";
      }
      function progressOffset(pct) {
        const circumference = 339.292;
        if (pct == null || !isFinite(pct) || pct <= 0) return circumference;
        return circumference - (circumference * Math.max(0, Math.min(100, pct)) / 100);
      }
      function fmtAvg(value, suffix) {
        return value == null || !isFinite(value) ? "—" : value.toFixed(0) + suffix;
      }
      function safeText(value, fallback) {
        if (value == null || value === "") return fallback;
        // Device-supplied strings (job names, errors, labels) are interpolated
        // into innerHTML, so HTML-escape here to neutralize a malicious/buggy
        // printer reporting markup like <img src=x onerror=...>.
        return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
      }
      function loadPersistedFilters() {
        try {
          const raw = localStorage.getItem(FILTERS_STORAGE_KEY);
          if (!raw) return;
          const parsed = JSON.parse(raw);
          if (!Array.isArray(parsed)) return;
          parsed.forEach((value) => {
            if (uiStatusFilters.some((filter) => filter.id === value)) {
              stateFilter.add(value);
            }
          });
        } catch (error) {
          console.warn("Failed to restore filters:", error);
        }
      }
      function persistFilters() {
        try {
          localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(Array.from(stateFilter)));
        } catch (error) {
          console.warn("Failed to persist filters:", error);
        }
      }
      function applyFilters(allPrinters) {
        if (stateFilter.size === 0) return allPrinters;
        return allPrinters.filter((p) => {
          const offlineFlag = isOffline(p);
          if (stateFilter.has("online") && !offlineFlag) return true;
          if (stateFilter.has("offline") && offlineFlag) return true;
          if (stateFilter.has("printing") && p.state === "printing") return true;
          if (stateFilter.has("error") && p.state === "error") return true;
          if (stateFilter.has("paused") && p.state === "paused") return true;
          if (stateFilter.has("idle") && (p.state === "idle" || p.state === "finished" || p.state === "unknown")) return true;
          return false;
        });
      }
      function sortPrinters(printers) {
        const sortKey = currentSort;
        const alerts = alertsTop;
        // Единый порядок групп. «offline» обязан быть в списке: раньше его тут
        // не было, он получал вес 0 наравне с «idle» — и офлайн-карточки
        // перемешивались с простаивающими по алфавиту.
        const GROUP = { error: 5, paused: 4, finished: 3, printing: 2, idle: 1, unknown: 1, offline: 0 };
        return printers.slice().sort((a, b) => {
          const ua = computeUiStatus(a);
          const ub = computeUiStatus(b);
          const ga = GROUP[ua] || 0;
          const gb = GROUP[ub] || 0;
          if (alerts && ga !== gb) return gb - ga;
          if (sortKey === "eta") {
            const etaA = (a.eta_seconds > 0) ? a.eta_seconds : 86400;
            const etaB = (b.eta_seconds > 0) ? b.eta_seconds : 86400;
            if (etaA !== etaB) return etaA - etaB;
            // Все без ETA равны (86400) — не даём алфавиту перемешать статусы:
            // простой выше, офлайн в самом хвосте.
            if (ga !== gb) return gb - ga;
          }
          return String(a.label || "").localeCompare(String(b.label || ""), "ru", { numeric: true });
        });
      }
      function renderFilters() {
        if (filtersBtnBadge) {
          filtersBtnBadge.textContent = String(stateFilter.size);
          filtersBtnBadge.style.display = stateFilter.size > 0 ? "" : "none";
        }
        filtersEl.innerHTML = "";
        uiStatusFilters.forEach((filter) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "filter-btn" + (stateFilter.has(filter.id) ? " active" : "");
          btn.textContent = filter.label;
          btn.addEventListener("click", () => {
            if (stateFilter.has(filter.id)) {
              stateFilter.delete(filter.id);
            } else {
              stateFilter.add(filter.id);
            }
            persistFilters();
            renderFilters();
            applyAndRender();
          });
          filtersEl.appendChild(btn);
        });
      }

      function setActiveView(view) {
        viewTabs.forEach((tab) => {
          tab.classList.toggle("active", tab.dataset.view === view);
        });
        viewPanels.forEach((panel) => {
          panel.classList.toggle("active", panel.dataset.panel === view);
        });
        try { localStorage.setItem("forge-ops-active-view", view); } catch (e) {}
        if (view === 'history') { loadTimeline(); }
        if (view === 'dashboard') { loadStatsSummary(); }
        if (view === 'forecast') { renderForecast(); }
        if (view === 'ams') { Promise.resolve().then(function () { loadAms(); }); }
        // Загрузка и при клике, и при восстановлении вкладки после перезагрузки
        // страницы (раньше данные грузились только по клику на таб). Микротаска:
        // при восстановлении setActiveView выполняется до того, как IIFE присвоит
        // var-переменные админ-блока (AUDIT_LIMIT и др.) — прямой вызов ловил
        // limit=undefined и 422 от аудита.
        if (view === 'admin') {
          Promise.resolve().then(function () { loadAdminPrinters(); loadAdminUsers(); loadAdminAudit(true); });
        }
        if (view === 'settings') {
          Promise.resolve().then(function () { loadServerSettings(); });
        }
      }
      function loadSortPrefs() {
        try {
          const savedSort = localStorage.getItem(SORT_STORAGE_KEY);
          if (savedSort === "name" || savedSort === "eta") {
            currentSort = savedSort;
          }
          const savedAlerts = localStorage.getItem(ALERTS_TOP_KEY);
          if (savedAlerts !== null) {
            alertsTop = savedAlerts === "true";
          }
        } catch (e) {}
        if (sortBtnLabel) {
          sortBtnLabel.textContent = "Сортировка: " + (currentSort === "name" ? "По имени (А-Я)" : "По времени до конца");
        }
        if (alertsTopToggle) {
          alertsTopToggle.checked = alertsTop;
        }
        document.querySelectorAll(".sort-option").forEach((opt) => {
          opt.classList.toggle("active", opt.dataset.sort === currentSort);
        });
      }

      if (sortBtn) {
        sortBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          closeFiltersPopup();
          sortDropdown.classList.toggle("open");
        });
      }

      document.addEventListener("click", () => {
        closeSortPopup();
      });

      if (sortDropdown) {
        sortDropdown.addEventListener("click", (e) => {
          const opt = e.target.closest(".sort-option");
          if (!opt) return;
          e.stopPropagation();
          const value = opt.dataset.sort;
          if (value) {
            currentSort = value;
            sortBtnLabel.textContent = "Сортировка: " + (currentSort === "name" ? "По имени (А-Я)" : "По времени до конца");
            sortDropdown.classList.remove("open");
            document.querySelectorAll(".sort-option").forEach((o) => {
              o.classList.toggle("active", o.dataset.sort === currentSort);
            });
            try { localStorage.setItem(SORT_STORAGE_KEY, currentSort); } catch (e) {}
            if (lastFilteredPrinters) {
              const sorted = sortPrinters(lastFilteredPrinters);
              renderFleet(sorted);
            }
          }
        });
      }

      if (alertsTopToggle) {
        alertsTopToggle.addEventListener("change", () => {
          alertsTop = alertsTopToggle.checked;
          try { localStorage.setItem(ALERTS_TOP_KEY, String(alertsTop)); } catch (e) {}
          if (lastFilteredPrinters) {
            const sorted = sortPrinters(lastFilteredPrinters);
            renderFleet(sorted);
          }
        });
      }
      let eventLogOffset = 0;
      const EVENT_LOG_LIMIT = 50;
      let eventLogHasMore = false;
      let eventLogLoading = false;
      let eventPrinterFilter = ""; // printer_id, "" = все
      let eventStateFilter = "";   // new_state, "" = все

      const EVENT_STATE_LABELS = {
        idle: "Простой", printing: "Печатает", paused: "Пауза",
        finished: "Завершил", error: "Ошибка", offline: "Офлайн", unknown: "Неизвестно"
      };

      async function loadEventLog() {
        if (eventLogLoading) return;
        eventLogLoading = true;
        const from = Date.now() / 1000 - 86400;
        const to = Date.now() / 1000;
        const el = document.getElementById("eventLog");
        if (!el) { eventLogLoading = false; return; }

        if (eventLogLoader) eventLogLoader.style.display = "";

        try {
          var url = "/api/history/events?fr=" + from + "&to=" + to + "&limit=" + EVENT_LOG_LIMIT + "&offset=" + eventLogOffset;
          if (eventPrinterFilter) url += "&printer_id=" + encodeURIComponent(eventPrinterFilter);
          if (eventStateFilter) url += "&state=" + encodeURIComponent(eventStateFilter);
          const resp = await apiFetch(url);
          const data = await resp.json();
          const events = data.rows || [];
          eventLogHasMore = data.has_more;

          if (eventLogOffset === 0) {
            el.innerHTML = "";
          }

          el.innerHTML += events.map(function(e) {
            const dt = new Date(e.time * 1000);
            const time = dt.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) + " " + dt.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
            const st = e.new_state || "unknown";
            let desc = "";
            if (e.job_name) {
              desc += '<span class="event-job" data-tip="' + safeText(e.job_name, "") + '">' + safeText(jobShortName(e.job_name), "") + '</span>';
            }
            if (e.last_error && st === "error") {
              desc += (desc ? " · " : "") + '<span class="event-err">' + escHtml(e.last_error) + '</span>';
            }
            return '<div class="event-row">' +
              '<span class="event-time">' + time + '</span>' +
              '<span class="event-printer">' + escHtml(e.label) + '</span>' +
              '<span class="event-badge event-badge-' + st + '">' + escHtml(EVENT_STATE_LABELS[st] || st) + '</span>' +
              '<span class="event-desc">' + desc + '</span>' +
            "</div>";
          }).join("");

          if (eventLogOffset === 0 && !events.length) {
            el.innerHTML = '<div class="dash-stats-empty">Нет событий по выбранным фильтрам.</div>';
          }
          eventLogOffset += events.length;

          if (eventLogLoader) eventLogLoader.style.display = eventLogHasMore ? "" : "none";
          if (eventLogEnd) eventLogEnd.style.display = eventLogHasMore ? "none" : "";
        } catch (e) {
          console.error("loadEventLog failed", e);
        } finally {
          eventLogLoading = false;
        }
      }

      function reloadEventLog() {
        eventLogOffset = 0;
        eventLogHasMore = false;
        loadEventLog();
        lastEventLogLoad = Date.now();
      }

      function initEventLogFilters() {
        var btn = document.getElementById("eventPrinterBtn");
        var dropdown = document.getElementById("eventPrinterDropdown");
        var labelEl = document.getElementById("eventPrinterLabel");
        var stateBtns = document.getElementById("eventStateBtns");
        if (btn && dropdown) {
          btn.addEventListener("click", function (e) {
            e.stopPropagation();
            if (dropdown.classList.contains("open")) {
              dropdown.classList.remove("open");
              return;
            }
            // Список принтеров собираем при каждом открытии — из живого кэша.
            var opts = '<button type="button" class="sort-option' + (eventPrinterFilter ? '' : ' active') + '" data-pid="">Все принтеры</button>';
            cachedAllPrinters.slice().sort(function (a, b) {
              return String(a.label || "").localeCompare(String(b.label || ""), "ru", { numeric: true });
            }).forEach(function (p) {
              opts += '<button type="button" class="sort-option' + (eventPrinterFilter === p.id ? ' active' : '') + '" data-pid="' + safeText(p.id, "") + '">' + safeText(p.label, p.id) + '</button>';
            });
            dropdown.innerHTML = opts;
            dropdown.classList.add("open");
          });
          dropdown.addEventListener("click", function (e) {
            var opt = e.target.closest(".sort-option");
            if (!opt) return;
            e.stopPropagation();
            eventPrinterFilter = opt.dataset.pid || "";
            if (labelEl) labelEl.textContent = opt.textContent;
            dropdown.classList.remove("open");
            reloadEventLog();
          });
          document.addEventListener("click", function () {
            dropdown.classList.remove("open");
          });
        }
        if (stateBtns) {
          stateBtns.addEventListener("click", function (e) {
            var b = e.target.closest(".event-state-btn");
            if (!b) return;
            eventStateFilter = b.dataset.state || "";
            stateBtns.querySelectorAll(".event-state-btn").forEach(function (x) {
              x.classList.toggle("active", x === b);
            });
            reloadEventLog();
          });
        }
      }

      function setupInfiniteScroll() {
        var sentinel = document.getElementById("eventLogSentinel");
        if (sentinel) {
          var obs = new IntersectionObserver(function(entries) {
            if (entries[0].isIntersecting && eventLogHasMore && !eventLogLoading) {
              loadEventLog();
            }
          }, { rootMargin: "200px" });
          obs.observe(sentinel);
        }
        var scrollBtn = document.getElementById("scrollTopBtn");
        window.addEventListener("scroll", function() {
          var scrollY = window.scrollY || window.pageYOffset;
          if (scrollBtn) {
            scrollBtn.classList.toggle("visible", scrollY > window.innerHeight * 0.4);
          }
        }, { passive: true });
        if (scrollBtn) {
          scrollBtn.addEventListener("click", function() {
            window.scrollTo({ top: 0, behavior: "smooth" });
          });
        }
      }
      function renderSummary(allPrinters) {
        const totalCount = allPrinters.length;
        // Online is the exact complement of offline (same isOffline() the cards
        // and filters use) — otherwise a printer that's online-but-without-
        // telemetry (state 'unknown') was counted in BOTH KPIs.
        const offlineCount = allPrinters.filter((p) => isOffline(p)).length;
        const onlineCount = allPrinters.length - offlineCount;
        const printingCount = allPrinters.filter((p) => p.state === "printing").length;
        const pausedCount = allPrinters.filter((p) => p.state === "paused").length;
        const errorCount = allPrinters.filter((p) => p.state === "error").length;
        const utilization = totalCount ? Math.round((printingCount / totalCount) * 100) : 0;

        if (kpiTotal) kpiTotal.textContent = totalCount;
        if (kpiOnline) kpiOnline.textContent = onlineCount;
        if (kpiPrinting) kpiPrinting.textContent = printingCount;
        if (kpiPaused) kpiPaused.textContent = pausedCount;
        if (kpiError) kpiError.textContent = errorCount;
        if (kpiOffline) kpiOffline.textContent = offlineCount;
        if (metaUtil) metaUtil.textContent = utilization + "%";

        const printingWithEta = allPrinters.filter((p) => p.state === "printing" && p.progress_pct != null && p.eta_seconds != null && p.eta_seconds > 0);
        const nextFinishSeconds = printingWithEta.length
          ? Math.min.apply(null, printingWithEta.map((p) => p.eta_seconds))
          : null;
        const nextFinishPrinter = nextFinishSeconds != null
          ? printingWithEta.find((p) => p.eta_seconds === nextFinishSeconds)
          : null;
        const farmIdleAtSeconds = printingWithEta.length
          ? Math.max.apply(null, printingWithEta.map((p) => p.eta_seconds))
          : null;

        if (metaNextFinish) {
          metaNextFinish.textContent = nextFinishSeconds != null
            ? timeFmt(nextFinishSeconds) + " \u00B7 " + nextFinishPrinter.label
            : "\u2014";
        }
        if (metaFarmIdle) {
          metaFarmIdle.textContent = farmIdleAtSeconds != null
            ? "через " + timeFmt(farmIdleAtSeconds)
            : "ферма свободна";
        }

        const nozzleValues = allPrinters.map((p) => p.nozzle_temp).filter((v) => v != null && isFinite(v));
        const bedValues = allPrinters.map((p) => p.bed_temp).filter((v) => v != null && isFinite(v));
        const avgNozzleValue = nozzleValues.length
          ? nozzleValues.reduce((sum, v) => sum + Number(v), 0) / nozzleValues.length
          : null;
        const avgBedValue = bedValues.length
          ? bedValues.reduce((sum, v) => sum + Number(v), 0) / bedValues.length
          : null;
        if (metaNozzle) metaNozzle.textContent = fmtAvg(avgNozzleValue, "\u00B0C");
        if (metaBed) metaBed.textContent = fmtAvg(avgBedValue, "\u00B0C");
      }

      const STATE_ORDER = ["printing", "idle", "paused", "error", "finished", "offline"];
      const STATE_LABELS_RU = {
        printing: "печатал", idle: "простой", paused: "пауза",
        error: "ошибка", finished: "завершено", offline: "офлайн", unknown: "неизвестно"
      };
      let currentStatsPeriod = 604800; // 7 days default
      let statsLoading = false;

      async function loadStatsSummary() {
        if (statsLoading) return;
        statsLoading = true;
        var loader = document.getElementById('statesSummaryLoader');
        if (loader) loader.style.display = '';
        const to = Date.now() / 1000;
        const fr = to - currentStatsPeriod;
        try {
          const resp = await apiFetch("/api/history/states-summary?fr=" + fr + "&to=" + to);
          const data = await resp.json();
          if (!statsList) return;
          if (!data || data.length === 0) {
            statsList.innerHTML = '<div class="dash-stats-empty">Нет данных за выбранный период.</div>';
            return;
          }
          statsList.innerHTML = data.map(function(item) {
            const currentState = item.current_state || "unknown";
            const states = item.states || {};
            const totalSecs = STATE_ORDER.reduce(function(sum, s) { return sum + (states[s] || 0); }, 0);
            var segs = "";
            STATE_ORDER.forEach(function(s) {
              var sec = states[s] || 0;
              if (!totalSecs || sec <= 0) return;
              var pct = sec / totalSecs * 100;
              segs += '<i class="dash-seg seg-' + s + '" style="width:' + pct.toFixed(2) + '%" data-tip="' +
                STATE_LABELS_RU[s] + ' · ' + timeFmt(sec) + ' (' + pct.toFixed(1) + '%)"></i>';
            });
            var printPct = totalSecs ? ((states.printing || 0) / totalSecs * 100) : 0;
            return '<div class="dash-stats-row">' +
              '<span class="dash-stats-label"><span class="dash-stats-dot dot-' + currentState + '"></span>' + safeText(item.label, item.printer_id) + '</span>' +
              '<span class="dash-stats-bar">' + segs + '</span>' +
              '<span class="dash-stats-key"><b>' + printPct.toFixed(1) + '%</b> печать</span>' +
            '</div>';
          }).join("");
        } catch (e) {
          console.error("loadStatsSummary failed", e);
          if (statsList) {
            statsList.innerHTML = '<div class="dash-stats-empty">Не удалось загрузить статистику.</div>';
          }
        } finally {
          statsLoading = false;
          if (loader) loader.style.display = 'none';
        }
      }

      function initStatsPeriodButtons() {
        document.querySelectorAll(".stats-period-btn").forEach(function(btn) {
          btn.addEventListener("click", function() {
            document.querySelectorAll(".stats-period-btn").forEach(function(b) { b.classList.remove("active"); });
            btn.classList.add("active");
            currentStatsPeriod = parseInt(btn.dataset.period, 10);
            loadStatsSummary();
          });
        });
      }

      // Клик по KPI-карточке = включить соответствующий фильтр на вкладке «Принтеры».
      function initKpiClicks() {
        document.querySelectorAll(".dash-kpi").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var f = btn.dataset.kpiFilter;
            stateFilter.clear();
            if (f) stateFilter.add(f);
            persistFilters();
            renderFilters();
            applyAndRender();
            setActiveView("fleet");
          });
        });
      }

      function icon(name) {
        return '<svg class="pc-ic" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
      }
      function jobShortName(name) {
        var base = String(name || "").split(/[\\/]/).pop();
        return base.replace(/\.(gcode|gco|3mf)$/i, "");
      }
      function fmtClock(ts) {
        return new Date(ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      }
      // Cold → gray, heating toward target → yellow with →target, at working temp → warm.
      function tempChip(icName, name, cur, target, editable) {
        if (cur == null || !isFinite(cur)) return "";
        var val = Math.round(cur);
        var cls = "pc-chip";
        var arrow = "";
        var hasTarget = target != null && target > 0;
        if (hasTarget && Math.round(target) !== val) {
          arrow = ' <span class="pc-target">→' + Math.round(target) + '°</span>';
        }
        if (hasTarget && Math.abs(cur - target) > 3) cls += " pc-heating";
        else if (val >= 40) cls += " pc-hot";
        var edit = editable ? ' pc-editable" data-edit="' + icName + '" data-cur="' + (hasTarget ? Math.round(target) : val) : '';
        return '<span class="' + cls + edit + '" data-tip="' + name + '">' + icon(icName) + val + '°' + arrow + '</span>';
      }

      function fanChip(p, editable) {
        var part = p.fan_speed_pct != null ? p.fan_speed_pct : null;
        var f = p.fans || {};
        var vals = [part, f.aux, f.chamber, f.heatbreak].filter(function (v) { return v != null; });
        // на управляемом принтере чип показываем всегда (можно включить обдув с нуля)
        if (!editable && (!vals.length || Math.max.apply(null, vals) <= 0)) return "";
        var tipParts = [];
        if (part != null) tipParts.push("🌀 " + part + "%");
        if (f.aux != null) tipParts.push("💨 " + f.aux + "%");
        if (f.chamber != null) tipParts.push("📦 " + f.chamber + "%");
        if (f.heatbreak != null) tipParts.push("🔥 " + f.heatbreak + "%");
        var main = part != null ? part : (vals.length ? Math.max.apply(null, vals) : 0);
        var edit = editable
          ? ' pc-editable" data-edit="fan" data-part="' + (part || 0) + '" data-aux="' + (f.aux || 0) + '" data-chamber="' + (f.chamber || 0)
          : '';
        return '<span class="pc-chip' + edit + '" data-tip="' + (tipParts.join(" · ") || "Вентилятор") + '" style="color:hsl(' + Math.round(120 + main * 0.8) + ',70%,55%)">' + icon("wind") + main + '%</span>';
      }
      function amsChip(p) {
        var a = p.ams;
        if (!a || !a.units || !a.units.length) return "";
        var trayNow = a.tray_now;
        var parts = [];
        var globalIdx = 0;
        var drying = false;
        for (var u = 0; u < a.units.length; u++) {
          var unit = a.units[u];
          if (unit.dry_time != null && unit.dry_time > 0) drying = true;
          var slots = unit.slots || [];
          for (var i = 0; i < slots.length; i++) {
            var slot = slots[i];
            // A printer can't feed from an empty slot: when printing from the
            // external spool the P2S keeps tray_now pointing at slot 0 instead
            // of reporting 254, so an empty "active" slot must not get a ring.
            var active = trayNow != null && trayNow === globalIdx && !slot.empty;
            var tip;
            if (slot.empty) {
              tip = "Слот " + (globalIdx + 1) + " · пусто";
            } else {
              var remain = slot.remain_pct != null && slot.remain_pct >= 0
                ? "остаток " + slot.remain_pct + "%"
                : "остаток: н/д";
              tip = "Слот " + (globalIdx + 1) + " · " + safeText(slot.type, "?")
                + (slot.name ? " · " + safeText(slot.name, "") : "")
                + " · " + remain;
            }
            var hexColor = (typeof slot.color === "string" && /^[0-9a-fA-F]{3,8}$/.test(slot.color)) ? slot.color : "";
            parts.push('<span class="pc-ams-slot' + (slot.empty ? " empty" : "") + (active ? " active" : "") + '"'
              + (hexColor ? ' style="background:#' + hexColor + '"' : "")
              + ' data-tip="' + tip + '"></span>');
            globalIdx++;
          }
          if (unit.humidity_pct != null || unit.humidity != null) {
            var pct = unit.humidity_pct;
            var hcls, htxt;
            if (pct != null) {
              hcls = pct <= 25 ? "ok" : pct <= 40 ? "warn" : "bad";
              htxt = "💧" + pct + "%";
            } else {
              // older AMS units report only the 5-level index
              hcls = unit.humidity <= 2 ? "ok" : unit.humidity === 3 ? "warn" : "bad";
              htxt = "💧" + unit.humidity + "/5";
            }
            var htip = "AMS: влажность " + (pct != null ? pct + "%" : "уровень " + unit.humidity + "/5");
            parts.push('<span class="pc-ams-hum pc-ams-hum-' + hcls + '" data-tip="' + htip + '">' + htxt + '</span>');
          }
          if (unit.temp != null) {
            var ttip = unit.dry_time != null && unit.dry_time > 0 ? "Сушка AMS" : "Температура AMS";
            parts.push('<span class="pc-ams-temp" data-tip="' + ttip + '">' + icon("steam") + Math.round(unit.temp) + '°</span>');
          }
        }
        return '<span class="pc-chip pc-ams' + (drying ? " pc-ams-drying" : "") + '"><span class="pc-ams-lbl" data-tip="AMS">' + icon("spool") + '</span>' + parts.join("") + '</span>';
      }
      function renderPrinterCard(p) {
        const uiStatus = computeUiStatus(p);
        const offlineFlag = isOffline(p);
        const isPrinting = p.state === "printing";
        const progress = Math.max(0, Math.min(100, Number(p.progress_pct || 0)));
        const showProgress = isPrinting || p.progress_pct != null;
        const ringOffset = progressOffset(showProgress ? progress : 0);

        var wifi = "";
        if (p.wifi_signal != null) {
          var lvl = p.wifi_signal >= -50 ? "good" : p.wifi_signal >= -70 ? "ok" : "weak";
          var bars = lvl === "good" ? "wifi-3" : lvl === "ok" ? "wifi-2" : "wifi-1";
          wifi = '<span class="pc-wifi pc-wifi-' + lvl + '" data-tip="WiFi ' + p.wifi_signal + ' dBm">' + icon(bars) + p.wifi_signal + '</span>';
        }
        var eta = "";
        if (p.eta_seconds != null && p.eta_seconds > 0) {
          eta = timeFmt(p.eta_seconds) + ' <span class="pc-finish">\u2192 ' + fmtClock(Date.now() + p.eta_seconds * 1000) + '</span>';
        }
        var layers = (p.current_layer != null && p.current_layer > 0) || (p.total_layers != null && p.total_layers > 0)
          ? '<span class="pc-layers" data-tip="\u0421\u043B\u043E\u0439">' + icon("layers") + safeText(p.current_layer, "?") + '/' + safeText(p.total_layers, "?") + '</span>'
          : "";
        var job = p.job_name
          ? '<span class="pc-job" title="' + safeText(p.job_name, "") + '">' + icon("file") + '<span class="pc-job-text">' + safeText(jobShortName(p.job_name), "\u2014") + '</span></span>'
          : "";
        var stage = stageLabel(p.stage);
        var speedMode = p.feedrate_pct == null || p.feedrate_pct <= 0 ? null
          : p.feedrate_pct < 100 ? "slow" : p.feedrate_pct > 124 ? "turbo" : p.feedrate_pct > 100 ? "fast" : "norm";
        var speedIcon = speedMode === "slow" ? ' \u2193' : speedMode === "turbo" ? ' \uD83D\uDE80' : speedMode === "fast" ? ' \u26A1' : '';
        // print_cmds === true — зонд ПОДТВЕРДИЛ, что прошивка принимает print-класс.
        // null (ещё не проверено) и false — не даём редактировать: иначе на 2S/заблок.
        // прошивках чипы кликаются, а команда потом отклоняется с ошибкой.
        var canEdit = isAdmin() && p.kind === "bambu" && !offlineFlag && p.print_cmds === true;
        var speedEdit = canEdit ? ' pc-editable" data-edit="speed" data-cur="' + (p.feedrate_pct || 100) : '';
        var chips = [
          tempChip("nozzle", "\u0421\u043E\u043F\u043B\u043E", p.nozzle_temp, p.target_nozzle_temp, canEdit),
          tempChip("bed", "\u0421\u0442\u043E\u043B", p.bed_temp, p.target_bed_temp, canEdit),
          p.chamber_temp != null ? '<span class="pc-chip' + (p.chamber_temp > 55 ? " pc-heating" : "") + '" data-tip="\u041A\u0430\u043C\u0435\u0440\u0430">' + icon("temp") + Math.round(p.chamber_temp) + '\u00B0</span>' : "",
          fanChip(p, canEdit),
          speedMode ? '<span class="pc-chip pc-speed-' + speedMode + speedEdit + '" data-tip="\u0421\u043A\u043E\u0440\u043E\u0441\u0442\u044C \u043F\u0435\u0447\u0430\u0442\u0438">' + icon("gauge") + p.feedrate_pct + '%' + speedIcon + '</span>' : "",
        ].filter(Boolean).join("");

        // Панель управления рендерится ТОЛЬКО когда зонд подтвердил приём
        // print-класса (=== true). null/false — панели нет вовсе (не пустой блок),
        // чтобы карточка не росла и не предлагала команды, которые упадут на 2S.
        var controls = "";
        if (isAdmin() && p.kind === "bambu" && !offlineFlag && p.print_cmds === true) {
          var printing = p.state === "printing";
          var pausedSt = p.state === "paused";
          var printBtns =
            (pausedSt
              ? '<button type="button" class="pc-ctl" data-cmd="resume" data-tip="Продолжить">▶</button>'
              : '<button type="button" class="pc-ctl" data-cmd="pause" data-tip="Пауза"' + (printing ? "" : " disabled") + '>⏸</button>') +
            '<button type="button" class="pc-ctl pc-ctl-danger" data-cmd="stop" data-tip="Стоп"' + (printing || pausedSt ? "" : " disabled") + '>⏹</button>' +
            '<button type="button" class="pc-ctl" data-cmd="svc" data-tip="Сервис">' + icon("gear") + '</button>';
          controls = '<div class="pc-controls">' + printBtns + '</div>';
        }

        return `
          <article class="printer-card${offlineFlag ? " offline" : ""}${" " + uiStatus}" data-id="${p.id}" data-state="${uiStatus}" data-kind="${p.kind}" data-label="${safeText(p.label, "")}">
            <div class="pc-head">
              <div class="printer-visual">
                <svg class="progress-ring" viewBox="0 0 120 120" aria-hidden="true">
                  <circle class="bg" cx="60" cy="60" r="54"></circle>
                  <circle class="fg" cx="60" cy="60" r="54" style="stroke-dashoffset:${ringOffset}"></circle>
                </svg>
                <div class="printer-thumb">
                  <img src="${imgFor(p)}" alt="${safeText(p.label, "Принтер")}">
                </div>
              </div>
              <div class="pc-headmain">
                <div class="pc-toprow">
                  <div class="printer-name" title="${safeText(p.label, "")}">${safeText(p.label, "Без имени")}${p.grace_period_active ? '<span class="grace-dot" title="Нет связи, данные устарели"></span>' : ''}</div>
                  <div class="pc-toprow-right">
                    ${wifi}
                    <div class="status-badge ${uiStatus}">${statusLabel(uiStatus)}</div>
                  </div>
                </div>
                <div class="pc-subrow">
                  ${amsChip(p) || '<span class="printer-meta">' + safeText((p.kind || "").toUpperCase(), "UNKNOWN") + ' · ' + safeText((p.device_type || "").toUpperCase(), "GENERIC") + '</span>'}
                </div>
              </div>
            </div>
            ${showProgress ? `
            <div class="pc-progress">
              <div class="pc-statrow">
                <span class="pc-pct">${Math.round(progress)}%</span>
                ${eta ? '<span class="pc-eta">' + eta + '</span>' : ''}
                ${layers}
              </div>
              <div class="progress-track">
                <div class="progress-fill" style="width:${progress}%"></div>
              </div>
              ${job ? '<div class="pc-jobrow">' + job + '</div>' : ''}
              ${stage ? '<div class="stage-badge">' + safeText(stage, "") + '</div>' : ''}
            </div>` : ""}
            ${chips ? '<div class="pc-chips">' + chips + '</div>' : ""}
            ${controls}
            ${p.last_error && loadShowErrors() ? '<div class="alert-box"><div class="alert-value">' + safeText(p.last_error, "—") + '</div></div>' : ""}
          </article>
        `;
      }
      // Keyed per-card reconcile: only cards whose markup changed are replaced,
      // untouched cards keep their DOM nodes (hover, transitions) between polls.
      var fleetCardCache = new Map();
      function renderFleet(printers) {
        if (!printers.length) {
          fleetCardCache.clear();
          var emptyMarkup = '<div class="empty-state"><strong>Нет устройств для выбранного фильтра</strong>Снимите часть фильтров или дождитесь новых данных телеметрии.</div>';
          if (grid.innerHTML !== emptyMarkup) grid.innerHTML = emptyMarkup;
          return;
        }
        var emptyEl = grid.querySelector(".empty-state");
        if (emptyEl) emptyEl.remove();
        var seen = new Set();
        var prev = null;
        printers.forEach(function (p) {
          var id = String(p.id);
          seen.add(id);
          var markup = renderPrinterCard(p);
          var el = grid.querySelector('.printer-card[data-id="' + id + '"]');
          if (!el || fleetCardCache.get(id) !== markup) {
            var tpl = document.createElement("template");
            tpl.innerHTML = markup.trim();
            var fresh = tpl.content.firstElementChild;
            if (el) el.replaceWith(fresh); else grid.appendChild(fresh);
            el = fresh;
            fleetCardCache.set(id, markup);
          }
          var expected = prev ? prev.nextElementSibling : grid.firstElementChild;
          if (expected !== el) grid.insertBefore(el, expected);
          prev = el;
        });
        Array.from(grid.querySelectorAll(".printer-card")).forEach(function (el) {
          var id = el.getAttribute("data-id");
          if (!seen.has(id)) { el.remove(); fleetCardCache.delete(id); }
        });
      }
      function applyAndRender() {
        var printers = cachedAllPrinters;
        var filtered = applyFilters(printers);
        var sorted = sortPrinters(filtered);
        lastFilteredPrinters = filtered;
        renderSummary(printers);
        renderFleet(sorted);
        var forecastPanel = document.querySelector('.view-panel[data-panel="forecast"]');
        if (forecastPanel && forecastPanel.classList.contains('active')) renderForecast();
      }

      // --- Text scale setting: scales the rem base (html font-size), 50–150% ---
      var TEXT_SCALE_KEY = "forge-ops-text-scale";
      (function initTextScale() {
        var range = document.getElementById("textScaleRange");
        var label = document.getElementById("textScaleValue");
        if (!range) return;
        function apply(pct) {
          document.documentElement.style.fontSize = pct === 100 ? "" : (16 * pct / 100) + "px";
          if (label) label.textContent = pct + "%";
        }
        var saved = 100;
        try {
          var v = parseInt(localStorage.getItem(TEXT_SCALE_KEY), 10);
          if (v >= 50 && v <= 150) saved = v;
        } catch (e) {}
        range.value = saved;
        apply(saved);
        range.addEventListener("input", function () {
          var v = Math.max(50, Math.min(150, parseInt(range.value, 10) || 100));
          apply(v);
          try { localStorage.setItem(TEXT_SCALE_KEY, String(v)); } catch (e) {}
        });
      })();

      // --- Forecast tab: single time axis with per-printer finish flags ---
      var FORECAST_HOURS_KEY = "forge-ops-forecast-hours";
      var forecastHours = 12;
      try {
        var savedForecastHours = parseInt(localStorage.getItem(FORECAST_HOURS_KEY), 10);
        if (savedForecastHours === 12 || savedForecastHours === 24) forecastHours = savedForecastHours;
      } catch (e) {}

      document.querySelectorAll(".forecast-controls .time-btn").forEach(function (btn) {
        btn.classList.toggle("active", parseInt(btn.dataset.hours, 10) === forecastHours);
        btn.addEventListener("click", function () {
          forecastHours = parseInt(btn.dataset.hours, 10) || 12;
          try { localStorage.setItem(FORECAST_HOURS_KEY, String(forecastHours)); } catch (e) {}
          document.querySelectorAll(".forecast-controls .time-btn").forEach(function (b) {
            b.classList.toggle("active", b === btn);
          });
          renderForecast();
        });
      });

      function shortPrinterName(label) {
        // Имена по схеме «модель+номер» (P1, C3, K1, E5, Reborn 2…) уже короткие —
        // показываем как есть. Старый формат «(D) Flying Bear …» выведен из обихода.
        return label || "";
      }

      function renderForecast() {
        var area = document.getElementById("forecastArea");
        var meta = document.getElementById("forecastMeta");
        var noEta = document.getElementById("forecastNoEta");
        if (!area) return;
        var now = Date.now();
        var HORIZON_MS = forecastHours * 3600 * 1000;
        var end = now + HORIZON_MS;
        function hhmm(ts) {
          return new Date(ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
        }
        if (meta) meta.innerHTML = '<span class="forecast-now-time">' + hhmm(now) + '</span> → ' + hhmm(end);

        var printing = cachedAllPrinters.filter(function (p) { return p.state === "printing"; });
        var withEta = printing.filter(function (p) { return p.eta_seconds != null && p.eta_seconds > 0; });
        var withoutEta = printing.filter(function (p) { return p.eta_seconds == null || p.eta_seconds <= 0; });

        if (noEta) {
          if (withoutEta.length) {
            noEta.style.display = "";
            noEta.textContent = "Печатают без ETA: " + withoutEta.map(function (p) { return shortPrinterName(p.label); }).join(", ");
          } else {
            noEta.style.display = "none";
          }
        }

        if (!withEta.length) {
          area.style.height = "";
          area.innerHTML = '<div class="forecast-empty">Сейчас ничего не печатается' +
            (withoutEta.length ? " с известным ETA" : "") + '</div>';
          return;
        }

        var flags = withEta.map(function (p) {
          var finish = now + p.eta_seconds * 1000;
          var over = finish > end;
          return {
            p: p,
            finish: finish,
            over: over,
            pct: over ? 100 : ((finish - now) / HORIZON_MS) * 100
          };
        }).sort(function (a, b) { return a.pct - b.pct; });

        // Greedy stagger: bump a flag up a level while it would overlap the
        // previous flag on that level (labels are ~14% of the axis wide).
        var MIN_GAP_PCT = 14;
        var levelLast = [];
        flags.forEach(function (f) {
          var lvl = 0;
          while (lvl < levelLast.length && f.pct - levelLast[lvl] < MIN_GAP_PCT) lvl++;
          f.level = lvl;
          levelLast[lvl] = f.pct;
        });
        var levels = levelLast.length;

        var LEVEL_H = 30;
        var axisTop = levels * LEVEL_H + 8;
        var html = "";

        var tickStepMs = (forecastHours > 12 ? 2 : 1) * 3600 * 1000;
        var tick = new Date(now);
        tick.setMinutes(0, 0, 0);
        if (forecastHours > 12 && tick.getHours() % 2 !== 0) tick.setTime(tick.getTime() + 3600 * 1000);
        for (var t = tick.getTime() + (tick.getTime() <= now ? tickStepMs : 0); t < end - 15 * 60 * 1000; t += tickStepMs) {
          var tp = ((t - now) / HORIZON_MS) * 100;
          if (tp < 4 || tp > 96) continue;
          html += '<div class="forecast-tick" style="left:' + tp.toFixed(2) + '%;top:' + (axisTop - 5) + 'px"></div>' +
            '<div class="forecast-tick-label" style="left:' + tp.toFixed(2) + '%;top:' + (axisTop + 9) + 'px">' + hhmm(t) + '</div>';
        }
        html += '<div class="forecast-axis" style="top:' + axisTop + 'px"></div>' +
          '<div class="forecast-now" style="top:' + (axisTop - 8) + 'px"></div>' +
          '<div class="forecast-now-label" style="top:' + (axisTop + 9) + 'px">' + hhmm(now) + '</div>' +
          '<div class="forecast-end-label" style="top:' + (axisTop + 9) + 'px">' + hhmm(end) + '</div>';

        flags.forEach(function (f) {
          var top = f.level * LEVEL_H;
          var stemH = axisTop - top - 24;
          var text = shortPrinterName(f.p.label) + (f.over ? " → " : " · ") + hhmm(f.finish);
          var title = f.p.label +
            (f.p.job_name ? "\n" + f.p.job_name : "") +
            (f.p.progress_pct != null ? "\nПрогресс: " + Math.round(f.p.progress_pct) + "%" : "") +
            "\nОсталось: " + timeFmt(f.p.eta_seconds) +
            "\nЗакончит: " + hhmm(f.finish) + (f.over ? " (за пределами окна)" : "");
          var edge = f.pct < 6 ? " forecast-flag-left" : (f.pct > 94 ? " forecast-flag-right" : "");
          html += '<div class="forecast-flag' + (f.over ? " forecast-flag-over" : "") + edge +
            '" style="left:' + f.pct.toFixed(2) + '%;top:' + top + 'px" title="' + escHtml(title) + '">' +
            '<span class="forecast-flag-label">' + escHtml(text) + '</span>' +
            '<span class="forecast-flag-stem" style="height:' + Math.max(stemH, 4) + 'px"></span>' +
            '</div>';
        });

        area.style.height = (axisTop + 44) + "px";
        area.innerHTML = html;
      }

      /* ── Команды с карточек (админ, Bambu) ────── */
      async function sendPrinterCmd(pid, action, extra) {
        try {
          var r = await apiFetch("/api/admin/printers/" + encodeURIComponent(pid) + "/command", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(Object.assign({ action: action }, extra || {})),
          });
          var d = await r.json();
          if (!r.ok) { showToast(d.detail || "Ошибка команды", false); return false; }
          showToast("Команда отправлена", true);
          setTimeout(load, 1500);
          return true;
        } catch (e) { showToast("Сеть недоступна", false); return false; }
      }
      // ── Поповер смены параметров (клик по чипу сопла/стола/скорости/вентилятора и по 🔧) ──
      var pcPop = null, pcPopPid = null;
      function ensurePop() {
        if (pcPop) return pcPop;
        pcPop = document.createElement("div");
        pcPop.className = "pc-pop";
        pcPop.style.display = "none";
        document.body.appendChild(pcPop);
        pcPop.addEventListener("click", onPopClick);
        pcPop.addEventListener("input", function (e) {
          var r = e.target.closest("[data-fan]");
          if (r) { var b = r.parentNode.querySelector("b"); if (b) b.textContent = r.value; }
        });
        return pcPop;
      }
      function closePop() { if (pcPop) pcPop.style.display = "none"; pcPopPid = null; }
      function openPop(anchor, pid, html, kind) {
        var pop = ensurePop();
        pcPopPid = pid;
        pop.dataset.kind = kind || "";
        pop.innerHTML = html;
        pop.style.display = "block";
        pop.style.visibility = "hidden";
        var r = anchor.getBoundingClientRect();
        var pw = pop.offsetWidth, ph = pop.offsetHeight;
        var left = Math.max(8, Math.min(r.left, window.innerWidth - pw - 8));
        var top = r.bottom + 6;
        if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
        pop.style.left = left + "px";
        pop.style.top = top + "px";
        pop.style.visibility = "";
        var inp = pop.querySelector("input.pc-pop-in");
        if (inp) { inp.focus(); inp.select && inp.select(); }
      }
      function tempPopHtml(kind, cur, presets) {
        var chips = presets.map(function (v) {
          return '<button type="button" class="pc-pop-preset" data-set="' + v[1] + '">' + v[0] + '</button>';
        }).join("");
        return '<div class="pc-pop-h">' + (kind === "nozzle" ? "Сопло" : "Стол") + ', °C</div>'
          + '<div class="pc-pop-presets">' + chips + '</div>'
          + '<div class="pc-pop-row"><input type="text" inputmode="numeric" class="pc-pop-in" value="' + cur + '">'
          + '<button type="button" class="pc-pop-ok" data-ok="preheat">OK</button></div>';
      }
      function speedPopHtml() {
        var lv = [["Тихий", 1], ["Стандарт", 2], ["Спорт", 3], ["Ludicrous", 4]];
        return '<div class="pc-pop-h">Скорость печати</div><div class="pc-pop-col">'
          + lv.map(function (x) { return '<button type="button" class="pc-pop-preset" data-speed="' + x[1] + '">' + x[0] + '</button>'; }).join("")
          + '</div>';
      }
      function fanPopHtml(part, aux, chamber) {
        function row(lbl, key, val) {
          return '<label class="pc-pop-fan">' + lbl
            + '<input type="range" min="0" max="100" value="' + val + '" data-fan="' + key + '"><b>' + val + '</b></label>';
        }
        return '<div class="pc-pop-h">Вентиляторы, %</div>'
          + row("Деталь", "part", part) + row("Вспом.", "aux", aux) + row("Камера", "chamber", chamber)
          + '<div class="pc-pop-row"><button type="button" class="pc-pop-ok" data-ok="fans">Применить</button></div>';
      }
      function svcPopHtml() {
        return '<div class="pc-pop-h">Обслуживание</div><div class="pc-pop-col">'
          + '<button type="button" class="pc-pop-preset" data-svc="cooldown">Остудить всё</button>'
          + '<button type="button" class="pc-pop-preset" data-svc="eject">Стол вниз (снять деталь)</button>'
          + '<button type="button" class="pc-pop-preset" data-svc="ams_unload">AMS выгрузка</button>'
          + '</div>'
          + '<div class="pc-pop-row"><input type="text" inputmode="numeric" class="pc-pop-slot" placeholder="слот 0–15"><button type="button" class="pc-pop-ok" data-svc2="ams_load">Загрузка</button></div>'
          + '<div class="pc-pop-row"><input type="text" class="pc-pop-skip" placeholder="id: 3,7"><button type="button" class="pc-pop-ok" data-svc2="skip_objects">Пропуск</button></div>';
      }
      async function onPopClick(e) {
        var pid = pcPopPid;
        if (!pid) return;
        var t = e.target.closest("[data-set],[data-speed],[data-ok],[data-svc],[data-svc2]");
        if (!t) return;
        if (t.dataset.set != null) {
          var inp = pcPop.querySelector(".pc-pop-in");
          if (inp) inp.value = t.dataset.set;
          return;
        }
        if (t.dataset.speed != null) {
          if (await sendPrinterCmd(pid, "speed", { level: parseInt(t.dataset.speed, 10) })) closePop();
          return;
        }
        if (t.dataset.svc != null) {
          if (t.dataset.svc === "eject" && !confirm("Отвести стол вниз? Только на простое.")) return;
          if (await sendPrinterCmd(pid, t.dataset.svc, null)) closePop();
          return;
        }
        if (t.dataset.svc2 === "ams_load") {
          var s = parseInt((pcPop.querySelector(".pc-pop-slot").value || "").trim(), 10);
          if (!(s >= 0 && s <= 15)) { pcPop.querySelector(".pc-pop-slot").focus(); return; }
          if (await sendPrinterCmd(pid, "ams_load", { slot: s })) closePop();
          return;
        }
        if (t.dataset.svc2 === "skip_objects") {
          var raw = (pcPop.querySelector(".pc-pop-skip").value || "").split(",").map(function (x) { return parseInt(x.trim(), 10); }).filter(function (x) { return !isNaN(x); });
          if (!raw.length) { pcPop.querySelector(".pc-pop-skip").focus(); return; }
          if (await sendPrinterCmd(pid, "skip_objects", { obj_list: raw })) closePop();
          return;
        }
        if (t.dataset.ok === "preheat") {
          var v = parseInt((pcPop.querySelector(".pc-pop-in").value || "").trim(), 10);
          if (isNaN(v)) { pcPop.querySelector(".pc-pop-in").focus(); return; }
          var extra = {}; extra[pcPop.dataset.kind] = v;
          if (await sendPrinterCmd(pid, "preheat", extra)) closePop();
          return;
        }
        if (t.dataset.ok === "fans") {
          var fx = {};
          pcPop.querySelectorAll("[data-fan]").forEach(function (r) { fx[r.dataset.fan] = parseInt(r.value, 10); });
          if (await sendPrinterCmd(pid, "fans", fx)) closePop();
          return;
        }
      }

      // Плавающий тултип легенд карточки: позиционируется у элемента и зажимается
      // в границы окна — не обрезается ни у левого, ни у правого края, корректен
      // при любом масштабе текста. Заменяет обрезавшийся CSS ::after у карточек.
      (function initChipTooltips() {
        var tip = null, timer = null, curEl = null;
        function ensure() {
          if (!tip) { tip = document.createElement("div"); tip.className = "pc-tip"; document.body.appendChild(tip); }
          return tip;
        }
        function place(el) {
          var t = ensure();
          t.textContent = el.getAttribute("data-tip") || "";
          var r = el.getBoundingClientRect();
          var tw = t.offsetWidth, th = t.offsetHeight, m = 8;
          var x = Math.max(m, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - m));
          var y = r.top - th - 8;
          if (y < m) y = r.bottom + 8; // нет места сверху — показать снизу
          t.style.left = Math.round(x) + "px";
          t.style.top = Math.round(y) + "px";
          t.classList.add("show");
        }
        function hide() {
          if (timer) { clearTimeout(timer); timer = null; }
          curEl = null;
          if (tip) tip.classList.remove("show");
        }
        grid.addEventListener("mouseover", function (e) {
          var el = e.target.closest("[data-tip]");
          if (!el || !grid.contains(el) || el === curEl) return;
          curEl = el;
          if (timer) clearTimeout(timer);
          timer = setTimeout(function () { if (curEl === el) place(el); }, 250);
        });
        grid.addEventListener("mouseout", function (e) {
          var el = e.target.closest("[data-tip]");
          if (!el) return;
          if (e.relatedTarget && el.contains(e.relatedTarget)) return; // ушли на своего ребёнка
          hide();
        });
        window.addEventListener("scroll", hide, true);
      })();

      (function initPrinterControls() {
        grid.addEventListener("click", function (e) {
          var chip = e.target.closest(".pc-editable");
          if (chip && grid.contains(chip)) {
            e.stopPropagation();
            var c = chip.closest(".printer-card");
            if (!c) return;
            var pid = c.dataset.id, kind = chip.dataset.edit;
            if (kind === "nozzle" || kind === "bed") {
              var presets = kind === "nozzle"
                ? [["PLA 220", 220], ["PETG 250", 250], ["ABS 260", 260], ["Выкл", 0]]
                : [["60", 60], ["80", 80], ["100", 100], ["Выкл", 0]];
              openPop(chip, pid, tempPopHtml(kind, chip.dataset.cur || "", presets), kind);
            } else if (kind === "speed") {
              openPop(chip, pid, speedPopHtml());
            } else if (kind === "fan") {
              openPop(chip, pid, fanPopHtml(+chip.dataset.part || 0, +chip.dataset.aux || 0, +chip.dataset.chamber || 0));
            }
            return;
          }
          var btn = e.target.closest(".pc-ctl");
          if (!btn || btn.disabled) return;
          var card = btn.closest(".printer-card");
          if (!card) return;
          var pid2 = card.dataset.id, label = card.dataset.label, cmd = btn.dataset.cmd;
          if (!pid2 || !cmd) return;
          if (cmd === "svc") { e.stopPropagation(); openPop(btn, pid2, svcPopHtml()); return; }
          if (cmd === "stop" && !confirm("Остановить печать на «" + label + "»? Прогресс будет потерян.")) return;
          sendPrinterCmd(pid2, cmd, null);
        });
        document.addEventListener("click", function (e) {
          if (pcPop && pcPop.style.display !== "none" && !pcPop.contains(e.target)
              && !e.target.closest(".pc-editable") && !e.target.closest("[data-cmd=svc]")) closePop();
        });
        document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePop(); });
      })();

      async function load() {
        try {
          const response = await apiFetch("/api/printers");
          const data = await response.json();
          cachedAllPrinters = data.map((p) => Object.assign({}, p));
          applyAndRender();
          if (Date.now() - lastEventLogLoad > 15000
              && !eventLogLoading && eventLogOffset <= EVENT_LOG_LIMIT) {
            // Auto-refresh the FIRST page only. Skip when the user has scrolled in
            // extra pages (offset past the first page) or a load is in flight —
            // otherwise the reset discards their loaded pages and races the
            // pending request. Resumes after a manual reloadEventLog().
            eventLogOffset = 0;
            loadEventLog();
            lastEventLogLoad = Date.now();
          }
          const settings = loadNotificationSettings();
          cachedAllPrinters.forEach(function(p) {
            var prev = prevPrinterStates[p.id];
            if (prev && prev !== p.state) {
              if (settings.enabled && ("Notification" in window) && Notification.permission === "granted") {
                if (settings.finish && prev === "printing" && p.state === "finished") {
                  showBrowserNotification("✅ Печать завершена", p.label);
                }
                if (settings.error && p.state === "error" && prev !== "error" && prev !== "offline") {
                  showBrowserNotification("🔴 Ошибка принтера", p.label + (p.last_error ? ": " + p.last_error : ""));
                }
                if (settings.paused && prev === "printing" && p.state === "paused") {
                  showBrowserNotification("⚠️ Пауза печати", p.label);
                }
              }
            }
            prevPrinterStates[p.id] = p.state;
          });
          lastUpdate.textContent = new Date().toLocaleTimeString("ru-RU", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
          });
          lastUpdate.classList.remove("stale");
        } catch (error) {
          console.error("Load failed:", error);
          // Единичный сбой поллинга (каждые 3с) не должен стирать сетку —
          // если есть последние данные, оставляем их и лишь помечаем «устарело».
          if (cachedAllPrinters && cachedAllPrinters.length) {
            if (lastUpdate) lastUpdate.classList.add("stale");
          } else {
            grid.innerHTML = `
            <div class="empty-state">
              <strong>Не удалось загрузить телеметрию</strong>
              Проверьте доступность API <code>/api/printers</code> и повторите попытку.
            </div>
          `;
          }
        }
      }
      viewTabs.forEach((tab) => {
        tab.addEventListener("click", () => setActiveView(tab.dataset.view));
      });
      (function () {
        let saved;
        try {
          saved = localStorage.getItem("forge-ops-active-view");
        } catch (e) {}
        if (!saved || saved === "overview") saved = "fleet";
        setActiveView(saved);
      })();
      loadPersistedFilters();
      loadSortPrefs();
      renderFilters();
      load();
      setInterval(load, 3000);
      initStatsPeriodButtons();
      initKpiClicks();
      initEventLogFilters();
      setupInfiniteScroll();
      initTheme();
      syncNotificationControls();
      var themeBtns = document.querySelectorAll(".theme-seg-btn");
      themeBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
          setTheme(btn.getAttribute("data-theme") || "");
        });
      });
      if (filtersBtn) {
        filtersBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          toggleFiltersPopup();
        });
      }
      if (filtersDropdown) {
        filtersDropdown.addEventListener("click", function (e) {
          e.stopPropagation();
        });
      }
      document.addEventListener("click", function () {
        if (filtersDropdown && filtersDropdown.classList.contains("open")) {
          closeFiltersPopup();
        }
      });
      notificationsEnabledToggle.addEventListener("change", function () {
        updateNotificationSetting("enabled", notificationsEnabledToggle.checked);
      });
      notificationsFinishToggle.addEventListener("change", function () {
        updateNotificationSetting("finish", notificationsFinishToggle.checked);
      });
      notificationsErrorToggle.addEventListener("change", function () {
        updateNotificationSetting("error", notificationsErrorToggle.checked);
      });
      notificationsPausedToggle.addEventListener("change", function () {
        updateNotificationSetting("paused", notificationsPausedToggle.checked);
      });
      showErrorsToggle.addEventListener("change", function () {
        saveShowErrors(showErrorsToggle.checked);
        applyAndRender();
      });

      /* ── Timeline ────────────────────────────── */
      // Модель «как в картах»: viewStart/viewEnd — видимое окно времени (unix c).
      // Период задаёт окно 1:1, колесо зумит к курсору, перетаскивание панит,
      // дабл-клик сбрасывает. Шаг сетки всегда автоматический от видимого окна.
      var viewStart = 0;
      var viewEnd = 0;
      var dragActive = false;
      var dragStartX = 0;
      var dragBaseViewStart = 0;
      var printerMap = {};
      var TIMELINE_MIN_SPAN = 600; // зум максимум до 10 минут на всю ширину

      function timelineBounds() {
        var lo = timelineData.length ? timelineData[0].recorded_at : 0;
        if (timelineRange.from != null && timelineRange.from < lo) lo = timelineRange.from;
        var hi = timelineRange.to || (Date.now() / 1000);
        return { lo: lo, hi: hi };
      }
      function clampView() {
        if (!timelineData.length) return;
        var b = timelineBounds();
        var span = viewEnd - viewStart;
        if (span >= b.hi - b.lo) { viewStart = b.lo; viewEnd = b.hi; return; }
        if (viewStart < b.lo) { viewStart = b.lo; viewEnd = b.lo + span; }
        if (viewEnd > b.hi) { viewEnd = b.hi; viewStart = b.hi - span; }
      }
      function maybeLoadMoreAtLeftEdge() {
        if (!timelineHasMore || !timelineData.length) return;
        var span = viewEnd - viewStart;
        if (viewStart <= timelineData[0].recorded_at + span * 0.1) {
          loadMoreTimeline();
        }
      }
      var stateOrder = { printing: 0, paused: 1, error: 2, finished: 3, idle: 4, unknown: 5, offline: 6 };

      function naturalCompare(a, b) {
        var re = /(\d+)|(\D+)/g;
        var aa = a.match(re); var bb = b.match(re);
        var len = Math.min(aa.length, bb.length);
        for (var i = 0; i < len; i++) {
          var ca = aa[i], cb = bb[i];
          var na = parseInt(ca, 10), nb = parseInt(cb, 10);
          if (!isNaN(na) && !isNaN(nb)) {
            if (na !== nb) return na - nb;
          } else {
            var cmp = ca.localeCompare(cb, 'ru');
            if (cmp !== 0) return cmp;
          }
        }
        return aa.length - bb.length;
      }

      async function loadTimeline() {
        var gen = ++timelineLoadGen;
        var loader = document.getElementById('timelineLoader');
        if (loader) loader.style.display = '';
        var from = timelineRange.from || (Date.now() / 1000 - 86400);
        var to = timelineRange.to || (Date.now() / 1000);
        timelineOffset = 0;
        var url = '/api/history/timeline?fr=' + from + '&to=' + to +
                  '&desc=true&limit=' + timelineLimit + '&offset=0';
        if (timelinePrinter) url += '&printer=' + encodeURIComponent(timelinePrinter);
        try {
          var resp = await apiFetch(url);
          var data = await resp.json();
          if (gen !== timelineLoadGen) return; // диапазон уже сменили
          timelineData = data.rows.reverse();
          timelineHasMore = data.has_more;
          timelineTotal = data.total;
          applyScale();
        } catch (e) {
          console.error('loadTimeline failed', e);
          return;
        } finally {
          if (loader) loader.style.display = 'none';
        }
        // Остальные страницы окна — фоном: график уже нарисован и живой,
        // старые сегменты дорисовываются слева по мере прихода.
        while (timelineHasMore && gen === timelineLoadGen) {
          var prevOffset = timelineOffset;
          await loadMoreTimeline();
          // Ничего не продвинулось (параллельная догрузка или ошибка) — выходим,
          // чтобы не крутить цикл вхолостую.
          if (timelineOffset === prevOffset) break;
        }
      }

      async function loadMoreTimeline() {
        if (!timelineHasMore || timelineLoadingMore) return;
        timelineLoadingMore = true;
        var gen = timelineLoadGen;
        var from = timelineRange.from || (Date.now() / 1000 - 86400);
        var to = timelineRange.to || (Date.now() / 1000);
        timelineOffset += timelineLimit;
        var url = '/api/history/timeline?fr=' + from + '&to=' + to +
                  '&desc=true&limit=' + timelineLimit + '&offset=' + timelineOffset;
        if (timelinePrinter) url += '&printer=' + encodeURIComponent(timelinePrinter);
        try {
          var resp = await apiFetch(url);
          var data = await resp.json();
          if (gen !== timelineLoadGen) return; // ответ для уже сменённого диапазона
          timelineData = data.rows.reverse().concat(timelineData);
          timelineHasMore = data.has_more;
          timelineTotal = data.total;
          // Не applyScale: догрузка старых строк не должна сбрасывать вид.
          drawTimeline();
        } catch (e) {
          console.error('loadMoreTimeline failed', e);
          // Не зацикливаем фоновую догрузку на постоянно падающем запросе.
          if (gen === timelineLoadGen) timelineHasMore = false;
        } finally {
          timelineLoadingMore = false;
        }
      }

      function applyScale() {
        // Still redraw on empty so the canvas is cleared (drawTimeline handles
        // the empty case) instead of leaving the previous range's pixels behind.
        if (timelineData.length === 0) { drawTimeline(); return; }
        // Видимое окно = выбранный период целиком, без скрытого приближения.
        var b = timelineBounds();
        viewStart = timelineRange.from != null ? timelineRange.from : b.lo;
        viewEnd = b.hi;
        drawTimeline();
      }

      function updateScaleLabel() {
        var el = document.getElementById('timeline-scale-label');
        if (!el) return;
        function fmtDur(sec) {
          if (sec < 120) return Math.round(sec) + ' сек';
          if (sec < 3600) return Math.round(sec / 60) + ' мин';
          if (sec < 86400) return (Math.round(sec / 3600 * 10) / 10) + ' ч';
          return (Math.round(sec / 86400 * 10) / 10) + ' дн';
        }
        var label = 'видно ' + fmtDur(Math.max(0, viewEnd - viewStart)) + ' · деление ' + fmtDur(tickStep);
        el.textContent = '\u29D6 ' + label;
      }

      // Офлайн на канвасе — диагональная штриховка (как seg-offline в «Сводке»):
      // сплошной серый неотличим от простоя и пустого фона.
      function makeOfflinePattern(ctx) {
        var pc = document.createElement('canvas');
        pc.width = 8;
        pc.height = 8;
        var p = pc.getContext('2d');
        var st = getComputedStyle(document.documentElement);
        p.fillStyle = (st.getPropertyValue('--gray-5') || '#33475c').trim() || '#33475c';
        p.fillRect(0, 0, 8, 8);
        p.strokeStyle = (st.getPropertyValue('--gray-7') || '#475e77').trim() || '#475e77';
        p.lineWidth = 2.5;
        p.beginPath();
        p.moveTo(-2, 10); p.lineTo(10, -2);
        p.moveTo(-2, 2); p.lineTo(2, -2);
        p.moveTo(6, 10); p.lineTo(10, 6);
        p.stroke();
        return ctx.createPattern(pc, 'repeat');
      }

      function drawTimeline() {
        var canvas = document.getElementById('timeline-canvas');
        if (!canvas) return;
        var ctx = canvas.getContext('2d');
        var dpr = window.devicePixelRatio || 1;
        // Reassigning width/height clears the bitmap — do it BEFORE the empty
        // check so switching to a range with no data wipes the previous render
        // instead of leaving stale pixels on screen.
        canvas.width = canvas.offsetWidth * dpr;
        canvas.height = canvas.offsetHeight * dpr;
        ctx.scale(dpr, dpr);
        var W = canvas.offsetWidth;
        var H = canvas.offsetHeight;
        if (timelineData.length === 0) return;

        var tc = getTimelineColors();
        var offlinePattern = makeOfflinePattern(ctx);
        // Background
        var bgGrad = ctx.createLinearGradient(0, 0, 0, H);
        bgGrad.addColorStop(0, tc.bgTop);
        bgGrad.addColorStop(1, tc.bgBottom);
        ctx.fillStyle = bgGrad;
        ctx.fillRect(0, 0, W, H);

        // Group by printer
        printerMap = {};
        timelineData.forEach(function (r) {
          if (!printerMap[r.printer_id]) {
            printerMap[r.printer_id] = { label: r.label || r.printer_id, rows: [], curState: 'unknown' };
          }
          printerMap[r.printer_id].rows.push(r);
          printerMap[r.printer_id].curState = r.state;
        });

        // Sort: printing/paused first, then error, finished, idle, offline
        var pids = Object.keys(printerMap).sort(function (a, b) {
          return naturalCompare(printerMap[a].label, printerMap[b].label);
        });

        if (pids.length === 0) return;

        var tMin = timelineData[0].recorded_at;
        var tMax = timelineData[timelineData.length - 1].recorded_at;
        var now = Date.now() / 1000;
        if (!viewStart || !viewEnd || viewEnd <= viewStart) {
          var b0 = timelineBounds();
          viewStart = b0.lo;
          viewEnd = b0.hi;
        }
        var viewSpan = (viewEnd - viewStart) || 1;
        tickStep = autoTickStep(viewSpan);

        var labelW = 200;
        var marginR = 10;
        var axisH = 28;
        var barAreaW = W - labelW - marginR;
        var barAreaH = H - axisH - 8;
        var barH = Math.min(44, Math.floor(barAreaH / pids.length) - 3);
        var gap = 3;
        var y0 = 6;

        function txof(ts) {
          return labelW + ((ts - viewStart) / viewSpan) * barAreaW;
        }

        // --- Zebra rows (full width) ---
        for (var i = 0; i < pids.length; i++) {
          if (i % 2 === 0) {
            ctx.fillStyle = tc.zebra;
            ctx.fillRect(0, y0 + i * (barH + gap), W, barH);
          }
        }

        // --- Vertical grid: major lines every 5 ticks, minor every tick ---
        var gridStart = Math.ceil(viewStart / tickStep) * tickStep;
        for (var gt = gridStart; gt <= viewEnd; gt += tickStep) {
          var gx = txof(gt);
          if (gx < labelW - 5 || gx > W - marginR + 5) continue;
          var tickNum = Math.round(gt / tickStep);
          var isMajor = (tickNum % 5 === 0) || (tickStep >= 21600);
          ctx.strokeStyle = isMajor ? tc.gridMajor : tc.gridMinor;
          ctx.lineWidth = isMajor ? 0.8 : 0.3;
          ctx.beginPath();
          ctx.moveTo(gx, 0);
          ctx.lineTo(gx, barAreaH + y0);
          ctx.stroke();
        }

        // --- "Now" line ---
        if (now >= viewStart && now <= viewEnd) {
          var nx = txof(now);
          if (nx >= labelW && nx <= W - marginR) {
            ctx.strokeStyle = tc.nowLine;
            ctx.lineWidth = 1;
            ctx.setLineDash([6, 4]);
            ctx.beginPath();
            ctx.moveTo(nx, 0);
            ctx.lineTo(nx, barAreaH + y0);
            ctx.stroke();
            ctx.setLineDash([]);
            // "сейчас" label
            ctx.fillStyle = tc.nowText;
            ctx.font = 'bold 10px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            ctx.fillText('сейчас', nx, y0 - 1);
          }
        }

        // --- Time axis ---
        var tickH = barAreaH + y0;
        ctx.strokeStyle = tc.axisLine;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(labelW, tickH);
        ctx.lineTo(W - marginR, tickH);
        ctx.stroke();

        ctx.fillStyle = tc.axisText;
        ctx.font = 'bold 12px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        var tickStart = Math.ceil(viewStart / tickStep) * tickStep;
        var showDate = viewSpan > 21600;
        var lastLabelX = -100;
        var minGap = 45;
        for (var tt = tickStart; tt <= viewEnd; tt += tickStep) {
          var ttx = txof(tt);
          if (ttx < labelW - 30 || ttx > W - marginR + 30) continue;
          var tickNum2 = Math.round(tt / tickStep);
          var isMajor2 = (tickNum2 % 5 === 0) || (tickStep >= 21600);
          // Tick mark
          ctx.strokeStyle = isMajor2 ? tc.tickMajor : tc.tickMinor;
          ctx.lineWidth = isMajor2 ? 1.2 : 0.5;
          ctx.beginPath();
          ctx.moveTo(ttx, tickH);
          ctx.lineTo(ttx, tickH + (isMajor2 ? 6 : 3));
          ctx.stroke();
          // Label only on major ticks, skip if overlap
          if (!isMajor2) continue;
          if (ttx - lastLabelX < minGap) continue;
          lastLabelX = ttx;
          var td = new Date(tt * 1000);
          var tl;
          if (showDate) {
            tl = td.getDate().toString().padStart(2,'0') + '.' + (td.getMonth()+1).toString().padStart(2,'0') + ' ' +
                 td.getHours().toString().padStart(2,'0') + ':' + td.getMinutes().toString().padStart(2,'0');
          } else {
            tl = td.getHours().toString().padStart(2,'0') + ':' + td.getMinutes().toString().padStart(2,'0');
          }
          ctx.fillText(tl, ttx, tickH + 8);
        }

        // --- Labels + dots (BEFORE clip, in label area) ---
        pids.forEach(function (pid, i) {
          var y = y0 + i * (barH + gap);
          var curColor = STATE_COLORS[printerMap[pid].curState] || '#9ca3af';
          // State dot
          ctx.fillStyle = curColor;
          ctx.beginPath();
          ctx.arc(12, y + barH / 2, 5, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = tc.labelDot;
          ctx.beginPath();
          ctx.arc(11, y + barH / 2 - 1, 2.5, 0, Math.PI * 2);
          ctx.fill();
          // Printer name
          ctx.font = '12px sans-serif';
          ctx.textBaseline = 'middle';
          ctx.textAlign = 'left';
          var dn = printerMap[pid].label;
          if (dn.length > 30) dn = dn.substring(0, 29) + '\u2026';
          ctx.fillStyle = tc.labelText;
          ctx.fillText(dn, 22, y + barH / 2);
        });

        // --- Clip for bars ---
        ctx.save();
        ctx.beginPath();
        ctx.rect(labelW, 0, barAreaW, barAreaH + y0);
        ctx.clip();

        pids.forEach(function (pid, i) {
          var y = y0 + i * (barH + gap);
          var rows = printerMap[pid].rows;
          if (rows.length === 0) return;

          // Bars with gradient + shadow
          var segStart = 0;
          var segState = rows[0].state;
          for (var j = 1; j <= rows.length; j++) {
            if (j === rows.length || rows[j].state !== segState) {
              var sx = txof(rows[segStart].recorded_at);
              var ex = txof(rows[j - 1].recorded_at);
              var sw = Math.max(3, ex - sx);
              var baseColor = STATE_COLORS[segState] || '#9ca3af';
              var rr = 3;
              var rx = sx;
              var ry = y;
              var rw = sw;
              var rh = barH;

              // Shadow
              ctx.fillStyle = tc.barShadow;
              ctx.beginPath();
              ctx.moveTo(rx + rr, ry + 1);
              ctx.lineTo(rx + rw - rr, ry + 1);
              ctx.quadraticCurveTo(rx + rw, ry + 1, rx + rw, ry + rr + 1);
              ctx.lineTo(rx + rw, ry + rh - rr + 1);
              ctx.quadraticCurveTo(rx + rw, ry + rh + 1, rx + rw - rr, ry + rh + 1);
              ctx.lineTo(rx + rr, ry + rh + 1);
              ctx.quadraticCurveTo(rx, ry + rh + 1, rx, ry + rh - rr + 1);
              ctx.lineTo(rx, ry + rr + 1);
              ctx.quadraticCurveTo(rx, ry + 1, rx + rr, ry + 1);
              ctx.closePath();
              ctx.fill();

              // Gradient bar (офлайн — штриховкой)
              if (segState === 'offline') {
                ctx.fillStyle = offlinePattern;
              } else {
                var grad = ctx.createLinearGradient(rx, ry, rx, ry + rh);
                grad.addColorStop(0, lightenColor(baseColor, 0.15));
                grad.addColorStop(1, baseColor);
                ctx.fillStyle = grad;
              }
              ctx.beginPath();
              ctx.moveTo(rx + rr, ry);
              ctx.lineTo(rx + rw - rr, ry);
              ctx.quadraticCurveTo(rx + rw, ry, rx + rw, ry + rr);
              ctx.lineTo(rx + rw, ry + rh - rr);
              ctx.quadraticCurveTo(rx + rw, ry + rh, rx + rw - rr, ry + rh);
              ctx.lineTo(rx + rr, ry + rh);
              ctx.quadraticCurveTo(rx, ry + rh, rx, ry + rh - rr);
              ctx.lineTo(rx, ry + rr);
              ctx.quadraticCurveTo(rx, ry, rx + rr, ry);
              ctx.closePath();
              ctx.fill();

              // Top highlight
              ctx.fillStyle = tc.barHighlight;
              ctx.beginPath();
              ctx.moveTo(rx + rr, ry);
              ctx.lineTo(rx + rw - rr, ry);
              ctx.quadraticCurveTo(rx + rw, ry, rx + rw, ry + rr);
              ctx.lineTo(rx + rw, ry + rh / 2);
              ctx.lineTo(rx, ry + rh / 2);
              ctx.lineTo(rx, ry + rr);
              ctx.quadraticCurveTo(rx, ry, rx + rr, ry);
              ctx.closePath();
              ctx.fill();

              if (j < rows.length) {
                segStart = j;
                segState = rows[j].state;
              }
            }
          }
        });

        ctx.restore();
        updateScaleLabel();
      }

      // Lighten a hex color by factor (0-1)
      function lightenColor(hex, factor) {
        var r = parseInt(hex.substring(1,3), 16);
        var g = parseInt(hex.substring(3,5), 16);
        var b = parseInt(hex.substring(5,7), 16);
        r = Math.min(255, Math.round(r + (255 - r) * factor));
        g = Math.min(255, Math.round(g + (255 - g) * factor));
        b = Math.min(255, Math.round(b + (255 - b) * factor));
        return '#' + [r,g,b].map(function(v){return v.toString(16).padStart(2,'0')}).join('');
      }

      // Zoom-to-cursor (колесо) + пан (перетаскивание) + дабл-клик сброс + тултип
      (function () {
        var canvas = document.getElementById('timeline-canvas');
        if (!canvas) return;

        canvas.addEventListener('wheel', function (e) {
          e.preventDefault();
          if (timelineData.length === 0) return;
          var rect = canvas.getBoundingClientRect();
          var labelW = 200;
          var barAreaW = canvas.offsetWidth - labelW - 10;
          if (barAreaW <= 0) return;
          var frac = Math.max(0, Math.min(1, (e.clientX - rect.left - labelW) / barAreaW));
          var span = viewEnd - viewStart;
          var b = timelineBounds();
          var newSpan = span * (e.deltaY > 0 ? 1.25 : 0.8);
          newSpan = Math.max(TIMELINE_MIN_SPAN, Math.min(newSpan, b.hi - b.lo));
          var anchor = viewStart + frac * span;
          viewStart = anchor - frac * newSpan;
          viewEnd = viewStart + newSpan;
          clampView();
          drawTimeline();
          maybeLoadMoreAtLeftEdge();
        }, { passive: false });

        canvas.addEventListener('mousedown', function (e) {
          dragActive = true;
          dragStartX = e.clientX;
          dragBaseViewStart = viewStart;
          e.preventDefault();
        });

        window.addEventListener('mousemove', function (e) {
          if (!dragActive) return;
          var c2 = document.getElementById('timeline-canvas');
          if (!c2 || timelineData.length === 0) return;
          var baw2 = c2.offsetWidth - 200 - 10;
          if (baw2 <= 0) return;
          var span = viewEnd - viewStart;
          var dt = ((dragStartX - e.clientX) / baw2) * span;
          viewStart = dragBaseViewStart + dt;
          viewEnd = viewStart + span;
          clampView();
          drawTimeline();
        });

        window.addEventListener('mouseup', function () {
          if (!dragActive) return;
          dragActive = false;
          maybeLoadMoreAtLeftEdge();
        });

        canvas.addEventListener('dblclick', function () {
          if (timelineData.length === 0) return;
          var b = timelineBounds();
          viewStart = b.lo;
          viewEnd = b.hi;
          drawTimeline();
        });

        // Hover tooltip
        var tip = document.getElementById('timeline-tooltip');
        var stateNames = { printing: 'Печать', idle: 'Простой', paused: 'Пауза', finished: 'Завершено', error: 'Ошибка', offline: 'Офлайн', unknown: 'Неизвестно' };
        canvas.addEventListener('mousemove', function (e) {
          if (dragActive) { tip.style.display = 'none'; return; }
          var rc = canvas.getBoundingClientRect();
          var mx2 = e.clientX - rc.left;
          var my2 = e.clientY - rc.top;
          var lw3 = 200; var mr3 = 10;
          var baw3 = canvas.offsetWidth - lw3 - mr3;
          var barH3 = Math.min(44, Math.floor((canvas.offsetHeight - 34) / Object.keys(printerMap || {}).length) - 3);
          var gap3 = 3; var y03 = 6;

          if (mx2 < lw3 || mx2 > lw3 + baw3) { tip.style.display = 'none'; return; }
          var idx = Math.floor((my2 - y03) / (barH3 + gap3));
          var pidsSorted = Object.keys(printerMap || {}).sort(function(a,b){
            return naturalCompare(printerMap[a].label, printerMap[b].label);
          });
          if (idx < 0 || idx >= pidsSorted.length) { tip.style.display = 'none'; return; }

          var pid2 = pidsSorted[idx];
          var tAt = viewStart + ((mx2 - lw3) / baw3) * (viewEnd - viewStart);
          var rows2 = printerMap[pid2].rows;
          var best = null;
          for (var ri=0; ri<rows2.length; ri++) { if (rows2[ri].recorded_at<=tAt) best=rows2[ri]; }
          if (!best) { tip.style.display='none'; return; }

          var dt = new Date(best.recorded_at * 1000);
          var timeStr = dt.toLocaleString('ru-RU', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' });
          var desc = '<div style="font-weight:600;margin-bottom:2px;color:' + (STATE_COLORS[best.state]||'#9ca3af') + '">' +
                     safeText(printerMap[pid2].label, '') + '</div>' +
                     '<div style="color:var(--text-dim)">' + timeStr + ' &mdash; ' + safeText(stateNames[best.state]||best.state, '') + '</div>';
          if (best.job_name) desc += '<div style="color:var(--text-dim);font-size:11px;margin-top:1px">' + safeText(best.job_name.substring(0,50), '') + '</div>';
          var details = [];
          if (best.progress != null) details.push('Прогресс: ' + Math.round(best.progress) + '%');
          if (best.nozzle_temp != null && best.nozzle_temp > 0) details.push('Сопло: ' + Math.round(best.nozzle_temp) + '\u00b0C');
          if (best.bed_temp != null && best.bed_temp > 0) details.push('Стол: ' + Math.round(best.bed_temp) + '\u00b0C');
          if (best.eta_sec != null && best.eta_sec > 0) {
            var em = Math.round(best.eta_sec / 60);
            details.push('Осталось: ' + (em>=60?Math.floor(em/60)+'ч'+em%60+'м':em+'м'));
          }
          if (details.length > 0) desc += '<div style="color:var(--text-muted);font-size:11px;margin-top:3px;line-height:1.4">' + details.join(' &middot; ') + '</div>';
          tip.innerHTML = desc;
          tip.style.display = 'block';
          tip.style.left = Math.min(e.clientX + 15, window.innerWidth - 280) + 'px';
          tip.style.top = (e.clientY - 10) + 'px';
        });

        canvas.addEventListener('mouseleave', function () {
          if (tip) tip.style.display = 'none';
        });
      })();

      /* ── Toast (уведомления настроек и пр.) ───── */
      const appToast = document.getElementById("appToast");

      function showToast(msg, ok) {
        appToast.textContent = msg;
        appToast.className = "app-toast show " + (ok ? "success" : "fail");
        setTimeout(function () { appToast.className = "app-toast"; }, 2800);
      }

      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          closeFiltersPopup();
        }
      });

      /* ── Timeline controls ───────────────────── */
      var HISTORY_NICE_TICKS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
      function autoTickStep(rangeSec) {
        var t = HISTORY_NICE_TICKS[HISTORY_NICE_TICKS.length - 1];
        for (var ni = 0; ni < HISTORY_NICE_TICKS.length; ni++) {
          if (rangeSec / HISTORY_NICE_TICKS[ni] <= 30) { t = HISTORY_NICE_TICKS[ni]; break; }
        }
        return t;
      }
      // Пресеты диапазона: от «сейчас» назад на N часов. Шаг сетки и лейбл
      // масштаба — автоматика внутри drawTimeline.
      document.querySelectorAll('.history-range-btns .time-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
          document.querySelectorAll('.history-range-btns .time-btn').forEach(function (b) { b.classList.remove('active'); });
          btn.classList.add('active');
          var hours = parseInt(btn.dataset.rangeHours, 10) || 24;
          timelineRange.from = Date.now() / 1000 - hours * 3600;
          timelineRange.to = Date.now() / 1000;
          timelineOffset = 0;
          loadTimeline();
        });
      });

      var applyBtn = document.getElementById('history-apply');
      if (applyBtn) {
        applyBtn.addEventListener('click', function () {
          var f = document.getElementById('history-from').value;
          var t = document.getElementById('history-to').value;
          if (f) timelineRange.from = new Date(f).getTime() / 1000;
          if (t) timelineRange.to = new Date(t).getTime() / 1000;
          document.querySelectorAll('.history-range-btns .time-btn').forEach(function (b) { b.classList.remove('active'); });
          timelineOffset = 0;
          loadTimeline();
        });
      }

      // Фильтр по принтеру: timelinePrinter давно есть в коде и API, UI не было.
      (function initHistoryPrinterFilter() {
        var btn = document.getElementById('historyPrinterBtn');
        var dropdown = document.getElementById('historyPrinterDropdown');
        var labelEl = document.getElementById('historyPrinterLabel');
        if (!btn || !dropdown) return;
        btn.addEventListener('click', function (e) {
          e.stopPropagation();
          if (dropdown.classList.contains('open')) {
            dropdown.classList.remove('open');
            return;
          }
          var opts = '<button type="button" class="sort-option' + (timelinePrinter ? '' : ' active') + '" data-pid="">Все принтеры</button>';
          cachedAllPrinters.slice().sort(function (a, b) {
            return String(a.label || '').localeCompare(String(b.label || ''), 'ru', { numeric: true });
          }).forEach(function (p) {
            opts += '<button type="button" class="sort-option' + (timelinePrinter === p.id ? ' active' : '') + '" data-pid="' + safeText(p.id, '') + '">' + safeText(p.label, p.id) + '</button>';
          });
          dropdown.innerHTML = opts;
          dropdown.classList.add('open');
        });
        dropdown.addEventListener('click', function (e) {
          var opt = e.target.closest('.sort-option');
          if (!opt) return;
          e.stopPropagation();
          timelinePrinter = opt.dataset.pid || null;
          if (labelEl) labelEl.textContent = opt.textContent;
          dropdown.classList.remove('open');
          timelineOffset = 0;
          loadTimeline();
        });
        document.addEventListener('click', function () { dropdown.classList.remove('open'); });
      })();

      var timelineCanvas = document.getElementById('timeline-canvas');
      if (timelineCanvas) {
        var resizeObserver = new ResizeObserver(function () {
          if (timelineData.length > 0) drawTimeline();
        });
        resizeObserver.observe(timelineCanvas);
      }

      // --- Admin panel ---
      function escHtml(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
      }

      // ===== Вкладка AMS: влажность / температура / сушка =====
      var amsHours = 24;
      var amsInited = false;
      function amsPanelActive() {
        var p = document.querySelector('.view-panel[data-panel="ams"]');
        return p && p.classList.contains('active');
      }
      function amsInit() {
        if (amsInited) return;
        amsInited = true;
        var rng = document.getElementById('amsRange');
        if (rng) rng.querySelectorAll('button').forEach(function (b) {
          b.addEventListener('click', function () {
            amsHours = parseInt(b.dataset.h, 10) || 24;
            rng.querySelectorAll('button').forEach(function (x) { x.classList.toggle('active', x === b); });
            loadAms();
          });
        });
        // Данные пишутся раз в минуту — чаще опрашивать смысла нет.
        setInterval(function () { if (amsPanelActive()) loadAms(); }, 60000);
      }
      function amsHumClass(pct, lvl) {
        if (pct != null) return pct <= 25 ? 'ok' : pct <= 40 ? 'warn' : 'bad';
        if (lvl != null) return lvl <= 2 ? 'ok' : lvl === 3 ? 'warn' : 'bad';
        return '';
      }
      function amsHumColorVar(cls) {
        return cls === 'bad' ? 'var(--danger)' : cls === 'warn' ? 'var(--warn)' : 'var(--ok)';
      }
      // SVG-линия по точкам [{t,v}] в окне [fr,to]; bands=цветовые зоны влажности.
      function amsChart(points, fr, to, ymin, ymax, color, bands) {
        var W = 378, H = 88, PL = 26, PR = 8, PT = 8, PB = 16;
        if (!points.length) return '<div class="ams-nodata">нет данных за период</div>';
        var iw = W - PL - PR, ih = H - PT - PB;
        function X(t) { return PL + iw * Math.max(0, Math.min(1, (t - fr) / (to - fr))); }
        function Y(v) { return PT + ih * (1 - (v - ymin) / (ymax - ymin)); }
        var line = '', area = '';
        points.forEach(function (p, i) {
          line += (i ? 'L' : 'M') + X(p.t).toFixed(1) + ' ' + Y(p.v).toFixed(1) + ' ';
        });
        area = line + 'L' + X(points[points.length - 1].t).toFixed(1) + ' ' + (PT + ih) +
          ' L' + X(points[0].t).toFixed(1) + ' ' + (PT + ih) + ' Z';
        var gl = '', yl = '';
        for (var k = 0; k <= 2; k++) {
          var val = ymin + (ymax - ymin) * k / 2, y = PT + ih * (1 - k / 2);
          gl += '<line class="gridline" x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1) + '"/>';
          yl += '<text class="axis" x="' + (PL - 4) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + Math.round(val) + '</text>';
        }
        var bandRects = '';
        if (bands) {
          [[0, 25, 'var(--ok)'], [25, 40, 'var(--warn)'], [40, 50, 'var(--danger)']].forEach(function (z) {
            var yTop = Y(Math.min(z[1], ymax)), yBot = Y(Math.max(z[0], ymin));
            bandRects += '<rect x="' + PL + '" y="' + yTop.toFixed(1) + '" width="' + iw +
              '" height="' + (yBot - yTop).toFixed(1) + '" fill="' + z[2] + '" opacity="0.05"/>';
          });
        }
        var gid = 'amsg' + Math.round(Math.abs(color.length * 13 + ymax * 7 + points.length));
        var xl = '<text class="axis" x="' + PL + '" y="' + (H - 4) + '" text-anchor="start">-' + amsHours + 'ч</text>' +
          '<text class="axis" x="' + (W - PR) + '" y="' + (H - 4) + '" text-anchor="end">сейчас</text>';
        return '<svg class="ams-chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
          '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0" stop-color="' + color + '" stop-opacity="0.28"/>' +
          '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
          bandRects + gl +
          '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
          '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round"/>' +
          yl + xl + '</svg>';
      }
      function amsCardShell(u) {
        var cls = 'ams-card' + (u.drying ? ' drying' : '') +
          (!u.drying && amsHumClass(u.humidity_pct, u.humidity) === 'bad' ? ' bad' : '');
        var hcls = amsHumClass(u.humidity_pct, u.humidity);
        var humTxt = u.humidity_pct != null ? '💧 ' + u.humidity_pct + '%'
          : (u.humidity != null ? '💧 ' + u.humidity + '/5' : '💧 н/д');
        var badges = '<span class="ams-badge ' + hcls + '">' + humTxt + '</span>';
        if (u.temp != null) badges += '<span class="ams-badge">🌡 ' + Math.round(u.temp) + '°</span>';
        var slots = '<div class="ams-slots">' + (u.slots || []).map(function (s, i) {
          if (s.empty) return '<div class="ams-slot empty"></div>';
          var active = u.tray_now_local === i;
          var color = (typeof s.color === 'string' && /^[0-9a-fA-F]{3,8}$/.test(s.color)) ? '#' + s.color : '';
          return '<div class="ams-slot' + (active ? ' active' : '') + '"' + (color ? ' style="background:' + color + '"' : '') + '></div>';
        }).join('') + '</div>';
        var dry = u.drying
          ? '<div class="ams-dry"><span>🔥</span><span class="lbl">Идёт сушка</span>' +
            '<span class="rem">' + (u.dry_time != null ? 'осталось ' + amsFmtMin(u.dry_time) : '') + '</span></div>'
          : '';
        var key = u.printer_id + '__' + u.unit_index;
        var model = u.device_type ? escHtml(u.device_type) : 'AMS';
        return '<div class="ams-card' + cls.slice(8) + '" data-key="' + key + '">' +
          '<div class="ams-chead"><div class="ams-spool">🧵</div>' +
          '<div class="ams-ctitle"><div class="ams-cname">' + escHtml(u.label) + ' · AMS ' + (u.unit_index + 1) +
          ' <span class="ams-dot ' + (u.online ? 'on' : 'off') + '"></span></div>' +
          '<div class="ams-csub">' + model + '</div></div>' +
          '<div class="ams-badges">' + badges + '</div></div>' +
          slots + dry +
          '<div class="ams-gwrap"><div class="ams-gtitle"><span class="t">' +
          (u.humidity_pct != null ? 'Влажность, %' : 'Уровень влажности (0–5)') + '</span>' +
          '<span class="mm" data-mm="hum-' + key + '"></span></div>' +
          '<div data-g="hum-' + key + '"><div class="ams-nodata">загрузка…</div></div></div>' +
          '<div class="ams-gwrap"><div class="ams-gtitle"><span class="t">Температура, °C</span>' +
          '<span class="mm"></span></div>' +
          '<div data-g="temp-' + key + '"><div class="ams-nodata">загрузка…</div></div></div>' +
          '</div>';
      }
      function amsFmtMin(m) {
        m = parseInt(m, 10) || 0;
        var h = Math.floor(m / 60), mm = m % 60;
        return h > 0 ? h + ':' + (mm < 10 ? '0' : '') + mm : m + ' мин';
      }
      function amsDrawGraphs(key, u, rows, fr, to) {
        var humBox = document.querySelector('[data-g="hum-' + key + '"]');
        var tempBox = document.querySelector('[data-g="temp-' + key + '"]');
        var mm = document.querySelector('[data-mm="hum-' + key + '"]');
        if (!humBox || !tempBox) return;
        var usePct = u.humidity_pct != null;
        var humPts = [], tempPts = [];
        rows.forEach(function (r) {
          var v = usePct ? r.humidity_pct : r.humidity_idx;
          if (v != null) humPts.push({ t: r.recorded_at, v: v });
          if (r.temp != null) tempPts.push({ t: r.recorded_at, v: r.temp });
        });
        var hcls = amsHumClass(u.humidity_pct, u.humidity);
        humBox.innerHTML = usePct
          ? amsChart(humPts, fr, to, 10, 50, amsHumColorVar(hcls), true)
          : amsChart(humPts, fr, to, 0, 5, 'var(--ok)', false);
        tempBox.innerHTML = amsChart(tempPts, fr, to, 20, 60, 'var(--info)', false);
        if (mm && humPts.length) {
          var vals = humPts.map(function (p) { return p.v; });
          var suf = usePct ? '%' : '';
          mm.textContent = 'мин ' + Math.round(Math.min.apply(null, vals)) + suf +
            ' · макс ' + Math.round(Math.max.apply(null, vals)) + suf;
        }
      }
      async function loadAms() {
        amsInit();
        var grid = document.getElementById('amsGrid');
        var empty = document.getElementById('amsEmpty');
        if (!grid) return;
        var units;
        try {
          var resp = await apiFetch('/api/ams/current');
          units = await resp.json();
        } catch (e) { console.error('loadAms current failed', e); return; }
        if (!units.length) { grid.innerHTML = ''; if (empty) empty.style.display = ''; return; }
        if (empty) empty.style.display = 'none';
        function sev(u) {
          if (u.drying) return 0;
          var c = amsHumClass(u.humidity_pct, u.humidity);
          return c === 'bad' ? 1 : c === 'warn' ? 2 : 3;
        }
        units.sort(function (a, b) { return sev(a) - sev(b) || String(a.label).localeCompare(String(b.label)); });
        grid.innerHTML = units.map(amsCardShell).join('');
        var to = Date.now() / 1000, fr = to - amsHours * 3600;
        units.forEach(function (u) {
          var key = u.printer_id + '__' + u.unit_index;
          var url = '/api/ams/history?printer_id=' + encodeURIComponent(u.printer_id) +
            '&unit=' + u.unit_index + '&fr=' + fr + '&to=' + to;
          apiFetch(url).then(function (r) { return r.json(); }).then(function (d) {
            amsDrawGraphs(key, u, d.rows || [], fr, to);
          }).catch(function (e) { console.error('ams history failed', e); });
        });
      }

      var ROLE_BADGES = {
        admin: '<span class="badge badge-admin">Админ</span>',
        viewer: '<span class="badge badge-viewer">Зритель</span>'
      };

      function pad2(n) { return (n < 10 ? '0' : '') + n; }

      function fmtAdmDate(ts) {
        var d = new Date(ts * 1000);
        return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear();
      }

      function fmtAuditTime(ts) {
        var d = new Date(ts * 1000);
        return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
      }

      function uaDevice(ua) {
        ua = ua || '';
        var br = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera'
          : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome'
          : /Safari\//.test(ua) ? 'Safari' : '';
        var os = /Windows/.test(ua) ? 'Windows' : /Android/.test(ua) ? 'Android'
          : /iPhone|iPad/.test(ua) ? 'iOS' : /Mac OS X/.test(ua) ? 'macOS'
          : /Linux/.test(ua) ? 'Linux' : '';
        return [br, os].filter(Boolean).join(' · ');
      }

      function lastLoginCell(ll) {
        if (!ll) return '<td class="muted">—</td>';
        var dev = uaDevice(ll.user_agent);
        return '<td class="ll-cell" title="' + escHtml(ll.user_agent || '') + '">' +
          '<span class="mono dim">' + fmtAuditTime(ll.created_at) + '</span>' +
          '<div class="ll-sub"><span class="mono">' + escHtml(ll.ip_address || '—') + '</span>' +
          (dev ? ' · ' + escHtml(dev) : '') + '</div></td>';
      }

      async function loadAdminUsers() {
        var tbody = document.querySelector('#adminUsersTable tbody');
        if (!tbody) return;
        try {
          var res = await apiFetch('/api/admin/users');
          var users = await res.json();
          tbody.innerHTML = users.map(function(u) {
            var uid = escHtml(u.id), uname = escHtml(u.username), urole = escHtml(u.role);
            return '<tr><td class="mono muted">' + escHtml(u.id) + '</td><td><b>' + uname + '</b></td>' +
              '<td>' + (ROLE_BADGES[u.role] || urole) + '</td>' +
              '<td class="mono dim">' + fmtAdmDate(u.created_at) + '</td>' +
              lastLoginCell(u.last_login) + '<td class="adm-actions">' +
              '<button class="ghost-btn" data-uact="role" data-uid="' + uid + '" data-uname="' + uname + '" data-urole="' + urole + '">Роль</button> ' +
              '<button class="ghost-btn" data-uact="pass" data-uid="' + uid + '" data-uname="' + uname + '">Пароль</button> ' +
              '<button class="ghost-btn danger" data-uact="del" data-uid="' + uid + '" data-uname="' + uname + '">Удалить</button></td></tr>';
          }).join('');
          if (!tbody.dataset.uactBound) {
            tbody.dataset.uactBound = '1';
            tbody.addEventListener('click', function(ev) {
              var btn = ev.target.closest('button[data-uact]');
              if (!btn) return;
              var id = btn.dataset.uid, name = btn.dataset.uname, role = btn.dataset.urole;
              if (btn.dataset.uact === 'role') window._adminToggleRole(id, name, role);
              else if (btn.dataset.uact === 'pass') window._adminEditUser(id, name);
              else if (btn.dataset.uact === 'del') window._adminDeleteUser(id, name);
            });
          }
        } catch (e) { console.error('loadAdminUsers', e); }
      }

      var auditOffset = 0;
      var AUDIT_LIMIT = 50;
      var auditHasMore = false;
      var auditLoading = false;

      async function loadAdminAudit(reset) {
        if (auditLoading) return;
        if (reset) { auditOffset = 0; auditHasMore = false; }
        auditLoading = true;
        var tbody = document.querySelector('#adminAuditTable tbody');
        var loader = document.getElementById('adminAuditLoader');
        if (!tbody) { auditLoading = false; return; }
        if (loader) loader.style.display = '';
        if (reset) tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-dim);padding:20px;">Загрузка...</td></tr>';
        try {
          var res = await apiFetch('/api/admin/audit?limit=' + AUDIT_LIMIT + '&offset=' + auditOffset);
          var data = await res.json();
          var entries = data.rows || [];
          auditHasMore = data.has_more;
          if (auditOffset === 0) tbody.innerHTML = '';
          var AUDIT_BADGES = {
            login_ok: ['\u0412\u0445\u043e\u0434', 'ok'],
            login_fail: ['\u041d\u0435\u0443\u0434\u0430\u0447\u043d\u044b\u0439 \u0432\u0445\u043e\u0434', 'err'],
            rate_limit_block: ['\u0411\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0430', 'err'],
            logout: ['\u0412\u044b\u0445\u043e\u0434', 'steel'],
            token_refresh: ['\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u0442\u043e\u043a\u0435\u043d\u0430', 'steel'],
            user_create: ['\u0421\u043e\u0437\u0434\u0430\u043d \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c', 'admin'],
            user_update: ['\u0418\u0437\u043c\u0435\u043d\u0451\u043d \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c', 'admin'],
            user_delete: ['\u0423\u0434\u0430\u043b\u0451\u043d \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c', 'warn'],
            printer_create: ['\u0414\u043e\u0431\u0430\u0432\u043b\u0435\u043d \u043f\u0440\u0438\u043d\u0442\u0435\u0440', 'admin'],
            printer_update: ['\u0418\u0437\u043c\u0435\u043d\u0451\u043d \u043f\u0440\u0438\u043d\u0442\u0435\u0440', 'admin'],
            printer_delete: ['\u0423\u0434\u0430\u043b\u0451\u043d \u043f\u0440\u0438\u043d\u0442\u0435\u0440', 'warn'],
            printer_restore: ['\u0412\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u043f\u0440\u0438\u043d\u0442\u0435\u0440', 'steel'],
            printers_apply: ['\u041f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u0430 \u043a\u043e\u043d\u0444\u0438\u0433\u0443\u0440\u0430\u0446\u0438\u044f', 'ok'],
            printers_discard: ['\u041f\u0440\u0430\u0432\u043a\u0438 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u044b', 'steel'],
            settings_update: ['\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u044b \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438', 'admin']
          };
          var html = '';
          for (var i = 0; i < entries.length; i++) {
            var e = entries[i];
            var ua = (e.user_agent || '').substring(0, 50);
            var b = AUDIT_BADGES[e.action] || [e.action, 'steel'];
            html += '<tr><td class="mono dim">' + fmtAuditTime(e.created_at) + '</td>' +
              '<td><b>' + escHtml(e.user_id || '\u2014') + '</b></td>' +
              '<td><span class="badge badge-' + b[1] + '">' + escHtml(b[0]) + '</span></td>' +
              '<td class="mono muted">' + escHtml(e.ip_address || '\u2014') + '</td>' +
              '<td class="ua-cell muted" title="' + escHtml(e.user_agent || '') + '">' + escHtml(ua) + '</td></tr>';
          }
          tbody.insertAdjacentHTML('beforeend', html);
          auditOffset += entries.length;
        } catch (e) { console.error('loadAdminAudit', e); }
        finally {
          auditLoading = false;
          if (loader) loader.style.display = auditHasMore ? '' : 'none';
        }
      }

      function setupAuditScroll() {
        var sentinel = document.getElementById('adminAuditSentinel');
        if (sentinel) {
          var obs = new IntersectionObserver(function(entries) {
            if (entries[0].isIntersecting && auditHasMore && !auditLoading) {
              loadAdminAudit(false);
            }
          }, { rootMargin: '200px' });
          obs.observe(sentinel);
        }
      }
      setupAuditScroll();

      window._adminDeleteUser = async function(id, username) {
        if (!confirm('Удалить пользователя ' + username + '?')) return;
        try {
          var res = await apiFetch('/api/admin/users/' + id, { method: 'DELETE' });
          if (res.ok) loadAdminUsers();
          else { var d = await res.json(); alert(d.detail || 'Ошибка'); }
        } catch (e) { alert('Ошибка сети'); }
      };

      window._adminToggleRole = async function(id, username, role) {
        if (currentUser && username === currentUser.username) {
          alert('Свою роль менять нельзя — иначе можно остаться без админов.');
          return;
        }
        var newRole = role === 'admin' ? 'viewer' : 'admin';
        var label = newRole === 'admin' ? 'Админ' : 'Зритель';
        if (!confirm('Сменить роль ' + username + ' на «' + label + '»? Активные сессии пользователя завершатся.')) return;
        try {
          var res = await apiFetch('/api/admin/users/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: newRole }),
          });
          if (res.ok) { showToast('Роль обновлена', true); loadAdminUsers(); }
          else { var d = await res.json(); alert(d.detail || 'Ошибка'); }
        } catch (e) { alert('Ошибка сети'); }
      };

      window._adminEditUser = async function(id, username) {
        var newPass = prompt('Новый пароль для ' + username + ' (мин 8 символов):');
        if (!newPass) return;
        if (newPass.length < 8) { alert('Пароль должен быть не менее 8 символов'); return; }
        try {
          var res = await apiFetch('/api/admin/users/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: newPass }),
          });
          if (res.ok) { showToast('Пароль обновлён', true); }
          else { var d = await res.json(); alert(d.detail || 'Ошибка'); }
        } catch (e) { alert('Ошибка сети'); }
      };

      (function() {
        var addUserBtn = document.getElementById('addUserBtn');
        if (addUserBtn) {
          addUserBtn.addEventListener('click', function() {
            var username = document.getElementById('newUsername').value.trim();
            var password = document.getElementById('newPassword').value;
            var role = document.getElementById('newUserRole').value;
            var err = document.getElementById('adminError');
            err.textContent = '';
            if (!username || !password) { err.textContent = 'Заполните логин и пароль'; return; }
            if (password.length < 8) { err.textContent = 'Пароль должен быть не менее 8 символов'; return; }
            apiFetch('/api/admin/users', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username: username, password: password, role: role }),
            }).then(function(res) { return res.json().then(function(data) { return {ok: res.ok, data: data}; }); })
              .then(function(r) {
                if (r.ok) {
                  document.getElementById('newUsername').value = '';
                  document.getElementById('newPassword').value = '';
                  loadAdminUsers();
                } else {
                  err.textContent = r.data.detail || 'Ошибка';
                }
              })
              .catch(function() { err.textContent = 'Ошибка сети'; });
          });
        }
      })();

      // --- Admin: реестр принтеров ---
      // Правки копятся в БД (pending-бейджи), опрос живёт на старой конфигурации
      // до «Применить и перезапустить».
      var KIND_BADGES = {
        bambu: '<span class="badge badge-ok">Bambu</span>',
        creality: '<span class="badge badge-admin">Creality</span>',
        klipper: '<span class="badge badge-warn">Klipper</span>',
        mks: '<span class="badge badge-steel">MKS</span>'
      };
      var PENDING_BADGES = {
        new: '<span class="badge badge-ok">новый</span>',
        modified: '<span class="badge badge-warn">изменён</span>',
        deleted: '<span class="badge badge-err">удалён</span>'
      };
      var prnRows = [];
      var prnEditingId = null; // null = форма закрыта, '' = создание, иначе id
      var prnSort = { key: 'label', dir: 1 };

      function prnCompare(a, b) {
        var sa = a[prnSort.key] == null ? '' : String(a[prnSort.key]);
        var sb = b[prnSort.key] == null ? '' : String(b[prnSort.key]);
        // numeric: «(2)» < «(10)», IP-октеты сравниваются как числа
        return sa.localeCompare(sb, 'ru', { numeric: true, sensitivity: 'base' }) * prnSort.dir;
      }

      function prnById(id) {
        for (var i = 0; i < prnRows.length; i++) if (prnRows[i].id === id) return prnRows[i];
        return null;
      }

      // Прошивки Bambu вида "01.02.00.00": посегментное числовое сравнение.
      function fwCmp(a, b) {
        var pa = String(a).split('.'), pb = String(b).split('.');
        for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
          var na = parseInt(pa[i], 10) || 0, nb = parseInt(pb[i], 10) || 0;
          if (na !== nb) return na < nb ? -1 : 1;
        }
        return 0;
      }

      async function loadAdminPrinters() {
        var tbody = document.querySelector('#adminPrintersTable tbody');
        if (!tbody) return;
        try {
          var res = await apiFetch('/api/admin/printers');
          prnRows = await res.json();
          // Прошивка/флаг обновления приходят из живой телеметрии — сшиваем по id,
          // чтобы колонка сортировалась вместе с остальными полями строки.
          prnRows.forEach(function (r) {
            var st = (cachedAllPrinters || []).find(function (s) { return String(s.id) === String(r.id); });
            r.firmware = st && st.firmware_version ? st.firmware_version : '';
            r.fw_update = st ? st.fw_update : null;
          });
          // new_version_state принтер сообщает только с доступом к облаку Bambu;
          // в LAN/Developer Mode он молчит. Поэтому дополнительно сравниваем
          // прошивки внутри парка: если одномодельный собрат уже на более новой
          // версии — обновление точно существует.
          var fwMaxByModel = {};
          prnRows.forEach(function (r) {
            if (r.kind !== 'bambu' || !r.firmware || !r.model) return;
            var m = fwMaxByModel[r.model];
            if (!m || fwCmp(r.firmware, m) > 0) fwMaxByModel[r.model] = r.firmware;
          });
          prnRows.forEach(function (r) {
            if (r.kind !== 'bambu' || !r.firmware || !r.model) return;
            if (fwCmp(r.firmware, fwMaxByModel[r.model]) < 0) r.fw_update = true;
          });
          renderAdminPrinters();
        } catch (e) { console.error('loadAdminPrinters', e); }
      }

      function fwCell(p) {
        var v = p.firmware ? '<span class="mono dim">' + escHtml(p.firmware) + '</span>' : '<span class="muted">—</span>';
        // Показываем «не обновлять» для принтеров с закреплённой прошивкой
        // (флаг fw_pinned в реестре) — некоторые машины намеренно держат на
        // старой версии; без флага бейдж не показывается.
        if (p.fw_pinned === true) return v + ' <span class="fw-badge fw-lock">не обновлять 🔒</span>';
        if (p.fw_update === true) return v + ' <span class="fw-badge fw-avail">есть обновление</span>';
        return v;
      }

      function renderAdminPrinters() {
        var tbody = document.querySelector('#adminPrintersTable tbody');
        if (!tbody) return;
        document.querySelectorAll('#adminPrintersTable th.sortable').forEach(function (th) {
          th.classList.toggle('sorted-asc', th.dataset.sort === prnSort.key && prnSort.dir === 1);
          th.classList.toggle('sorted-desc', th.dataset.sort === prnSort.key && prnSort.dir === -1);
        });
        var pending = 0;
        tbody.innerHTML = prnRows.slice().sort(prnCompare).map(function (p) {
          if (p.pending) pending++;
          var name = '<b>' + escHtml(p.label) + '</b>' + (p.pending ? ' ' + PENDING_BADGES[p.pending] : '');
          var ip = '<span class="mono dim">' + escHtml(p.host) +
            (p.port ? '<span class="muted">:' + p.port + '</span>' : '') + '</span>';
          var code = p.access_code
            ? '<span class="secret">••••••••</span><button type="button" class="eye" data-act="eye" data-id="' + escHtml(p.id) + '" title="Показать">&#128065;</button>'
            : '<span class="muted">—</span>';
          var actions = p.pending === 'deleted'
            ? '<button type="button" class="ghost-btn" data-act="restore" data-id="' + escHtml(p.id) + '">Вернуть</button>'
            : '<button type="button" class="ghost-btn" data-act="edit" data-id="' + escHtml(p.id) + '">Изменить</button> ' +
              '<button type="button" class="ghost-btn danger" data-act="delete" data-id="' + escHtml(p.id) + '">Удалить</button>';
          return '<tr' + (p.pending === 'deleted' ? ' class="row-del"' : '') + '>' +
            '<td>' + name + '</td>' +
            '<td>' + (KIND_BADGES[p.kind] || escHtml(p.kind)) + '</td>' +
            '<td class="dim">' + escHtml(p.model || '—') + '</td>' +
            '<td>' + ip + '</td>' +
            '<td class="mono muted">' + escHtml(p.serial || '—') + '</td>' +
            '<td>' + fwCell(p) + '</td>' +
            '<td>' + code + '</td>' +
            '<td class="adm-actions">' + actions + '</td></tr>';
        }).join('');
        var bar = document.getElementById('printersApplyBar');
        if (bar) bar.style.display = pending ? '' : 'none';
        var cnt = document.getElementById('printersPendingCount');
        if (cnt) cnt.textContent = pending;
      }

      function prnSetKind(kind) {
        document.querySelectorAll('#prnKindSeg button').forEach(function (b) {
          b.classList.toggle('on', b.dataset.kind === kind);
        });
        document.querySelectorAll('#prnForm .fld').forEach(function (f) {
          f.style.display = (f.dataset.kinds || '').split(' ').indexOf(kind) >= 0 ? '' : 'none';
        });
      }

      function prnCurrentKind() {
        var on = document.querySelector('#prnKindSeg button.on');
        return on ? on.dataset.kind : 'bambu';
      }

      function prnOpenForm(id) {
        var form = document.getElementById('prnForm');
        if (!form) return;
        prnEditingId = id;
        form.style.display = '';
        document.getElementById('prnError').textContent = '';
        var p = id ? prnById(id) : null;
        document.getElementById('prnFormTitle').textContent = p ? 'Изменить: ' + p.label : 'Добавить принтер';
        document.querySelectorAll('#prnKindSeg button').forEach(function (b) { b.disabled = !!p; });
        var kind = p ? p.kind : 'bambu';
        prnSetKind(kind);
        document.getElementById('prnLabel').value = p ? p.label : '';
        document.getElementById('prnHost').value = p ? p.host : '';
        document.getElementById('prnPort').value = p && p.port ? p.port : '';
        document.getElementById('prnAccessCode').value = p && p.access_code ? p.access_code : '';
        document.getElementById('prnAccessCode').type = 'password';
        document.getElementById('prnSerial').value = p && p.serial ? p.serial : '';
        if (kind === 'bambu') document.getElementById('prnModelBambu').value = (p && p.model) || 'X1C';
        else if (kind === 'creality') document.getElementById('prnModelCreality').value = (p && p.model) || 'k1max';
        else document.getElementById('prnModelText').value = (p && p.model) || '';
        form.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }

      function prnCloseForm() {
        prnEditingId = null;
        var form = document.getElementById('prnForm');
        if (form) form.style.display = 'none';
      }

      async function prnSave() {
        var err = document.getElementById('prnError');
        err.textContent = '';
        var kind = prnCurrentKind();
        var payload = {
          label: document.getElementById('prnLabel').value.trim(),
          host: document.getElementById('prnHost').value.trim()
        };
        if (!payload.label || !payload.host) { err.textContent = 'Заполните название и IP'; return; }
        if (kind === 'bambu') {
          payload.model = document.getElementById('prnModelBambu').value;
          payload.access_code = document.getElementById('prnAccessCode').value.trim();
          payload.serial = document.getElementById('prnSerial').value.trim();
          if (!payload.access_code || !payload.serial) { err.textContent = 'Для Bambu нужны access code и серийный номер'; return; }
        } else if (kind === 'creality') {
          payload.model = document.getElementById('prnModelCreality').value;
        } else {
          var mt = document.getElementById('prnModelText').value.trim();
          if (mt) payload.model = mt;
          var port = parseInt(document.getElementById('prnPort').value, 10);
          if (port) {
            if (port < 1 || port > 65535) { err.textContent = 'Порт должен быть 1–65535'; return; }
            payload.port = port;
          }
        }
        var creating = !prnEditingId;
        if (creating) payload.kind = kind;
        try {
          var res = await apiFetch(creating ? '/api/admin/printers' : '/api/admin/printers/' + prnEditingId, {
            method: creating ? 'POST' : 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (res.ok) {
            prnCloseForm();
            showToast(creating ? 'Принтер добавлен' : 'Принтер обновлён', true);
            loadAdminPrinters();
          } else {
            var d = await res.json();
            err.textContent = d.detail || 'Ошибка';
          }
        } catch (e) { err.textContent = 'Ошибка сети'; }
      }

      function prnWaitForRestart() {
        showToast('Сервис перезапускается…', true);
        var start = Date.now();
        setTimeout(function poll() {
          fetch('/api/health').then(function (r) {
            if (r.ok) {
              showToast('Сервис перезапущен, конфигурация применена', true);
              loadAdminPrinters();
              return;
            }
            throw new Error('not up');
          }).catch(function () {
            if (Date.now() - start > 90000) {
              showToast('Сервис не поднялся за 90 сек — проверь вручную', false);
              return;
            }
            setTimeout(poll, 3000);
          });
        }, 6000);
      }

      (function initAdminPrinters() {
        var table = document.getElementById('adminPrintersTable');
        if (!table) return;
        table.addEventListener('click', function (e) {
          var btn = e.target.closest('button[data-act]');
          if (!btn) return;
          var id = btn.dataset.id;
          var p = prnById(id);
          if (btn.dataset.act === 'eye') {
            var secret = btn.previousElementSibling;
            if (secret && p) {
              var hidden = secret.textContent.indexOf('•') >= 0;
              secret.textContent = hidden ? p.access_code : '••••••••';
            }
          } else if (btn.dataset.act === 'edit') {
            prnOpenForm(id);
          } else if (btn.dataset.act === 'delete') {
            if (!p) return;
            if (!confirm('Удалить принтер «' + p.label + '»? История печати останется в БД.')) return;
            apiFetch('/api/admin/printers/' + id, { method: 'DELETE' }).then(function (res) {
              if (res.ok) { if (prnEditingId === id) prnCloseForm(); loadAdminPrinters(); }
              else res.json().then(function (d) { alert(d.detail || 'Ошибка'); });
            }).catch(function () { alert('Ошибка сети'); });
          } else if (btn.dataset.act === 'restore') {
            apiFetch('/api/admin/printers/' + id + '/restore', { method: 'POST' }).then(function (res) {
              if (res.ok) loadAdminPrinters();
            }).catch(function () { alert('Ошибка сети'); });
          }
        });
        table.querySelector('thead').addEventListener('click', function (e) {
          var th = e.target.closest('th.sortable');
          if (!th) return;
          if (prnSort.key === th.dataset.sort) prnSort.dir = -prnSort.dir;
          else prnSort = { key: th.dataset.sort, dir: 1 };
          renderAdminPrinters();
        });
        document.getElementById('addPrinterBtn').addEventListener('click', function () { prnOpenForm(''); });
        document.getElementById('prnSaveBtn').addEventListener('click', prnSave);
        document.getElementById('prnCancelBtn').addEventListener('click', prnCloseForm);
        document.querySelectorAll('#prnKindSeg button').forEach(function (b) {
          b.addEventListener('click', function () { if (!b.disabled) prnSetKind(b.dataset.kind); });
        });
        var eye = document.getElementById('prnAccessEye');
        eye.addEventListener('click', function () {
          var inp = document.getElementById('prnAccessCode');
          inp.type = inp.type === 'password' ? 'text' : 'password';
        });
        document.getElementById('printersDiscardBtn').addEventListener('click', function () {
          if (!confirm('Отменить все непримененные правки принтеров?')) return;
          apiFetch('/api/admin/printers/discard', { method: 'POST' }).then(function (res) {
            if (res.ok) { prnCloseForm(); showToast('Правки отменены', true); loadAdminPrinters(); }
          }).catch(function () { alert('Ошибка сети'); });
        });
        document.getElementById('printersApplyBtn').addEventListener('click', function () {
          if (!confirm('Применить изменения и перезапустить сервис? Дашборд будет недоступен ~10 секунд.')) return;
          apiFetch('/api/admin/printers/apply', { method: 'POST' }).then(function () {
            prnWaitForRestart();
          }).catch(function () { prnWaitForRestart(); });
        });
      })();

      /* ── Настройки сервера: Telegram и прокси (админ) ─────────────── */
      // Сохранение на лету: тумблеры — сразу, текстовые поля — с дебаунсом.
      // Бэкенд применяет без рестарта сервиса (смена токена/включение бота
      // перезапускает только телеграм-поток).
      var SRV_DEBOUNCE_MS = 600;
      var srvValues = null;       // последний известный values с сервера
      var srvTimers = {};         // дебаунс-таймеры по ключам

      function srvEl(id) { return document.getElementById(id); }

      function maskProxyUrl(url) {
        return url.replace(/(\/\/[^:@/]+):([^@]+)@/, '$1:•••@');
      }

      function srvFlash(id) {
        var el = srvEl(id);
        if (!el) return;
        el.classList.add('show');
        setTimeout(function () { el.classList.remove('show'); }, 1800);
      }

      function srvError(id, msg) {
        var el = srvEl(id);
        if (!el) return;
        if (msg) { el.textContent = msg; el.style.display = ''; }
        else { el.style.display = 'none'; }
      }

      function renderTgStatus(tg) {
        var line = srvEl('tgStatusLine');
        if (!line || !tg) return;
        var parts;
        if (!tg.enabled) {
          parts = ['<span class="srv-off">выключен</span>'];
        } else if (tg.restarting) {
          parts = ['<span class="srv-warn">перезапускается…</span>'];
        } else if (tg.running) {
          parts = ['<span class="srv-ok">работает</span>',
                   tg.chat_established ? 'чат установлен' : 'ожидает /start в чате'];
        } else {
          parts = ['<span class="srv-off">не запущен</span>'];
        }
        line.innerHTML = 'Статус: ' + parts.join(' · ');
      }

      function renderProxies(tg) {
        var tbody = document.querySelector('#proxyTable tbody');
        if (!tbody || !tg) return;
        var rows = tg.proxies.map(function (p) {
          var st = p.online
            ? '<span class="proxy-st ok"><span class="d"></span>онлайн</span>'
            : '<span class="proxy-st err"><span class="d"></span>офлайн</span>';
          var lat = p.latency_ms !== null ? '<span class="mono dim">' + p.latency_ms + ' мс</span>' : '<span class="muted">—</span>';
          var best = p.is_best ? '<span class="badge badge-ok">используется</span>' : '';
          return '<tr data-url="' + encodeURIComponent(p.url) + '">' +
            '<td class="mono dim proxy-url"><span class="proxy-url-text">' + escHtml(maskProxyUrl(p.url)) + '</span> ' +
            '<button type="button" class="eye" data-act="eye">👁</button></td>' +
            '<td>' + st + '</td><td>' + lat + '</td><td>' + best + '</td>' +
            '<td class="adm-actions"><button type="button" class="ghost-btn danger" data-act="del">Удалить</button></td></tr>';
        });
        tbody.innerHTML = rows.join('') ||
          '<tr><td colspan="5" class="muted" style="text-align:center;padding:14px">Прокси не настроены — бот ходит напрямую.</td></tr>';
        var lc = srvEl('proxyLastCheck');
        if (lc) {
          lc.textContent = 'Последняя проверка: ' + (tg.last_check
            ? new Date(tg.last_check * 1000).toLocaleTimeString('ru-RU') : '—');
        }
      }

      function fillServerSettings(values, tg) {
        srvValues = values;
        srvEl('tgEnabledToggle').checked = !!values.telegram_enabled;
        // Токен/поля не перетираем, пока в них печатают.
        [['tgToken', values.telegram_token],
         ['tgChatId', values.telegram_chat_id === null ? '' : String(values.telegram_chat_id)],
         ['tgUpdateInterval', String(values.telegram_update_interval)],
         ['tgFinishRepeatInterval', String(values.telegram_finish_repeat_interval_min)],
         ['tgTplFinish', values.telegram_finish_template],
         ['tgTplError', values.telegram_error_template],
         ['tgTplPaused', values.telegram_paused_template],
         ['proxyCheckInterval', String(values.proxy_check_interval)]
        ].forEach(function (pair) {
          var el = srvEl(pair[0]);
          if (el && document.activeElement !== el) el.value = pair[1];
        });
        srvEl('tgNotifyFinish').checked = !!values.telegram_notify_on_finish;
        srvEl('tgNotifyFinishRepeat').checked = !!values.telegram_notify_on_finish_repeat;
        srvEl('tgNotifyError').checked = !!values.telegram_notify_on_error;
        srvEl('tgNotifyPaused').checked = !!values.telegram_notify_on_paused;
        ['Finish', 'Error', 'Paused'].forEach(function (k) {
          var tpl = srvEl('tgTpl' + k);
          if (tpl) tpl.classList.toggle('tpl-dim', !srvEl('tgNotify' + k).checked);
        });
        renderTgStatus(tg);
        renderProxies(tg);
      }

      function loadServerSettings() {
        if (!isAdmin()) return;
        apiFetch('/api/admin/settings').then(function (res) { return res.json(); })
          .then(function (data) { fillServerSettings(data.values, data.telegram); })
          .catch(function () {});
      }

      function saveServerSettings(patch, flashId, errId) {
        return apiFetch('/api/admin/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch),
        }).then(function (res) {
          if (!res.ok) {
            return res.json().then(function (e) { throw new Error(e.detail || 'Ошибка сохранения'); });
          }
          return res.json();
        }).then(function (data) {
          srvError(errId, null);
          fillServerSettings(data.values, data.telegram);
          srvFlash(flashId);
          // Перезапуск телеграм-потока занимает до ~25 сек (long-poll) —
          // подтягиваем статус, чтобы «перезапускается…» сменилось итогом.
          if (data.bot_restarting) {
            setTimeout(loadServerSettings, 4000);
            setTimeout(loadServerSettings, 28000);
          }
          return data;
        }).catch(function (e) {
          srvError(errId, e.message);
          loadServerSettings(); // вернуть поля к сохранённому состоянию
          throw e;
        });
      }

      function srvDebounced(key, getPatch, flashId, errId) {
        clearTimeout(srvTimers[key]);
        srvTimers[key] = setTimeout(function () {
          var patch;
          try { patch = getPatch(); }
          catch (e) { srvError(errId, e.message); return; }
          saveServerSettings(patch, flashId, errId).catch(function () {});
        }, SRV_DEBOUNCE_MS);
      }

      function srvParseInt(el, name) {
        var v = String(el.value).trim();
        if (!/^\d+$/.test(v)) throw new Error(name + ': нужно целое число');
        return parseInt(v, 10);
      }

      (function initServerSettings() {
        var tgSection = srvEl('tgSection');
        if (!tgSection) return;

        srvEl('tgEnabledToggle').addEventListener('change', function () {
          saveServerSettings({ telegram_enabled: this.checked }, 'tgSavedFlash', 'tgError').catch(function () {});
        });
        srvEl('tgTokenEye').addEventListener('click', function () {
          var inp = srvEl('tgToken');
          inp.type = inp.type === 'password' ? 'text' : 'password';
        });
        srvEl('tgToken').addEventListener('input', function () {
          var el = this;
          srvDebounced('token', function () { return { telegram_token: el.value.trim() }; }, 'tgSavedFlash', 'tgError');
        });
        srvEl('tgChatId').addEventListener('input', function () {
          var el = this;
          srvDebounced('chat', function () {
            var v = el.value.trim();
            if (v === '') return { telegram_chat_id: null };
            if (!/^-?\d+$/.test(v)) throw new Error('Chat ID: нужно целое число');
            return { telegram_chat_id: parseInt(v, 10) };
          }, 'tgSavedFlash', 'tgError');
        });
        srvEl('tgUpdateInterval').addEventListener('input', function () {
          var el = this;
          srvDebounced('interval', function () {
            return { telegram_update_interval: srvParseInt(el, 'Интервал обновления') };
          }, 'tgSavedFlash', 'tgError');
        });
        srvEl('tgNotifyFinishRepeat').addEventListener('change', function () {
          saveServerSettings({ telegram_notify_on_finish_repeat: this.checked }, 'tgSavedFlash', 'tgError').catch(function () {});
        });
        srvEl('tgFinishRepeatInterval').addEventListener('input', function () {
          var el = this;
          srvDebounced('finishRepeat', function () {
            return { telegram_finish_repeat_interval_min: srvParseInt(el, 'Интервал повтора') };
          }, 'tgSavedFlash', 'tgError');
        });

        [['Finish', 'telegram_notify_on_finish', 'telegram_finish_template'],
         ['Error', 'telegram_notify_on_error', 'telegram_error_template'],
         ['Paused', 'telegram_notify_on_paused', 'telegram_paused_template']
        ].forEach(function (row) {
          var suffix = row[0], toggleKey = row[1], tplKey = row[2];
          srvEl('tgNotify' + suffix).addEventListener('change', function () {
            var patch = {};
            patch[toggleKey] = this.checked;
            saveServerSettings(patch, 'tgSavedFlash', 'tgError').catch(function () {});
          });
          srvEl('tgTpl' + suffix).addEventListener('input', function () {
            var el = this;
            srvDebounced(tplKey, function () {
              var patch = {};
              patch[tplKey] = el.value;
              return patch;
            }, 'tgSavedFlash', 'tgError');
          });
        });

        srvEl('proxyCheckInterval').addEventListener('input', function () {
          var el = this;
          srvDebounced('proxyInterval', function () {
            return { proxy_check_interval: srvParseInt(el, 'Интервал проверки') };
          }, 'proxySavedFlash', 'proxyError');
        });

        srvEl('proxyAddBtn').addEventListener('click', function () {
          var inp = srvEl('proxyAddInput');
          var url = inp.value.trim();
          if (!url) return;
          if (!/^https?:\/\//.test(url)) {
            srvError('proxyError', 'Прокси-URL должен начинаться с http:// или https://');
            return;
          }
          var list = (srvValues ? srvValues.proxy_list : []).concat([url]);
          saveServerSettings({ proxy_list: list }, 'proxySavedFlash', 'proxyError')
            .then(function () { inp.value = ''; })
            .catch(function () {});
        });
        srvEl('proxyAddInput').addEventListener('keydown', function (e) {
          if (e.key === 'Enter') srvEl('proxyAddBtn').click();
        });

        document.querySelector('#proxyTable tbody').addEventListener('click', function (e) {
          var btn = e.target.closest('button[data-act]');
          if (!btn) return;
          var tr = btn.closest('tr');
          var url = decodeURIComponent(tr.getAttribute('data-url') || '');
          if (btn.dataset.act === 'eye') {
            var span = tr.querySelector('.proxy-url-text');
            var revealed = span.getAttribute('data-revealed') === '1';
            span.textContent = revealed ? maskProxyUrl(url) : url;
            span.setAttribute('data-revealed', revealed ? '0' : '1');
          } else if (btn.dataset.act === 'del') {
            if (!confirm('Удалить прокси ' + maskProxyUrl(url) + '?')) return;
            var list = (srvValues ? srvValues.proxy_list : []).filter(function (p) { return p !== url; });
            saveServerSettings({ proxy_list: list }, 'proxySavedFlash', 'proxyError').catch(function () {});
          }
        });

        srvEl('proxyCheckNowBtn').addEventListener('click', function () {
          var btn = this;
          btn.disabled = true;
          btn.textContent = 'Проверяю…';
          apiFetch('/api/admin/settings/proxy-check', { method: 'POST' })
            .then(function (res) { return res.json(); })
            .then(function (data) { renderTgStatus(data.telegram); renderProxies(data.telegram); })
            .catch(function () {})
            .finally(function () { btn.disabled = false; btn.textContent = 'Проверить сейчас'; });
        });
      })();

      // Logout button (футер настроек)
      var logoutBtn = document.getElementById('logoutBtn');
      if (logoutBtn) {
        logoutBtn.addEventListener('click', function() {
          fetch('/api/auth/logout', { method: 'POST' }).then(function() {
            window.location.replace('/');
          }).catch(function() {
            window.location.replace('/');
          });
        });
      }

      // Версия деплоя из git на сервере — футер настроек
      (function loadAppVersion() {
        var el = document.getElementById('appVersion');
        if (!el) return;
        apiFetch('/api/version').then(function (res) { return res.json(); }).then(function (v) {
          var date = v.date ? v.date.split('-').reverse().join('.') : '';
          el.textContent = (v.commit || 'dev') + (date ? ' · ' + date : '');
        }).catch(function () {});
      })();

      // Загрузка админ-данных живёт в setActiveView — и клик по табу, и
      // восстановление вкладки после перезагрузки страницы проходят через неё.
    })();