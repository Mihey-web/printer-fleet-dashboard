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
        // The auto-dry section is read-only for viewers: re-render it with the role now known.
        if (amsAutodry) autodryRender();
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
        "bambu-p1": "/static/img/bambu-p2s.png",
        "bambu-p1p": "/static/img/bambu-p2s.png",
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
        "klipper-ghost6": "/static/img/flyingbear-ghost6-cutout.png",
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
      // Перевод этапа печати (stage) на русский. Ключи — значения
      // CURRENT_STAGE_IDS (app/vendor/pybambu/const.py) в нижнем регистре,
      // включая опечатку upstream check_plaform. Возвращает null, если
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
        paused_nozzle_clog: "Пауза: засор сопла",
        check_absolute_accuracy_before_calibration: "Проверка точности перед калибровкой",
        absolute_accuracy_calibration: "Калибровка абсолютной точности",
        check_absolute_accuracy_after_calibration: "Проверка точности после калибровки",
        calibrate_nozzle_offset: "Калибровка смещения сопел",
        bed_level_high_temperature: "Калибровка горячего стола",
        check_quick_release: "Проверка фиксатора хотенда",
        check_door_and_cover: "Проверка дверцы и крышки",
        laser_calibration: "Калибровка лазера",
        check_plaform: "Проверка платформы",
        check_birdeye_camera_position: "Проверка положения обзорной камеры",
        calibrate_birdeye_camera: "Калибровка обзорной камеры",
        bed_level_phase_1: "Калибровка стола: этап 1",
        bed_level_phase_2: "Калибровка стола: этап 2",
        heating_chamber: "Нагрев камеры",
        heated_bedcooling: "Охлаждение стола",
        print_calibration_lines: "Печать калибровочных линий",
        check_material: "Проверка материала",
        calibrating_live_view_camera: "Калибровка камеры",
        waiting_for_heatbed_temperature: "Ожидание нагрева стола",
        check_material_position: "Проверка положения материала",
        calibrating_cutter_model_offset: "Калибровка смещения режущего модуля",
        measuring_surface: "Измерение поверхности",
        thermal_preconditioning: "Термостабилизация",
        homing_blade_holder: "Парковка держателя лезвия",
        calibrating_camera_offset: "Калибровка смещения камеры",
        calibrating_blade_holder_position: "Калибровка положения держателя лезвия",
        hotend_pick_place_test: "Тест захвата хотенда",
        waiting_chamber_temperature_equalize: "Выравнивание температуры камеры",
        preparing_hotend: "Подготовка хотенда",
        calibrating_detection_nozzle_clumping: "Калибровка детекции налипания на сопло",
        purifying_chamber_air: "Очистка воздуха камеры",
        measuring_rotary_attachment: "Измерение поворотной насадки",
        moving_toolhead_above_purge_chute: "Перемещение головы к жёлобу сброса",
        cooling_nozzle: "Охлаждение сопла",
        moving_toolhead_to_center_of_heatbed: "Перемещение головы в центр стола",
        active_arc_fitting: "Активная подгонка дуг",
        hotend_type_detection: "Определение типа хотенда",
        build_plate_alignment_detection: "Проверка положения печатной пластины",
        heatbed_surface_foreign_object_detection: "Поиск посторонних предметов на столе",
        heatbed_underside_foreign_object_detection: "Поиск посторонних предметов под столом",
        pre_extrusion_before_printing: "Предэкструзия перед печатью",
        preparing_ams: "Подготовка AMS"
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

      const STATE_ORDER = ["printing", "idle", "paused", "error", "finished", "offline", "nodata"];
      const STATE_LABELS_RU = {
        printing: "печатал", idle: "простой", paused: "пауза",
        error: "ошибка", finished: "завершено", offline: "офлайн", unknown: "неизвестно", nodata: "нет данных"
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
            // share of the time with data: a history gap ("нет данных") is not idle time
            var knownSecs = totalSecs - (states.nodata || 0);
            var printPct = knownSecs > 0 ? ((states.printing || 0) / knownSecs * 100) : 0;
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
      function tempChip(kind, name, cur, target, editable, icName) {
        icName = icName || kind;
        if (cur == null || !isFinite(cur)) return "";
        var val = Math.round(cur);
        var cls = "pc-chip";
        var arrow = "";
        var hasTarget = target != null && target > 0;
        // The target only while really heating/cooling (same > 3° as pc-heating): a
        // nozzle wanders ±1–2° around it, and "256° →255°" widened the chip and
        // wrapped the whole row. The target is always in the tooltip.
        var moving = hasTarget && Math.abs(cur - target) > 3;
        if (moving) arrow = ' <span class="pc-target">→' + Math.round(target) + '</span>';
        if (moving) cls += " pc-heating";
        else if (val >= 40) cls += " pc-hot";
        var tip = name + (hasTarget ? " · цель " + Math.round(target) + "°" : "");
        var edit = editable ? ' pc-editable" data-edit="' + kind + '" data-cur="' + (hasTarget ? Math.round(target) : val) : '';
        return '<span class="' + cls + edit + '" data-tip="' + tip + '">' + icon(icName) + val + '°' + arrow + '</span>';
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
      function amsChip(p, editable) {
        var a = p.ams;
        if (!a || !a.units || !a.units.length) return "";
        var trayNow = a.tray_now;
        var parts = [];
        var globalIdx = 0;
        var drying = false;
        for (var u = 0; u < a.units.length; u++) {
          var unit = a.units[u];
          var unitDrying = dryActive(unit);
          if (unitDrying) drying = true;
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
          // While drying, the time left takes the AMS temperature's place (that
          // goes into the tooltip): the chip shares a nowrap row and must not grow.
          var remaining = unit.dry ? unit.dry.remaining_min : unit.dry_time;
          if (unitDrying && remaining > 0) {
            var dtip = dryStatusText(unit) + (unit.temp != null ? " · в AMS " + Math.round(unit.temp) + "°" : "");
            parts.push('<span class="pc-ams-temp" data-tip="' + dtip + '">' + icon("hourglass") + amsFmtMin(remaining) + '</span>');
          } else if (unit.temp != null) {
            var ttip = unitDrying ? dryStatusText(unit) : "Температура AMS";
            parts.push('<span class="pc-ams-temp" data-tip="' + ttip + '">' + icon("steam") + Math.round(unit.temp) + '°</span>');
          }
        }
        var edit = editable ? ' pc-editable" data-edit="ams' : '';
        return '<span class="pc-chip pc-ams' + (drying ? " pc-ams-drying" : "") + edit + '"><span class="pc-ams-lbl" data-tip="AMS">' + icon("spool") + '</span>' + parts.join("") + '</span>';
      }
      /* ams-ui:begin */
      // AMS load / unload, drying, filament-change progress and card indicators:
      // pure helpers only (no DOM, no globals besides safeText, icon, amsUnitName
      // and amsFmtMin), run in node by tests/test_web_ams_ui.py. Every device
      // string goes through safeText, colours only as 3-8 hex digits. Texts for
      // confirm() are plain text and stay unescaped.
      var DRY_STATUS_LABELS = {
        off: "Сушка выключена", checking: "Сушка: проверка", drying: "Сушка",
        cooling: "Остывание после сушки", stopping: "Сушка останавливается",
        error: "Ошибка сушки — см. экран принтера", overheat: "Перегрев AMS — сушку не остановить",
        testing: "Заводской тест сушки"
      };
      var DRY_PHASE_LABELS = { heating: "нагрев", dehumidify: "осушение" };
      var FILAMENT_ACTION_LABELS = { load: "Загрузка филамента", unload: "Выгрузка филамента" };
      var FILAMENT_STEP_LABELS = {
        heat: "нагрев сопла", cut: "отрезка филамента", pull: "извлечение текущего филамента",
        push: "подача нового филамента", confirm: "подтвердите выдавливание на экране принтера",
        purge: "очистка от старого филамента", check: "проверка положения"
      };
      var NOZZLE_TYPE_LABELS = {
        stainless_steel: "нержавеющая сталь", hardened_steel: "закалённая сталь",
        tungsten_carbide: "карбид вольфрама", brass: "латунь", tpu_high_flow: "высокий поток для TPU"
      };
      var DRY_TEMP_MIN = 45, DRY_HOURS_MAX = 24;

      function dryActive(unit) {
        if (!unit) return false;
        if (unit.dry) return !!unit.dry.active;
        return unit.dry_time != null && unit.dry_time > 0;
      }
      // "Сушка · осушение · 55 °C · PETG · осталось 11:52" (HTML-escaped).
      function dryStatusText(unit) {
        var dry = unit && unit.dry;
        var active = dryActive(unit);
        var parts = [];
        if (dry && dry.status) {
          parts.push(DRY_STATUS_LABELS[dry.status] || "Сушка");
          if (dry.phase && DRY_PHASE_LABELS[dry.phase]) parts.push(DRY_PHASE_LABELS[dry.phase]);
        } else {
          parts.push(active ? "Сушка" : "Сушка выключена");
        }
        if (active && dry) {
          if (dry.temp) parts.push(Number(dry.temp) + " °C");
          if (dry.filament) parts.push(safeText(dry.filament, ""));
        }
        var remaining = dry ? dry.remaining_min : unit && unit.dry_time;
        if (active && remaining > 0) parts.push("осталось " + amsFmtMin(remaining));
        return parts.join(" · ");
      }
      // Why the drying temperature is capped (HTML-escaped).
      function dryLimitText(dry) {
        if (!dry) return "";
        var by = dry.max_by === "?" ? "катушка без материала" : safeText(dry.max_by, "");
        if (dry.feeding) {
          return "не выше " + dry.max_temp + " °C — идёт печать из этого AMS" + (dry.max_by ? " (" + by + ")" : "");
        }
        if (dry.max_by) return "не выше " + dry.max_temp + " °C — в AMS есть " + by;
        return DRY_TEMP_MIN + "–" + dry.max_temp + " °C";
      }
      function filamentChangeText(fc) {
        if (!fc) return null;
        var text = FILAMENT_ACTION_LABELS[fc.action] || "Смена филамента";
        if (fc.step && FILAMENT_STEP_LABELS[fc.step]) text += " · " + FILAMENT_STEP_LABELS[fc.step];
        return text;
      }
      function nozzleTypeLabel(type) {
        if (type && NOZZLE_TYPE_LABELS[type]) return NOZZLE_TYPE_LABELS[type];
        if (type && type.indexOf("high_flow_") === 0) {
          var base = NOZZLE_TYPE_LABELS[type.slice(10)];
          return "высокий поток" + (base ? ", " + base : "");
        }
        return "тип неизвестен";
      }
      // {text: "Ø0.4 HF", tip, standard}; standard = a plain 0.4 mm nozzle, which
      // the card shows only in the nozzle temperature tooltip.
      function nozzleChip(nozzle) {
        if (!nozzle || !(nozzle.diameter > 0)) return null;
        var type = typeof nozzle.type === "string" ? nozzle.type : "";
        var d = Math.round(nozzle.diameter * 100) / 100;
        var suffix = type === "tpu_high_flow" ? " TPU" : type.indexOf("high_flow_") === 0 ? " HF" : "";
        return { text: "Ø" + d + suffix, tip: "Сопло " + d + " мм · " + nozzleTypeLabel(type),
                 standard: d === 0.4 && !suffix };
      }
      // Why AMS load / unload is not possible right now, or null.
      function filamentOpsBlock(p) {
        if (p.dual_nozzle) return "Принтеры с двумя экструдерами пока не поддерживаются — загрузка и выгрузка с экрана принтера";
        if (p.filament_swap_ok === false) return "Прошивка не поддерживает смену филамента командой";
        if (p.filament_swap_ok !== true) return "Версия прошивки ещё не получена — загрузка и выгрузка пока недоступны";
        if (p.filament_change) return "Идёт смена филамента…";
        if (p.state === "printing" || p.state === "paused") return "Только в простое — принтер печатает или на паузе";
        return null;
      }
      function amsUnits(p) {
        return (p && p.ams && p.ams.units) || [];
      }
      // Unload of the external spool from the card's AMS button and its popover (printers without an AMS).
      function externalUnloadOk(p) {
        return !amsUnits(p).length && p.filament_loaded === true && !p.grace_period_active && !filamentOpsBlock(p);
      }
      function amsHex(color) {
        return typeof color === "string" && /^[0-9a-fA-F]{3,8}$/.test(color) ? color : "";
      }
      function amsSlotDot(slot) {
        var hex = slot.empty ? "" : amsHex(slot.color);
        return '<span class="pc-ams-slot' + (slot.empty ? " empty" : "") + '"' + (hex ? ' style="background:#' + hex + '"' : "") + '></span>';
      }
      function amsPopHtml(p) {
        var a = p.ams || {};
        var units = amsUnits(p);
        var block = filamentOpsBlock(p);
        var canSwap = p.filament_swap_ok === true && !p.dual_nozzle;
        // Why load / unload is off: once, under the first unit's slots and above
        // every drying control (drying is allowed while printing).
        var note = block ? '<div class="pc-pop-note">' + block + '</div>' : "";
        var html = "", base = 0;
        units.forEach(function (unit) {
          var dry = unit.dry;
          var head = amsUnitName(unit.index) + (dry && dry.type === "n3f" ? " · AMS 2 Pro" : "")
            + (unit.humidity_pct != null ? " · 💧" + Number(unit.humidity_pct) + "%" : "");
          html += '<div class="pc-pop-h">' + head + '</div>';
          var slots = unit.slots || [];
          if (canSwap) {
            html += '<div class="pc-pop-presets">';
            slots.forEach(function (slot, i) {
              var num = base + i + 1;
              if (slot.empty) {
                html += '<button type="button" class="pc-pop-preset" disabled>' + amsSlotDot(slot) + num + ' · пусто</button>';
                return;
              }
              var type = safeText(slot.type, "?");
              if (a.tray_now === base + i && p.filament_loaded === true) {
                html += '<button type="button" class="pc-pop-preset on" disabled title="Загружен сейчас">' + amsSlotDot(slot) + num + ' · ' + type + '</button>';
                return;
              }
              var rng = slot.nozzle_temp ? " · " + Number(slot.nozzle_temp[0]) + "–" + Number(slot.nozzle_temp[1]) + " °C" : "";
              html += '<button type="button" class="pc-pop-preset" data-ams-load="' + Number(unit.index) + ':' + i + '" title="Загрузить: ' + type + rng + '"'
                + (block ? " disabled" : "") + '>' + amsSlotDot(slot) + num + ' · ' + type + '</button>';
            });
            html += '</div>';
          }
          html += note;
          note = "";
          base += slots.length;
          if (!dry) return;
          var alert = dry.status === "error" || dry.status === "overheat";
          html += '<div class="pc-pop-note' + (alert ? " warn" : "") + '">' + dryStatusText(unit) + '</div>';
          var blockers = dry.blockers || [];
          blockers.forEach(function (b) {
            html += '<div class="pc-pop-note warn">Сушка недоступна: ' + safeText(b.text, "") + '</div>';
          });
          if (dryActive(unit)) {
            html += '<div class="pc-pop-row"><button type="button" class="pc-pop-ok" data-dry-stop="' + Number(unit.index) + '">Остановить сушку</button></div>';
          } else if (p.ams_dry_ok === true && !blockers.length && dry.status !== "stopping") {
            html += '<div class="pc-pop-row"><button type="button" class="pc-pop-ok" data-dry-open="' + Number(unit.index) + '">Сушить…</button></div>';
          }
        });
        html += note;   // no units
        if (canSwap && !block && p.filament_loaded === true && a.tray_now != null && a.tray_now !== 255) {
          html += '<div class="pc-pop-row"><button type="button" class="pc-pop-ok" data-ams-unload>Выгрузить</button></div>';
        }
        return html;
      }
      function dryPopHtml(p, unit, sel) {
        var dry = unit.dry;
        sel = sel || dry.suggest || {};
        var html = '<div class="pc-pop-h">Сушка · ' + amsUnitName(unit.index) + '</div><div class="pc-pop-presets">';
        (dry.presets || []).forEach(function (pr) {
          var name = safeText(pr.filament, "");
          html += '<button type="button" class="pc-pop-preset' + (pr.filament === sel.filament ? " on" : "") + '" data-dry-mat="' + name
            + '" data-temp="' + Number(pr.temp) + '" data-hours="' + Number(pr.hours) + '">' + name + '</button>';
        });
        html += '</div><div class="pc-pop-row">'
          + '<input type="text" inputmode="numeric" class="pc-pop-in pc-pop-num" data-dry="temp" value="' + Number(sel.temp) + '"><span class="pc-pop-unit">°C</span>'
          + '<input type="text" inputmode="numeric" class="pc-pop-num" data-dry="hours" value="' + Number(sel.hours) + '"><span class="pc-pop-unit">ч</span></div>'
          + '<div class="pc-pop-note">' + dryLimitText(dry) + '</div>'
          + '<div class="pc-pop-row"><button type="button" class="pc-pop-preset" data-ams-back>← Назад</button>'
          + '<button type="button" class="pc-pop-ok" data-dry-start="' + Number(unit.index) + '">Начать сушку</button></div>';
        return html;
      }
      // Drying form values, or {bad: "temp"|"hours"|"mat"} for the field to fix.
      function dryFormCheck(dry, filament, tempStr, hoursStr) {
        function num(s) { return /^\s*\d+\s*$/.test(s || "") ? parseInt(s, 10) : NaN; }
        var temp = num(tempStr), hours = num(hoursStr);
        if (!filament) return { bad: "mat" };
        if (!(temp >= DRY_TEMP_MIN && temp <= dry.max_temp)) return { bad: "temp" };
        if (!(hours >= 1 && hours <= DRY_HOURS_MAX)) return { bad: "hours" };
        return { temp: temp, hours: hours, filament: filament };
      }
      function amsLoadConfirmText(p, ams, slot) {
        var num = 0, type = "?", units = amsUnits(p);
        for (var u = 0; u < units.length; u++) {
          var slots = units[u].slots || [];
          if (units[u].index === ams) {
            num += slot + 1;
            type = (slots[slot] && slots[slot].type) || "?";
            break;
          }
          num += slots.length;
        }
        return "Загрузить слот " + num + " (" + type + ") на «" + p.label + "»? Принтер нагреет сопло"
          + (p.filament_loaded === true ? ", отрежет и уберёт текущий филамент." : " и подаст филамент.");
      }
      function amsUnloadConfirmText(p) {
        var external = !amsUnits(p).length || (p.ams && p.ams.tray_now === 254);
        return "Выгрузить филамент на «" + p.label + "»? Принтер нагреет сопло и "
          + (external ? "выведет филамент из экструдера." : "вернёт филамент в AMS.");
      }
      // The AMS tab card's drying line (u: an /api/ams/current unit; can: this
      // viewer may send commands to the unit's printer): status, the reasons a
      // drying cannot start, and Stop / Dry… (the drying form popover).
      function amsTabDryHtml(u, p, can) {
        var attrs = ' data-pid="' + safeText(u.printer_id, "") + '" data-ams="' + Number(u.unit_index) + '"';
        var dry = u.dry;
        if (dryActive(u)) {
          return '<div class="ams-dry"><span>🔥</span><span class="lbl">' + dryStatusText(u) + '</span>'
            + (can && dry ? '<button type="button" class="pc-ctl" data-ams-dry-stop' + attrs + '>Стоп</button>' : "") + '</div>';
        }
        if (!dry) return "";
        var blockers = dry.blockers || [];
        var startable = !!(can && p && p.ams_dry_ok === true && !blockers.length && dry.status !== "stopping");
        if (!blockers.length && !startable) return "";
        var text = dryStatusText(u) + blockers.map(function (b) {
          return '<br><span class="warn">Сушка недоступна: ' + safeText(b.text, "") + '</span>';
        }).join("");
        return '<div class="ams-dry off"><span class="lbl">' + text + '</span>'
          + (startable ? '<button type="button" class="pc-ctl" data-ams-dry-open' + attrs + '>Сушить…</button>' : "") + '</div>';
      }
      /* ams-ui:end */
      /* autodry-ui:begin */
      // AMS auto-drying: status lines, plans, the rules form, chart overlays,
      // journal rows and audit badges — pure helpers only (no DOM, no globals
      // besides safeText, amsUnitName, amsFmtMin and the ams-ui block), run in
      // node by tests/test_web_autodry_ui.py. Every server / device string goes
      // through safeText; the returned texts are HTML.
      var AD_FAULT_RU = { error: "ошибка", overheat: "перегрев" };
      var AD_HM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
      var AD_UNIT_DEFAULT = { enabled: true, mode: "inherit", temp: 55, hours: 8 };
      var AD_JOURNAL_BADGES = {
        start: ["Старт", "admin"], repeat: ["Повтор", "admin"], run_now: ["Вручную", "admin"],
        manual: ["Вручную", "admin"], finish: ["Финиш", "ok"], foreign_start: ["Чужая сушка", "steel"],
        foreign_end: ["Чужая: конец", "steel"], interrupted: ["Срыв", "err"], disabled: ["Отключена", "err"],
        lost: ["Прервана", "warn"], fault: ["Ошибка", "err"], manual_stop: ["Стоп", "warn"],
        refused: ["Отказ", "warn"], blocked: ["Ждёт", "warn"], released: ["Пауза снята", "steel"],
        reenabled: ["Включена", "steel"], dry_run: ["Пробный", "steel"], unreachable: ["Цель", "warn"]
      };
      var AUDIT_PREFIX_BADGES = {
        autodry_start: ["Автосушка: старт", "ok"], autodry_disable: ["Автосушка: блок отключён", "warn"],
        autodry_config: ["Настройки автосушки", "admin"], autodry_run_now: ["Сушка вручную", "admin"],
        autodry_stop: ["Стоп сушки", "warn"], autodry_release: ["Пауза снята", "steel"],
        printer_command: ["Команда принтеру", "steel"]
      };

      function adPlural(n, one, few, many) {
        n = Math.abs(Math.round(Number(n) || 0));
        if (n % 10 === 1 && n % 100 !== 11) return one;
        if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) return few;
        return many;
      }
      function adPad(n) { return (n < 10 ? "0" : "") + n; }
      // 425 -> "7 ч 05 мин", 40 -> "40 мин" (non-breaking: a unit never wraps alone)
      function adFmtHM(min) {
        min = Math.max(0, Math.round(Number(min) || 0));
        var h = Math.floor(min / 60), m = min % 60;
        return h ? h + "\u00a0ч " + adPad(m) + "\u00a0мин" : m + "\u00a0мин";
      }
      // time left as the AMS cards show it ("7:05", "40 мин"), unbreakable
      function adLeft(min) { return amsFmtMin(min).replace(/ /g, "\u00a0"); }
      // "HH:MM" on the same local day as nowTs, else "DD.MM HH:MM" (browser time)
      function adClock(ts, nowTs) {
        if (ts == null || !isFinite(ts)) return "—";
        var d = new Date(ts * 1000), n = new Date((nowTs == null ? ts : nowTs) * 1000);
        var hm = adPad(d.getHours()) + ":" + adPad(d.getMinutes());
        if (d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()) return hm;
        return adPad(d.getDate()) + "." + adPad(d.getMonth() + 1) + " " + hm;
      }
      function adNum(v) { return v == null || !isFinite(v) ? "—" : String(Math.round(Number(v))); }
      // A unit's automation status for the card line and the table:
      // {cls, card, main, sub, noKey, link: "release" | "reenable" | null}, HTML-escaped.
      function autodryLine(u, cfg, limits, nowTs) {
        var s = (u && u.status) || { kind: "pending" };
        cfg = cfg || {};
        limits = limits || {};
        var S = adNum(cfg.start_rh), T = adNum(cfg.target_rh), hold = adNum(cfg.hold_h), max = Number(cfg.max_runs) || 0;
        var rest = s.rest || {};
        var rh = u && u.rh != null ? adNum(u.rh) : adNum(rest.last);
        var lim = Number(limits.interrupt_limit) || 2;
        var rem = s.remaining_min != null ? s.remaining_min : (u && u.dry ? u.dry.remaining_min : null);
        var until = adClock(s.until, nowTs);
        function L(cls, card, main, sub, link) {
          return { cls: cls, card: card, main: main, sub: sub, noKey: cls === "alert", link: link || null };
        }
        var r;
        switch (s.kind) {
          case "pending": r = L("off", "проверяем…", "—", "автосушка ещё не проверила блок"); break;
          case "off":
            if (s.reason === "interrupts") {
              r = L("alert", "⚠ сушка срывается · блок отключён", "⚠ Выкл: " + lim + " " + adPlural(lim, "срыв", "срыва", "срывов") + " подряд",
                    "проверьте AMS, потом «включить снова»", "reenable");
            } else if (s.scope === "master") {
              r = L("off", "выкл (общий выключатель)" + (s.drying ? " · идущую сушку не трогаем" : ""), "Выкл", "общий выключатель");
            } else {
              r = L("off", "выкл" + (s.drying ? " · идущую сушку не трогаем" : ""), "Выкл", "только наблюдаем · сейчас " + rh + "%");
            }
            break;
          case "offline": r = L("off", "принтер офлайн", "Офлайн", "ждём связи с принтером"); break;
          case "unsupported":
            r = s.reason === "locked"
              ? L("off", "прошивка блокирует сторонние команды", "Недоступна", "команды с дашборда заблокированы — не отправляем")
              : L("off", "прошивка не даёт сушить удалённо", "Недоступна", "прошивка не сообщает о поддержке удалённой сушки");
            break;
          case "empty": r = L("off", "нет катушек", "Нет катушек", "зарядите катушки — автосушка их ждёт"); break;
          case "no_sensor": r = L("off", "нет данных о влажности", "Нет данных", "датчик влажности молчит"); break;
          case "wait":
            if (rest.reason === "holding") {
              var held = rest.above_since != null ? (nowTs - rest.above_since) / 60 : 0;
              r = L("wait", "ждёт · " + rh + "% держится " + adFmtHM(held) + " из " + hold + "\u00a0ч", "Ждёт",
                    "≥" + S + "% с " + adClock(rest.above_since, nowTs) + " · старт в " + adClock(rest.ready_at, nowTs));
            } else if (rest.reason === "after_dry") {
              var from = rest.ready_at != null ? rest.ready_at - (Number(cfg.hold_h) || 0) * 3600 : null;
              r = L("wait", "ждёт · покой считаем с " + adClock(from, nowTs), "Ждёт", "после сушки — " + hold + "\u00a0ч покоя до старта");
            } else if (rest.reason === "no_data" || rest.reason === "drying") {
              r = L("wait", "ждёт · мало данных о влажности", "Ждёт", "мало замеров за последние " + hold + "\u00a0ч");
            } else {
              r = L("wait", "ждёт · " + rh + "% (старт от " + S + "%)", "Ждёт", rh + "% — ниже старта " + S + "%");
            }
            break;
          case "dry_run":
            r = L("foreign", "пробный режим: запустили бы сейчас", "Пробный режим",
                  (u && u.plan ? adNum(u.plan.temp) + "° · " + adNum(u.plan.hours) + "\u00a0ч — " : "") + "команды не отправляются");
            break;
          case "refused":
            r = L("alert", "старт не прошёл · повтор в " + until, "Отказ", safeText(s.detail, "отказ") + " · повтор в " + until);
            r.noKey = false;
            break;
          case "blocked": r = L("wait", "ждёт: " + safeText(s.text, ""), "Ждёт", safeText(s.text, "")); break;
          case "printing": r = L("wait", "ждёт конца печати", "Ждёт печать", "сушка во время печати выключена в правилах"); break;
          case "limit":
            r = L("wait", "лимит " + max + " " + adPlural(max, "прогон", "прогона", "прогонов") + " в сутки · дальше после " + until,
                  "Лимит", adNum(s.runs_24h) + " из " + max + " за 24\u00a0ч (с чужой сушкой) · следующий не раньше " + until);
            break;
          case "window":
            var w = s.window || cfg.window || {};
            r = L("wait", "ждёт окна с " + safeText(w.from, "—"), "Ждёт окна",
                  "окно " + safeText(w.from, "—") + "–" + safeText(w.to, "—") + " (МСК)");
            break;
          case "starting": r = L("dry", "запускаем сушку…", "Запуск", "ждём подтверждения от AMS"); break;
          case "dry":
            var left = rem != null ? " · ещё " + adFmtHM(rem) : "";
            var what = (rem != null ? "осталось " + adLeft(rem) + " · " : "") + adNum(s.temp) + "° · " + safeText(s.filament, "—");
            if (s.origin === "auto") {
              r = L("dry", "сушит · прогон " + adNum(s.series) + left, "Сушит · прогон " + adNum(s.series),
                    what + " · " + adNum(u && u.runs_24h) + (max ? " из " + max : "") + " за сутки");
            } else {
              r = L("dry", "сушит (вручную)" + left, "Сушит вручную", what + " · в лимит не входит");
            }
            break;
          case "foreign":
            r = L("foreign", "чужая сушка" + (rem != null ? " · ещё " + adFmtHM(rem) : "") + " — не вмешиваемся", "Чужая сушка",
                  "с " + adClock(s.since, nowTs) + (rem != null ? " · осталось " + adLeft(rem) : "") + " · засчитаем как прогон");
            break;
          case "fault":
            if (s.reason === "stuck") {
              r = L("alert", "⚠ сушка идёт дольше плана — проверьте блок", "⚠ Дольше плана",
                    (rem != null ? "осталось " + adLeft(rem) + " · " : "") + "сами не останавливаем");
            } else {
              var ru = AD_FAULT_RU[s.reason] || "ошибка";
              r = L("alert", "⚠ ошибка сушки AMS (" + ru + ") — проверьте блок", "⚠ Ошибка сушки", ru + " · сами не останавливаем");
            }
            break;
          case "cool":
            if (s.measuring) {
              var warm = s.temp != null && s.temp_max != null && s.temp > s.temp_max;
              r = warm
                ? L("cool", "остывает · " + adNum(s.temp) + "° → ≤" + adNum(s.temp_max) + "°, замер не позже " + adClock(s.deadline, nowTs),
                    "Остывает", "тёплый AMS занижает влажность — ждём " + adNum(s.temp_max) + "° (до сушки было " + adNum(s.t_before) + "°)")
                : L("cool", "остывает · замер влажности…", "Замер", "ждём свежих замеров влажности");
            } else {
              var after = s.end_kind === "stop" ? "остановки" : s.end_kind === "foreign_end" ? "чужой сушки" : "прогона " + adNum(s.series);
              r = L("cool", "остывает после " + after + " · замер в " + until, "Остывает",
                    "закончилась в " + adClock(s.dry_end_at, nowTs) + " · замер в " + until + ", цель ≤ " + T + "%");
            }
            break;
          case "pause":
            var mh = adNum(cfg.manual_hold_h);
            if (s.reason === "interrupted") {
              r = L("alert", "⚠ сушка срывается · пауза до " + until, "⚠ Срыв · пауза до " + until,
                    "погасла раньше срока (" + adNum(s.interrupts) + " из " + lim + " подряд)", "release");
            } else if (s.reason === "foreign_stop") {
              r = L("pause", "пауза до " + until + " — чужую сушку остановили раньше срока", "Пауза до " + until,
                    "кто-то остановил сушку — не трогаем " + mh + "\u00a0ч", "release");
            } else {
              r = L("pause", "пауза до " + until + " — остановлена вручную", "Пауза до " + until,
                    "остановлена вручную · правило " + mh + "\u00a0ч", "release");
            }
            break;
          case "backoff":
            r = L("alert", "⚠ цель не достигается · новая серия после " + until, "⚠ Цель не достигается",
                  max + " " + adPlural(max, "прогон", "прогона", "прогонов") + " подряд выше " + T + "% — проверьте осушитель/цель", "release");
            break;
          default: r = L("off", "—", "—", "");
        }
        if (s.repeat && ["window", "printing", "refused", "blocked", "dry_run"].indexOf(s.kind) >= 0) {
          r.card = "повтор (" + adNum(s.repeat.rh) + "% > " + adNum(s.repeat.target) + "%) · " + r.card;
        }
        return r;
      }
      // The plan of a unit for a mode (D14: computed by the server; "custom" is
      // reproduced from u.plan_ctx). The caller resolves "inherit".
      function autodryPlanFor(u, mode, custom) {
        if (!u) return null;
        if (mode === "custom") {
          var c = u.plan_ctx;
          if (!c || !custom) return null;
          var t = Math.max(DRY_TEMP_MIN, Math.min(c.cap, Number(custom.temp)));
          var why = "задано вручную" + (Number(custom.temp) > c.cap ? " · потолок " + c.model + " " + c.cap + "°" : "");
          var limit = null;
          if (t > c.max_temp) { t = c.max_temp; limit = c.limit_text; }
          return { mode: "custom", name: "Своё", filament: c.softest, temp: t,
                   hours: Math.max(1, Math.min(DRY_HOURS_MAX, Number(custom.hours))), why: why, warn: false,
                   limit: limit, empty: false };
        }
        return (u.plans && u.plans[mode]) || null;
      }
      // The «Прогон» cell of a unit for its plan (null: none). Slots without fed filament
      // still hold spools: such a unit dries too, PLA-safe unless its own mode says otherwise.
      function autodryRunCell(u, p) {
        if (!u || !u.dry) return '<b>—</b><span class="why">блок не на связи</span>';
        var note = "";
        if (!p) return "<b>—</b>" + note;
        return "<b>" + safeText(p.name, "") + " → " + Number(p.temp) + "° · " + Number(p.hours) + "\u00a0ч</b>" +
          (note || (p.why ? '<span class="why' + (p.warn ? " warn" : "") + '">' + safeText(p.why, "") + "</span>" : "")) +
          (p.limit ? '<span class="why prn">' + safeText(p.limit, "") + "</span>" : "");
      }
      function adInt(s) {
        s = s == null ? "" : String(s);
        return /^\s*-?\d+\s*$/.test(s) ? parseInt(s, 10) : NaN;
      }
      // The rules form (input strings, checkbox booleans) -> {config} or {error, field}.
      // Integers everywhere; with `ranges` (limits.ranges) also the custom temperature and
      // hours, global and per unit, in the server's words. The server validates the rest.
      function autodryFormToConfig(form, base, ranges) {
        function outOf(k, v) {
          var r = ranges && ranges[k];
          return r && r.length === 2 && (v < r[0] || v > r[1]) ? r : null;
        }
        function rangeText(label, r, unit) { return label + ": целое " + r[0] + "–" + r[1] + unit; }
        var cfg = JSON.parse(JSON.stringify(base || {}));
        cfg.enabled = !!form.master;
        cfg.dry_run = !!(base && base.dry_run);
        cfg.while_printing = !!form.print;
        cfg.mode = form.mode;
        var nums = [["hold", "hold_h", "Не дёргаться от скачков"],
                    ["target", "target_rh", "Держать влажность не выше"], ["custT", "custom_temp", "Своя температура"],
                    ["custH", "custom_hours", "Своя длительность"], ["pause", "pause_h", "Пауза между прогонами"],
                    ["limit", "max_runs", "Лимит"], ["holdOff", "manual_hold_h", "После ручной остановки"]];
        for (var i = 0; i < nums.length; i++) {
          var v = adInt(form[nums[i][0]]);
          if (isNaN(v)) return { error: nums[i][2] + ": нужно целое число", field: nums[i][0] };
          cfg[nums[i][1]] = v;
        }
        // One number for the owner (12.10): "keep it not above N %". The start
        // threshold is derived — one point above — so the two can never clash.
        var tr = outOf("target_rh", cfg.target_rh);
        if (tr) return { error: rangeText("Держать влажность не выше", tr, " %"), field: "target" };
        cfg.start_rh = cfg.target_rh + 1;
        var bad = outOf("custom_temp", cfg.custom_temp);
        if (bad) return { error: rangeText("Своя температура", bad, " °C"), field: "custT" };
        bad = outOf("custom_hours", cfg.custom_hours);
        if (bad) return { error: rangeText("Своя длительность", bad, " ч"), field: "custH" };
        if (form.win === "night") cfg.window = { from: "22:00", to: "08:00" };
        else if (form.win === "custom") {
          var a = String(form.winFrom || "").trim(), b = String(form.winTo || "").trim();
          if (!AD_HM_RE.test(a)) return { error: "Окно запуска: время ЧЧ:ММ", field: "winFrom" };
          if (!AD_HM_RE.test(b)) return { error: "Окно запуска: время ЧЧ:ММ", field: "winTo" };
          cfg.window = { from: a, to: b };
        } else cfg.window = null;
        cfg.units = cfg.units || {};
        var units = form.units || {};
        for (var key in units) {
          if (!Object.prototype.hasOwnProperty.call(units, key)) continue;
          var f = units[key], prev = cfg.units[key] || AD_UNIT_DEFAULT;
          var ent = { enabled: !!f.enabled, mode: f.mode || "inherit", temp: prev.temp, hours: prev.hours };
          var ft = adInt(f.temp), fh = adInt(f.hours);
          var bt = isNaN(ft) ? null : outOf("custom_temp", ft), bh = isNaN(fh) ? null : outOf("custom_hours", fh);
          // outside the unit's own mode a bad value is not sent: the saved one is kept
          if (!isNaN(ft) && !bt) ent.temp = ft;
          else if (ent.mode === "custom") {
            return { error: bt ? rangeText("Своя температура блока", bt, " °C") : "Своя температура блока: нужно целое число",
                     field: "u:" + key + ":temp" };
          }
          if (!isNaN(fh) && !bh) ent.hours = fh;
          else if (ent.mode === "custom") {
            return { error: bh ? rangeText("Своя длительность блока", bh, " ч") : "Своя длительность блока: нужно целое число",
                     field: "u:" + key + ":hours" };
          }
          cfg.units[key] = ent;
        }
        delete cfg.rev;
        return { config: cfg };
      }
      // A comparable form of a config: sorted keys, default unit entries dropped.
      function autodryCanon(cfg) {
        cfg = cfg || {};
        var out = {}, keys = Object.keys(cfg).sort();
        keys.forEach(function (k) {
          if (k !== "units") { out[k] = cfg[k]; return; }
          var units = {};
          Object.keys(cfg.units || {}).sort().forEach(function (uk) {
            var e = cfg.units[uk] || {}, d = AD_UNIT_DEFAULT;
            if (e.enabled === d.enabled && e.mode === d.mode && e.temp === d.temp && e.hours === d.hours) return;
            units[uk] = { enabled: e.enabled, mode: e.mode, temp: e.temp, hours: e.hours };
          });
          out.units = units;
        });
        if (!("units" in out)) out.units = {};
        return JSON.stringify(out);
      }
      // History rows -> drying periods [[t0, t1], ...]: rows with dry_time > 0 at
      // most maxGapS apart merge; a lone row covers 90 s.
      function amsDryBands(rows, maxGapS) {
        var out = [], cur = null;
        (rows || []).forEach(function (r) {
          if (!(r && r.dry_time > 0)) { cur = null; return; }
          var t = r.recorded_at;
          if (cur && t - cur[1] <= maxGapS) cur[1] = t;
          else { cur = [t, t]; out.push(cur); }
        });
        return out.map(function (b) { return b[0] === b[1] ? [b[0], b[0] + 90] : b; });
      }
      // SVG markup over an AMS chart: drying periods and threshold lines (under:
      // between the grid and the area) and the threshold labels (over the line).
      // opts = {dry: bands, thr: [[value, label, colorVar], ...]}; a threshold
      // outside [ymin, ymax] is skipped.
      function amsChartOverlay(geo, fr, to, ymin, ymax, opts) {
        opts = opts || {};
        var iw = geo.W - geo.PL - geo.PR, ih = geo.H - geo.PT - geo.PB;
        function X(t) { return geo.PL + iw * Math.max(0, Math.min(1, (t - fr) / (to - fr))); }
        function Y(v) { return geo.PT + ih * (1 - (v - ymin) / (ymax - ymin)); }
        var under = "", labels = "";
        (opts.dry || []).forEach(function (b) {
          var a = Math.max(b[0], fr), e = Math.min(b[1], to);
          if (!(e > a)) return;
          var x0 = X(a), w = Math.max(1.5, X(e) - x0);
          under += '<rect x="' + x0.toFixed(1) + '" y="' + geo.PT + '" width="' + w.toFixed(1) + '" height="' + ih +
            '" fill="var(--warn)" opacity="0.11"/>' +
            '<rect x="' + x0.toFixed(1) + '" y="' + geo.PT + '" width="' + w.toFixed(1) + '" height="2" fill="var(--warn)" opacity="0.75"/>';
        });
        (opts.thr || []).forEach(function (t, i) {
          var v = Number(t[0]);
          if (!(v >= ymin && v <= ymax)) return;
          var y = Y(v);
          under += '<line class="thr" x1="' + geo.PL + '" y1="' + y.toFixed(1) + '" x2="' + (geo.W - geo.PR) + '" y2="' + y.toFixed(1) +
            '" stroke="' + t[2] + '" opacity="0.85"/>';
          labels += '<text class="thr-l" x="' + (geo.W - geo.PR - 2) + '" y="' + (i === 0 ? y - 2.5 : y + 8.5).toFixed(1) +
            '" text-anchor="end" fill="' + t[2] + '">' + safeText(t[1], "") + " " + adNum(v) + '%</text>';
        });
        return { under: under, labels: labels };
      }
      // Audit badge by the action's prefix (before the first ":"): [label, cls, tail] or null.
      function auditPrefixBadge(action) {
        if (action == null) return null;
        var s = String(action), i = s.indexOf(":");
        var b = AUDIT_PREFIX_BADGES[i < 0 ? s : s.slice(0, i)];
        return b ? [b[0], b[1], i < 0 ? "" : s.slice(i + 1)] : null;
      }
      // The section's banner lines (HTML): shadow mode (with its off button for admins),
      // the clock wait, a stalled or failing engine.
      function autodryBanner(ad, admin) {
        var e = ad.engine || {}, now = ad.now, lines = [];
        if (ad.config && ad.config.dry_run) {
          lines.push("<div>Пробный режим: автосушка только пишет в журнал, команды на принтеры не отправляет." +
            (admin ? ' <button type="button" class="ad-link ad-admin" id="adDryRunOff">выключить пробный режим</button>' : "") + "</div>");
        }
        if (e.clock_wait) lines.push("<div>Часы сервера отстают — автосушка ждёт синхронизации времени.</div>");
        var stalled = !!e.error || (e.started && ((e.last_tick == null && e.started_at != null && now - e.started_at > 300) ||
                                                   (e.last_tick != null && now - e.last_tick > 300)));
        if (stalled) {
          lines.push("<div>Автосушка не отвечает (" + (e.last_tick != null ? "последняя проверка в " + adClock(e.last_tick, now)
            : "проверок ещё не было") + ") — проверьте сервис.</div>");
        }
        return lines;
      }
      // One journal row (HTML).
      function autodryJournalRow(e, nowTs) {
        var b = AD_JOURNAL_BADGES[e.kind] || [safeText(e.kind, "?"), "steel"];
        var text = safeText(e.text, "") + (e.count > 1 ? ' <span class="jr-n">×' + Number(e.count) + '</span>' : "");
        var rh = "", cls = "jr-rh";
        var before = e.rh_before != null ? adNum(e.rh_before) + "%" : "?";
        if (e.rh_after != null) rh = "RH " + before + " → " + adNum(e.rh_after) + "%";
        else if (e.pending && e.rh_before != null) rh = "RH " + before + " → ?";
        else if (e.rh_before != null) { rh = "RH " + before; cls += " none"; }
        var title = e.t_before != null || e.t_after != null
          ? ' title="t° AMS: ' + (e.t_before != null ? adNum(e.t_before) + "°" : "?") + " → " +
            (e.t_after != null ? adNum(e.t_after) + "°" : "?") + '"' : "";
        return '<div class="jr-row"><span class="jr-t">' + adClock(e.ts, nowTs) + '</span><span class="jr-u">' + safeText(e.label, "") +
          '</span><span class="jr-k"><span class="badge badge-' + b[1] + '">' + b[0] + '</span></span><span class="jr-x">' + text +
          '</span><span class="' + cls + '"' + title + '>' + rh + '</span></div>';
      }
      /* autodry-ui:end */
      /* ctl-ui:begin */
      // Card control row, ⋯ menu, command results and adaptive chip rows: pure
      // helpers only (no DOM, no globals besides safeText, icon, amsUnitName,
      // amsFmtMin and the ams-ui block), run in node by tests/test_web_ctl_ui.py.
      // Server and device strings go through safeText, in data-tip and
      // aria-label too. What a printer can do comes from p.caps (the server's
      // printer_commands.caps_for): a control is shown only for caps[x] === true.
      var CTL_MSG = {   // pause_* … unknown mirror printer_commands.STATE_MSG verbatim (parity test)
        pause_paused: "Уже на паузе", pause_finished: "Печать завершена",
        pause_error: "Принтер в ошибке — см. карточку", pause_other: "Принтер не печатает",
        resume: "Принтер не на паузе", stop: "Печать не идёт — останавливать нечего",
        cooldown: "Во время печати остудить нельзя — сначала пауза или стоп",
        eject: "Стол опускается, только когда принтер стоит — не во время печати и не на паузе",
        unknown: "Состояние принтера неизвестно — повторите через несколько секунд",
        change: "Идёт смена филамента — дождитесь окончания",
        grace: "Нет связи с принтером — данные устарели",
        speed: "Скорость меняется только во время печати",
        busy: "Ждём ответа на предыдущую команду",
        eject_wait: "Стол уже опускается — подождите",          // = printer_commands.MSG_EJECT_INFLIGHT (parity)
        cool_paused: "Стол остынет — деталь может отклеиться. Подогреет ли принтер при продолжении — не проверено."
      };
      var NET_LOST = "связь прервалась — итог покажет статус принтера";
      var CMD_TEXT = {   // [while waiting / failed, after a confirmation]
        light_on: ["Свет", "Свет включён"], light_off: ["Свет", "Свет выключен"],
        pause: ["Пауза", "Пауза"], resume: ["Продолжить", "Печать продолжена"], stop: ["Стоп", "Печать остановлена"],
        cooldown: ["Остудить", "Остывание"], eject: ["Стол вниз", "Стол вниз"], speed: ["Скорость", "Скорость"],
        ams_load: ["Загрузка", "Загрузка"], ams_unload: ["Выгрузка", "Выгрузка"],
        dry_start: ["Сушка", "Сушка запущена"], dry_stop: ["Стоп сушки", "Сушка остановлена"],
        preheat: ["Нагрев", "Нагрев"], chamber: ["Камера", "Камера"], fans: ["Вентиляторы", "Вентиляторы"]
      };
      var SPEED_LEVELS = [["Тихий", 1, 50], ["Стандарт", 2, 100], ["Спорт", 3, 124], ["Безумный", 4, 166]];

      function ctlCaps(p) {
        return (p && p.caps) || {};
      }
      // Why `action` is blocked in the printer's current state (a CTL_MSG text), or null.
      function ctlStateBlock(action, p) {
        var s = p.state;
        var print = action === "pause" || action === "resume" || action === "stop";
        if (print && (s == null || s === "unknown")) return CTL_MSG.unknown;   // = printer_commands.state_refusal
        if (action === "pause") {
          if (s === "printing") return null;
          return s === "paused" ? CTL_MSG.pause_paused : s === "finished" ? CTL_MSG.pause_finished
            : s === "error" ? CTL_MSG.pause_error : CTL_MSG.pause_other;
        }
        if (action === "resume") return s === "paused" ? null : CTL_MSG.resume;
        if (action === "stop") return s === "printing" || s === "paused" ? null : CTL_MSG.stop;
        if (action === "cooldown" || action === "eject") {   // = printer_commands._state_check, fail closed
          if (p.filament_change) return CTL_MSG.change;
          if (s === "printing" || (action === "eject" && s === "paused")) return CTL_MSG[action];
          return s == null || s === "unknown" || s === "offline" ? CTL_MSG.unknown : null;
        }
        if (action === "speed") return s === "printing" || s === "paused" ? null : CTL_MSG.speed;
        return null;
      }
      // Speed level 1..4 whose percentage the printer reports, else null.
      function speedLevelOf(feedrate) {
        for (var i = 0; i < SPEED_LEVELS.length; i++) {
          if (SPEED_LEVELS[i][2] === feedrate) return SPEED_LEVELS[i][1];
        }
        return null;
      }
      // A server `detail` as text: FastAPI's 422 is a list of {msg, ...}.
      function detailText(d) {
        if (d == null || d === "") return "";
        if (typeof d === "string") return d;
        if (Array.isArray(d)) {
          return d.map(function (x) { return x && typeof x === "object" ? detailText(x.msg) : String(x); })
            .filter(function (t) { return t; }).join("; ");
        }
        if (typeof d === "object") return d.msg ? String(d.msg) : JSON.stringify(d);
        return String(d);
      }
      // One fetch -> {status, detail}. transport: "ok" (a response came back),
      // "lost" (dispatched, then a network error / abort / timeout: the printer
      // may have acted), "offline" (nothing was sent).
      function httpOutcome(httpStatus, body, transport) {
        if (transport === "offline") return { status: "rejected", detail: "Нет сети — команда не отправлена" };
        var json = body != null && typeof body === "object";
        if (transport === "lost" || !json) return { status: "sent", detail: NET_LOST };
        if (httpStatus >= 200 && httpStatus < 300) return { status: body.status || "sent", detail: detailText(body.detail) };
        if (httpStatus >= 400 && httpStatus < 500) {
          return { status: body.status || "rejected", detail: detailText(body.detail) || "Ошибка команды" };
        }
        return { status: "sent", detail: NET_LOST };
      }
      function cmdWaitLine(action) {
        return { k: "wait", t: (CMD_TEXT[action] || [action])[0] + " — ждём принтер…" };
      }
      function cmdResult(action, out) {
        var t = CMD_TEXT[action] || [action, action];
        if (out.status === "confirmed") {
          // a confirmation with a note (H2 / X2: what the second lamp did) shows the server's text
          var note = out.detail && out.detail !== "подтверждено принтером";
          return { k: "ok", t: t[1] + " — " + (note ? out.detail : "подтверждено") };
        }
        if (out.status === "sent") return { k: "unk", t: t[0] + " — " + (out.detail === NET_LOST ? NET_LOST : "отправлено, ответа нет") };
        return { k: "bad", t: t[0] + " — " + (out.detail || "ошибка") };
      }
      var EJECT_GUARD_MS = 20000;   // = printer_commands.EJECT_GUARD_SECONDS (parity test)
      // A card command's outcome -> what the card shows and keeps: the button's
      // mark (res; slot null = a menu item, no mark), the status line, the eject
      // guard (ejectUntil, ms; null = unchanged) and the printer fields to show
      // until the next poll (patch: a confirmed light only — an unconfirmed one
      // may have switched nothing).
      function cardAfterResult(action, slot, out, now) {
        var line = cmdResult(action, out);
        var light = action === "light_on" || action === "light_off";
        var went = out.status === "confirmed" || out.status === "sent";
        return { res: slot ? { slot: slot, k: line.k } : null, line: line,
                 patch: light && out.status === "confirmed" ? { light_on: action === "light_on" } : null,
                 ejectUntil: action === "eject" && went ? now + EJECT_GUARD_MS : null };
      }
      // The card's own answer under its row (not a global toast).
      function cmdLineHtml(line) {
        if (!line) return "";
        var lead = line.k === "wait" ? '<span class="spin-sm"></span>' : line.k === "ok" ? "✓"
          : line.k === "bad" ? "✕" : line.k === "unk" ? "?" : "ⓘ";
        return '<div class="pc-cmd ' + line.k + '" role="status"><span class="lead">' + lead + '</span><span class="t">'
          + safeText(line.t, "") + '</span>'
          + (line.k === "bad" ? '<button type="button" class="x" data-cmd="dismiss" aria-label="Закрыть">×</button>' : "")
          + '</div>';
      }
      // One icon button; `block` (a reason) makes it aria-disabled — never
      // `disabled`, so its tooltip and a phone tap still reach it.
      function ctlBtnHtml(slot, cmd, ic, tip, block, ui, extra) {
        var cls = "pc-ctl" + (extra ? " " + extra : "");
        if (ui.busy === slot) cls += " is-wait";
        if (ui.res && ui.res.slot === slot) cls += " is-" + ui.res.k;
        var t = safeText(block || tip, "");
        return '<button type="button" class="' + cls + '" data-cmd="' + cmd + '" data-slot="' + slot + '" data-tip="' + t
          + '" aria-label="' + t + '"' + (block ? ' aria-disabled="true"' : "") + '>' + icon(ic) + '</button>';
      }
      function ctlConfirmHtml(ui) {
        var cool = ui.confirm === "cooldown";
        return '<div class="pc-confirm" role="group" aria-label="Подтверждение">'
          + (cool ? '<span class="q" data-tip="' + safeText(CTL_MSG.cool_paused, "") + '">' + icon("snow") + 'На паузе — остудить?</span>'
                  + '<button type="button" class="cf-go" data-cmd="confirm-yes">Остудить</button>'
                  : '<span class="q">' + icon("stop") + 'Остановить печать?</span>'
                  + '<button type="button" class="cf-stop" data-cmd="confirm-yes">Стоп</button>')
          + '<button type="button" class="cf-cancel" data-cmd="confirm-no">Отмена</button></div>';
      }
      // The card's control row (opts: {admin, offline, now}; ui: the card's
      // runtime state, may be undefined): light, pause/resume, stop, cool,
      // eject, AMS, ⋯ — each only where its cap is true; state blocks it.
      function ctlRowHtml(p, ui, opts) {
        ui = ui || {};
        opts = opts || {};
        if (!opts.admin || opts.offline) return "";
        if (ui.confirm) return ctlConfirmHtml(ui) + cmdLineHtml(ui.line);
        var caps = ctlCaps(p), grace = !!p.grace_period_active;
        function blk(reason) { return grace ? CTL_MSG.grace : reason; }
        var head = '<div class="pc-controls" role="toolbar" aria-label="Управление принтером">';
        var light = "";
        if (caps.light === true) {
          light = ctlBtnHtml("light", "light", "bulb",
            p.light_on === true ? "Свет горит — выключить" : p.light_on === false ? "Свет выключен — включить"
              : "Свет: состояние неизвестно — включить", blk(null), ui, p.light_on === true ? "lit" : "");
        }
        if (caps.pause !== true) {
          if (caps.light !== true) return "";
          var note = safeText(p.caps_note, "управление не подключено");
          return head + light + '<span class="pc-ctl-lock" data-tip="Печать: ' + note + '">' + icon("lock")
            + '<span>Печать: ' + note + '</span></span></div>' + cmdLineHtml(ui.line);
        }
        var b = [light];
        b.push(p.state === "paused"
          ? ctlBtnHtml("pp", "resume", "play", "Продолжить печать", blk(null), ui)
          : ctlBtnHtml("pp", "pause", "pause", "Пауза", blk(ctlStateBlock("pause", p)), ui));
        if (caps.stop === true) {
          b.push(ctlBtnHtml("stop", "stop", "stop", "Остановить печать", blk(ctlStateBlock("stop", p)), ui, "danger"));
        }
        if (caps.cooldown === true) {
          b.push(ctlBtnHtml("cool", "cool", "snow", p.chamber_ctl === true ? "Остудить всё: сопло, стол, камера"
            : "Остудить всё: сопло и стол", blk(ctlStateBlock("cooldown", p)), ui));
        }
        if (caps.eject === true) {
          var ejectBlock = ctlStateBlock("eject", p) || (ui.ejectUntil > opts.now ? CTL_MSG.eject_wait : null);
          b.push(ctlBtnHtml("eject", "eject", "bed-down", "Опустить стол на 3 см, чтобы снять деталь", blk(ejectBlock), ui));
        }
        if (caps.ams === true) {
          var units = amsUnits(p).length > 0, amsBlock = null;
          if (!units && !externalUnloadOk(p)) {
            var why = filamentOpsBlock(p);
            amsBlock = why && p.filament_loaded === true ? why : "AMS не подключена";
          }
          b.push(ctlBtnHtml("ams", "ams", "spool", units ? "AMS: слоты, загрузка, выгрузка, сушка" : "Внешняя катушка: выгрузка",
            blk(amsBlock), ui, ui.pop === "ams" ? "open" : ""));
        }
        if (caps.cooldown === true || caps.eject === true || caps.tune === true) {
          b.push(ctlBtnHtml("more", "more", "dots", "Ещё: остудить, стол, скорость", blk(null), ui, ui.pop === "menu" ? "open" : ""));
        }
        return head + b.join("") + '</div>' + cmdLineHtml(ui.line);
      }
      // The ⋯ menu: cooldown, eject (with why they are off) and the print speed.
      function ctlMenuHtml(p, ui, now) {
        ui = ui || {};
        var caps = ctlCaps(p), html = "";
        var coolBlock = caps.cooldown === true ? ctlStateBlock("cooldown", p) : null;
        var ejectBlock = caps.eject === true
          ? ctlStateBlock("eject", p) || (ui.ejectUntil > now ? CTL_MSG.eject_wait : null) : null;
        if (caps.cooldown === true) {
          html += '<button type="button" class="pm-item" data-menu="cooldown"' + (coolBlock ? ' aria-disabled="true"' : "") + '>'
            + icon("snow") + '<span class="lbl">Остудить всё<span class="sub">'
            + (p.chamber_ctl === true ? "(сопло, стол, камера)" : "(сопло и стол)") + '</span></span></button>';
        }
        if (caps.eject === true) {
          html += '<button type="button" class="pm-item" data-menu="eject"' + (ejectBlock ? ' aria-disabled="true"' : "") + '>'
            + icon("bed-down") + '<span class="lbl">Опустить стол на 3 см<span class="sub">(снять деталь)</span></span></button>';
        }
        if (coolBlock || ejectBlock) {
          var reason = p.filament_change ? CTL_MSG.change
            : p.state === "printing" ? "Остудить и опустить стол можно, когда печать не идёт."
            : p.state === "paused" ? "Стол опускается только после остановки печати."
            : ejectBlock || coolBlock;
          html += '<div class="pop-why">' + icon("lock") + '<span>' + safeText(reason, "") + '</span></div>';
        }
        if (caps.tune === true) {
          var speedBlock = ctlStateBlock("speed", p), lvl = speedLevelOf(p.feedrate_pct);
          var val = lvl ? SPEED_LEVELS[lvl - 1][0] + " · " + SPEED_LEVELS[lvl - 1][2] + "%"
            : p.feedrate_pct != null && isFinite(p.feedrate_pct) ? Number(p.feedrate_pct) + "%" : "—";
          if (html) html += '<div class="pm-sep"></div>';
          html += '<button type="button" class="pm-item" data-menu="speed-toggle"' + (speedBlock ? ' aria-disabled="true"' : "") + '>'
            + icon("gauge") + '<span class="lbl">Скорость печати…</span><span class="val">' + val + '</span></button>';
          if (ui.speedOpen && !speedBlock) {
            html += '<div class="pm-speed">' + SPEED_LEVELS.map(function (s) {
              return '<button type="button" class="pc-pop-preset' + (lvl === s[1] ? " on" : "") + '" data-speed="' + s[1] + '">'
                + s[0] + " · " + s[2] + '%</button>';
            }).join("") + '</div>';
          }
        }
        return html;
      }
      // AMS button on a printer without an AMS: unload of the external spool.
      function extSpoolPopHtml(p) {
        var block = filamentOpsBlock(p);
        return '<div class="pc-pop-h">Внешняя катушка</div>' + (block ? '<div class="pc-pop-note">' + block + '</div>'
          : '<div class="pc-pop-row"><button type="button" class="pc-pop-ok" data-ams-unload>Выгрузить филамент</button></div>');
      }
      // One row, then one dense row, then balanced rows. normal/dense: chip widths
      // in the regular and the dense variant (dense null when not measured).
      // -> {dense: bool, sizes: null | row sizes from chipRows on the regular widths}
      function chipLayout(normal, dense, rowWidth, gap, denseGap) {
        if (!chipRows(normal, rowWidth, gap)) return { dense: false, sizes: null };
        if (dense && !chipRows(dense, rowWidth, denseGap)) return { dense: true, sizes: null };
        return { dense: false, sizes: chipRows(normal, rowWidth, gap) };
      }
      // widths: natural chip widths (px); rowWidth: the row's content width; gap: column gap (px).
      // -> null (one flexible row) or row sizes, larger rows first: 5 -> [3,2], 6 -> [3,3], 7 -> [4,3], 8 -> [4,4]
      // (when cells clip, the order that clips least: chipRowOrder).
      function chipRows(widths, rowWidth, gap) {
        var n = widths.length;
        if (n < 2 || !(rowWidth > 0)) return null;
        var total = widths.reduce(function (a, b) { return a + b; }, 0) + gap * (n - 1);
        if (total <= rowWidth) return null;
        var maxW = Math.max.apply(null, widths);
        var cmax = Math.max(1, Math.floor((rowWidth + gap) / (maxW + gap)));   // equal cells must hold the widest chip
        var r = Math.min(n, Math.max(Math.ceil(total / rowWidth), Math.ceil(n / cmax)));
        // Never a lone chip (n >= 3): at most n / 2 rows, so every row holds two chips or more. Where
        // the widths need more rows (at most two chips fit a row, or one when a chip is wider than half
        // the row), some cells are narrower than their chips (min-width:0 + clipping) — preferred over
        // orphans.
        if (n >= 3) r = Math.min(r, Math.floor(n / 2));
        return chipRowOrder(widths, rowWidth, gap, r, Math.floor(n / r), n % r);
      }
      // Where the `extra` rows of base + 1 chips go among the r rows: larger rows first, unless another
      // order clips less — the smallest worst clip, then the smallest sum (a chip's clip: its width
      // minus its cell's). So the narrower cells of a fallback row get narrow chips, not the wide
      // heating ones; with nothing clipped every order ties and larger rows stay first.
      function chipRowOrder(widths, rowWidth, gap, r, base, extra) {
        var best = null, bestWorst = 0, bestSum = 0;
        (function place(sizes, big) {
          if (sizes.length === r) {
            var worst = 0, sum = 0, i = 0;
            sizes.forEach(function (k) {
              var cell = (rowWidth - (k - 1) * gap) / k;
              for (var j = 0; j < k; j++, i++) {
                var clip = Math.max(0, widths[i] - cell);
                worst = Math.max(worst, clip);
                sum += clip;
              }
            });
            // 0.01 px: float noise never displaces an earlier (larger-first) order
            if (!best || worst < bestWorst - 0.01 || (worst <= bestWorst + 0.01 && sum < bestSum - 0.01)) {
              best = sizes.slice();
              bestWorst = worst;
              bestSum = sum;
            }
            return;
          }
          if (big > 0) { sizes.push(base + 1); place(sizes, big - 1); sizes.pop(); }   // larger rows tried first
          if (r - sizes.length > big) { sizes.push(base); place(sizes, big); sizes.pop(); }
        })([], extra);
        return best;
      }

      // --- the fleet bar «Вся ферма» and its pause / resume dialog ---
      var RESUME_HOLD = {   // why a paused printer is listed but NOT pre-checked in the bulk resume dialog
        gcode: "пауза из G-code — сначала сделайте то, ради чего она в файле",
        printer: "пауза принтером"
      };
      function plural(n, one, few, many) {
        var m10 = n % 10, m100 = n % 100;
        if (m10 === 1 && m100 !== 11) return one;
        return m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
      }
      // null: a user pause (pre-checked for a bulk resume), else a RESUME_HOLD key.
      function resumeHold(p) {
        var s = String(p.stage || "").toLowerCase();
        if (!s || s === "paused" || s === "paused_user") return null;
        if (s === "paused_user_gcode" || s === "m400_pause") return "gcode";
        return s.indexOf("paused_") === 0 ? "printer" : null;
      }
      // "пауза принтером: закончился филамент" (stageText: the card's stage label; plain text).
      function resumeHoldText(code, stageText) {
        if (code === "gcode") return RESUME_HOLD.gcode;
        if (code !== "printer") return "";
        var t = String(stageText || "").replace(/^Пауза(:\s*|\s+)/, "");
        return RESUME_HOLD.printer + (t ? ": " + t.charAt(0).toLowerCase() + t.slice(1) : "");
      }
      // Why a bulk action leaves this printer out (lower case), or null — the
      // mirror of fleet_commands.skip_reason on the server.
      function bulkSkipReason(action, p) {
        var cap = action === "light_on" || action === "light_off" ? "light" : action;
        var c = ctlCaps(p)[cap];
        if (!p.online || p.state === "offline") return "офлайн" + (c === false && p.caps_note ? " · " + p.caps_note : "");
        if (p.grace_period_active) return "нет связи — данные устарели";
        if (c !== true && cap !== "light") return p.caps_note || "управление не подключено";
        if (c !== true) return c === false && p.caps_note ? p.caps_note : "нет управляемой подсветки";   // a known "no" says why
        var b = ctlStateBlock(cap === "light" ? null : action, p);
        return b ? b.charAt(0).toLowerCase() + b.slice(1) : null;
      }
      // {go: [{p, pre, hold}], skip: [{p, why}]}, both by name. A resume
      // pre-checks only user pauses: the printer paused itself for a reason.
      function bulkSplit(action, printers) {
        var go = [], skip = [];
        (printers || []).forEach(function (p) {
          var why = bulkSkipReason(action, p);
          if (why) { skip.push({ p: p, why: why }); return; }
          var hold = action === "resume" ? resumeHold(p) : null;
          go.push({ p: p, pre: !hold, hold: hold });
        });
        function byName(a, b) {
          return String(a.p.label || "").localeCompare(String(b.p.label || ""), "ru", { numeric: true });
        }
        go.sort(byName);
        skip.sort(byName);
        return { go: go, skip: skip };
      }
      // «Пауза выбранных» / «Продолжить выбранные»: each chosen row (the dialog's
      // snapshot) against the latest printers -> {ids: to send, rows: step 2}.
      // Not sent: a printer that is gone, left out by now (bulkSkipReason) or in
      // another state; for a resume also at another stage (it decides the hold).
      // A pause ignores the stage: it moves on during a print (heating, filament
      // change) and must not drop a printer from an urgent «Пауза всех».
      function bulkRevalidate(action, chosen, printers) {
        var byId = {}, ids = [];
        (printers || []).forEach(function (p) { byId[String(p.id)] = p; });
        var rows = chosen.map(function (x) {
          var p = byId[String(x.id)];
          var changed = !p || bulkSkipReason(action, p) !== null || p.state !== x.state
            || (action === "resume" && (p.stage || null) !== x.stage);
          if (!changed) ids.push(x.id);
          return { id: x.id, label: x.label, model: x.model, job: x.job, status: changed ? "skipped" : "wait",
                   detail: changed ? "состояние изменилось, не отправлено" : "" };
        });
        return { ids: ids, rows: rows };
      }
      // A bulk response merged into the dialog's rows (by id, as strings).
      function mergeBulkResults(dlg, results) {
        results.forEach(function (r) {
          dlg.rows.forEach(function (row) {
            if (String(row.id) === String(r.id)) { row.status = r.status; row.detail = r.detail; }
          });
        });
      }
      function bulkUnconfirmed(r) {
        return r.status === "rejected" || r.status === "sent";
      }
      function countBulk(rows) {
        var c = { confirmed: 0, sent: 0, rejected: 0, skipped: 0 };
        rows.forEach(function (r) { if (c[r.status] != null) c[r.status]++; });
        return c;
      }
      // Light for all, or «Повторить» of the bar's strip: result rows -> {patches:
      // [{id, fields}] (confirmed lights, shown until the next poll), failed: the
      // strip (this run's unconfirmed rows; a clean run of the same action clears
      // it, another action's strip stays), msg: the bar's message}.
      function bulkBarAfter(action, rows, prevFailed) {
        var light = action === "light_on" || action === "light_off";
        var c = countBulk(rows), bad = rows.filter(bulkUnconfirmed);
        var what = action === "light_on" ? "Свет включён" : action === "light_off" ? "Свет выключен"
          : action === "pause" ? "Пауза" : "Продолжение";
        return {
          patches: light ? rows.filter(function (r) { return r.status === "confirmed"; })
            .map(function (r) { return { id: r.id, fields: { light_on: action === "light_on" } }; }) : [],
          failed: bad.length ? { action: action, rows: bad }
            : prevFailed && prevFailed.action === action ? null : prevFailed || null,
          msg: { k: c.rejected || c.sent ? "bad" : "ok",
                 t: what + ": ✓ " + c.confirmed + (c.sent ? " · ? " + c.sent : "") + (c.rejected ? " · ✕ " + c.rejected : "") }
        };
      }
      // The bar's strip once the dialog closes: its unconfirmed rows (for a later
      // «Повторить»), else the strip as it was.
      function bulkDialogFailed(dlg, prevFailed) {
        var bad = dlg.rows.filter(bulkUnconfirmed);
        return bad.length ? { action: dlg.action, rows: bad } : prevFailed || null;
      }
      // Lights over the printers whose light the dashboard can switch.
      function fleetLightStats(printers) {
        var s = { total: 0, lit: 0, offline: 0 };
        (printers || []).forEach(function (p) {
          if (ctlCaps(p).light !== true) return;
          s.total++;
          if (!p.online || p.state === "offline") s.offline++;
          else if (p.light_on === true) s.lit++;
        });
        return s;
      }
      // The bar above the grid (bulk: {busy, msg, failed}); "" when nothing in
      // the farm can be switched or paused from here.
      function fleetBulkHtml(printers, bulk) {
        bulk = bulk || {};
        printers = printers || [];
        var anyPrint = printers.some(function (p) { return ctlCaps(p).pause === true; });
        var ls = fleetLightStats(printers);
        if (!ls.total && !anyPrint) return "";
        var meta = bulk.msg ? safeText(bulk.msg.t, "")
          : "горит <b>" + ls.lit + "</b> из " + ls.total + (ls.offline > 0 ? " · " + ls.offline + " офлайн" : "");
        var mcls = "fb-meta" + (bulk.msg ? (bulk.msg.k === "ok" ? " ok" : " bad") : "");
        function lightBtn(action, cls, tip, text) {
          var busy = bulk.busy === action;
          return '<button type="button" class="fb-btn' + cls + (busy ? " is-wait" : "") + '" data-bulk="' + action + '" data-tip="' + tip + '">'
            + (busy ? '<span class="spin-sm"></span>' : icon("bulb")) + text + '</button>';
        }
        // One line with the sort / filter controls (owner, 13.10: the header took too much room):
        // no title, no group labels — the buttons say what they do.
        var html = '<div class="fleet-bulk" role="toolbar" aria-label="Вся ферма">';
        if (ls.total) {
          html += '<span class="' + mcls + ' fb-meta-m">' + meta + '</span>'
            + '<div class="fb-group">'
            + lightBtn("light_on", " light", "Включить свет на всех онлайн-принтерах со светом", "Свет вкл")
            + lightBtn("light_off", "", "Выключить свет на всех онлайн-принтерах со светом", "Свет выкл")
            + '<span class="' + mcls + '">' + meta + '</span></div>';
        }
        if (anyPrint) {
          var n = bulkSplit("pause", printers).go.length, m = bulkSplit("resume", printers).go.length;
          html += (ls.total ? '<span class="fb-sep"></span>' : "") + '<div class="fb-group">'
            + '<button type="button" class="fb-btn pause" data-bulk="pause"' + (n ? "" : ' aria-disabled="true"') + ' data-tip="'
            + (n ? "Выбрать, какие поставить на паузу" : "Никто не печатает") + '">' + icon("pause") + 'Пауза всех <span class="fb-n">' + n + '</span></button>'
            + '<button type="button" class="fb-btn" data-bulk="resume"' + (m ? "" : ' aria-disabled="true"') + ' data-tip="'
            + (m ? "Выбрать, какие продолжить" : "На паузе никого нет") + '">' + icon("play") + 'Продолжить <span class="fb-n">' + m + '</span></button></div>';
        }
        html += '</div>';
        var failed = bulk.failed && bulk.failed.rows && bulk.failed.rows.length ? bulk.failed : null;
        if (failed) {
          var what = failed.action === "pause" ? "Пауза" : failed.action === "resume" ? "Продолжение" : "Свет";
          html += '<div class="fb-result" role="status"><span>' + what + ': не подтвердили ' + failed.rows.length + '</span>'
            + failed.rows.map(function (r) {
              return '<span class="fb-res-row"><b>' + safeText(r.label, "") + '</b> '
                + (r.status === "sent" ? "? без ответа" : "✕ " + safeText(r.detail, "ошибка")) + '</span>';
            }).join("")
            + '<button type="button" class="bd-link" data-bulk-retry>Повторить</button>'
            + '<button type="button" class="x" data-bulk-dismiss aria-label="Закрыть">×</button></div>';
        }
        return html;
      }
      // The bulk dialog. dlg: {action, step, go: [{id, label, model, pct, job,
      // eta, state, stage, pre, holdText}], skip: [{id, label, model, why}],
      // sel: {id: true}, rows: [{id, label, model, job, status, detail}], busy};
      // every text is pre-formatted by the opener and only escaped here.
      function bulkDialogHtml(dlg) {
        var pause = dlg.action === "pause", ic = icon(pause ? "pause" : "play");
        var head = '<div class="bd" role="dialog" aria-modal="true" aria-labelledby="bdTitle"><div class="bd-h"><div class="bd-ic'
          + (pause ? "" : " go") + '">' + ic + '</div><div>';
        function model(x) { return '<span class="bd-l1"><span class="bd-name">' + safeText(x.label, "") + '</span><span class="bd-model">' + safeText(x.model, "") + '</span></span>'; }
        function job(x) { return '<span class="bd-job">' + icon("file") + '<span>' + safeText(x.job, "—") + '</span></span>'; }
        if (dlg.step === 2) {
          var c = { confirmed: 0, sent: 0, rejected: 0, skipped: 0, wait: 0 };
          dlg.rows.forEach(function (r) { c[r.status] = (c[r.status] || 0) + 1; });
          var verb = pause ? "Пауза" : "Продолжение";
          var sum = (c.confirmed ? '<span class="bd-st ok">✓ ' + c.confirmed + '</span>' : "")
            + (c.wait ? '<span class="bd-st"><span class="spin-sm"></span>' + c.wait + '</span>' : "")
            + (c.sent ? '<span class="bd-st unk">? ' + c.sent + '</span>' : "")
            + (c.rejected ? '<span class="bd-st bad">✕ ' + c.rejected + '</span>' : "")
            + (c.skipped ? '<span class="bd-st">— ' + c.skipped + '</span>' : "");
          var rows = dlg.rows.map(function (r) {
            var chip = r.status === "wait" ? '<span class="bd-st"><span class="spin-sm"></span>ждём принтер…</span>'
              : r.status === "confirmed" ? '<span class="bd-st ok">✓ подтверждено</span>'
              : r.status === "sent" ? '<span class="bd-st unk">? ' + (r.detail === NET_LOST ? "связь прервалась — проверьте карточку" : "отправлено, ответа нет") + '</span>'
              : r.status === "rejected" ? '<span class="bd-st bad">✕ ' + safeText(r.detail, "ошибка") + '</span>'
              : '<span class="bd-st">— ' + safeText(r.detail, "") + '</span>';
            var retry = !dlg.busy && (r.status === "sent" || r.status === "rejected")
              ? '<button type="button" class="bd-retry" data-bd="retry1" data-id="' + safeText(r.id, "") + '">Повторить</button>' : "<span></span>";
            return '<div class="bd-row bd-res">' + model(r) + chip + job(r) + retry + '</div>';
          }).join("");
          var failed = dlg.busy ? 0 : c.sent + c.rejected;
          return head + '<h3 id="bdTitle">' + verb + (dlg.busy ? ": ждём ответа принтеров…" : ": подтверждено " + c.confirmed + " из " + dlg.rows.length)
            + '</h3><p>Каждый принтер отвечает сам, до 8 с. Карточки обновятся через несколько секунд.</p></div></div>'
            + '<div class="bd-sum">' + sum + '</div><div class="bd-list bd-results">' + rows + '</div>'
            + '<div class="bd-foot">' + (failed ? '<button type="button" class="bd-btn" data-bd="retry">Повторить для ' + failed + '</button>' : "")
            + '<button type="button" class="bd-btn warn" data-bd="close"' + (dlg.busy ? ' aria-disabled="true"' : "") + '>Готово</button></div></div>';
        }
        var n = dlg.go.filter(function (x) { return dlg.sel[x.id]; }).length;
        var held = dlg.go.some(function (x) { return x.holdText; });
        var noun = plural(n, "принтер", "принтера", "принтеров");
        var list = dlg.go.map(function (x) {
          var on = !!dlg.sel[x.id];
          return '<label class="bd-row' + (on ? "" : " unchecked") + (x.holdText ? " hold" : "") + '"><input type="checkbox" data-bd="toggle" data-id="'
            + safeText(x.id, "") + '"' + (on ? " checked" : "") + '>' + model(x) + '<span class="bd-pct">' + safeText(x.pct, "") + '</span>'
            + job(x) + '<span class="bd-eta">' + safeText(x.eta, "") + '</span>'
            + (x.holdText ? '<span class="bd-hold">' + icon("lock") + safeText(x.holdText, "") + '</span>' : "") + '</label>';
        }).join("");
        var skip = dlg.skip.map(function (x) {
          return '<div class="bd-off"><b>' + safeText(x.label, "") + '</b><span>' + safeText(x.model, "") + ' — ' + safeText(x.why, "") + '</span></div>';
        }).join("");
        return head + '<h3 id="bdTitle">' + (pause ? "Поставить на паузу " : "Снять с паузы ") + n + " " + noun + '?</h3><p>'
          + (pause ? "Печать продолжится с того же места — кнопкой ▶ на карточке или «Продолжить» сверху."
                   : "Каждый принтер продолжит печать с места остановки."
                     + (held ? " Принтеры, поставленные на паузу самим принтером, не отмечены — сначала проверьте их." : ""))
          + '</p></div></div>'
          + '<div class="bd-sec"><span>' + (pause ? "Печатают" : "На паузе") + " · " + dlg.go.length + '</span><button type="button" class="bd-link" data-bd="all">'
          + (n === dlg.go.length ? "Снять все" : "Выбрать все") + '</button></div><div class="bd-list">' + list + '</div>'
          + (dlg.skip.length ? '<div class="bd-sec"><span>Не попадут · ' + dlg.skip.length + '</span></div><div class="bd-skip">' + skip + '</div>' : "")
          + '<div class="bd-foot"><button type="button" class="bd-btn" data-bd="cancel">Отмена</button>'
          + '<button type="button" class="bd-btn ' + (pause ? "warn" : "go") + '" data-bd="go"' + (n ? "" : ' aria-disabled="true"') + '>' + ic
          + (pause ? "Пауза выбранных (" : "Продолжить выбранные (") + n + ')</button></div></div>';
      }
      /* ctl-ui:end */
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
        // A running filament change (load / unload step) replaces the print stage.
        var changeText = filamentChangeText(p.filament_change);
        var stage = changeText || stageLabel(p.stage);
        var speedMode = p.feedrate_pct == null || p.feedrate_pct <= 0 ? null
          : p.feedrate_pct < 100 ? "slow" : p.feedrate_pct > 124 ? "turbo" : p.feedrate_pct > 100 ? "fast" : "norm";
        var speedIcon = speedMode === "slow" ? ' \u2193' : speedMode === "turbo" ? ' \uD83D\uDE80' : speedMode === "fast" ? ' \u26A1' : '';
        // What this printer can do comes from the server (p.caps, printer_commands.caps_for):
        // tune = temperatures, fans, speed; ams = AMS / external spool commands.
        var caps = ctlCaps(p);
        var canEdit = isAdmin() && !offlineFlag && caps.tune === true;
        // AMS commands are checked on live telemetry: a card in its grace period
        // shows the last good data and the backend would refuse them.
        var canAms = isAdmin() && !offlineFlag && caps.ams === true && !p.grace_period_active;
        var speedEdit = canEdit ? ' pc-editable" data-edit="speed" data-cur="' + (p.feedrate_pct || 100) : '';
        // Nozzle diameter / type: in the nozzle temperature tooltip; a chip of
        // its own only for a non-standard nozzle (not 0.4 mm, high flow, TPU).
        var nz = nozzleChip(p.nozzle);
        var chips = [
          tempChip("nozzle", nz ? nz.tip : "\u0421\u043E\u043F\u043B\u043E", p.nozzle_temp, p.target_nozzle_temp, canEdit),
          tempChip("bed", "\u0421\u0442\u043E\u043B", p.bed_temp, p.target_bed_temp, canEdit),
          // Камера: цель (M141) есть только у H2-серии, у P/X это просто датчик.
          tempChip("chamber", "Камера", p.chamber_temp, p.target_chamber_temp,
                   canEdit && p.chamber_ctl === true, "temp"),
          fanChip(p, canEdit),
          speedMode ? '<span class="pc-chip pc-speed-' + speedMode + speedEdit + '" data-tip="\u0421\u043A\u043E\u0440\u043E\u0441\u0442\u044C \u043F\u0435\u0447\u0430\u0442\u0438">' + icon("gauge") + p.feedrate_pct + '%' + speedIcon + '</span>' : "",
          nz && !nz.standard ? '<span class="pc-chip" data-tip="' + nz.tip + '">' + nz.text + '</span>' : "",
          p.door_open === true ? '<span class="pc-chip pc-warn" data-tip="Дверца открыта">' + icon("door") + 'открыта</span>' : "",
          p.sdcard === "missing" ? '<span class="pc-chip pc-warn" data-tip="SD-карта не вставлена">' + icon("sd") + 'нет SD</span>'
            : p.sdcard === "abnormal" ? '<span class="pc-chip pc-warn" data-tip="SD-карта: ошибка или только чтение">' + icon("sd") + 'SD: ошибка</span>' : "",
        ].filter(Boolean).join("");

        // The control row: only for admins on online cards and only the controls
        // whose cap is true (no row at all otherwise — the card does not grow).
        var controls = ctlRowHtml(p, cardUi[p.id], { admin: isAdmin(), offline: offlineFlag, now: Date.now() });

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
                  ${amsChip(p, canAms) || '<span class="printer-meta">' + safeText((p.kind || "").toUpperCase(), "UNKNOWN") + ' · ' + safeText((p.device_type || "").toUpperCase(), "GENERIC") + '</span>'}
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
            ${!showProgress && changeText ? '<div class="stage-badge">' + changeText + '</div>' : ""}
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
        var freshCards = [];
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
            freshCards.push(fresh);
          }
          var expected = prev ? prev.nextElementSibling : grid.firstElementChild;
          if (expected !== el) grid.insertBefore(el, expected);
          prev = el;
        });
        Array.from(grid.querySelectorAll(".printer-card")).forEach(function (el) {
          var id = el.getAttribute("data-id");
          if (!seen.has(id)) { el.remove(); fleetCardCache.delete(id); }
        });
        balanceChips(freshCards);
      }
      // Chip rows (owner's amendment A1): one row whenever the chips fit (the
      // compact variant comes from the card's container query), otherwise
      // balanced rows of equal cells — never a lone chip on a line of its own.
      // All rows are measured first, then written: one forced layout per call.
      // The attributes live on the DOM nodes only; a replaced card is balanced
      // again by renderFleet.
      function balanceChips(cards) {
        var rows = [];
        cards.forEach(function (card) {
          var row = card.querySelector(".pc-chips");
          if (row) rows.push(row);
        });
        if (!rows.length) return;
        rows.forEach(function (row) {
          row.removeAttribute("data-balanced");
          row.removeAttribute("data-dense");
          row.setAttribute("data-measure", "");
        });
        var plans = rows.map(function (row) {
          var chips = Array.prototype.slice.call(row.children);
          var gap = parseFloat(getComputedStyle(row).columnGap) || 5;
          var widths = chips.map(function (c) { return c.getBoundingClientRect().width; });
          return { row: row, chips: chips, gap: gap, widths: widths, rowWidth: row.clientWidth };
        });
        // Rows that overflow get one more measure in the dense variant (a second
        // forced layout, only when some row needs it): one row stays one row
        // whenever tighter padding is enough, so cards look alike.
        var tight = plans.filter(function (p) { return chipRows(p.widths, p.rowWidth, p.gap); });
        tight.forEach(function (p) { p.row.setAttribute("data-dense", ""); });
        tight.forEach(function (p) {
          p.denseGap = parseFloat(getComputedStyle(p.row).columnGap) || 2;
          p.dense = p.chips.map(function (c) { return c.getBoundingClientRect().width; });
        });
        plans.forEach(function (plan) {
          var lay = chipLayout(plan.widths, plan.dense || null, plan.rowWidth, plan.gap, plan.denseGap);
          plan.sizes = lay.sizes;
          plan.row.removeAttribute("data-measure");
          if (!lay.dense) plan.row.removeAttribute("data-dense");
          if (!plan.sizes) return;
          plan.row.setAttribute("data-balanced", "");
          var i = 0;
          plan.sizes.forEach(function (k) {
            for (var j = 0; j < k; j++, i++) plan.chips[i].style.setProperty("--chip-k", k);
          });
        });
      }
      function balanceAllChips() {
        balanceChips(Array.prototype.slice.call(grid.querySelectorAll(".printer-card")));
      }
      (function initChipBalance() {
        // Re-balance when the card width can change: the grid's width (window,
        // columns), the text scale (handled where it is applied) and web fonts.
        var lastWidth = -1, queued = false;
        if (typeof ResizeObserver === "function") {
          new ResizeObserver(function (entries) {
            var w = Math.round(entries[0].contentRect.width);
            if (w === lastWidth || queued) return;
            lastWidth = w;
            queued = true;
            requestAnimationFrame(function () { queued = false; balanceAllChips(); });
          }).observe(grid);
        }
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(balanceAllChips);
      })();

      // --- Card commands: runtime state and lifecycle ---
      // pid -> {busy, res, line, confirm, pop, speedOpen, ejectUntil}: what the
      // card's row shows besides the printer's own data.
      var cardUi = Object.create(null);
      var CMD_RESULT_MS = 3000, CONFIRM_MS = 5000, CMD_FETCH_TIMEOUT_MS = 30000;   // EJECT_GUARD_MS: ctl-ui
      function cardUiOf(pid) {
        return cardUi[pid] || (cardUi[pid] = {});
      }
      // One POST -> {status, detail, body}; never throws (httpOutcome decides).
      async function postJson(url, payload) {
        if (navigator.onLine === false) return httpOutcome(0, null, "offline");
        var ctl = new AbortController();
        var timer = setTimeout(function () { ctl.abort(); }, CMD_FETCH_TIMEOUT_MS);
        try {
          var r = await apiFetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify(payload), signal: ctl.signal });
          var body = null;
          try { body = await r.json(); } catch (e) { body = null; }   // non-JSON (a proxy's 502 page): lost
          var out = httpOutcome(r.status, body, "ok");
          out.body = body;
          return out;
        } catch (e) {
          // apiFetch: 401 and the refresh failed — nothing was executed
          if (e && e.message === "Unauthorized") return httpOutcome(401, { detail: "Сессия истекла — войдите снова" }, "ok");
          return httpOutcome(0, null, "lost");   // dispatched, then a network error / abort: the printer may have acted
        } finally {
          clearTimeout(timer);
        }
      }
      function postCommand(pid, action, params) {
        return postJson("/api/admin/printers/" + encodeURIComponent(pid) + "/command",
                        Object.assign({ action: action }, params || {}));
      }
      // Optimistic copy of a confirmed result into the cached row until the next poll.
      function patchPrinter(pid, fields) {
        var p = findPrinter(pid);
        if (p) Object.assign(p, fields);
      }
      // Send one command from a card: the button waits, then shows the outcome
      // (3 s) and the status line says it (failures stay until dismissed).
      // -> the outcome ({status, detail}), or null when the card is busy.
      async function runCardCmd(pid, action, params, slot, hooks) {
        var ui = cardUiOf(pid);
        if (ui.busy) return null;                                // one command per card at a time
        ui.busy = slot || "_";
        ui.res = null;
        ui.line = cmdWaitLine(action);
        applyAndRender();
        var out = await postCommand(pid, action, params);
        var done = cardAfterResult(action, slot, out, Date.now()), r = done.line;
        ui.busy = null;
        ui.res = done.res;
        ui.line = r;
        if (done.patch) patchPrinter(pid, done.patch);
        if (done.ejectUntil) {
          ui.ejectUntil = done.ejectUntil;                        // mirrors the server's guard
          setTimeout(applyAndRender, EJECT_GUARD_MS + 50);
        }
        if (hooks && hooks.after) hooks.after(out);              // AMS popover: close on success, keep on failure
        applyAndRender();
        var token = ui.line;
        setTimeout(function () {
          if (ui.res && ui.res.slot === slot) ui.res = null;
          if (ui.line === token && r.k !== "bad") ui.line = null;
          applyAndRender();
        }, CMD_RESULT_MS);
        if (/^(ams_|dry_)/.test(action) && amsPanelActive()) setTimeout(loadAms, 1500);
        return out;
      }
      // A short note in the card's status line (e.g. why a button is off, on a phone tap).
      function setInfoLine(pid, text) {
        var ui = cardUiOf(pid), line = { k: "info", t: text };
        ui.line = line;
        applyAndRender();
        setTimeout(function () {
          if (ui.line === line) { ui.line = null; applyAndRender(); }
        }, CMD_RESULT_MS);
      }
      // Inline confirmation in place of the row; cancelled by itself 5 s after
      // the last touch of it (a tap on the question re-arms it).
      function askConfirm(pid, what) {
        cardUiOf(pid).confirm = what;
        armConfirm(pid);
        applyAndRender();
      }
      function armConfirm(pid) {
        var ui = cardUiOf(pid), token = {}, what = ui.confirm;
        ui.confirmToken = token;
        setTimeout(function () {
          if (ui.confirmToken !== token || ui.confirm !== what) return;
          ui.confirm = null;
          ui.confirmGoneAt = Date.now();   // a tap meant for the bar must not land on the row under it
          applyAndRender();
        }, CONFIRM_MS);
      }
      // The AMS button / ⋯ open their popover; a second click on the same one closes it.
      function togglePop(btn, pid, kind) {
        var same = pcPopPid === pid && cardUiOf(pid).pop === kind;
        closePop(true);
        var p = findPrinter(pid);
        if (!same && p) {
          var ui = cardUiOf(pid);
          ui.pop = kind;
          ui.speedOpen = false;
          var html = kind === "ams" ? (amsUnits(p).length ? amsPopHtml(p) : extSpoolPopHtml(p)) : ctlMenuHtml(p, ui, Date.now());
          openPop(btn, pid, html, kind);
        }
        applyAndRender();
      }

      // --- Fleet bar «Вся ферма» (admins): light for all, pause / resume with a choice ---
      var fleetBulkEl = document.getElementById("fleetBulk");
      var bulkUi = { busy: null, msg: null, failed: null };   // failed: {action, rows} until dismissed
      var bulkDlg = null, bulkModal = null, fleetBulkMarkup = null;
      var BULK_MSG_MS = 6000;
      function renderFleetBulk() {
        if (!fleetBulkEl) return;
        var html = isAdmin() ? fleetBulkHtml(cachedAllPrinters, bulkUi) : "";
        if (html !== fleetBulkMarkup) { fleetBulkEl.innerHTML = html; fleetBulkMarkup = html; }
      }
      function flashBulkMsg(msg) {
        bulkUi.msg = msg;
        renderFleetBulk();
        setTimeout(function () { if (bulkUi.msg === msg) { bulkUi.msg = null; renderFleetBulk(); } }, BULK_MSG_MS);
      }
      // One bulk request -> a result row per id. Nothing came back: 4xx = nothing
      // ran (rejected, «Повторить» possible); lost / 5xx = the server may have sent them.
      async function postBulk(action, ids) {
        var out = await postJson("/api/admin/printers/bulk-command", { action: action, printer_ids: ids });
        if (out.body && Array.isArray(out.body.results)) return out.body.results;
        return ids.map(function (id) {
          var p = findPrinter(id), sent = out.status === "sent";
          return { id: id, label: p ? p.label : id, ok: sent, status: sent ? "sent" : "rejected", detail: out.detail };
        });
      }
      // Light for all (no dialog), or «Повторить» of the failed strip: the
      // outcome in the bar's meta for 6 s, failures in the strip until dismissed.
      async function runBulkBar(action, ids) {
        if (bulkUi.busy) return;
        if (!ids) ids = bulkSplit(action, cachedAllPrinters).go.map(function (x) { return x.p.id; });
        if (!ids.length) { flashBulkMsg({ k: "bad", t: "Нет онлайн-принтеров со светом" }); return; }
        bulkUi.busy = action;
        renderFleetBulk();
        var rows = await postBulk(action, ids);
        bulkUi.busy = null;
        var done = bulkBarAfter(action, rows, bulkUi.failed);
        done.patches.forEach(function (x) { patchPrinter(x.id, x.fields); });
        bulkUi.failed = done.failed;
        flashBulkMsg(done.msg);
        applyAndRender();
      }
      function ensureBulkModal() {
        if (bulkModal) return bulkModal;
        bulkModal = document.createElement("div");
        bulkModal.id = "bulkModal";
        bulkModal.className = "bd-modal";
        document.body.appendChild(bulkModal);
        bulkModal.addEventListener("click", onBulkDialogClick);
        bulkModal.addEventListener("change", onBulkDialogChange);
        return bulkModal;
      }
      // focus: a selector to focus after the render (the toggled checkbox, «Готово»…).
      function renderBulkDialog(focus) {
        var m = ensureBulkModal();
        if (!bulkDlg) { m.classList.remove("open"); m.innerHTML = ""; return; }
        var old = m.querySelector(".bd"), scroll = old ? old.scrollTop : 0;
        m.innerHTML = bulkDialogHtml(bulkDlg);
        m.classList.add("open");
        var bd = m.querySelector(".bd");
        if (bd) bd.scrollTop = scroll;
        var f = focus ? m.querySelector(focus) : null;
        if (f) f.focus();
      }
      // The dialog works on a snapshot taken here: the list does not move under the cursor.
      function openBulkDialog(action) {
        var split = bulkSplit(action, cachedAllPrinters);
        if (!split.go.length || bulkDlg) return;
        function model(p) { return String(p.device_type || p.kind || "").toUpperCase(); }
        var sel = {};
        var go = split.go.map(function (x) {
          var p = x.p;
          if (x.pre) sel[p.id] = true;
          return { id: p.id, label: p.label, model: model(p), state: p.state, stage: p.stage || null, pre: x.pre,
                   pct: p.progress_pct != null && isFinite(p.progress_pct) ? Math.round(p.progress_pct) + "%" : "",
                   job: p.job_name ? jobShortName(p.job_name) : "—",
                   eta: p.eta_seconds > 0 ? "ещё " + timeFmt(p.eta_seconds) : "",
                   holdText: x.hold ? resumeHoldText(x.hold, stageLabel(p.stage)) : null };
        });
        var skip = split.skip.map(function (x) { return { id: x.p.id, label: x.p.label, model: model(x.p), why: x.why }; });
        bulkDlg = { action: action, step: 1, go: go, skip: skip, sel: sel, rows: [], busy: false };
        renderBulkDialog('.bd input[type="checkbox"]');
      }
      // Closing never loses results: not while the request runs; afterwards
      // the failures stay in the bar's strip for a later «Повторить».
      function closeBulkDialog() {
        if (!bulkDlg || bulkDlg.busy) return;
        bulkUi.failed = bulkDialogFailed(bulkDlg, bulkUi.failed);
        bulkDlg = null;
        renderBulkDialog();
        renderFleetBulk();
      }
      // «Пауза выбранных» / «Продолжить выбранные»: each chosen printer is checked
      // against the latest data first (bulkRevalidate) — what changed is not sent.
      async function bulkDialogGo() {
        var d = bulkDlg;
        var chosen = d.go.filter(function (x) { return d.sel[x.id]; });
        if (!chosen.length) return;
        var plan = bulkRevalidate(d.action, chosen, cachedAllPrinters);
        d.rows = plan.rows;
        d.step = 2;
        await bulkDialogSend(d, plan.ids);
      }
      async function bulkDialogSend(d, ids) {
        d.busy = ids.length > 0;
        renderBulkDialog();
        if (ids.length) {
          mergeBulkResults(d, await postBulk(d.action, ids));
          d.busy = false;
          applyAndRender();
        }
        if (bulkDlg === d) renderBulkDialog('[data-bd="close"]');
      }
      function bulkDialogRetry(ids) {
        var d = bulkDlg;
        if (!d || d.busy || !ids.length) return;
        d.rows.forEach(function (r) { if (ids.indexOf(String(r.id)) !== -1) { r.status = "wait"; r.detail = ""; } });
        bulkDialogSend(d, ids);
      }
      function onBulkDialogClick(e) {
        if (!bulkDlg) return;
        if (e.target === bulkModal) { closeBulkDialog(); return; }      // the overlay
        var t = e.target.closest("[data-bd]");
        if (!t || t.dataset.bd === "toggle" || t.getAttribute("aria-disabled") === "true") return;
        var what = t.dataset.bd, d = bulkDlg;
        if (what === "cancel" || what === "close") { closeBulkDialog(); return; }
        if (d.busy) return;
        if (what === "all") {
          var all = d.go.every(function (x) { return d.sel[x.id]; });
          d.sel = {};
          if (!all) d.go.forEach(function (x) { d.sel[x.id] = true; });
          renderBulkDialog('[data-bd="all"]');
        } else if (what === "go") {
          bulkDialogGo();
        } else if (what === "retry1") {
          bulkDialogRetry([t.dataset.id]);
        } else if (what === "retry") {
          bulkDialogRetry(d.rows.filter(bulkUnconfirmed).map(function (r) { return String(r.id); }));
        }
      }
      function onBulkDialogChange(e) {
        var t = e.target.closest('[data-bd="toggle"]');
        if (!t || !bulkDlg || bulkDlg.step !== 1) return;
        if (t.checked) bulkDlg.sel[t.dataset.id] = true;
        else delete bulkDlg.sel[t.dataset.id];
        renderBulkDialog('[data-bd="toggle"][data-id="' + (window.CSS && CSS.escape ? CSS.escape(t.dataset.id) : t.dataset.id) + '"]');
      }
      if (fleetBulkEl) {
        fleetBulkEl.addEventListener("click", function (e) {
          var b = e.target.closest("[data-bulk],[data-bulk-retry],[data-bulk-dismiss]");
          if (!b) return;
          if (b.hasAttribute("data-bulk-dismiss")) { bulkUi.failed = null; renderFleetBulk(); return; }
          if (b.hasAttribute("data-bulk-retry")) {
            var f = bulkUi.failed;
            if (f) runBulkBar(f.action, f.rows.map(function (r) { return r.id; }));
            return;
          }
          if (b.getAttribute("aria-disabled") === "true") return;
          var action = b.dataset.bulk;
          if (action === "light_on" || action === "light_off") runBulkBar(action, null);
          else openBulkDialog(action);
        });
      }
      document.addEventListener("keydown", function (e) { if (e.key === "Escape" && bulkDlg) closeBulkDialog(); });
      function applyAndRender() {
        var printers = cachedAllPrinters;
        var filtered = applyFilters(printers);
        var sorted = sortPrinters(filtered);
        lastFilteredPrinters = filtered;
        renderSummary(printers);
        renderFleetBulk();     // counts over all printers, filters ignored
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
          balanceAllChips();   // the chips' widths follow the text size
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
            noEta.textContent = "Печатают без оценки времени: " + withoutEta.map(function (p) { return shortPrinterName(p.label); }).join(", ");
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

      /* ── Команды с вкладки AMS (тост; карточки — runCardCmd) ────── */
      async function sendPrinterCmd(pid, action, extra) {
        var out = await postCommand(pid, action, extra);
        var ok = out.status === "confirmed" || out.status === "sent";
        var text = out.status === "confirmed" ? "Подтверждено принтером"
          : out.status === "sent" ? (out.detail === NET_LOST ? NET_LOST : "Отправлено, ответа нет")
          : out.detail || "Ошибка команды";
        showToast(text, out.status === "confirmed" || (ok && out.detail !== NET_LOST));
        if (ok) {
          setTimeout(load, 1500);
          if (amsPanelActive()) setTimeout(loadAms, 1500);
        }
        return ok;
      }
      // ── Поповер смены параметров (клик по чипу сопла/стола/скорости/вентилятора, AMS и ⋯) ──
      var pcPop = null, pcPopPid = null, pcPopAnchorRect = null, pcPopPressed = null;
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
      // Close the popover and clear the highlight of the card button that opened
      // it (re-rendered unless noRender: the caller renders itself right after).
      function closePop(noRender) {
        var ui = pcPopPid != null ? cardUi[pcPopPid] : null;
        if (pcPop) pcPop.style.display = "none";
        pcPopPid = null;
        if (ui && ui.pop) {
          ui.pop = null;
          ui.speedOpen = false;
          if (!noRender) applyAndRender();
        }
      }
      // A command from the popover: its buttons stay disabled until the reply
      // (a second click would send it twice); closes the popover on success.
      // Opened from a card, the card's own command path answers (cardPopCmd).
      async function popCmd(pid, action, extra) {
        if (pcPop && pcPop.dataset && pcPop.dataset.origin === "card") return cardPopCmd(pid, action, extra);
        var btns = pcPop ? Array.prototype.filter.call(pcPop.querySelectorAll("button"), function (b) { return !b.disabled; }) : [];
        btns.forEach(function (b) { b.disabled = true; });
        var ok = false;
        try { ok = await sendPrinterCmd(pid, action, extra); } finally { btns.forEach(function (b) { b.disabled = false; }); }
        if (ok) closePop();
        return ok;
      }
      // A command from a card's AMS popover: the popover stays open while the
      // printer answers (the pressed button waits), closes on success and keeps
      // the filled form on a refusal; the card's AMS button and status line show it too.
      async function cardPopCmd(pid, action, extra) {
        var btn = pcPopPressed;
        if (btn) { btn.setAttribute("aria-busy", "true"); btn.classList.add("is-wait"); }
        var out = await runCardCmd(pid, action, extra, "ams", { after: function (o) {
          if (o.status === "confirmed" || o.status === "sent") closePop(true);
          else showToast(o.detail || "Ошибка команды", false);
        } });
        if (btn) { btn.removeAttribute("aria-busy"); btn.classList.remove("is-wait"); }
        return !!out && (out.status === "confirmed" || out.status === "sent");
      }
      function findPrinter(pid) {
        return cachedAllPrinters.find(function (x) { return String(x.id) === String(pid); }) || null;
      }
      // Below the anchor rect r, or above it when there is no room below; inside
      // the window. Measured at left/top 0: a fixed box shrinks to the room
      // right of its left, so the last popover's position would squeeze it
      // against the right edge of a phone screen.
      function placePop(pop, r) {
        pop.style.left = "0px";
        pop.style.top = "0px";
        var pw = pop.offsetWidth, ph = pop.offsetHeight;
        var left = Math.max(8, Math.min(r.left, window.innerWidth - pw - 8));
        var top = r.bottom + 6;
        if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
        pop.style.left = left + "px";
        pop.style.top = top + "px";
      }
      // Next screen inside an open popover (AMS -> drying form -> back), placed
      // against the anchor's rect saved on open: the chip may have been
      // re-rendered by a poll meanwhile (a detached node measures 0,0).
      function swapPop(html, kind) {
        if (!pcPop) return;
        pcPop.dataset.kind = kind || "";
        pcPop.innerHTML = html;
        placePop(pcPop, pcPopAnchorRect || { left: 8, top: 8, bottom: 8 });
        var inp = pcPop.querySelector("input.pc-pop-in");
        if (inp) { inp.focus(); inp.select && inp.select(); }
      }
      // origin: "card" (default) or "amsTab" — whose command path its buttons use.
      function openPop(anchor, pid, html, kind, origin) {
        var pop = ensurePop();
        pcPopPid = pid;
        pop.dataset.kind = kind || "";
        pop.dataset.origin = origin || "card";
        pop.innerHTML = html;
        pop.style.display = "block";
        pop.style.visibility = "hidden";
        pcPopAnchorRect = anchor.getBoundingClientRect();
        placePop(pop, pcPopAnchorRect);
        pop.style.visibility = "";
        var inp = pop.querySelector("input.pc-pop-in");
        if (inp) { inp.focus(); inp.select && inp.select(); }
      }
      function tempPopHtml(kind, cur, presets) {
        var chips = presets.map(function (v) {
          return '<button type="button" class="pc-pop-preset" data-set="' + v[1] + '">' + v[0] + '</button>';
        }).join("");
        var title = kind === "nozzle" ? "Сопло" : kind === "chamber" ? "Камера" : "Стол";
        return '<div class="pc-pop-h">' + title + ', °C</div>'
          + '<div class="pc-pop-presets">' + chips + '</div>'
          + '<div class="pc-pop-row"><input type="text" inputmode="numeric" class="pc-pop-in" value="' + cur + '">'
          + '<button type="button" class="pc-pop-ok" data-ok="preheat">OK</button></div>';
      }
      function speedPopHtml() {
        var lv = [["Тихий", 1], ["Стандарт", 2], ["Спорт", 3], ["Безумный", 4]];
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
      // AMS popover and drying form actions; the printer is read at click time.
      async function onAmsPopClick(pid, t) {
        var p = findPrinter(pid);
        if (!p) { closePop(); return; }
        function unitOf(idx) {
          return amsUnits(p).find(function (u) { return u.index === idx; }) || null;
        }
        if (t.hasAttribute("data-ams-back")) {
          swapPop(amsPopHtml(p), "ams");
          return;
        }
        if (t.dataset.amsLoad != null) {
          var pair = t.dataset.amsLoad.split(":");
          var ams = parseInt(pair[0], 10), slot = parseInt(pair[1], 10);
          if (!confirm(amsLoadConfirmText(p, ams, slot))) return;
          await popCmd(pid, "ams_load", { ams: ams, slot: slot });
          return;
        }
        if (t.hasAttribute("data-ams-unload")) {
          if (!confirm(amsUnloadConfirmText(p))) return;
          await popCmd(pid, "ams_unload", null);
          return;
        }
        if (t.dataset.dryOpen != null) {
          var unit = unitOf(parseInt(t.dataset.dryOpen, 10));
          if (unit && unit.dry) swapPop(dryPopHtml(p, unit), "dry");
          return;
        }
        var tIn = pcPop.querySelector('[data-dry="temp"]'), hIn = pcPop.querySelector('[data-dry="hours"]');
        if (t.dataset.dryMat != null) {
          pcPop.querySelectorAll("[data-dry-mat]").forEach(function (b) { b.classList.toggle("on", b === t); });
          if (tIn) tIn.value = t.dataset.temp;
          if (hIn) hIn.value = t.dataset.hours;
          return;
        }
        if (t.dataset.dryStart != null) {
          var idx = parseInt(t.dataset.dryStart, 10), du = unitOf(idx);
          if (!du || !du.dry || !tIn || !hIn) return;
          // The material comes from the selected preset button, never from state
          // left over from an earlier form.
          var on = pcPop.querySelector(".pc-pop-preset.on[data-dry-mat]");
          var chk = dryFormCheck(du.dry, on ? on.dataset.dryMat : null, tIn.value, hIn.value);
          if (chk.bad === "temp") { tIn.focus(); tIn.select(); return; }
          if (chk.bad === "hours") { hIn.focus(); hIn.select(); return; }
          if (chk.bad) return;
          await popCmd(pid, "dry_start", { ams: idx, temp: chk.temp, hours: chk.hours, filament: chk.filament });
          return;
        }
        if (t.dataset.dryStop != null) {
          var sIdx = parseInt(t.dataset.dryStop, 10);
          if (!confirm("Остановить сушку " + amsUnitName(sIdx) + " на «" + p.label + "»?")) return;
          await popCmd(pid, "dry_stop", { ams: sIdx });
        }
      }
      async function onPopClick(e) {
        var pid = pcPopPid;
        if (!pid) return;
        var t = e.target.closest("[data-set],[data-speed],[data-ok],[data-menu],"
          + "[data-ams-back],[data-ams-load],[data-ams-unload],[data-dry-open],[data-dry-mat],[data-dry-start],[data-dry-stop]");
        if (!t || t.disabled || t.getAttribute("aria-disabled") === "true") return;
        // A card answers one command at a time: no new one from its popover meanwhile.
        var sends = t.matches("[data-speed],[data-ok],[data-ams-load],[data-ams-unload],[data-dry-start],[data-dry-stop]")
          || (t.dataset.menu != null && t.dataset.menu !== "speed-toggle");
        if (sends && pcPop.dataset.origin === "card" && cardUiOf(pid).busy) { showToast(CTL_MSG.busy, false); return; }
        if (!t.matches("[data-set],[data-speed],[data-ok],[data-menu]")) {
          pcPopPressed = t;
          try { await onAmsPopClick(pid, t); } finally { pcPopPressed = null; }
          return;
        }
        if (t.dataset.menu != null) {
          onMenuClick(pid, t.dataset.menu);
          return;
        }
        if (t.dataset.set != null) {
          var inp = pcPop.querySelector(".pc-pop-in");
          if (inp) inp.value = t.dataset.set;
          return;
        }
        var kind = pcPop.dataset.kind;
        if (t.dataset.speed != null) {
          closePop(true);
          runCardCmd(pid, "speed", { level: parseInt(t.dataset.speed, 10) }, kind === "menu" ? "more" : null);
          return;
        }
        if (t.dataset.ok === "preheat") {
          var v = parseInt((pcPop.querySelector(".pc-pop-in").value || "").trim(), 10);
          if (isNaN(v)) { pcPop.querySelector(".pc-pop-in").focus(); return; }
          closePop(true);
          // Камера — не преднагрев: своя команда с параметром temp.
          if (kind === "chamber") {
            runCardCmd(pid, "chamber", { temp: v }, null);
            return;
          }
          var extra = {}; extra[kind] = v;
          runCardCmd(pid, "preheat", extra, null);
          return;
        }
        if (t.dataset.ok === "fans") {
          var fx = {};
          pcPop.querySelectorAll("[data-fan]").forEach(function (r) { fx[r.dataset.fan] = parseInt(r.value, 10); });
          closePop(true);
          runCardCmd(pid, "fans", fx, null);
          return;
        }
      }
      // ⋯ menu items; a disabled one never gets here (onPopClick).
      function onMenuClick(pid, what) {
        var p = findPrinter(pid);
        if (!p) { closePop(); return; }
        var ui = cardUiOf(pid);
        if (what === "speed-toggle") {
          ui.speedOpen = !ui.speedOpen;
          swapPop(ctlMenuHtml(p, ui, Date.now()), "menu");
          return;
        }
        closePop(true);
        if (what === "cooldown") coolCmd(pid, p);
        else if (what === "eject") runCardCmd(pid, "eject", null, "eject");
        else applyAndRender();
      }
      // ❄: cooling a paused print asks first (the bed cools, the part may let go).
      function coolCmd(pid, p) {
        if (p.state === "paused") askConfirm(pid, "cooldown");
        else runCardCmd(pid, "cooldown", null, "cool");
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
        // The same tooltip for the cards and for the fleet bar above them.
        function bindTips(container) {
          if (!container) return;
          container.addEventListener("mouseover", function (e) {
            var el = e.target.closest("[data-tip]");
            if (!el || !container.contains(el) || el === curEl) return;
            curEl = el;
            if (timer) clearTimeout(timer);
            // a click may have re-rendered the card meanwhile: a detached node measures 0,0
            timer = setTimeout(function () { if (curEl === el && el.isConnected) place(el); }, 250);
          });
          container.addEventListener("mouseout", function (e) {
            var el = e.target.closest("[data-tip]");
            if (!el) return;
            if (e.relatedTarget && el.contains(e.relatedTarget)) return; // ушли на своего ребёнка
            hide();
          });
        }
        bindTips(grid);
        bindTips(document.getElementById("fleetBulk"));
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
            // Close first (clears another card's ⋯ / AMS highlight), render after
            // opening: the chip's rect is read while it is still in the page.
            closePop(true);
            if (kind === "nozzle" || kind === "bed" || kind === "chamber") {
              var presets = kind === "nozzle"
                ? [["PLA 220", 220], ["PETG 250", 250], ["ABS 260", 260], ["Выкл", 0]]
                : kind === "chamber"
                ? [["40", 40], ["50", 50], ["60", 60], ["65", 65], ["Выкл", 0]]
                : [["60", 60], ["80", 80], ["100", 100], ["Выкл", 0]];
              openPop(chip, pid, tempPopHtml(kind, chip.dataset.cur || "", presets), kind);
            } else if (kind === "speed") {
              openPop(chip, pid, speedPopHtml());
            } else if (kind === "fan") {
              openPop(chip, pid, fanPopHtml(+chip.dataset.part || 0, +chip.dataset.aux || 0, +chip.dataset.chamber || 0));
            } else if (kind === "ams") {
              var pa = findPrinter(pid);
              if (pa) {
                cardUiOf(pid).pop = "ams";
                openPop(chip, pid, amsPopHtml(pa), "ams");
              }
            }
            applyAndRender();
            return;
          }
          // Phone: a tap on the confirm question shows its reason in the status
          // line (and gives the reader another 5 s).
          var q = e.target.closest(".pc-confirm .q[data-tip]");
          if (q && grid.contains(q)) {
            var qc = q.closest(".printer-card");
            if (qc) { armConfirm(qc.dataset.id); setInfoLine(qc.dataset.id, q.dataset.tip); }
            return;
          }
          var btn = e.target.closest(".pc-controls .pc-ctl, .pc-confirm button, .pc-cmd .x");
          if (!btn || !grid.contains(btn)) return;
          var card = btn.closest(".printer-card"), pid2 = card && card.dataset.id, p = pid2 && findPrinter(pid2);
          if (!p) return;
          var ui = cardUiOf(pid2), cmd = btn.dataset.cmd;
          if (cmd === "dismiss") { ui.line = null; ui.res = null; applyAndRender(); return; }
          if (cmd === "confirm-no") { ui.confirm = null; applyAndRender(); return; }
          if (cmd === "confirm-yes") {
            var what = ui.confirm;
            ui.confirm = null;
            if (what) runCardCmd(pid2, what === "stop" ? "stop" : "cooldown", null, what === "stop" ? "stop" : "cool");
            else applyAndRender();
            return;
          }
          // The confirmation just timed out under the finger: this tap was meant for it.
          if (ui.confirmGoneAt && Date.now() - ui.confirmGoneAt < 1000) return;
          // aria-disabled, not disabled: the tap still arrives — show why.
          if (btn.getAttribute("aria-disabled") === "true") { if (btn.dataset.tip) setInfoLine(pid2, btn.dataset.tip); return; }
          if (ui.busy) return;
          switch (cmd) {
            case "light": runCardCmd(pid2, p.light_on === true ? "light_off" : "light_on", null, "light"); break;
            case "pause": case "resume": runCardCmd(pid2, cmd, null, "pp"); break;
            case "stop": askConfirm(pid2, "stop"); break;
            case "cool": coolCmd(pid2, p); break;
            case "eject": runCardCmd(pid2, "eject", null, "eject"); break;
            case "ams": e.stopPropagation(); togglePop(btn, pid2, "ams"); break;
            case "more": e.stopPropagation(); togglePop(btn, pid2, "menu"); break;
          }
        });
        document.addEventListener("click", function (e) {
          if (!pcPop || pcPop.style.display === "none") return;
          // composedPath() is fixed when the click starts: a button the popover
          // just replaced (a next screen) still counts as inside it.
          var path = e.composedPath ? e.composedPath() : [];
          if (path.indexOf(pcPop) !== -1 || pcPop.contains(e.target)) return;
          if (e.target.closest(".pc-editable") || e.target.closest(".pc-controls [data-cmd=ams], .pc-controls [data-cmd=more]")) return;
          closePop();
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
      var amsAutodry = null;          // GET /api/ams/autodry (null: not loaded or unavailable)
      var adForm = null;              // the rules form while it has unsaved edits (a config object), else null
      var adLoadedCfgJson = "";       // autodryCanon of the saved config the form was filled from
      var amsScrollToAutodry = false; // the settings tab link: scroll to the section after the next render
      var adJournalAll = false;
      var adDlgBusy = false;
      var adInited = false;
      (function () {
        var b = document.getElementById("autodryLinkBtn");
        if (b) b.addEventListener("click", function () { amsScrollToAutodry = true; setActiveView("ams"); });
      })();
      function amsPanelActive() {
        var p = document.querySelector('.view-panel[data-panel="ams"]');
        return p && p.classList.contains('active');
      }
      function amsInit() {
        if (amsInited) return;
        amsInited = true;
        autodryInit();
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
        var grid = document.getElementById('amsGrid');
        if (grid) grid.addEventListener('click', function (e) {
          var cfgLink = e.target.closest('[data-ad-row]');
          if (cfgLink && grid.contains(cfgLink)) {
            e.preventDefault();
            var row = document.getElementById('ad-row-' + cfgLink.getAttribute('data-ad-row'));
            if (row) {
              row.scrollIntoView({ behavior: 'smooth', block: 'center' });
              row.classList.add('flash');
              setTimeout(function () { row.classList.remove('flash'); }, 1600);
            }
            return;
          }
          var btn = e.target.closest('[data-ams-dry-open],[data-ams-dry-stop]');
          if (!btn || !grid.contains(btn) || btn.disabled) return;
          // The document click handler would close the popover this click opens.
          e.stopPropagation();
          var pid = btn.dataset.pid, idx = parseInt(btn.dataset.ams, 10);
          var p = findPrinter(pid);
          var unit = p ? amsUnits(p).find(function (x) { return x.index === idx; }) : null;
          if (!p || !unit || !unit.dry) return;
          if (btn.hasAttribute('data-ams-dry-open')) {
            closePop(true);
            openPop(btn, pid, dryPopHtml(p, unit), 'dry', 'amsTab');
            applyAndRender();
          } else if (confirm('Остановить сушку ' + amsUnitName(idx) + ' на «' + p.label + '»?')) {
            btn.disabled = true;   // until the reply: no second stop
            sendPrinterCmd(pid, 'dry_stop', { ams: idx }).then(function () { btn.disabled = false; });
          }
        });
      }
      function amsHumClass(pct, lvl) {
        if (pct != null) return pct <= 25 ? 'ok' : pct <= 40 ? 'warn' : 'bad';
        if (lvl != null) return lvl <= 2 ? 'ok' : lvl === 3 ? 'warn' : 'bad';
        return '';
      }
      function amsHumColorVar(cls) {
        return cls === 'bad' ? 'var(--danger)' : cls === 'warn' ? 'var(--warn)' : 'var(--ok)';
      }
      // Геометрия viewBox графиков AMS — общая для отрисовки и ховера.
      var AMS_GW = 378, AMS_GH = 88, AMS_GPL = 26, AMS_GPR = 8, AMS_GPT = 8, AMS_GPB = 16;
      // SVG-линия по точкам [{t,v}] в окне [fr,to]. opts: zones — цветовые зоны влажности,
      // dry — периоды сушки [[t0,t1]], thr — пороги автосушки [[значение, подпись, цвет]].
      function amsChart(points, fr, to, ymin, ymax, color, opts) {
        opts = opts || {};
        var W = AMS_GW, H = AMS_GH, PL = AMS_GPL, PR = AMS_GPR, PT = AMS_GPT, PB = AMS_GPB;
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
        if (opts.zones) {
          [[0, 25, 'var(--ok)'], [25, 40, 'var(--warn)'], [40, 50, 'var(--danger)']].forEach(function (z) {
            var yTop = Y(Math.min(z[1], ymax)), yBot = Y(Math.max(z[0], ymin));
            bandRects += '<rect x="' + PL + '" y="' + yTop.toFixed(1) + '" width="' + iw +
              '" height="' + (yBot - yTop).toFixed(1) + '" fill="' + z[2] + '" opacity="0.05"/>';
          });
        }
        var ov = amsChartOverlay({ W: W, H: H, PL: PL, PR: PR, PT: PT, PB: PB }, fr, to, ymin, ymax, opts);
        var gid = 'amsg' + Math.round(Math.abs(color.length * 13 + ymax * 7 + points.length));
        var xl = '<text class="axis" x="' + PL + '" y="' + (H - 4) + '" text-anchor="start">-' + amsHours + 'ч</text>' +
          '<text class="axis" x="' + (W - PR) + '" y="' + (H - 4) + '" text-anchor="end">сейчас</text>';
        return '<svg class="ams-chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
          '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0" stop-color="' + color + '" stop-opacity="0.28"/>' +
          '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
          bandRects + gl + ov.under +
          '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
          '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round"/>' +
          ov.labels + yl + xl + '</svg>';
      }
      function amsTipTime(t) {
        var d = new Date(t * 1000);
        var s = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
        return amsHours > 24 ? pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + ' ' + s : s;
      }
      // Ховер по графику: вертикальная линия, маркер ближайшей точки и тултип «время · значение».
      function amsAttachHover(box, points, fr, to, ymin, ymax, color, suf) {
        var svg = box.querySelector('svg.ams-chart');
        if (!svg || !points.length) return;
        box.classList.add('ams-gbox');
        var iw = AMS_GW - AMS_GPL - AMS_GPR, ih = AMS_GH - AMS_GPT - AMS_GPB;
        var pts = points.map(function (p) {
          return {
            x: AMS_GPL + iw * Math.max(0, Math.min(1, (p.t - fr) / (to - fr))),
            y: AMS_GPT + ih * (1 - (p.v - ymin) / (ymax - ymin)),
            t: p.t, v: p.v
          };
        });
        var line = document.createElement('div'); line.className = 'ams-hline';
        var dot = document.createElement('div'); dot.className = 'ams-hdot';
        var tip = document.createElement('div'); tip.className = 'ams-htip';
        line.style.top = (AMS_GPT / AMS_GH * 100) + '%';
        line.style.height = (ih / AMS_GH * 100) + '%';
        dot.style.background = color;
        box.appendChild(line); box.appendChild(dot); box.appendChild(tip);
        function hide() {
          line.style.display = 'none'; dot.style.display = 'none'; tip.style.display = 'none';
        }
        hide();
        function move(e) {
          var rect = svg.getBoundingClientRect();
          if (!rect.width) return;
          var vx = (e.clientX - rect.left) / rect.width * AMS_GW;
          var best = pts[0];
          for (var i = 1; i < pts.length; i++) {
            if (Math.abs(pts[i].x - vx) < Math.abs(best.x - vx)) best = pts[i];
          }
          var xp = best.x / AMS_GW * 100;
          var yp = Math.max(0, Math.min(1, best.y / AMS_GH)) * 100;
          line.style.left = xp + '%'; line.style.display = '';
          dot.style.left = xp + '%'; dot.style.top = yp + '%'; dot.style.display = '';
          tip.textContent = amsTipTime(best.t) + ' · ' + (Math.round(best.v * 10) / 10) + suf;
          tip.style.left = xp + '%';
          tip.style.transform = xp < 18 ? 'translateX(0)' : xp > 82 ? 'translateX(-100%)' : 'translateX(-50%)';
          tip.style.display = '';
        }
        box.addEventListener('pointermove', move);
        box.addEventListener('pointerdown', move);
        box.addEventListener('pointerleave', hide);
      }
      // Card title only: unit_index is the physical unit id (history key) — 0-3 for
      // AMS / AMS 2 Pro / AMS Lite, 128-135 for AMS HT.
      function amsUnitName(index) {
        return index >= 128 ? 'AMS HT ' + (index - 127) : 'AMS ' + (index + 1);
      }
      function amsCardShell(u) {
        var cls = 'ams-card' + (u.drying ? ' drying' : '') +
          (!u.drying && amsHumClass(u.humidity_pct, u.humidity) === 'bad' ? ' bad' : '');
        var hcls = amsHumClass(u.humidity_pct, u.humidity);
        var humTxt = u.humidity_pct != null ? '💧 ' + u.humidity_pct + '%'
          : (u.humidity != null ? '💧 ' + u.humidity + '/5' : '💧 н/д');
        var badges = '<span class="ams-badge ' + hcls + '">' + humTxt + '</span>';
        if (u.temp != null) badges += '<span class="ams-badge">🌡 ' + Math.round(u.temp) + '°</span>';
        var slots = '<div class="ams-slots typed">' + (u.slots || []).map(function (s, i) {
          if (s.empty) return '<div class="ams-slot-w"><div class="ams-slot empty"></div><div class="ams-slot-t none">—</div></div>';
          var active = u.tray_now_local === i;
          var color = (typeof s.color === 'string' && /^[0-9a-fA-F]{3,8}$/.test(s.color)) ? '#' + s.color : '';
          return '<div class="ams-slot-w"><div class="ams-slot' + (active ? ' active' : '') + '"' + (color ? ' style="background:' + color + '"' : '') +
            '></div><div class="ams-slot-t' + (active ? ' on' : '') + '">' + safeText(s.type, '?') + '</div></div>';
        }).join('') + '</div>';
        // Drying controls: the same gate as the fleet card (admin, commands
        // allowed, fresh telemetry), against the printer's /api/printers row.
        var p = findPrinter(u.printer_id);
        var full = p ? amsUnits(p).find(function (x) { return x.index === u.unit_index; }) : null;
        var can = !!(isAdmin() && p && !isOffline(p) && ctlCaps(p).ams === true
          && !p.grace_period_active && full && full.dry);
        var dry = amsTabDryHtml(u, p, can);
        var key = u.printer_id + '__' + u.unit_index;
        var amsModel = u.dry && u.dry.type === 'n3f' ? 'AMS 2 Pro' : u.dry && u.dry.type === 'n3s' ? 'AMS HT' : '';
        var model = [u.device_type ? escHtml(u.device_type) : '', amsModel].filter(Boolean).join(' · ') || 'AMS';
        var adu = u.dry ? autodryUnit(key) : null, auto = '';
        if (adu) {
          var al = autodryLine(adu, amsAutodry.config, amsAutodry.limits, amsAutodry.now), ek = safeText(key, '');
          auto = '<div class="ams-auto st-' + al.cls + '"><i class="d"></i>' + (al.noKey ? '' : '<span class="k">Авто:</span>') +
            '<span class="v">' + al.card + '</span><a class="cfg" href="#ad-row-' + ek + '" data-ad-row="' + ek +
            '" title="К настройке блока">⚙</a></div>';
        }
        return '<div class="ams-card' + cls.slice(8) + '" data-key="' + key + '">' +
          '<div class="ams-chead"><div class="ams-spool">🧵</div>' +
          '<div class="ams-ctitle"><div class="ams-cname">' + escHtml(u.label) + ' · ' + amsUnitName(u.unit_index) +
          ' <span class="ams-dot ' + (u.online ? 'on' : 'off') + '"></span></div>' +
          '<div class="ams-csub">' + model + '</div></div>' +
          '<div class="ams-badges">' + badges + '</div></div>' +
          slots + dry + auto +
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
        var bands = amsDryBands(rows, 600);
        var adc = amsAutodry && amsAutodry.config;
        var thr = adc && adc.enabled ? [[adc.start_rh, 'старт', 'var(--warn)'], [adc.target_rh, 'цель', 'var(--ok)']] : [];
        humBox.innerHTML = usePct
          ? amsChart(humPts, fr, to, 10, 50, amsHumColorVar(hcls), { zones: true, dry: bands, thr: thr })
          : amsChart(humPts, fr, to, 0, 5, 'var(--ok)', { dry: bands });
        tempBox.innerHTML = amsChart(tempPts, fr, to, 20, 60, 'var(--info)', { dry: bands });
        if (usePct) amsAttachHover(humBox, humPts, fr, to, 10, 50, amsHumColorVar(hcls), '%');
        else amsAttachHover(humBox, humPts, fr, to, 0, 5, 'var(--ok)', '/5');
        amsAttachHover(tempBox, tempPts, fr, to, 20, 60, 'var(--info)', '°');
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
        var units, ad = null;
        try {
          var both = await Promise.all([
            apiFetch('/api/ams/current'),
            apiFetch('/api/ams/autodry').then(function (r) { return r.ok ? r.json() : null; })
              .catch(function () { return null; })
          ]);
          units = await both[0].json();
          ad = both[1];
        } catch (e) { console.error('loadAms current failed', e); return; }
        amsAutodry = ad;
        if (!units.length) {
          grid.innerHTML = '';
          if (empty) empty.style.display = '';
          autodryRender();
          adScrollIfAsked();
          return;
        }
        if (empty) empty.style.display = 'none';
        function sev(u) {
          if (u.drying) return 0;
          var c = amsHumClass(u.humidity_pct, u.humidity);
          return c === 'bad' ? 1 : c === 'warn' ? 2 : 3;
        }
        units.sort(function (a, b) { return sev(a) - sev(b) || String(a.label).localeCompare(String(b.label)); });
        grid.innerHTML = units.map(amsCardShell).join('');
        autodryRender();
        adScrollIfAsked();
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

      function adScrollIfAsked() {
        if (!amsScrollToAutodry) return;
        amsScrollToAutodry = false;
        var sec = document.getElementById('autodry');
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      // ===== AMS auto-drying section (DOM; the pure helpers are the autodry-ui block) =====
      var AD_MODE_OPTS = [["inherit", "Как общие"], ["auto", "Авто по пластику"], ["pla", "PLA"], ["petg", "PETG"],
                          ["tpu", "TPU"], ["custom", "Своё…"]];
      var AD_FIELD_IDS = { start: "adStart", hold: "adHold", target: "adTarget", custT: "adCustT", custH: "adCustH",
                           pause: "adPause", limit: "adLimit", holdOff: "adHoldOff", winFrom: "adWinFrom", winTo: "adWinTo" };
      function adEl(id) { return document.getElementById(id); }
      function adSetText(id, text) { var el = adEl(id); if (el) el.textContent = text; }
      function autodryUnit(key) {
        var units = (amsAutodry && amsAutodry.units) || [];
        for (var i = 0; i < units.length; i++) if (units[i].key === key) return units[i];
        return null;
      }
      // The rules the derived cells show: the form while it has edits, else the saved config.
      function adViewCfg() { return adForm || (amsAutodry && amsAutodry.config) || null; }
      function adReadForm() {
        function v(id) { var el = adEl(id); return el ? el.value : ""; }
        function c(id) { var el = adEl(id); return !!(el && el.checked); }
        var units = {};
        document.querySelectorAll("#adRows tr[data-u]").forEach(function (tr) {
          var on = tr.querySelector("[data-ad-on]"), mode = tr.querySelector("[data-ad-mode]");
          var ct = tr.querySelector("[data-ad-ct]"), ch = tr.querySelector("[data-ad-ch]");
          units[tr.getAttribute("data-u")] = { enabled: !!(on && on.checked), mode: mode ? mode.value : "inherit",
                                               temp: ct ? ct.value : "", hours: ch ? ch.value : "" };
        });
        return { master: c("adMaster"), start: v("adStart"), hold: v("adHold"), target: v("adTarget"), mode: v("adMode"),
                 custT: v("adCustT"), custH: v("adCustH"), pause: v("adPause"), limit: v("adLimit"), win: v("adWin"),
                 winFrom: v("adWinFrom"), winTo: v("adWinTo"), print: c("adPrint"), holdOff: v("adHoldOff"), units: units };
      }
      function adWriteForm(cfg) {
        function sv(id, val) { var el = adEl(id); if (el) el.value = val == null ? "" : String(val); }
        function sc(id, val) { var el = adEl(id); if (el) el.checked = !!val; }
        // The limit is a select: a saved value without its own option gets one.
        var lim = adEl("adLimit");
        if (lim && lim.tagName === "SELECT" && cfg.max_runs != null &&
            !Array.prototype.some.call(lim.options, function (o) { return o.value === String(cfg.max_runs); })) {
          var opt = document.createElement("option");
          opt.value = String(cfg.max_runs);
          opt.textContent = "не больше " + cfg.max_runs + " подряд";
          lim.appendChild(opt);
        }
        sc("adMaster", cfg.enabled); sv("adHold", cfg.hold_h); sv("adTarget", cfg.target_rh);
        sv("adMode", cfg.mode); sv("adCustT", cfg.custom_temp); sv("adCustH", cfg.custom_hours); sv("adPause", cfg.pause_h);
        sv("adLimit", cfg.max_runs); sc("adPrint", cfg.while_printing); sv("adHoldOff", cfg.manual_hold_h);
        var w = cfg.window;
        sv("adWin", !w ? "any" : (w.from === "22:00" && w.to === "08:00") ? "night" : "custom");
        sv("adWinFrom", w ? w.from : ""); sv("adWinTo", w ? w.to : "");
        adToggleInl();
      }
      function adToggleInl() {
        var m = adEl("adMode"), cu = adEl("adCustom"), w = adEl("adWin"), wc = adEl("adWinCustom");
        if (cu && m) cu.classList.toggle("on", m.value === "custom");
        if (wc && w) wc.classList.toggle("on", w.value === "custom");
        document.querySelectorAll("#adRows [data-ad-mode]").forEach(function (sel) {
          var inl = sel.parentNode.querySelector(".inl");
          if (inl) inl.classList.toggle("on", sel.value === "custom");
        });
      }
      function adMatBadges(u) {
        var m = u.materials || [];
        if (!m.length) return '<span class="mat none">пусто</span>';
        return m.map(function (x) {
          var hex = amsHex(x.color);
          return '<span class="mat' + (x.ht ? " ht" : "") + '"><i' + (hex ? ' style="background:#' + hex + '"' : "") + '></i>' +
            safeText(x.type, "?") + (x.count > 1 ? " ×" + Number(x.count) : "") + "</span>";
        }).join("");
      }
      function adPlanOf(u) {
        if (!u.dry) return null;
        if (!adForm) return u.plan;
        var uc = (adForm.units && adForm.units[u.key]) || { mode: "inherit" };
        var mode = uc.mode && uc.mode !== "inherit" ? uc.mode : adForm.mode;
        var custom = uc.mode === "custom" ? { temp: uc.temp, hours: uc.hours } : { temp: adForm.custom_temp, hours: adForm.custom_hours };
        return autodryPlanFor(u, mode, custom);
      }
      function adRunCell(u) {
        return autodryRunCell(u, adPlanOf(u));
      }
      function adStCell(u) {
        var l = autodryLine(u, amsAutodry.config, amsAutodry.limits, amsAutodry.now);
        var link = l.link && isAdmin() ? '<button type="button" class="ad-link ad-admin" data-ad-release="' + safeText(u.key, "") + '">' +
          (l.link === "reenable" ? "включить снова" : "снять паузу") + "</button>" : "";
        return '<div class="stt st-' + l.cls + '"><i class="d"></i><div><span class="m">' + l.main + "</span>" +
          (l.sub ? '<span class="sub">' + l.sub + "</span>" : "") + link + "</div></div>";
      }
      function adRenderRows() {
        var tb = adEl("adRows");
        if (!tb || !amsAutodry) return;
        var cfg = amsAutodry.config, units = amsAutodry.units || [];
        if (!units.length) { tb.innerHTML = '<tr><td colspan="6" class="muted">Нет блоков AMS 2 Pro / AMS HT.</td></tr>'; return; }
        tb.innerHTML = units.map(function (u) {
          var k = safeText(u.key, ""), lbl = safeText(u.label, "");
          var uc = (cfg.units && cfg.units[u.key]) || u.unit_cfg || { enabled: true, mode: "inherit", temp: 55, hours: 8 };
          var opts = AD_MODE_OPTS.map(function (o) {
            return '<option value="' + o[0] + '"' + (uc.mode === o[0] ? " selected" : "") + ">" + o[1] + "</option>";
          }).join("");
          var state = !u.online ? "офлайн" : u.printing ? "печатает" : "готов";
          return '<tr id="ad-row-' + k + '" data-u="' + k + '">' +
            '<td class="c-unit"><div class="ad-unit"><strong>' + lbl + '</strong><span class="sub">' +
            [safeText(u.device_type, ""), safeText(u.name, ""), state].filter(Boolean).join(" · ") + "</span></div></td>" +
            '<td class="c-mat" data-l="Пластик">' + adMatBadges(u) + "</td>" +
            '<td class="c-auto"><label class="toggle-switch" title="Автосушка для блока"><input type="checkbox" data-ad-on="' + k + '"' +
            (uc.enabled ? " checked" : "") + ' aria-label="Автосушка для ' + lbl + '" /><span class="toggle-slider"></span></label></td>' +
            '<td class="c-mode" data-l="Режим"><select class="sel sm" data-ad-mode="' + k + '" aria-label="Режим для ' + lbl + '">' + opts + "</select>" +
            '<span class="inl' + (uc.mode === "custom" ? " on" : "") + '"><input class="num sm" data-ad-ct="' + k + '" value="' + Number(uc.temp) +
            '" inputmode="numeric" aria-label="Температура, °C" />° · <input class="num sm" data-ad-ch="' + k + '" value="' + Number(uc.hours) +
            '" inputmode="numeric" aria-label="Часы" /> ч</span></td>' +
            '<td class="c-run run" data-l="Прогон" data-run="' + k + '">' + adRunCell(u) + "</td>" +
            '<td class="c-st" data-l="Статус" data-st="' + k + '">' + adStCell(u) + "</td></tr>";
        }).join("");
      }
      // While the form has edits only the status and plan cells follow the server.
      function adRefreshCells() {
        document.querySelectorAll("#adRows tr[data-u]").forEach(function (tr) {
          var u = autodryUnit(tr.getAttribute("data-u"));
          if (!u) return;
          var run = tr.querySelector("[data-run]"), st = tr.querySelector("[data-st]");
          if (run) run.innerHTML = adRunCell(u);
          if (st) st.innerHTML = adStCell(u);
        });
      }
      function adRenderJump() {
        var b = adEl("amsAdJump");
        if (!b) return;
        b.hidden = !amsAutodry;
        if (!amsAutodry) return;
        var c = amsAutodry.config, n = 0;
        (amsAutodry.units || []).forEach(function (u) {
          if (u.dry && u.unit_cfg && u.unit_cfg.enabled && !u.disabled_reason) n++;
        });
        b.classList.toggle("on", !!c.enabled);
        adSetText("amsAdJumpState", !c.enabled ? "выкл" : c.dry_run ? "пробный режим"
          : "вкл · " + n + " " + adPlural(n, "блок", "блока", "блоков"));
      }
      function adRenderLegend() {
        var c = amsAutodry && amsAutodry.config;
        document.querySelectorAll(".ams-legend .ad-lg").forEach(function (el) { el.hidden = !(c && c.enabled); });
        if (c) { adSetText("lgStart", String(c.start_rh)); adSetText("lgTarget", String(c.target_rh)); }
      }
      function adRenderDerived() {
        var c = adViewCfg();
        if (!c) return;
        var lim = Number(c.max_runs) || 0;
        var vals = { start: c.start_rh, hold: c.hold_h, pause: c.pause_h, target: c.target_rh, mhold: c.manual_hold_h,
                     limitText: lim ? "до " + lim + " " + adPlural(lim, "прогона", "прогонов", "прогонов") + " подряд"
                                    : "без лимита, пока не дойдёт до цели" };
        document.querySelectorAll("#autodry [data-b]").forEach(function (el) {
          var v = vals[el.getAttribute("data-b")];
          el.textContent = v == null ? "—" : String(v);
        });
        var sec = adEl("autodry");
        if (sec) sec.classList.toggle("master-off", !c.enabled);
        var units = (amsAutodry && amsAutodry.units) || [], cap = 65;
        units.forEach(function (u) { if (u.plan_ctx && u.plan_ctx.cap > cap) cap = u.plan_ctx.cap; });
        adSetText("adMasterSub", "Следит за " + units.length + " " + adPlural(units.length, "блоком", "блоками", "блоками") +
          " и запускает сушку по правилам ниже. Выключение не гасит уже идущие прогоны.");
        adSetText("adCustLim", "45–" + cap + "°, 1–24 ч");
      }
      function adRenderBanner() {
        var el = adEl("adBanner");
        if (!el || !amsAutodry) return;
        var lines = autodryBanner(amsAutodry, isAdmin());
        el.innerHTML = lines.join("");
        el.hidden = !lines.length;
      }
      function adRenderJournal() {
        var box = adEl("adJournal"), more = adEl("adJournalMore");
        if (!box || !amsAutodry) return;
        var rows = amsAutodry.journal || [];
        if (!rows.length) {
          box.innerHTML = '<div class="jr-empty">Событий пока нет</div>';
          if (more) more.hidden = true;
          return;
        }
        var shown = adJournalAll ? rows : rows.slice(0, 8);
        box.innerHTML = shown.map(function (e) { return autodryJournalRow(e, amsAutodry.now); }).join("");
        if (more) {
          more.hidden = rows.length <= 8;
          more.textContent = adJournalAll ? "Свернуть" : "Показать все (" + rows.length + ")";
        }
      }
      function adSetDirtyUI(on) {
        var d = adEl("adDirty"), s = adEl("adSave"), f = adEl("adSaved");
        if (d) d.classList.toggle("on", on);
        if (s) s.disabled = !on || !isAdmin();
        if (on && f) f.classList.remove("show");
      }
      function adShowError(text) {
        var el = adEl("adError");
        if (!el) return;
        el.textContent = text || "";
        el.hidden = !text;
      }
      function adFieldEl(field) {
        var el = null;
        if (AD_FIELD_IDS[field]) el = adEl(AD_FIELD_IDS[field]);
        else if (field && field.indexOf("u:") === 0) {
          var parts = field.split(":"), attr = parts[2] === "hours" ? "data-ad-ch" : "data-ad-ct";
          document.querySelectorAll("#adRows [" + attr + "]").forEach(function (x) {
            if (x.getAttribute(attr) === parts[1]) el = x;
          });
        }
        return el;
      }
      function adFocusField(field) {
        var el = adFieldEl(field);
        if (el) { el.focus(); if (el.select) el.select(); }
      }
      // The invalid field keeps the error colour until the form is valid again (null: clear).
      function adMarkBad(field) {
        document.querySelectorAll("#autodry .num.bad").forEach(function (x) {
          x.classList.remove("bad");
          x.removeAttribute("aria-invalid");
        });
        var el = field ? adFieldEl(field) : null;
        if (el) { el.classList.add("bad"); el.setAttribute("aria-invalid", "true"); }
      }
      function adRanges() { return amsAutodry && amsAutodry.limits ? amsAutodry.limits.ranges : null; }
      function autodryRender() {
        var sec = adEl("autodry");
        adRenderJump();
        adRenderLegend();
        if (!sec) return;
        var body = adEl("adBody"), na = adEl("adUnavailable");
        if (!amsAutodry) {
          var banner = adEl("adBanner");                   // outside #adBody: never leave the last text up
          if (banner) { banner.hidden = true; banner.innerHTML = ""; }
          if (body) body.hidden = true;
          if (na) na.hidden = false;
          return;
        }
        if (body) body.hidden = false;
        if (na) na.hidden = true;
        var admin = !!isAdmin();
        adSetText("adRoleChip", admin ? "только админ" : "только просмотр");
        if (!adForm) {
          adWriteForm(amsAutodry.config);
          adRenderRows();
          adLoadedCfgJson = autodryCanon(amsAutodry.config);
        } else adRefreshCells();
        sec.querySelectorAll(".ad-admin").forEach(function (el) { el.hidden = !admin; });
        sec.querySelectorAll("input, select").forEach(function (el) { el.disabled = !admin; });
        adToggleInl();
        adRenderDerived();
        adRenderBanner();
        adRenderJournal();
        adSetDirtyUI(!!adForm);
      }
      function adOnFormInput(e) {
        var t = e.target;
        if (!amsAutodry || !isAdmin() || !t || !t.closest || !t.closest("#adBody")) return;
        if (!(t.matches("input, select"))) return;
        adToggleInl();
        var res = autodryFormToConfig(adReadForm(), amsAutodry.config, adRanges());
        if (res.error) {                                     // keep the edits; say what is wrong right away
          adForm = adForm || JSON.parse(JSON.stringify(amsAutodry.config));
          adMarkBad(res.field);
          adSetDirtyUI(true);
          // an empty field mid-typing is not worth a red line yet
          if (!(t.value === "" && /нужно целое число/.test(res.error))) adShowError(res.error);
          return;
        }
        adForm = autodryCanon(res.config) !== adLoadedCfgJson ? res.config : null;
        adMarkBad(null);
        adShowError(null);
        adSetDirtyUI(!!adForm);
        adRefreshCells();
        adRenderDerived();
      }
      async function adSave() {
        if (!amsAutodry || !isAdmin()) return;
        var res = autodryFormToConfig(adReadForm(), amsAutodry.config, adRanges());
        if (res.error) { adShowError(res.error); adMarkBad(res.field); adFocusField(res.field); return; }
        var btn = adEl("adSave");
        if (btn) btn.disabled = true;
        try {
          var r = await apiFetch("/api/admin/ams/autodry", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                             body: JSON.stringify(res.config) });
          var d = null;
          try { d = await r.json(); } catch (x) { d = null; }
          if (!r.ok || !d) {
            adShowError(d && typeof d.detail === "string" ? d.detail : "Не сохранилось (" + r.status + ")");
            if (btn) btn.disabled = false;
            return;
          }
          amsAutodry = d;
          adForm = null;
          adShowError(null);
          adMarkBad(null);
          autodryRender();
          var f = adEl("adSaved");
          if (f) { f.classList.add("show"); setTimeout(function () { f.classList.remove("show"); }, 2200); }
          loadAms();
          setTimeout(loadAms, 1500);                         // the engine wakes on save
        } catch (x) {
          adShowError("Ошибка сети — не сохранилось");
          if (btn) btn.disabled = false;
        }
      }
      function adReset() {
        adForm = null;
        adShowError(null);
        adMarkBad(null);
        autodryRender();
      }
      // --- dialogs: step 1 a checklist, step 2 the per-unit results ---
      function adOpenDlg(html) {
        var back = adEl("adDlgBack"), dlg = adEl("adDlg");
        if (!back || !dlg) return;
        dlg.innerHTML = html;
        back.classList.add("on");
        var f = dlg.querySelector("button:not([disabled])");
        if (f) f.focus();
      }
      function adCloseDlg(force) {
        if (adDlgBusy && !force) return;
        var back = adEl("adDlgBack");
        if (back) back.classList.remove("on");
      }
      function adResultsHtml(results, err) {
        var items = results ? results.map(function (x) {
          return '<div class="ad-dlg-item' + (x.ok ? "" : " fail") + '"><span>' + (x.ok ? "✓ " : "✗ ") + "<b>" + safeText(x.label, "") +
            " · " + safeText(x.name, "") + '</b><span class="sub">' + safeText(x.detail, "") + "</span></span></div>";
        }).join("") : '<div class="ad-dlg-item fail"><span>' + safeText(err, "") + "</span></div>";
        return "<h4>" + (results ? "Готово" : "Не получилось") + '</h4><div class="ad-dlg-list">' + items +
          '</div><div class="ad-dlg-foot"><button type="button" class="pill-btn" data-done>Готово</button></div>';
      }
      function adBindDlg(url, busyText) {
        var dlg = adEl("adDlg");
        if (!dlg) return;
        dlg.querySelectorAll("[data-x]").forEach(function (b) { b.onclick = function () { adCloseDlg(); }; });
        var go = dlg.querySelector("[data-go]");
        if (!go) return;
        var label = go.textContent;
        function upd() {
          var k = dlg.querySelectorAll("[data-pick]:checked").length;
          go.textContent = label.replace(/\(\d+\)/, "(" + k + ")");
          go.disabled = !k;
        }
        dlg.querySelectorAll("[data-pick]").forEach(function (c) { c.onchange = upd; });
        upd();
        go.onclick = async function () {
          var keys = Array.prototype.map.call(dlg.querySelectorAll("[data-pick]:checked"), function (c) { return c.getAttribute("data-pick"); });
          if (!keys.length) return;
          adDlgBusy = true;
          dlg.querySelectorAll("button, input").forEach(function (b) { b.disabled = true; });
          go.textContent = busyText;
          var results = null, err = null;
          try {
            var r = await apiFetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ units: keys }) });
            var d = null;
            try { d = await r.json(); } catch (x) { d = null; }
            if (r.ok && d) { results = d.results || []; if (d.snapshot) amsAutodry = d.snapshot; }
            else err = d && typeof d.detail === "string" ? d.detail : "Ошибка " + r.status;
          } catch (x) { err = "Ошибка сети — результат неизвестен, смотрите журнал"; }
          adDlgBusy = false;
          adOpenDlg(adResultsHtml(results, err));
          var done = dlg.querySelector("[data-done]");
          if (done) done.onclick = function () { adCloseDlg(); loadAms(); setTimeout(loadAms, 1500); };
          autodryRender();
        };
      }
      function adRunNowDlg() {
        if (!amsAutodry || !isAdmin()) return;
        var n = 0;
        var items = (amsAutodry.units || []).map(function (u) {
          var head = "<b>" + safeText(u.label, "") + " · " + safeText(u.name, "") + "</b>";
          if (u.dry && u.dry.active) {
            return '<label class="ad-dlg-item dis"><input type="checkbox" disabled /><span>' + head + ' — уже сушится<span class="sub">осталось ' +
              adLeft(u.dry.remaining_min) + " · пропустим</span></span></label>";
          }
          var dis = !u.dry || !u.online ? "принтер офлайн"
            : u.phase === "running" && !u.seen_active ? "запуск ещё не подтверждён AMS"
            : u.dry_ok !== true ? "прошивка не даёт сушить удалённо"
            : u.cmd_ok === false ? "прошивка блокирует сторонние команды"
            : u.dry.blockers && u.dry.blockers.length ? safeText(u.dry.blockers[0].text, "") : null;
          var p = u.plan_now;
          if (!dis && !p) dis = "нечего сушить";
          if (dis) return '<label class="ad-dlg-item dis"><input type="checkbox" disabled /><span>' + head + '<span class="sub">' + dis + "</span></span></label>";
          var checked = !p.empty;
          if (checked) n++;
          var warns = [];
          if (u.interrupts > 0) warns.push("⚠ последние запуски срывались (" + Number(u.interrupts) + " подряд)");
          if (u.disabled_reason || (u.unit_cfg && !u.unit_cfg.enabled)) warns.push("автосушка блока отключена — ручной запуск всё равно можно");
          if (u.hold_until) warns.push("снимет паузу после ручной остановки");
          if (u.dry.feeding && !p.limit) warns.push("идёт печать из этого AMS — не выше " + Number(u.dry.max_temp) + "°");
          var sub = safeText(p.limit || p.why, "");
          return '<label class="ad-dlg-item"><input type="checkbox" data-pick="' + safeText(u.key, "") + '"' + (checked ? " checked" : "") + " /><span><b>" +
            safeText(u.label, "") + " · " + safeText(u.name, "") + " · " + safeText(p.name, "") + " → " + Number(p.temp) + "° · " + Number(p.hours) +
            '\u00a0ч</b><span class="sub">' + sub + "</span>" +
            warns.map(function (w) { return '<span class="sub warn">' + w + "</span>"; }).join("") + "</span></label>";
        }).join("");
        adOpenDlg('<h4>Сушить сейчас</h4><p class="sub0">Ручной запуск: порог, окно и лимит в сутки не проверяются. Параметры — из колонки «Прогон».' +
          (adForm ? " Несохранённые правки не учитываются — сначала сохраните." : "") + "</p>" +
          '<div class="ad-dlg-list">' + items + "</div>" +
          '<div class="ad-dlg-foot"><span class="note">Команда уходит на принтер; если прошивка откажет — будет в журнале.</span>' +
          '<button type="button" class="ad-btn" data-x>Отмена</button><button type="button" class="pill-btn" data-go>Запустить (' + n + ")</button></div>");
        adBindDlg("/api/admin/ams/autodry/run-now", "Запускаем…");
      }
      function adStopDlg() {
        if (!amsAutodry || !isAdmin()) return;
        var list = (amsAutodry.units || []).filter(function (u) {
          return (u.dry && u.dry.active) || u.phase === "running" || u.phase === "foreign";
        });
        if (!list.length) {
          adOpenDlg('<h4>Остановить сушку</h4><p class="sub0">Сейчас ничего не сушится.</p><div class="ad-dlg-foot">' +
                    '<button type="button" class="ad-btn" data-x>Закрыть</button></div>');
          adBindDlg();
          return;
        }
        var items = list.map(function (u) {
          var what = u.dry && u.dry.active ? "— осталось " + adLeft(u.dry.remaining_min) : "— запуск ещё не подтверждён";
          var who = u.phase === "running" && u.origin === "auto" ? "прогон " + Number(u.series) + " автосушки"
            : u.phase === "running" ? "запущена вручную" : "сушка запущена не автосушкой";
          return '<label class="ad-dlg-item"><input type="checkbox" checked data-pick="' + safeText(u.key, "") + '" /><span><b>' +
            safeText(u.label, "") + " · " + safeText(u.name, "") + "</b> " + what + '<span class="sub">' + who + "</span></span></label>";
        }).join("");
        adOpenDlg('<h4>Остановить сушку</h4><p class="sub0">После остановки автосушка не будет трогать эти блоки ' +
          Number(amsAutodry.config.manual_hold_h) + "\u00a0ч (правило «После ручной остановки»).</p>" +
          '<div class="ad-dlg-list">' + items + '</div><div class="ad-dlg-foot"><button type="button" class="ad-btn" data-x>Отмена</button>' +
          '<button type="button" class="ad-btn-danger" data-go>Остановить (' + list.length + ")</button></div>");
        adBindDlg("/api/admin/ams/autodry/stop-all", "Останавливаем…");
      }
      function adDryRunOffDlg() {
        if (!amsAutodry || !isAdmin()) return;
        adOpenDlg('<h4>Выключить пробный режим?</h4><p class="sub0">Автосушка начнёт сама запускать сушку по правилам ниже. ' +
          'Остановить можно общим выключателем.</p><div class="ad-dlg-foot"><button type="button" class="ad-btn" data-x>Отмена</button>' +
          '<button type="button" class="ad-btn-danger" data-off>Выключить</button></div>');
        var dlg = adEl("adDlg");
        adBindDlg();
        var off = dlg.querySelector("[data-off]");
        off.onclick = async function () {
          adDlgBusy = true;
          dlg.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
          var ok = false, detail = "Не получилось";
          try {
            var cfg = JSON.parse(JSON.stringify(amsAutodry.config));
            cfg.dry_run = false;
            var r = await apiFetch("/api/admin/ams/autodry", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                               body: JSON.stringify(cfg) });
            var d = null;
            try { d = await r.json(); } catch (x) { d = null; }
            if (r.ok && d) { amsAutodry = d; ok = true; }
            else if (d && typeof d.detail === "string") detail = d.detail;
          } catch (x) { detail = "Ошибка сети"; }
          adDlgBusy = false;
          adCloseDlg(true);
          showToast(ok ? "Пробный режим выключен" : detail, ok);
          autodryRender();
          if (ok) { loadAms(); setTimeout(loadAms, 1500); }
        };
      }
      async function adOnRelease(e) {
        var b = e.target.closest("[data-ad-release]");
        if (!b || b.disabled || !isAdmin()) return;
        b.disabled = true;
        try {
          var r = await apiFetch("/api/admin/ams/autodry/release", { method: "POST", headers: { "Content-Type": "application/json" },
                                                                     body: JSON.stringify({ units: [b.getAttribute("data-ad-release")] }) });
          var d = null;
          try { d = await r.json(); } catch (x) { d = null; }
          if (r.ok && d) { amsAutodry = d; autodryRender(); loadAms(); setTimeout(loadAms, 1500); return; }
          showToast(d && typeof d.detail === "string" ? d.detail : "Не получилось", false);
        } catch (x) { showToast("Ошибка сети", false); }
        b.disabled = false;
      }
      function autodryInit() {
        if (adInited) return;
        adInited = true;
        var sec = adEl("autodry");
        if (sec) {
          sec.addEventListener("input", adOnFormInput);
          sec.addEventListener("change", adOnFormInput);
          sec.addEventListener("click", function (e) {
            if (e.target.closest("#adDryRunOff")) { adDryRunOffDlg(); return; }
            if (e.target.closest("#adJournalMore")) { adJournalAll = !adJournalAll; adRenderJournal(); }
          });
        }
        function on(id, fn) { var el = adEl(id); if (el) el.addEventListener("click", fn); }
        on("adSave", adSave);
        on("adReset", adReset);
        on("adDryAll", adRunNowDlg);
        on("adStopAll", adStopDlg);
        on("amsAdJump", function () {
          var s = adEl("autodry");
          if (s) s.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        var rows = adEl("adRows");
        if (rows) rows.addEventListener("click", adOnRelease);
        var back = adEl("adDlgBack");
        if (back) back.addEventListener("click", function (e) { if (e.target === back) adCloseDlg(); });
        document.addEventListener("keydown", function (e) {
          var b = adEl("adDlgBack");
          if (e.key === "Escape" && b && b.classList.contains("on")) adCloseDlg();
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
            var b = AUDIT_BADGES[e.action] || auditPrefixBadge(e.action) || [e.action, 'steel'];
            html += '<tr><td class="mono dim">' + fmtAuditTime(e.created_at) + '</td>' +
              '<td><b>' + escHtml(e.user_id || '\u2014') + '</b></td>' +
              '<td><span class="badge badge-' + b[1] + '">' + escHtml(b[0]) + '</span>' +
              (b[2] ? ' <span class="mono muted">' + escHtml(b[2]) + '</span>' : '') + '</td>' +
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
          if (!payload.access_code || !payload.serial) { err.textContent = 'Для Bambu нужны код доступа и серийный номер'; return; }
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
            if (!/^-?\d+$/.test(v)) throw new Error('ID чата: нужно целое число');
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