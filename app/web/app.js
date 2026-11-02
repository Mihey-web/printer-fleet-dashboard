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
        renderMeWho();
        // «Админ» opened by its address before /api/auth/me answered: adminLoadSub waited for
        // the role (Парк, Автоматика's codes, Система's settings) — an admin's data now, once.
        adminLoadWaited();
        // The AMS strip's drying commands and «Правила ›» are the admins': re-render with the role now known.
        if (amsAutodry) autodryRender();
        // And the journal's unfolded rows: the admins' get the auto-ignore's presses in them
        // (jobsEls: undefined until the IIFE assigns it).
        if (jobsEls && jobsStats) renderJobsTable();
        // «⇩ Выгрузить» and «Действия» of «История» are the admins' only (the server answers a
        // viewer 403 for the CSV): no button for them.
        histRoleUI();
        // «Действия» opened by its address before /api/auth/me answered: loadActs waited for the role
        if (isAdmin() && historySub === "actions" && acts && !acts.rows.length && !acts.busy) {
          var hp = document.querySelector('.view-panel[data-panel="history"]');
          if (hp && hp.classList.contains("active")) Promise.resolve().then(function () { loadActs(); });
        }
        routeCheckRole();
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
          } else {
            tryRefresh().then(function(refreshed) {
              if (!refreshed) showLogin();
            });
          }
        }).catch(function() {
          tryRefresh().then(function(refreshed) {
            if (!refreshed) showLogin();
          });
        });
      })();

      // ---- End auth ----

      const grid = document.getElementById("printerGrid");
      const sortSeg = document.getElementById("sortSeg");
      // «Порядок: Имя | Финиш» (IA §5): "name" | "eta", kept as it always was.
      const SORT_STORAGE_KEY = "forge-ops-sort";
      let currentSort = "name";
      const filtersEl = document.getElementById("filters");
      const viewTabs = Array.from(document.querySelectorAll(".view-tab"));
      const viewPanels = Array.from(document.querySelectorAll(".view-panel"));
      const lastUpdate = document.getElementById("lastUpdate");
      const settingsList = document.getElementById("settingsList");
      const notificationsEnabledToggle = document.getElementById("notificationsEnabledToggle");
      const notificationsFinishToggle = document.getElementById("notificationsFinishToggle");
      const notificationsErrorToggle = document.getElementById("notificationsErrorToggle");
      const notificationsPausedToggle = document.getElementById("notificationsPausedToggle");

      // The chips' filter (IA §5): the states shown, [] — everyone; kept in forge-ops-state-filters.
      let fleetFilter = [];
      let cachedAllPrinters = [];
      const FILTERS_STORAGE_KEY = "forge-ops-state-filters";
      // Its format mark: a choice without it was written by the old «Фильтры ▾» (read once, mapped, marked).
      const FILTERS_FORMAT_KEY = "forge-ops-state-filters-v";
      const FILTERS_FORMAT = "2";
      const NOTIFICATION_SETTINGS_KEY = "forge-ops-notification-settings";
      const THEME_STORAGE_KEY = "forge-ops-theme";
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
        // The browser's chrome follows the page background (status bar of the
        // installed app, its window title bar, Chrome Android's tab colour).
        // Read from the CSS token: a new theme needs no table here.
        var themeMeta = document.querySelector('meta[name="theme-color"]');
        var themeBg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
        if (themeMeta && themeBg && themeMeta.getAttribute("content") !== themeBg) themeMeta.setAttribute("content", themeBg);
        var btns = document.querySelectorAll(".theme-seg-btn");
        btns.forEach(function (btn) {
          var themeVal = btn.getAttribute("data-theme");
          if ((themeVal || "") === (value || "")) {
            btn.classList.add("active");
          } else {
            btn.classList.remove("active");
          }
        });
        glassPhotoSync();
        // Redraw timeline canvas if visible
        STATE_COLORS = getStateColors();
        var canvas = document.getElementById("timeline-canvas");
        if (canvas && timelineData.length > 0) {
          drawTimeline();
        }
      }
      function initTheme() {
        // «Ошибки в карточках» is gone (the card's notifications replace it): drop its stored value once.
        try { localStorage.removeItem("forge-ops-show-errors"); } catch (e) {}
        var stored = null;
        try { stored = localStorage.getItem(THEME_STORAGE_KEY); } catch (e) {}
        // «Неон» is gone: whoever kept it moves to «Янтарь», silently (theme.js does the same first)
        if (stored === "neon") {
          stored = "amber";
          try { localStorage.setItem(THEME_STORAGE_KEY, stored); } catch (e) {}
        }
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
      // «Стекло» is light or dark as the system is: when the system switches, the canvas
      // colours (the timeline, the charts) are read again
      try {
        var schemeMq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
        if (schemeMq && schemeMq.addEventListener) schemeMq.addEventListener("change", function () {
          if (document.documentElement.getAttribute("data-theme") === "glass") applyTheme("glass");
        });
      } catch (e) {}
      function setTheme(value) {
        applyTheme(value);
        try { localStorage.setItem(THEME_STORAGE_KEY, value || ""); } catch (e) {}
      }
      // «Своё фото» under «Стекло»: made smaller here (the long side 1920 px, a JPEG, about
      // 0.3–0.6 MB), kept in this browser only (forge-ops-glass-photo); theme.js shows it.
      // The row is there in «Стекло» only: the other themes are opaque, a photo would not show.
      const GLASS_PHOTO_KEY = "forge-ops-glass-photo";
      function glassPhotoSync() {
        var row = document.getElementById("glassPhotoRow");
        if (!row) return;
        row.hidden = document.documentElement.getAttribute("data-theme") !== "glass";
        var has = false;
        try { has = !!localStorage.getItem(GLASS_PHOTO_KEY); } catch (e) {}
        document.getElementById("glassPhotoClear").hidden = !has;
      }
      function glassPhotoEncode(img, side, quality) {
        var k = Math.min(1, side / Math.max(img.naturalWidth, img.naturalHeight));
        var c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(img.naturalWidth * k));
        c.height = Math.max(1, Math.round(img.naturalHeight * k));
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        return c.toDataURL("image/jpeg", quality);
      }
      function glassPhotoPick(file) {
        if (!file) return;
        var url = URL.createObjectURL(file), img = new Image();
        img.onload = function () {
          URL.revokeObjectURL(url);
          // the browser keeps about 5 MB for the whole panel: a smaller copy if the first does not fit
          var saved = [[1920, 0.82], [1280, 0.72]].some(function (s) {
            try { localStorage.setItem(GLASS_PHOTO_KEY, glassPhotoEncode(img, s[0], s[1])); return true; } catch (e) { return false; }
          });
          if (!saved) { showToast("Фото не поместилось в память браузера", false); return; }
          if (typeof applyGlassPhoto === "function") applyGlassPhoto();
          glassPhotoSync();
        };
        img.onerror = function () {
          URL.revokeObjectURL(url);
          showToast("Браузер не открыл эту картинку — попробуйте JPG или PNG", false);
        };
        img.src = url;
      }
      function glassPhotoClear() {
        try { localStorage.removeItem(GLASS_PHOTO_KEY); } catch (e) {}
        if (typeof applyGlassPhoto === "function") applyGlassPhoto();
        glassPhotoSync();
      }
      const prevPrinterStates = {};
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
      // pid: the printer — a click on the push opens it (IA §4, plan step 15): «Принтеры», its card in
      // view and flashed; notes: with its notifications' popover (an error or a pause with a reason).
      function showBrowserNotification(title, body, pid, notes) {
        if (!("Notification" in window) || Notification.permission !== "granted") return;
        try {
          var n = new Notification(title, {
            body: body,
            icon: "/static/img/bambu-x1c.png"
          });
          if (pid != null) n.onclick = function () {
            try { window.focus(); } catch (e) {}
            n.close();
            pushOpen(pid, notes);
          };
        } catch (error) {}
      }
      // Why the browser's notifications cannot be switched on: said in «Этот браузер» under
      // the switch (IA §10), a toast only when the panel is closed.
      function nfPermReply(text) {
        var sec = document.getElementById("settingsList");
        replyIn(sec, { k: "bad", t: text });
      }
      async function updateNotificationSetting(key, value) {
        var settings = loadNotificationSettings();
        if (key === "enabled") {
          if (value) {
            if (!("Notification" in window)) {
              settings.enabled = false;
              saveNotificationSettings(settings);
              syncNotificationControls();
              nfPermReply("Браузер не поддерживает уведомления");
              return;
            }
            if (Notification.permission === "default") {
              try {
                const permission = await Notification.requestPermission();
                if (permission !== "granted") {
                  settings.enabled = false;
                  saveNotificationSettings(settings);
      syncNotificationControls();
                  nfPermReply(permission === "denied" ? "Доступ к уведомлениям запрещён" : "Разрешение не выдано");
                  return;
                }
              } catch (error) {
                settings.enabled = false;
                saveNotificationSettings(settings);
                syncNotificationControls();
                nfPermReply("Не удалось запросить разрешение");
                return;
              }
            } else if (Notification.permission === "denied") {
              settings.enabled = false;
              saveNotificationSettings(settings);
              syncNotificationControls();
              var httpsUrl = "https://" + window.location.hostname;
              nfPermReply("Уведомления не работают через HTTP. Откройте " + httpsUrl);
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
        // a job stopped by the owner: grey, not the red «ОШИБКА»
        if (p.state === "cancelled") return "cancelled";
        return "unknown";
      }
      // The card's badge: the word of the one dictionary (IA §10), in capitals.
      function statusLabel(status) {
        const labels = {
          printing: "ПЕЧАТАЕТ",
          idle: "ПРОСТОЙ",
          error: "ОШИБКА",
          paused: "ПАУЗА",
          finished: "ЗАВЕРШЕНО",
          cancelled: "ОТМЕНЕНО",
          unknown: "НЕТ ДАННЫХ",
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
      // forge-ops-state-filters: the chips chosen (IA §5 «выбор запоминается»). An earlier
      // version is taken without «Онлайн» (fleetFiltersFrom, IA plan step 6); one of the old
      // «Фильтры ▾» (no format mark) also has its «Простаивают» spread over today's chips —
      // read once, mapped, written back with the mark; today's is written back only when
      // something was dropped.
      function loadPersistedFilters() {
        try {
          const raw = localStorage.getItem(FILTERS_STORAGE_KEY);
          if (!raw) return;
          const old = localStorage.getItem(FILTERS_FORMAT_KEY) !== FILTERS_FORMAT;
          let parsed = null;
          try { parsed = JSON.parse(raw); } catch (e) {}
          fleetFilter = fleetFiltersFrom(parsed, old);
          if (old || JSON.stringify(fleetFilter) !== raw) persistFilters();
        } catch (error) {
          console.warn("Failed to restore filters:", error);
        }
      }
      function persistFilters() {
        try {
          localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(fleetFilter));
          localStorage.setItem(FILTERS_FORMAT_KEY, FILTERS_FORMAT);
        } catch (error) {
          console.warn("Failed to persist filters:", error);
        }
      }
      // The chips' states only (fleetBucket: maintenance mode first, any state; then offline); [] — everyone.
      function applyFilters(allPrinters) {
        if (!fleetFilter.length) return allPrinters;
        return allPrinters.filter((p) => fleetFilter.indexOf(fleetBucket(p)) >= 0);
      }
      // IA §5, §10: by name (natural, P2 before P10); a printer that wants a decision
      // (nfUrgency: a fatal / a pause on an error that holds it, never in maintenance
      // mode) always first — no «Проблемные сверху» switch any more. «Финиш»: the
      // printing ones with an ETA by the time left, the rest by name after them.
      function sortPrinters(printers) {
        const byEta = currentSort === "eta";
        function eta(p) {
          return byEta && !inMaint(p) && p.state === "printing" && !isOffline(p) && p.eta_seconds > 0 ? p.eta_seconds : Infinity;
        }
        return printers.slice().sort((a, b) => {
          const na = nfUrgency(a);
          const nb = nfUrgency(b);
          if (na !== nb) return nb - na;
          const ea = eta(a);
          const eb = eta(b);
          if (ea !== eb) return ea - eb;
          return String(a.label || "").localeCompare(String(b.label || ""), "ru", { numeric: true });
        });
      }
      // The chips with their counts over every printer (the filter does not change them).
      var filtersMarkup = null;
      function renderFilters() {
        if (!filtersEl) return;
        var html = fleetChipsHtml(fleetCounts(cachedAllPrinters), fleetFilter);
        if (html !== filtersMarkup) { filtersEl.innerHTML = html; filtersMarkup = html; }
      }

      /* route:begin */
      // Screen addresses #/<screen>[/<sub>][?key=value&…] (IA §3): a reload keeps
      // the screen, «Назад» returns to the previous one, a link can point at one.
      // Pure: no DOM, run in node by tests/test_web_route_ui.py. Its vars are
      // read only once the IIFE has finished (the router starts in a microtask).
      // view: the panel (data-view / data-panel) the screen shows today; subs:
      // address → the panel's own sub-view name; keys: the query keys the screen
      // keeps (printers: finish=open, the «Финиши» window unfolded, and bell=open, the
      // bell's list opened once — the PWA shortcut «Нужно решение», plan step 15; history: the
      // period, «Свой…» and the printer of its one head, IA §7); admin: only for
      // admins; adminSubs: the subs only admins get («История → Действия»).
      var ROUTE_DEFAULT = "printers";
      var ROUTE_SCREENS = {
        printers: { view: "fleet", subs: {}, keys: ["finish", "bell"] },
        ams: { view: "ams", subs: {}, keys: [] },
        history: { view: "history", subs: { jobs: "jobs", chrono: "chrono", stats: "stats", actions: "actions" },
                   keys: ["period", "from", "to", "printer"], adminSubs: ["actions"] },
        admin: { view: "admin", subs: { park: "park", auto: "auto", access: "access", system: "system" }, keys: [], admin: true }
      };
      // Old names an address may carry: the tabs' earlier data-view values and sub names.
      // «Настройки» are gone (IA plan step 3): their old address opens «Принтеры», the
      // browser's own settings are in 👤 now, the server's in «Админ → Система».
      // «Окно» is the «Финиши» window of «Принтеры» now (plan step 7): its old address
      // opens it there, unfolded (ROUTE_ALIAS_QUERY: the query an old name stands for).
      // «Сводка» is gone (plan step 8): its numbers are «История → Статистика» now
      // (ROUTE_ALIAS_SUB: the sub an old name stands for).
      var ROUTE_ALIASES = { fleet: "printers", overview: "printers", cameras: "printers", settings: "printers",
                            forecast: "printers", dashboard: "history" };
      var ROUTE_ALIAS_QUERY = { forecast: { finish: "open" } };
      var ROUTE_ALIAS_SUB = { dashboard: "stats" };
      var ROUTE_SUB_ALIASES = { history: { timeline: "chrono" } };
      // Every value forge-ops-active-view ever held (05.2026 on) → the address it
      // opens once; the tabs that go away start on «Принтеры» (IA plan step 2),
      // «Окно» with the «Финиши» window unfolded (plan step 7).
      var ROUTE_FROM_SAVED_VIEW = {
        fleet: "printers", overview: "printers", cameras: "printers", dashboard: "printers",
        forecast: "printers?finish=open", settings: "printers", history: "history", ams: "ams", admin: "admin"
      };
      function routeOwn(o, k) { return !!o && typeof k === "string" && Object.prototype.hasOwnProperty.call(o, k); }
      function routeDecode(s) {
        try { return decodeURIComponent(String(s).replace(/\+/g, " ")); } catch (e) { return null; }
      }
      // A route as the app keeps it: a known screen, a known sub of it or "",
      // only the keys the screen keeps, as non-empty strings.
      function routeNormalize(r) {
        r = r || {};
        var screen = routeOwn(ROUTE_SCREENS, r.screen) ? r.screen : ROUTE_DEFAULT, conf = ROUTE_SCREENS[screen];
        var sub = screen === r.screen && routeOwn(conf.subs, r.sub) ? r.sub : "";
        var query = {};
        conf.keys.forEach(function (k) {
          var v = r.query && routeOwn(r.query, k) ? r.query[k] : null;
          if (typeof v === "string" && v !== "") query[k] = v;
        });
        return { screen: screen, sub: sub, query: query };
      }
      // "#/history/jobs?printer=p1" → { screen: "history", sub: "jobs", query: {…} }:
      // the path case-insensitive, old names mapped, anything unknown falls back.
      function routeParse(hash) {
        var s = String(hash == null ? "" : hash).replace(/^#/, ""), q = s.indexOf("?");
        var path = (q < 0 ? s : s.slice(0, q)).split("/").filter(Boolean).map(function (x) {
          return (routeDecode(x) || "").toLowerCase();
        });
        var screen = path[0] || "", sub = path[1] || "", query = {};
        var implied = routeOwn(ROUTE_ALIAS_QUERY, screen) ? ROUTE_ALIAS_QUERY[screen] : null;
        if (routeOwn(ROUTE_ALIAS_SUB, screen)) sub = ROUTE_ALIAS_SUB[screen];
        if (routeOwn(ROUTE_ALIASES, screen)) screen = ROUTE_ALIASES[screen];
        if (routeOwn(ROUTE_SUB_ALIASES, screen) && routeOwn(ROUTE_SUB_ALIASES[screen], sub)) sub = ROUTE_SUB_ALIASES[screen][sub];
        (q < 0 ? "" : s.slice(q + 1)).split("&").forEach(function (kv) {
          var i = kv.indexOf("="), k = routeDecode(i < 0 ? kv : kv.slice(0, i)), v = i < 0 ? "" : routeDecode(kv.slice(i + 1));
          if (k && v !== null && !routeOwn(query, k)) query[k] = v;
        });
        if (implied) Object.keys(implied).forEach(function (k) { if (!routeOwn(query, k)) query[k] = implied[k]; });
        return routeNormalize({ screen: screen, sub: sub, query: query });
      }
      function routeBuild(r) {
        r = routeNormalize(r);
        var qs = ROUTE_SCREENS[r.screen].keys.filter(function (k) { return routeOwn(r.query, k); }).map(function (k) {
          return encodeURIComponent(k) + "=" + encodeURIComponent(r.query[k]);
        });
        return "#/" + r.screen + (r.sub ? "/" + r.sub : "") + (qs.length ? "?" + qs.join("&") : "");
      }
      // The route of a panel (data-view) showing its sub-view (the panel's own name).
      function routeOfView(view, sub) {
        var screen = ROUTE_DEFAULT, addr = "";
        Object.keys(ROUTE_SCREENS).some(function (k) { return ROUTE_SCREENS[k].view === view && !!(screen = k); });
        var subs = ROUTE_SCREENS[screen].subs;
        Object.keys(subs).some(function (k) { return subs[k] === sub && !!(addr = k); });
        return routeNormalize({ screen: screen, sub: addr, query: {} });
      }
      // The panel and its sub-view a route shows: { view, sub } (sub "": the panel's last one).
      function routeView(r) {
        r = routeNormalize(r);
        var conf = ROUTE_SCREENS[r.screen];
        return { view: conf.view, sub: r.sub ? conf.subs[r.sub] : "" };
      }
      // A screen, or a sub of one, only admins get.
      function routeAdminOnly(r) {
        r = routeNormalize(r);
        var conf = ROUTE_SCREENS[r.screen];
        return !!conf.admin || (!!r.sub && (conf.adminSubs || []).indexOf(r.sub) >= 0);
      }
      // admin: true / false, or null while the role is not known yet: the screen
      // stays (an admin's reload must not flash «Принтеры»), it is checked again then.
      // A viewer on an admins' sub keeps the screen, on the panel's last sub.
      function routeForRole(r, admin) {
        r = routeNormalize(r);
        if (admin !== false || !routeAdminOnly(r)) return r;
        if (ROUTE_SCREENS[r.screen].admin) return routeNormalize({ screen: ROUTE_DEFAULT });
        return routeNormalize({ screen: r.screen, sub: "", query: r.query });
      }
      // The first screen: the address in the URL; without one, the tab
      // forge-ops-active-view remembered (read once, then removed); else «Принтеры».
      function routeFirst(hash, saved) {
        if (String(hash == null ? "" : hash).replace(/^[#/]+/, "") !== "") return routeParse(hash);
        return routeParse(routeOwn(ROUTE_FROM_SAVED_VIEW, saved) ? ROUTE_FROM_SAVED_VIEW[saved] : ROUTE_DEFAULT);
      }
      /* route:end */

      // The router's DOM side. The address follows what is on screen: a move
      // adds a history entry («Назад» returns), a correction (an old or unknown
      // address, a missing sub, a screen the role may not see) rewrites it in
      // place. pushState / replaceState fire no hashchange: only the user's own
      // moves do (Back / Forward, a typed or followed address).
      function routeActiveView() {
        var p = viewPanels.filter(function (x) { return x.classList.contains("active"); })[0];
        return p ? p.dataset.panel : null;
      }
      function routeNow() {
        var view = routeActiveView();
        var r = routeOfView(view, view === "history" ? historySub : view === "admin" ? adminSub : "");
        if (view === "fleet" && finOpen) r.query.finish = "open";     // the «Финиши» window unfolded
        if (view === "history") r.query = histQueryOf(histFilter);    // the period and the printer
        return r;
      }
      function routeSync(push) {
        var want = routeBuild(routeNow());
        if (location.hash === want) return;
        // a move right after a phone sheet closed takes the sheet's «Назад» entry over (sheetClosed)
        if (push && sheetNav && sheetNav.dead) { sheetNav.dead = false; push = false; }
        try {
          window.history[push ? "pushState" : "replaceState"](null, "", want);
        } catch (e) {
          location.replace(want);
        }
      }
      function routeRole() { return currentUser ? currentUser.role === "admin" : null; }
      // Show a route (never writes the address); force: reopen the panel already on screen too.
      function routeApply(r, force) {
        var shown = routeForRole(r, routeRole()), to = routeView(shown), cur = routeActiveView();
        if (to.view === "history") {
          // the sub-tab and the head's period / printer, then the panel (its load reads them)
          var sub = to.sub || historySub, moved = false;
          if (routeRole() === false && routeAdminOnly({ screen: "history", sub: sub })) sub = HIST_SUBS[0];
          if (sub !== historySub) { setHistorySub(sub); moved = true; }
          if (histApplyQuery(shown.query)) moved = true;
          if (cur !== "history" || force) setActiveView("history", true);
          else if (moved) histLoadSub();
        } else if (to.view === "admin") {
          // the same for «Админ»: its sub-tab lives in this session only (the address keeps it)
          var asub = to.sub || adminSub;
          if (asub !== adminSub) {
            setAdminSub(asub);
            if (cur === "admin" && !force) adminLoadSub();
          }
          if (cur !== "admin" || force) setActiveView("admin", true);
        } else if (to.view !== cur || force) {
          setActiveView(to.view, true);
        }
        if (to.view === "fleet") {
          // A phone has no window: «Финиши» (the PWA shortcut, plan step 15) opens the list sheet
          // once and the address drops the key. The sheet waits for the address to be written
          // (a microtask), so its «Назад» entry is the next one.
          var finWant = shown.query.finish === "open";
          finSetOpen(finWant && !finPhone(), true);
          if (finWant && finPhone()) Promise.resolve().then(function () { finSheetSet(true); });
          // «Нужно решение» (the PWA shortcut): the bell's list, once; the address keeps nothing
          if (shown.query.bell === "open") Promise.resolve().then(function () { nfOpen("farm", null, null); });
        }
      }
      function routeOnHash() {
        routeApply(routeParse(location.hash));
        routeSync(false);
      }
      // Runs once, in a microtask after the IIFE: setActiveView and setHistorySub
      // read vars assigned further down it (appjs-iife-var-hoisting-trap). The tab
      // forge-ops-active-view kept is read once and removed: the address keeps the
      // screen now, and a reload (also the automatic one after a deploy) keeps the address.
      function routeStart() {
        var saved = null;
        try {
          saved = localStorage.getItem("forge-ops-active-view");
          localStorage.removeItem("forge-ops-active-view");
        } catch (e) {}
        routeApply(routeFirst(location.hash, saved), true);
        routeSync(false);
        window.addEventListener("hashchange", routeOnHash);
      }
      // The role arrived (or changed): a viewer on an admin's screen goes to «Принтеры», on an
      // admins' sub to the screen's last sub, in place.
      function routeCheckRole() {
        if (routeRole() !== false || !routeAdminOnly(routeNow())) return;
        routeApply(routeNow());
        routeSync(false);
      }

      // --- A phone (IA §4, plan step 10): the bottom navigation, the bar's screen name, the
      // sheets and «Назад». A sheet on screen owns one history entry of the same address, so
      // Android's / the browser's «Назад» closes the sheet instead of leaving the screen. A
      // sheet closed any other way gives its entry back (history.back), or — closed by a move
      // to another screen in the same click — the move takes the entry over (routeSync). A
      // desktop gets no entries: its popovers stay popovers.
      var navMq = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;     // the bottom navigation
      var sheetMq = window.matchMedia ? window.matchMedia("(max-width: 899px)") : null;   // the sheets (as finMq)
      // open: the closer of the sheet on screen; entry: its history entry is the current one;
      // dead: it closed, its entry is given back in a microtask unless a move takes it over;
      // skip: the popstate events our own history.back() causes.
      var sheetNav = { open: null, entry: false, dead: false, skip: 0 };
      function navPhone() { return !!(navMq && navMq.matches); }
      function sheetPhone() { return !!(sheetMq && sheetMq.matches); }
      // close: the sheet's own closer (a stable function), called by «Назад».
      function sheetOpened(close) {
        sheetNav.open = close;
        if (sheetNav.entry || !sheetPhone()) return;
        if (sheetNav.dead) { sheetNav.dead = false; sheetNav.entry = true; return; }   // one sheet replaced another
        try {
          window.history.pushState({ forgeSheet: 1 }, "", location.href);
          sheetNav.entry = true;
        } catch (e) {}
      }
      function sheetClosed(close) {
        if (sheetNav.open !== close) return;
        sheetNav.open = null;
        if (!sheetNav.entry) return;
        sheetNav.entry = false;
        sheetNav.dead = true;
        Promise.resolve().then(function () {
          if (!sheetNav.dead) return;                   // a move or the next sheet took it over
          sheetNav.dead = false;
          var st = window.history.state;
          if (st && st.forgeSheet) { sheetNav.skip++; window.history.back(); }
        });
      }
      function sheetCloseAll() {
        var close = sheetNav.open;
        if (close) close();
      }
      window.addEventListener("popstate", function () {
        if (sheetNav.skip) { sheetNav.skip--; return; }
        if (!sheetNav.entry) return;
        sheetNav.entry = false;                         // «Назад» took the entry: the closer must not go back again
        sheetCloseAll();
      });
      // The bar's screen name: «Принтеры», «История · Печати», «Админ · Парк».
      function renderScrName() {
        var el = document.getElementById("topbarScr");
        if (!el) return;
        var view = routeActiveView();
        var names = { fleet: "Принтеры", ams: "AMS", history: "История", admin: "Админ" };
        var sel = view === "history" ? '#historySub button[aria-pressed="true"]' : view === "admin" ? '#adminSub button[aria-pressed="true"]' : null;
        var b = sel ? document.querySelector(sel) : null;
        el.textContent = names[view] || "";
        if (b) {
          var sub = document.createElement("span");
          sub.className = "ts-sub";
          sub.textContent = " · " + b.textContent.trim();
          el.appendChild(sub);
        }
      }
      // A second tap on the screen's own item of the bottom navigation: back to the top, what is
      // unfolded folds (IA §4) — «Не в работе» (as its «Скрыть»), the history filters; a sheet closes.
      function phoneRetap(view) {
        sheetCloseAll();
        if (view === "fleet" && fleetRestOpen) {
          fleetRestOpen = false;
          fleetFilter = fleetFilter.filter(function (k) { return k !== "offline" && k !== "maintenance"; });
          persistFilters();
          applyAndRender();
        }
        if (view === "ams") amsRowSet(null);
        if (view === "history") {
          var hh = document.getElementById("histHead"), hs = document.getElementById("histSum");
          if (hh) hh.classList.remove("hist-open");
          if (hs) hs.setAttribute("aria-expanded", "false");
        }
        window.scrollTo({ top: 0, behavior: "smooth" });
      }

      // fromAddress: the router shows what the address says, so the address is not rewritten.
      function setActiveView(view, fromAddress) {
        viewTabs.forEach((tab) => {
          tab.classList.toggle("active", tab.dataset.view === view);
        });
        viewPanels.forEach((panel) => {
          panel.classList.toggle("active", panel.dataset.panel === view);
        });
        // the address keeps the screen now (#/…), forge-ops-active-view is no longer written
        if (!fromAddress) routeSync(true);
        // Microtask: the history sub-view state (historySub, the head's filter) is
        // assigned further down the IIFE than the tab restore that calls this.
        if (view === 'history') { Promise.resolve().then(function () { histLoadSub(); }); }
        if (view === 'ams') { Promise.resolve().then(function () { loadAms(); }); }
        // «Финиши»: the window unfolded is drawn as the panel comes back (microtask: forecastHours)
        if (view === 'fleet') { Promise.resolve().then(function () { if (cachedAllPrinters.length) renderFinish(Date.now() / 1000); }); }
        // Загрузка и при клике, и при восстановлении вкладки после перезагрузки
        // страницы (раньше данные грузились только по клику на таб). Микротаска:
        // при восстановлении setActiveView выполняется до того, как IIFE присвоит
        // var-переменные админ-блока (AUDIT_LIMIT и др.) — прямой вызов ловил
        // limit=undefined и 422 от аудита.
        if (view === 'admin') {
          Promise.resolve().then(function () { adminLoadSub(); });
        }
        renderScrName();
      }

      // «Админ» (IA §8): Парк · Автоматика · Доступ · Система. The sub-tab on screen lives in
      // the address (#/admin/<sub>) and in this session, a fresh tab opens «Парк».
      var adminSub = "park";
      function setAdminSub(sub) {
        adminSub = routeOwn(ROUTE_SCREENS.admin.subs, sub) ? sub : "park";
        document.querySelectorAll("#adminSub button[data-asub]").forEach(function (b) {
          var on = b.dataset.asub === adminSub;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        document.querySelectorAll("[data-asub-panel]").forEach(function (p) {
          p.hidden = p.dataset.asubPanel !== adminSub;
        });
        renderScrName();
      }
      // A sub-tab's data, each time it opens. The printers always: their
      // «неприменённые изменения» bar shows on every sub-tab. Автоматика: the
      // auto-dry rules and blocks; Доступ: the users (the audit is «История →
      // Действия» since plan step 12); Система: Telegram and its proxies.
      // Admins only: a viewer's /api/admin/* gets 403. The role not known yet (#/admin/… opened by
      // its address before /api/auth/me answered): nothing now, adminLoadWaited loads it then.
      var adminLoadWaits = false;
      function adminLoadSub() {
        if (!isAdmin()) { if (!currentUser) adminLoadWaits = true; return; }
        loadAdminPrinters();
        if (adminSub === "auto") { loadAutodry(); loadAutoIgnore(); }
        if (adminSub === "access") loadAdminUsers();
        if (adminSub === "system") loadServerSettings();
      }
      // The role has arrived (updateAuthUI): an admin gets the sub-tab's data adminLoadSub waited
      // with — once, in a microtask (the admin block's vars are assigned further down the IIFE).
      function adminLoadWaited() {
        if (!adminLoadWaits || !currentUser) return;
        adminLoadWaits = false;
        var panel = document.querySelector('.view-panel[data-panel="admin"]');
        if (isAdmin() && panel && panel.classList.contains('active')) {
          Promise.resolve().then(function () { adminLoadSub(); });
        }
      }
      (function initAdminSub() {
        var bar = document.getElementById("adminSub");
        if (!bar) return;
        bar.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-asub]");
          if (!b || b.dataset.asub === adminSub) return;
          setAdminSub(b.dataset.asub);
          routeSync(true);
          adminLoadSub();
        });
      })();
      // forge-ops-sort stays as it was ("name" | "eta", the owner keeps «по времени»);
      // forge-ops-alerts-top is gone with its switch (IA plan step 6): removed once here.
      function loadSortPrefs() {
        try {
          const savedSort = localStorage.getItem(SORT_STORAGE_KEY);
          if (savedSort === "name" || savedSort === "eta") {
            currentSort = savedSort;
          }
          localStorage.removeItem("forge-ops-alerts-top");
        } catch (e) {}
        renderSortSeg();
      }
      function renderSortSeg() {
        if (!sortSeg) return;
        sortSeg.querySelectorAll("button[data-sort]").forEach(function (b) {
          var on = b.dataset.sort === currentSort;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
      }
      if (sortSeg) {
        sortSeg.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-sort]");
          if (!b || b.dataset.sort === currentSort) return;
          currentSort = b.dataset.sort === "eta" ? "eta" : "name";
          try { localStorage.setItem(SORT_STORAGE_KEY, currentSort); } catch (e2) {}
          renderSortSeg();
          applyAndRender();
        });
      }
      // A chip (IA §5): only this state, the same chip again — everyone, Ctrl / ⌘ — add or
      // take away. «Офлайн» or «Обслуживание» unfolds «Не в работе», where such cards may be.
      if (filtersEl) {
        filtersEl.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-filter]");
          if (!b) return;
          var id = b.dataset.filter;
          fleetFilter = fleetFilterClick(fleetFilter, id, e.ctrlKey || e.metaKey);
          if (fleetFilter.indexOf(id) >= 0 && (id === "offline" || id === "maintenance")) fleetRestOpen = true;
          persistFilters();
          applyAndRender();
          var again = filtersEl.querySelector('button[data-filter="' + id + '"]');
          if (again) again.focus({ preventScroll: true });
        });
      }
      // «Наверх»: the round button once the page is scrolled down
      function initScrollTop() {
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
        // Not settable here (a viewer, Creality / Klipper / MKS, a chamber without a
        // heater): the chip still opens the temperature chart, read-only.
        var edit = editable ? ' pc-editable" data-edit="' + kind + '" data-cur="' + (hasTarget ? Math.round(target) : val)
          : ' pc-chartable" data-chart="' + kind;
        return '<span class="' + cls + edit + '" data-tip="' + tip + '">' + icon(icName) + val + '°' + arrow + '</span>';
      }

      // The fans a card's fan popover sets (data-fans): Bambu takes M106 P1/P2/P3
      // on every model; elsewhere (Creality) only the ones the printer reports —
      // its M106 macro turns an index it has no fan for into the part fan.
      function fanKeys(p) {
        if (p.kind === "bambu") return ["part", "aux", "chamber"];
        var f = p.fans || {};
        return [p.fan_speed_pct != null ? "part" : null, f.aux != null ? "aux" : null,
                f.chamber != null ? "chamber" : null].filter(Boolean);
      }
      function fanChip(p, editable) {
        var part = p.fan_speed_pct != null ? p.fan_speed_pct : null;
        var f = p.fans || {};
        var vals = [part, f.aux, f.chamber, f.heatbreak].filter(function (v) { return v != null; });
        var keys = fanKeys(p);
        if (!keys.length) editable = false;
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
            + '" data-fans="' + keys.join(",")
          : '';
        return '<span class="pc-chip' + edit + '" data-tip="' + (tipParts.join(" · ") || "Вентилятор") + '" style="color:hsl(calc(var(--fan-h, 120) + ' + main + ' * var(--fan-k, 0.8)),70%,var(--fan-l))">' + icon("wind") + main + '%</span>';
      }
      // Print speed, the feed rate % the printer reports (Bambu's speed modifier,
      // Creality's curFeedratePct): ↓ below 100, ⚡ above, 🚀 above 124. Its popover
      // (speedPopHtml): on a Creality only while a print runs (the server refuses
      // speed otherwise: printer_commands._state_check); a Bambu's in any state.
      function speedChip(p, editable) {
        var pct = p.feedrate_pct;
        if (pct == null || pct <= 0) return "";
        var mode = pct < 100 ? "slow" : pct > 124 ? "turbo" : pct > 100 ? "fast" : "norm";
        var mark = mode === "slow" ? ' ↓' : mode === "turbo" ? ' 🚀' : mode === "fast" ? ' ⚡' : '';
        var edit = editable && (p.kind !== "creality" || !ctlStateBlock("speed", p))
          ? ' pc-editable" data-edit="speed" data-cur="' + pct : '';
        return '<span class="pc-chip pc-speed-' + mode + edit + '" data-tip="Скорость печати">' + icon("gauge") + pct + '%' + mark + '</span>';
      }
      // editable: an admin's chip (the AMS popover with its commands); viewable: anyone
      // else's, which opens the read-only one (amsViewPopHtml) — IA §5, the chip is the
      // one way into the AMS popover.
      function amsChip(p, editable, viewable) {
        var a = p.ams;
        if (!a || !a.units || !a.units.length) return "";
        var trayNow = a.tray_now;
        var parts = [];
        var globalIdx = 0;
        var drying = false;
        // Two or more units (AMS 2 Pro + AMS HT): each unit's temperature goes into its humidity's
        // tooltip — slots, humidity and two temperatures did not fit the card's row
        var multi = a.units.length > 1;
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
            var htip = "AMS: влажность " + (pct != null ? pct + "%" : "уровень " + unit.humidity + "/5")
              + (multi && unit.temp != null ? " · " + Math.round(unit.temp) + "°" : "");
            parts.push('<span class="pc-ams-hum pc-ams-hum-' + hcls + '" data-tip="' + htip + '">' + htxt + '</span>');
          }
          var hasHum = unit.humidity_pct != null || unit.humidity != null;
          // While drying, the time left takes the AMS temperature's place (that
          // goes into the tooltip): the chip shares a nowrap row and must not grow.
          var remaining = unit.dry ? unit.dry.remaining_min : unit.dry_time;
          if (unitDrying && remaining > 0) {
            var dtip = dryStatusText(unit) + (unit.temp != null ? " · в AMS " + Math.round(unit.temp) + "°" : "");
            parts.push('<span class="pc-ams-temp" data-tip="' + dtip + '">' + icon("hourglass") + amsFmtMin(remaining) + '</span>');
          } else if (unit.temp != null && (!multi || !hasHum)) {
            var ttip = unitDrying ? dryStatusText(unit) : "Температура AMS";
            parts.push('<span class="pc-ams-temp" data-tip="' + ttip + '">' + icon("steam") + Math.round(unit.temp) + '°</span>');
          }
        }
        var edit = editable ? ' pc-editable" data-edit="ams' : viewable ? ' pc-chartable" data-chart="ams' : '';
        return '<span class="pc-chip pc-ams' + (drying ? " pc-ams-drying" : "") + edit + '"><span class="pc-ams-lbl" data-tip="AMS">' + icon("spool") + '</span>' + parts.join("") + '</span>';
      }
      /* ams-ui:begin */
      // AMS load / unload, drying, filament-change progress and card indicators:
      // pure helpers only (no DOM, no globals besides safeText, icon, amsUnitName
      // and amsFmtMin), run in node by tests/test_web_ams_ui.py. Every device
      // string goes through safeText, colours only as 3-8 hex digits. The texts of
      // the in-place questions are plain text; amsAskHtml escapes them.
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
      // Unload of the external spool from its chip's popover (printers without an AMS).
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
                html += '<button type="button" class="pc-pop-preset on" aria-disabled="true" data-tip="Загружен сейчас">' + amsSlotDot(slot) + num + ' · ' + type + '</button>';
                return;
              }
              var rng = slot.nozzle_temp ? " · " + Number(slot.nozzle_temp[0]) + "–" + Number(slot.nozzle_temp[1]) + " °C" : "";
              html += '<button type="button" class="pc-pop-preset" data-ams-load="' + Number(unit.index) + ':' + i + '" data-tip="Загрузить: ' + type + rng + '"'
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
      // The AMS popover without commands (IA §5: a viewer, or an admin whose card takes no
      // AMS command — offline, in its grace period, no AMS control): per unit its humidity and
      // temperature, the slots (the loaded one marked) and the drying status; no buttons.
      function amsViewPopHtml(p) {
        var a = p.ams || {}, html = "", base = 0;
        amsUnits(p).forEach(function (unit) {
          var dry = unit.dry, slots = unit.slots || [];
          html += '<div class="pc-pop-h">' + amsUnitName(unit.index) + (dry && dry.type === "n3f" ? " · AMS 2 Pro" : "")
            + (unit.humidity_pct != null ? " · 💧" + Number(unit.humidity_pct) + "%" : "")
            + (unit.temp != null && isFinite(unit.temp) ? " · 🌡" + Math.round(Number(unit.temp)) + "°" : "") + '</div>'
            + '<div class="pc-pop-presets">';
          slots.forEach(function (slot, i) {
            var on = !slot.empty && a.tray_now === base + i && p.filament_loaded === true;
            html += '<span class="pc-pop-slot' + (slot.empty ? " empty" : "") + (on ? " on" : "") + '">' + amsSlotDot(slot) + (base + i + 1)
              + ' · ' + (slot.empty ? "пусто" : safeText(slot.type, "?")) + '</span>';
          });
          html += '</div>';
          base += slots.length;
          if (dry || dryActive(unit)) html += '<div class="pc-pop-note">' + dryStatusText(unit) + '</div>';
        });
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
      // The popover's in-place question (IA §10: no native dialog — one object, asked where it is):
      // the plain text of what will happen, the button that does it (its label and data
      // attribute; danger: red) and «Отмена» back to the AMS screen (data-ams-back).
      function amsAskHtml(text, yesLabel, yesAttr, danger) {
        return '<div class="pc-pop-ask" role="group" aria-label="Подтверждение"><div class="pc-pop-note ask">'
          + safeText(text, "") + '</div><div class="pc-pop-row"><button type="button" class="pc-pop-preset" data-ams-back>Отмена</button>'
          + '<button type="button" class="pc-pop-ok' + (danger ? " danger" : "") + '" ' + yesAttr + '>' + safeText(yesLabel, "") + '</button></div></div>';
      }
      // The AMS tab card's one drying line (IA §6; u: an /api/ams/current unit; can:
      // this viewer may send commands to the unit's printer; auto: the auto-dry's word
      // for the unit, {card, cls, status} of autodryLine — none for a unit it does not
      // see). Drying: who dries («авто · прогон 2», «вручную», «чужая сушка — не
      // трогаем»), t° and the material, «ещё 9:30» and Stop; else what the unit waits
      // for, the reasons a drying cannot start and Dry… (the drying form popover).
      // Without auto: the printer's own status, only while it dries or when a start
      // is possible or blocked (no line otherwise). ask: Stop was pressed — the line asks
      // «Остановить сушку?» with Stop and «Отмена» in its place (IA §10, in place).
      function amsTabDryHtml(u, p, can, auto, ask) {
        var attrs = ' data-pid="' + safeText(u.printer_id, "") + '" data-ams="' + Number(u.unit_index) + '"';
        var dry = u.dry;
        if (dryActive(u)) {
          var stop = !(can && dry) ? ""
            : ask ? '<span class="ams-ask">Остановить сушку?</span><button type="button" class="pc-ctl cf-stop" data-ams-dry-stop-go' + attrs
                    + '>Стоп</button><button type="button" class="pc-ctl" data-ams-dry-stop-no' + attrs + '>Отмена</button>'
            : '<button type="button" class="pc-ctl" data-ams-dry-stop' + attrs + '>Стоп</button>';
          return '<div class="ams-dry' + (ask && stop ? " asking" : "") + '"><span aria-hidden="true">🔥</span><span class="lbl">' +
            (auto ? amsDryRunText(u, auto.status) : dryStatusText(u)) + '</span>' + stop + '</div>';
        }
        if (!dry && !auto) return "";
        var blockers = (dry && dry.blockers) || [];
        var startable = !!(can && dry && p && p.ams_dry_ok === true && !blockers.length && dry.status !== "stopping");
        if (!auto && !blockers.length && !startable) return "";
        var why = blockers.map(function (b) {
          return '<br><span class="warn">Сушка недоступна: ' + safeText(b.text, "") + '</span>';
        }).join("");
        // the printer's own word first when it says more than «off» (an error, a stop under way, a test)
        var own = dry && AMS_DRY_OWN[dry.status] ? DRY_STATUS_LABELS[dry.status] + " · " : "";
        var text = auto ? own + auto.card + why : dryStatusText(u) + why;
        return '<div class="ams-dry off' + (auto ? " st-" + safeText(auto.cls, "") : "") + '"><span class="lbl">' + text + '</span>'
          + (startable ? '<button type="button" class="pc-ctl" data-ams-dry-open' + attrs + '>Сушить…</button>' : "") + '</div>';
      }
      // The printer's drying statuses an idle line names before the auto-dry's word.
      var AMS_DRY_OWN = { checking: true, stopping: true, error: true, overheat: true, testing: true };
      // A drying unit's line (HTML): «авто · прогон 2 · 55 °C PETG · ещё 9:30» — the time
      // left in one format, as the card's chip shows it; a foreign run is not ours to touch.
      function amsDryRunText(u, s) {
        s = s || {};
        var dry = u.dry || {};
        var who = s.kind === "foreign" ? "чужая сушка"
          : s.kind === "dry" ? (s.origin === "auto" ? "авто · прогон " + (Number(s.series) || 1) : "вручную") : "сушка";
        var parts = [], temp = dry.temp || s.temp, mat = dry.filament || s.filament;
        if (temp) parts.push(Number(temp) + " °C" + (mat ? " " + safeText(mat, "") : ""));
        var rem = u.dry ? dry.remaining_min : u.dry_time;
        if (rem > 0) parts.push("ещё " + amsFmtMin(rem));
        return "<b>" + who + "</b>" + (parts.length ? " · " + parts.join(" · ") : "") +
          (s.kind === "foreign" ? ' <span class="dim">— не трогаем</span>' : "");
      }
      // A phone's AMS row (IA §6, plan step 15): the drying line short, HTML-safe — «🔥 авто · ещё 8:51»,
      // «🔥 чужая · ещё 11:38», the auto-dry's word when it does not dry («ждёт · 7% (старт от 10%)»),
      // «на обслуживании», «нет связи». auto: {card, status} of the unit's auto-dry line, or null.
      function amsRowText(u, auto) {
        if (!u) return "";
        if (dryActive(u)) {
          var st = (auto && auto.status) || {}, dry = u.dry || {};
          var who = st.kind === "foreign" ? "чужая" : st.kind === "dry" ? (st.origin === "auto" ? "авто" : "вручную") : "сушка";
          var rem = u.dry ? dry.remaining_min : u.dry_time;
          return "🔥 " + who + (rem > 0 ? " · ещё " + amsFmtMin(rem) : "");
        }
        if (u.maintenance) return "на обслуживании";
        if (u.online === false) return "нет связи";
        if (auto && auto.card) return auto.card;
        return u.dry ? safeText(DRY_STATUS_LABELS[u.dry.status] || "сушка выключена", "") : "покой";
      }
      // The AMS tab's blocks (IA §6): by printer name, naturally (P2 before P10), then the unit.
      function amsUnitOrder(a, b) {
        return String(a.label || "").localeCompare(String(b.label || ""), "ru", { numeric: true }) ||
          (Number(a.unit_index) || 0) - (Number(b.unit_index) || 0);
      }
      /* ams-ui:end */
      /* autodry-ui:begin */
      // AMS auto-drying: status lines, plans, the rules form, chart overlays,
      // journal rows, the AMS tab's strip and zones — pure helpers only (no DOM, no globals
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
        reenabled: ["Включена", "steel"], dry_run: ["Пробный", "steel"], unreachable: ["Цель", "warn"],
        maintenance_on: ["Обслуживание", "warn"], maintenance_off: ["Снят с обслуж.", "steel"]
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
      // "HH:MM" on the same local day as nowTs, else "DD.MM HH:MM" (browser time; "DD/MM" in English)
      function adClock(ts, nowTs) {
        if (ts == null || !isFinite(ts)) return "—";
        var d = new Date(ts * 1000), n = new Date((nowTs == null ? ts : nowTs) * 1000);
        var hm = adPad(d.getHours()) + ":" + adPad(d.getMinutes());
        if (d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()) return hm;
        return adPad(d.getDate()) + (typeof I18N === "object" ? I18N.dateSep() : ".") + adPad(d.getMonth() + 1) + " " + hm;
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
          case "maintenance":
            r = L("off", "принтер на обслуживании", "Обслуживание", "автосушка не трогает блок, пока принтер на обслуживании");
            break;
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
          case "restart":
            r = L("wait", "сушка погасла · перезапуск в " + until, "Перезапуск в " + until,
                  "принтер сам выключил сушку — включаем снова");
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
      // outside [ymin, ymax] is skipped. The temperature chart adds band
      // ([{t, lo, hi}]: a translucent min..max polygon per run, under; a null
      // breaks it) and target ([{t, v}]: a dashed step-after line over the
      // curve, a null v = heater off ends it there), in bandColor / targetColor.
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
        var run = [];
        function flushBand() {
          if (run.length > 1) {
            var d = run.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ' ' + Y(p.hi).toFixed(1); }).join(' ');
            for (var j = run.length - 1; j >= 0; j--) d += ' L' + X(run[j].t).toFixed(1) + ' ' + Y(run[j].lo).toFixed(1);
            under += '<path class="band" d="' + d + ' Z" fill="' + opts.bandColor + '" opacity="0.12"/>';
          }
          run = [];
        }
        (opts.band || []).forEach(function (p) { if (p.lo == null || p.hi == null) flushBand(); else run.push(p); });
        flushBand();
        var td = '', on = false;
        (opts.target || []).forEach(function (p) {
          // step-after: a run holds until the next point (a hole's marker repeats the last t: no width)
          if (p.v == null) { if (on) td += 'H' + X(p.t).toFixed(1); on = false; return; }
          var x = X(p.t).toFixed(1), y = Y(p.v).toFixed(1);
          td += on ? 'H' + x + 'V' + y : 'M' + x + ' ' + y;
          on = true;
        });
        if (td) labels += '<path class="tgt" d="' + td + '" fill="none" stroke="' + opts.targetColor +
          '" stroke-width="1.3" stroke-dasharray="4 3" opacity="0.75"/>';
        return { under: under, labels: labels };
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
        if (autodryStalled(ad)) {
          lines.push("<div>Автосушка не отвечает (" + (e.last_tick != null ? "последняя проверка в " + adClock(e.last_tick, now)
            : "проверок ещё не было") + ") — проверьте сервис.</div>");
        }
        return lines;
      }
      // The engine failed or has not checked for 5 min (started, or no check 5 min after the start).
      function autodryStalled(ad) {
        var e = (ad && ad.engine) || {}, now = ad && ad.now;
        return !!e.error || !!(e.started && ((e.last_tick == null && e.started_at != null && now - e.started_at > 300) ||
                                             (e.last_tick != null && now - e.last_tick > 300)));
      }
      // The AMS tab's auto-drying line (IA §6, HTML; ad: GET /api/ams/autodry or null): on /
      // off, how many units dry now of how many it watches (the foreign runs told apart), the
      // goal and the trial mode (.ams-strip-tail: a phone folds it away unless the trial mode
      // is on), and a stalled engine or one waiting for the clock as a phrase of its own.
      function autodryStripHtml(ad) {
        if (!ad) return "Автосушка недоступна";
        var c = ad.config || {}, e = ad.engine || {}, units = ad.units || [], n = 0, foreign = 0;
        units.forEach(function (u) {
          var k = u && u.status && u.status.kind;
          if (k === "foreign") foreign++;
          if ((u && u.dry && u.dry.active) || k === "dry" || k === "foreign" || k === "starting") n++;
        });
        var h = 'Автосушка: <b class="' + (c.enabled ? "on" : "off") + '">' + (c.enabled ? "вкл" : "выкл") + "</b> · сушит <b>" + n +
          "</b> из " + units.length + (foreign ? " (" + foreign + " " + adPlural(foreign, "чужая", "чужие", "чужих") + ")" : "") +
          '<span class="ams-strip-tail' + (c.dry_run ? " keep" : "") + '"> · цель ≤ ' + adNum(c.target_rh) + " % · пробный режим " +
          (c.dry_run ? '<b class="warn">вкл</b>' : "выкл") + "</span>";
        if (e.clock_wait) h += ' · <span class="warn">ждёт синхронизации часов</span>';
        if (autodryStalled(ad)) {
          h += ' · <span class="warn">не отвечает' + (e.last_tick != null ? " с " + adClock(e.last_tick, ad.now) : "") + "</span>";
        }
        return h;
      }
      // The humidity zones of the AMS tab (IA §6): «сухо» up to the auto-dry goal, «норма» up
      // to AMS_WARN_SPAN points above it, «влажно» over that. Without the auto-dry config (not
      // loaded, unavailable): the fixed 25 / 40 of before.
      var AMS_WARN_SPAN = 10;
      function amsZones(cfg) {
        var t = cfg && cfg.target_rh != null && isFinite(cfg.target_rh) ? Math.round(Number(cfg.target_rh)) : null;
        return t == null ? { ok: 25, warn: 40, goal: false } : { ok: t, warn: t + AMS_WARN_SPAN, goal: true };
      }
      function amsHumZone(pct, z) { return pct <= z.ok ? "ok" : pct <= z.warn ? "warn" : "bad"; }
      function amsLegendHtml(z) {
        return '<span><i class="z-ok"></i>сухо ≤ ' + z.ok + " %" + (z.goal ? " (цель автосушки)" : "") + "</span>" +
          '<span><i class="z-warn"></i>норма ≤ ' + z.warn + " %</span>" +
          '<span><i class="z-bad"></i>влажно</span><span><i class="z-dry"></i>шла сушка</span>';
      }
      // The humidity chart's top (% from 0): the peak with a margin, in steps of 5, at least 20
      // and with «влажно» in sight; at most 100.
      function amsHumTop(vals, z) {
        var m = 0;
        (vals || []).forEach(function (v) { if (Number(v) > m) m = Number(v); });
        return Math.min(100, Math.max(20, z.warn + 5, Math.ceil((m + 2) / 5) * 5));
      }
      // The zones as the chart draws them: [[from, to, colour], ...].
      function amsZoneBands(z, top) {
        return [[0, z.ok, "var(--ok)"], [z.ok, z.warn, "var(--warn)"], [z.warn, top, "var(--danger)"]];
      }
      // A «Блоки» row's last cell (HTML; line: autodryLine of the unit): a unit held by a pause
      // or a backoff, or switched off for its interruptions — with «снять паузу» / «включить
      // снова» for admins (POST …/autodry/release); else empty.
      function autodryRelCell(line, admin, key) {
        if (!line || !line.link) return "";
        return '<span class="stt st-' + line.cls + '"' + (line.sub ? ' data-tip="' + line.sub + '"' : "") + '><i class="d"></i><span class="m">' +
          line.main + "</span></span>" + (admin ? '<button type="button" class="ad-link" data-ad-release="' + safeText(key, "") + '">' +
          (line.link === "reenable" ? "включить снова" : "снять паузу") + "</button>" : "");
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
        var tip = e.t_before != null || e.t_after != null
          ? ' data-tip="t° AMS: ' + (e.t_before != null ? adNum(e.t_before) + "°" : "?") + " → " +
            (e.t_after != null ? adNum(e.t_after) + "°" : "?") + '"' : "";
        return '<div class="jr-row"><span class="jr-t">' + adClock(e.ts, nowTs) + '</span><span class="jr-u">' + safeText(e.label, "") +
          '</span><span class="jr-k"><span class="badge badge-' + b[1] + '">' + b[0] + '</span></span><span class="jr-x">' + text +
          '</span><span class="' + cls + '"' + tip + '>' + rh + '</span></div>';
      }
      /* autodry-ui:end */
      /* jobs-ui:begin */
      // The print journal (История → Печати): table rows and their details, the
      // result chips with their counts, frequent reasons and prints per day (for
      // «Статистика») and the request strings — pure helpers only (no DOM, no
      // globals besides safeText, timeFmt, jobShortName and stageLabel), run in
      // node by tests/test_web_jobs_ui.py. Rows and stats are /api/history/jobs and
      // /api/history/jobs/stats as served; times are epoch seconds. Every server /
      // device string goes through safeText; the returned texts are HTML.
      // No «Годно / Брак» (the owner, 29.10): nothing here reads or sets a verdict.
      var JOBS_PAGE = 50;             // rows per list request and «Показать ещё» (IA §10; the API allows le=2000)
      // The words are IA §10's dictionary «Результаты печати» — the chips over the list say the same
      var JOB_RESULTS = {
        finished: { label: "Успешно", badge: "finished", seg: "seg-finished", tip: "" },
        cancelled: { label: "Отменено", badge: "cancelled", seg: "seg-cancelled", tip: "" },
        failed: { label: "Ошибка", badge: "error", seg: "seg-error", tip: "" },
        power_loss: { label: "Питание", badge: "paused", seg: "seg-paused", tip: "пропало питание, печать не продолжилась" },
        stopped: { label: "Остановлено", badge: "idle", seg: "seg-idle", tip: "принтер не сообщил причину" },
        lost: { label: "Нет связи", badge: "offline", seg: "seg-nodata", tip: "связь пропала, и продолжения печати не было видно" },
        open: { label: "Идёт", badge: "printing", seg: "seg-printing", tip: "" }
      };
      var JOB_RESULT_ORDER = ["finished", "cancelled", "failed", "power_loss", "stopped", "lost", "open"];
      var JOB_PLAN_SRC = { slicer: "оценка слайсера", printer: "оценка принтера", linear: "оценка по прогрессу" };
      var JOB_COLS = ["Принтер", "Начало", "Длительность (план, Δ)", "Результат", "Паузы", "Слои", "Задание"];
      var JOB_DELTA_OK = 10;          // |fact / plan − 1| up to this many % reads as «по плану»

      function jbPad(n) { return (n < 10 ? "0" : "") + n; }
      // "14:32" on the local day of nowTs, else "17.10 14:32" (browser time)
      function jobClock(ts, nowTs) {
        if (ts == null || !isFinite(ts)) return "—";
        var d = new Date(ts * 1000), n = new Date((nowTs == null ? ts : nowTs) * 1000);
        var hm = jbPad(d.getHours()) + ":" + jbPad(d.getMinutes());
        if (d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()) return hm;
        return jbPad(d.getDate()) + (typeof I18N === "object" ? I18N.dateSep() : ".") + jbPad(d.getMonth() + 1) + " " + hm;
      }
      // 1.04 -> "1,04" (Russian decimal comma), "—" without a value
      function jobNum(v, digits) {
        // the decimal sign of the panel's language (app/web/i18n.js): "1,04" in Russian, "1.04" in English
        return v == null || v === "" || !isFinite(v) ? "—"
          : Number(v).toFixed(digits).replace(".", typeof I18N === "object" ? I18N.decimal() : ",");
      }
      // A running print by the printer's live status (when there is one):
      // "open" | "open_paused" | "open_offline"; a closed one: its result.
      function jobLiveState(job, printer) {
        if (job.result != null) return job.result;
        if (printer && (!printer.online || printer.state === "offline")) return "open_offline";
        if (printer && printer.state === "paused") return "open_paused";
        return "open";
      }
      // How long it ran (a running print: so far); "~" when its start or end is not exact
      function jobDurText(job, now) {
        var open = job.result == null;
        var sec = open ? (now != null && job.started_at != null ? now - job.started_at : null) : job.actual_sec;
        var text = timeFmt(sec);
        if (text === "—") return text;
        return (!job.start_exact || (!open && !job.end_exact) ? "~" : "") + text;
      }
      // +16% / −3% / 0%
      function jobPctText(pct) { return (pct > 0 ? "+" : pct < 0 ? "−" : "") + Math.abs(pct) + "%"; }
      // The plan and the deviation from it: {plan, planCls, delta, cls, tip}. Δ is the
      // duration the row shows (pauses included: «1ч 34м из плана 1ч 21м +16%», the
      // pauses cell explains the rest) against the printer's own forecast, for a
      // finished print with an exact start and end; the tip adds the time without the
      // pauses (the KPI «Факт / план»). Any other plan (linear: the progress-based
      // estimate of Klipper / MKS) is gray, without Δ.
      function jobPlanCells(job) {
        var planned = Number(job.planned_sec);
        if (!(planned > 0)) return { plan: "—", planCls: "", delta: "", cls: "", tip: "" };
        var src = JOB_PLAN_SRC[job.planned_src] || "";
        var out = { plan: timeFmt(planned), planCls: "", delta: "", cls: "", tip: src };
        if (job.planned_src !== "slicer" && job.planned_src !== "printer") {
          out.planCls = "jd-ok";
          return out;
        }
        if (job.result !== "finished" || !job.start_exact || !job.end_exact || job.actual_sec == null) return out;
        var fact = Math.max(0, Number(job.actual_sec)), paused = Number(job.paused_sec) || 0;
        var pct = Math.round((fact / planned - 1) * 100);
        out.delta = jobPctText(pct);
        out.cls = Math.abs(pct) <= JOB_DELTA_OK ? "jd-ok" : pct > 0 ? "jd-slow" : "jd-fast";
        out.tip = src + " · факт " + timeFmt(fact) + ", план " + out.plan;
        if (paused >= 60) {
          var net = Math.max(0, fact - paused);
          out.tip += "; без пауз " + timeFmt(net) + " (" + jobPctText(Math.round((net / planned - 1) * 100)) + ")";
        }
        return out;
      }
      // A pause / error reason: "stage:<slug>" -> the stage's label, else "<code>: <text>" or the code
      function jobReasonText(code, text) {
        if (code == null || code === "") return safeText(text, "");
        code = String(code);
        if (code.indexOf("stage:") === 0) {
          var slug = code.slice(6);
          return safeText(stageLabel(slug) || slug.replace(/_/g, " "), "");
        }
        return text ? safeText(code, "") + ": " + safeText(text, "") : safeText(code, "");
      }
      // Start → end; the end's date only when it is not the start's day
      function jobWhenHtml(job, now) {
        var start = (job.start_exact ? "" : "~") + jobClock(job.started_at, now);
        if (job.result == null || job.ended_at == null) return start;
        return start + '<span class="job-end"> → ' + (job.end_exact ? "" : "~") + jobClock(job.ended_at, job.started_at) + '</span>';
      }
      // The result cell: a badge with the code and text in its tip (a running print:
      // «Идёт · 47% · осталось 3ч 10м», «Пауза» or «Нет связи с 14:32» from the
      // printer's live status) plus the ⚡ (power came back) and maintenance flags.
      // The page re-renders it for running prints on every status tick.
      function jobStateHtml(job, printer, now) {
        var key = jobLiveState(job, printer);
        var html;
        if (job.result == null) {
          var badge = key === "open_paused" ? "paused" : key === "open_offline" ? "offline" : "printing";
          var label = key === "open_paused" ? "Пауза" : key === "open_offline"
            ? (job.last_seen != null ? "Нет связи с " + jobClock(job.last_seen, now) : "Нет связи") : "Идёт";
          html = '<span class="event-badge event-badge-' + badge + '">' + label + '</span>';
          if (key !== "open_offline") {
            var pct = printer && printer.progress_pct != null && isFinite(printer.progress_pct) ? printer.progress_pct : job.end_progress;
            var live = [];
            if (pct != null && isFinite(pct)) live.push(Math.floor(pct) + "%");
            if (key === "open" && printer && printer.eta_seconds > 0) live.push("осталось " + timeFmt(printer.eta_seconds));
            if (live.length) html += ' <span class="job-live">' + live.join(" · ") + '</span>';
          }
        } else {
          var r = JOB_RESULTS[key] || { label: key, badge: "unknown", tip: "" };
          var tip = jobReasonText(job.error_code, job.error_text) || (r.tip ? safeText(r.label + " — " + r.tip, "") : "");
          html = '<span class="event-badge event-badge-' + r.badge + '"' + (tip ? ' data-tip="' + tip + '"' : '') + '>' +
            safeText(r.label, "") + '</span>';
        }
        var losses = Number(job.power_losses) || 0;
        if (losses > 0) {
          html += ' <span class="job-flag" data-tip="Пропадало питание: ' + losses + '">⚡' + (losses > 1 ? losses : "") + '</span>';
        }
        if (job.maint) html += ' <span class="job-flag" data-tip="Шла в режиме обслуживания — в статистику не входит">обслуживание</span>';
        return html;
      }
      // Pauses: {text: "2 · 14м" | "—", tip: the reasons and the connection drops}
      function jobPausesCell(job) {
        var n = Number(job.pauses) || 0;
        var paused = timeFmt(job.paused_sec);
        var tips = (job.pause_codes || []).map(function (c) { return jobReasonText(c, null); }).filter(Boolean);
        var drops = Number(job.offline_n) || 0;
        if (drops > 0) tips.push("без связи ×" + drops + (job.offline_sec >= 60 ? ", " + timeFmt(job.offline_sec) : ""));
        return { text: n ? n + (paused !== "—" ? " · " + paused : "") : "—", tip: tips.join(", ") };
      }
      function jobLayersText(job) {
        var total = Number(job.total_layers) || 0, layer = Number(job.layer) || 0;
        if (job.result === "finished" && total) return String(total);
        if (!total) return layer ? String(layer) : "—";
        return layer ? Math.min(layer, total) + "/" + total : "—";
      }
      // «Длительность (план, Δ)» in one cell (IA §7): «5ч 26м · план 5ч +9%»; the plan and Δ keep
      // their classes and tips (jobPlanCells). A running print: how long so far, the plan, no Δ.
      function jobDurCellHtml(job, now) {
        var plan = jobPlanCells(job), html = jobDurText(job, now);
        if (plan.plan === "—") return html;
        html += ' <span class="job-plan' + (plan.planCls ? " " + plan.planCls : "") + '"' + (plan.tip ? ' data-tip="' + plan.tip + '"' : "") +
          '>· план ' + plan.plan + '</span>';
        if (plan.delta) html += ' <span class="job-delta ' + plan.cls + '" data-tip="' + plan.tip + '">' + plan.delta + '</span>';
        return html;
      }
      function jobsHeadHtml() {
        return "<tr>" + JOB_COLS.map(function (c) { return "<th>" + c + "</th>"; }).join("") + "</tr>";
      }
      // One table row <tr class="job-row" data-job-id data-pid tabindex="0" aria-expanded>: a click
      // (Enter) unfolds its details (jobDetailHtml) in the row after it. Every cell has data-l (its
      // label in the phone layout). opts: {now, open: the row is unfolded}.
      function jobRowHtml(job, printer, opts) {
        opts = opts || {};
        var running = job.result == null, pauses = jobPausesCell(job);
        function td(label, cls, tip, html) {
          return '<td data-l="' + label + '"' + (cls ? ' class="' + cls + '"' : '') + (tip ? ' data-tip="' + tip + '"' : '') +
            '>' + html + '</td>';
        }
        var label = safeText(job.label, safeText(job.printer_id, "—"));
        var model = job.model && job.model !== job.label ? ' <span class="job-model">' + safeText(job.model, "") + '</span>' : "";
        var name = job.job_name
          ? '<span data-tip="' + safeText(job.job_name, "") + '">' + safeText(jobShortName(job.job_name), "—") + '</span>' : "—";
        var cells = [
          td(JOB_COLS[0], "job-printer", "", '<span class="job-chev" aria-hidden="true">' + (opts.open ? "▾" : "▸") + '</span>' + label + model),
          td(JOB_COLS[1], "job-when", "", jobWhenHtml(job, opts.now)),
          td(JOB_COLS[2], "job-dur", "", jobDurCellHtml(job, opts.now)),
          td(JOB_COLS[3], "job-state", "", jobStateHtml(job, printer, opts.now)),
          td(JOB_COLS[4], "job-pauses" + (pauses.text === "—" ? " job-nil" : ""), pauses.tip, pauses.text),
          td(JOB_COLS[5], "job-layers", "", jobLayersText(job)),
          td(JOB_COLS[6], "job-name", "", name)
        ];
        return '<tr class="job-row' + (running ? " job-open" : "") + (opts.open ? " job-x" : "") + '" data-job-id="' +
          safeText(job.id, "") + '" data-pid="' + safeText(job.printer_id, "") + '" tabindex="0" aria-expanded="' +
          (opts.open ? "true" : "false") + '">' + cells.join("") + '</tr>';
      }
      // The auto-ignore's audit rows (/api/admin/audit?user=auto-ignore: «auto_ignore:<printer>:<code>:<status>»
      // or «…:skip:<why>») of this print's printer and time → [{at, code, ok, text}], oldest first.
      var JOB_AUTO_SKIP = { maintenance: "принтер на обслуживании", tries_used: "попытки кончились",
                            no_job_id: "нет номера задания" };
      function jobAutoHits(job, rows, now) {
        if (!job || !Array.isArray(rows)) return [];
        var lo = (Number(job.started_at) || 0) - 60;
        var hi = (job.result == null ? Number(now) || Infinity : Number(job.ended_at || job.last_seen) || Infinity) + 60;
        var out = [];
        rows.forEach(function (r) {
          var p = String((r && r.action) || "").split(":"), at = Number(r && r.created_at);
          if (p[0] !== "auto_ignore" || p[1] !== String(job.printer_id) || !p[2] || !(at >= lo && at <= hi)) return;
          var st = p[3] || "", text;
          if (st === "confirmed") text = "автопропуск нажал ✓";
          else if (st === "sent") text = "автопропуск нажал, принтер не подтвердил";
          else if (st === "skip") {
            var why = p[4] || "";
            text = "автопропуск не нажал: " + (JOB_AUTO_SKIP[why] || (why.indexOf("state_") === 0 ? "принтер не на паузе" : why || "—"));
          } else text = "автопропуск не нажал: " + (st || "—");
          out.push({ at: at, code: p[2], ok: st === "confirmed", text: text });
        });
        return out.sort(function (a, b) { return a.at - b.at; });
      }
      // A pause's reason: the code apart from its text, a stage in words; no reason yet: «пауза»
      function jobPauseWhy(p) {
        var c = p && p.code ? String(p.code) : "";
        if (!c) return p && p.text ? safeText(p.text, "") : "пауза";
        if (c.indexOf("stage:") === 0) return jobReasonText(c, null);
        return '<code>' + safeText(c, "") + '</code>' + (p.text ? " " + safeText(p.text, "") : "");
      }
      // The unfolded row (IA §7): every pause with its time, length and reason (pause_list; a
      // server without it: the codes), the auto-ignore's presses next to the pause they were for,
      // the connection drops and power losses, how the print ended, the plan's source, the whole
      // job name. opts: {now, hits: jobAutoHits — the admins' only (the audit is theirs)}.
      function jobDetailHtml(job, opts) {
        opts = opts || {};
        var now = opts.now, hits = (opts.hits || []).slice(), at = job.started_at;
        var list = Array.isArray(job.pause_list) && job.pause_list.length ? job.pause_list
          : (job.pause_codes || []).map(function (c) { return { at: null, sec: null, code: c, text: null }; });
        function tag(h) {
          return '<span class="job-tag' + (h.ok ? " ok" : "") + '">' + h.text + (h.at ? " " + jobClock(h.at, at) : "") + '</span>';
        }
        function take(p) {
          for (var i = 0; i < hits.length; i++) {
            var h = hits[i], end = p.at == null ? Infinity : p.at + Math.max(0, Number(p.sec) || 0) + 120;
            if (h.code === p.code && (p.at == null || (h.at >= p.at - 60 && h.at <= end))) return hits.splice(i, 1)[0];
          }
          return null;
        }
        var n = Number(job.pauses) || 0, paused = timeFmt(job.paused_sec);
        var pz = list.map(function (p) {
          var h = take(p), sec = Number(p.sec) || 0;
          return '<div class="r">' + (p.at != null ? '<span class="job-t">' + jobClock(p.at, at) + '</span> ' : '') +
            '<span>' + jobPauseWhy(p) + '</span>' + (sec >= 60 ? ' <span class="job-dim">· ' + timeFmt(sec) + '</span>' : '') +
            (h ? " " + tag(h) : '') + '</div>';
        }).join("");
        // a press of the auto-ignore means the printer stopped: with one left over, no «пауз не было»
        if (!pz) pz = '<div class="r job-dim">' + (n ? "причины не записаны" : hits.length ? "пауза не записана в журнал" : "пауз не было") + '</div>';
        if (n > list.length && list.length) pz += '<div class="r job-dim">и ещё ' + (n - list.length) + '</div>';
        pz += hits.map(function (h) { return '<div class="r"><code>' + safeText(h.code, "") + '</code> ' + tag(h) + '</div>'; }).join("");
        var drops = Number(job.offline_n) || 0, losses = Number(job.power_losses) || 0;
        if (drops) pz += '<div class="r job-dim">без связи ×' + drops + (job.offline_sec >= 60 ? ", " + timeFmt(job.offline_sec) : "") + '</div>';
        if (losses) pz += '<div class="r job-dim">⚡ пропадало питание ×' + losses + '</div>';
        var end;
        if (job.result == null) {
          end = "идёт" + (job.end_progress != null && isFinite(job.end_progress) ? " · " + Math.floor(job.end_progress) + "%" : "");
        } else {
          var r = JOB_RESULTS[job.result] || { label: job.result, tip: "" };
          end = (job.error_code ? jobPauseWhy({ code: job.error_code, text: job.error_text }) : safeText(r.label, "") + (r.tip ? " — " + r.tip : "")) +
            (job.result !== "finished" && job.end_progress != null && isFinite(job.end_progress) ? " на " + Math.floor(job.end_progress) + "%" : "") +
            (job.ended_at != null ? ' <span class="job-dim">· ' + (job.end_exact ? "" : "~") + jobClock(job.ended_at, at) + '</span>' : "");
        }
        var src = JOB_PLAN_SRC[job.planned_src];
        var tail = '<div class="r">' + end + '</div>' + (src && Number(job.planned_sec) > 0
          ? '<div class="r job-dim">план ' + timeFmt(job.planned_sec) + ' — ' + src + '</div>' : '');
        var what = job.job_name ? '<div class="r job-full">' + safeText(job.job_name, "") + '</div>' : '';
        what += '<div class="r job-dim">слои ' + jobLayersText(job) + (job.maint ? " · шла в режиме обслуживания" : "") + '</div>';
        return '<div class="job-xp"><div><h5>Паузы · ' + n + (paused !== "—" ? " · " + paused : "") + '</h5>' + pz + '</div>' +
          '<div><h5>Конец</h5>' + tail + '</div><div><h5>Задание</h5>' + what + '</div></div>';
      }
      // The result chips over the list (IA §7): the counts are the totals and the filter at once —
      // «Все 36 · Успешно 31 · …», a zero one dim, the chosen ones with ×. stats: the window's
      // /jobs/stats with the prints in maintenance mode, as the list shows them; null: no counts yet.
      var JOB_CHIPS = [
        { id: "", label: "Все" }, { id: "finished", label: "Успешно" }, { id: "cancelled", label: "Отменено" },
        { id: "failed", label: "Ошибка" }, { id: "power_loss", label: "Питание" }, { id: "lost", label: "Нет связи" },
        { id: "stopped", label: "Остановлено" }, { id: "open", label: "Идут" }
      ];
      function jobsChipsHtml(stats, results) {
        var t = (stats && stats.total) || null;
        results = results || [];
        return JOB_CHIPS.map(function (c) {
          var n = t ? Number(c.id ? t[c.id] : t.jobs) || 0 : null;
          var on = c.id ? results.indexOf(c.id) >= 0 : !results.length;
          var tip = !c.id ? "Все печати периода"
            : on ? (results.length > 1 ? "Ctrl+клик — убрать из выбора · клик — только «" + c.label + "»" : "Повторный клик — все печати")
            : "Показать только «" + c.label + "» · Ctrl+клик — добавить к выбору";
          return '<button type="button" class="farm-chip' + (n === 0 ? " zero" : "") + (on ? " on" : "") + '" data-result="' + c.id +
            '" aria-pressed="' + (on ? "true" : "false") + '" data-tip="' + tip + '">' + c.label + (n == null ? "" : " <b>" + n + "</b>") +
            (on && c.id ? '<span class="farm-x" aria-hidden="true">×</span>' : "") + '</button>';
        }).join("");
      }
      // A chip's click (as the farm's chips, IA §5): only this result, the same single one again —
      // all; add (Ctrl / ⌘): in or out; «Все» — all
      function jobsResultsClick(results, id, add) {
        var r = (results || []).filter(function (x) { return x && JOB_RESULTS[x]; }), i = r.indexOf(id);
        if (!id) return [];
        if (!JOB_RESULTS[id]) return r;
        if (add) {
          if (i >= 0) r.splice(i, 1); else r.push(id);
          return r;
        }
        return r.length === 1 && i === 0 ? [] : [id];
      }
      // Under the list: «показано 50 из 216» — the total of the chosen chips (all: the window's);
      // without the counts (or behind the list): «показано 50+»
      function jobsShownText(n, stats, results, hasMore) {
        var t = stats && stats.total, total = null;
        if (t) {
          total = results && results.length
            ? results.reduce(function (s, k) { return s + (Number(t[k]) || 0); }, 0) : Number(t.jobs) || 0;
        }
        if (total == null || total < n || (hasMore && total <= n)) return "показано " + n + (hasMore ? "+" : "");
        return "показано " + n + " из " + total;
      }
      // «Частые причины»: errors and interruptions (plus the cancel count) and pause reasons
      function jobsReasonsHtml(stats) {
        stats = stats || {};
        function item(code, text, n, cls) {
          return '<div class="jobs-reason' + (cls ? " " + cls : "") + '"><code>' + code + '</code><span class="jobs-reason-text"' +
            (text ? ' data-tip="' + text + '"' : '') + '>' + (text || "—") + '</span><b>' + n + '</b></div>';
        }
        function list(items, empty) {
          if (!items || !items.length) return '<div class="dash-stats-empty">' + empty + '</div>';
          return items.map(function (it) {
            var stage = String(it.code || "").indexOf("stage:") === 0;
            return item(stage ? "" : safeText(it.code, ""), stage ? jobReasonText(it.code, null) : safeText(it.text, ""),
              Number(it.n) || 0, "");
          }).join("");
        }
        var cancel = Number(stats.cancel_n) || 0;
        return '<div class="jobs-reasons-col"><h4>Ошибки и прерывания</h4>' + list(stats.failure_codes, "Ошибок нет") +
          (cancel ? item("", "Отменены", cancel, "jobs-cancel") : "") + '</div>' +
          '<div class="jobs-reasons-col"><h4>Паузы</h4>' + list(stats.pause_reasons, "Пауз с причиной нет") + '</div>';
      }
      // «Печатей в день»: a column per local day, finished at the bottom, the rest above.
      // A date label runs right from its column: one with less than a label spacing
      // of columns to its right is left out (it would stick out of the chart).
      function jobsDayBarsHtml(stats) {
        var days = (stats && stats.per_day) || [];
        var max = 0;
        days.forEach(function (d) { max = Math.max(max, Number(d.jobs) || 0); });
        if (!max) return '<div class="dash-stats-empty">Печатей за период нет</div>';
        var every = Math.max(1, Math.ceil(days.length / 10));   // ~10 date labels at most
        return '<div class="jobs-days">' + days.map(function (d, i) {
          var jobs = Number(d.jobs) || 0, ok = Math.min(jobs, Number(d.finished) || 0);
          var m = /^\d{4}-(\d{2})-(\d{2})$/.exec(String(d.day || ""));
          var label = m ? m[2] + (typeof I18N === "object" ? I18N.dateSep() : ".") + m[1] : safeText(d.day, "");
          return '<div class="jobs-day" data-tip="' + label + ": " + jobs + (jobs ? ", успешно " + ok : "") + '">' +
            '<span class="jobs-day-bar"><i class="jobs-day-other" style="height:' + ((jobs - ok) / max * 100).toFixed(1) + '%"></i>' +
            '<i class="jobs-day-ok" style="height:' + (ok / max * 100).toFixed(1) + '%"></i></span>' +
            '<span class="jobs-day-lbl">' + (i % every === 0 && days.length - i >= every ? label : "") + '</span></div>';
        }).join("") + '</div>';
      }
      // The filter's window {fr, to}: its own С / По when set, else the last `days` days to now
      function jobsWindow(f, now) {
        f = f || {};
        var to = f.to != null && isFinite(f.to) ? Number(f.to) : now;
        var fr = f.fr != null && isFinite(f.fr) ? Number(f.fr) : to - (Number(f.days) || 7) * 86400;
        return { fr: Math.floor(fr), to: Math.ceil(to) };
      }
      // f: {days, fr, to, printer, results: [...]}; results only for the list and the CSV
      function jobsFilterQuery(f, now, withResults) {
        f = f || {};
        var w = jobsWindow(f, now);
        var q = "fr=" + w.fr + "&to=" + w.to;
        if (f.printer) q += "&printer=" + encodeURIComponent(f.printer);
        var results = (f.results || []).filter(Boolean);
        if (withResults && results.length) q += "&result=" + encodeURIComponent(results.join(","));
        return q;
      }
      // The list's query string: the filters and a JOBS_PAGE page at `offset`
      function jobsQuery(f, now, offset) {
        return jobsFilterQuery(f, now, true) + "&limit=" + JOBS_PAGE + "&offset=" + Math.max(0, Math.floor(Number(offset) || 0));
      }
      // «Печати»'s stats: the chips' counts, so with the prints in maintenance mode, as the list shows them
      function jobsStatsQuery(f, now) { return jobsFilterQuery(f, now, false) + "&include_maint=true"; }
      function jobsCsvUrl(f, now) { return "/api/history/jobs.csv?" + jobsFilterQuery(f, now, true); }
      /* jobs-ui:end */
      /* adm-ui:begin */
      // «Админ» polish (IA §8, plan step 13): the park table's maintenance and control cells, the
      // users' rows with their in-row forms (no prompt / confirm / alert), the auto-ignore codes.
      // Pure helpers only (no DOM, no globals besides safeText, maintSinceText, maintTipText,
      // uaDevice and fmtAuditTime), run in node by tests/test_web_adm_ui.py. Server strings go
      // through safeText; the returned texts are HTML.
      var PARK_CTL = [["pause", "печать"], ["light", "свет"], ["tune", "t° и скорость"], ["ams", "AMS"]];
      // «Управление» (read only): what the dashboard can command on the printer — its caps in
      // /api/printers (for Bambu the probe's verdict: print-class or light only). live: that row or null.
      function parkCtlHtml(live) {
        if (!live) return '<span class="muted">—</span>';
        var caps = live.caps || {}, on = [], unknown = false;
        PARK_CTL.forEach(function (c) {
          if (caps[c[0]] === true) on.push(c[1]);
          else if (caps[c[0]] == null && c[0] !== "ams") unknown = true;
        });
        var tip = live.caps_note ? ' data-tip="' + safeText(live.caps_note, "") + '"' : "";
        if (!on.length) {
          var why = !unknown || live.kind === "mks" ? "не подключено" : live.online === false ? "нет связи" : "проверяется";
          return '<span class="act-tag"' + tip + ">" + why + "</span>";
        }
        return '<span class="act-tag' + (caps.pause === true ? " ok" : "") + '"' + tip + ">" + on.join(" · ") + "</span>";
      }
      // «Обслуживание»: «🔧 с 19.10» (the whole story in its tip) and «к карточке ›»; none: «—».
      // Put on / taken off through the card's ⋯ (IA §5: one way), never here.
      function parkMaintHtml(row, nowMs) {
        if (!row || row.maintenance_since == null) return '<span class="muted">—</span>';
        var m = { since: row.maintenance_since, note: row.maintenance_note, by: row.maintenance_by };
        return '<span class="badge badge-warn" data-tip="' + safeText(maintTipText(m), "") + '">🔧 ' +
          safeText(maintSinceText(m.since, nowMs), "обслуживание") + "</span> " +
          '<button type="button" class="adm-link" data-act="card" data-id="' + safeText(row.id, "") + '">к карточке ›</button>';
      }
      // A park row's actions (IA §10, in place, no native dialog): «Изменить · Удалить»; once «Удалить» was
      // pressed (ask: that printer's id) the row asks «Удалить «P3»? История печати останется.» with
      // «Удалить» and «Отмена»; a deleted one (not applied yet) has «Вернуть».
      function parkActsHtml(p, ask) {
        var id = safeText(p.id, "");
        if (p.pending === "deleted") return '<button type="button" class="ghost-btn" data-act="restore" data-id="' + id + '">Вернуть</button>';
        if (ask != null && String(ask) === String(p.id)) {
          return '<div class="adm-inrow"><span class="adm-ask">Удалить «' + safeText(p.label, "") + '»? История печати останется.</span>' +
            '<button type="button" class="ghost-btn danger" data-act="del-yes" data-id="' + id + '">Удалить</button>' +
            '<button type="button" class="adm-link" data-act="del-no" data-id="' + id + '">Отмена</button></div>';
        }
        return '<button type="button" class="ghost-btn" data-act="edit" data-id="' + id + '">Изменить</button> ' +
          '<button type="button" class="ghost-btn danger" data-act="delete" data-id="' + id + '">Удалить</button>';
      }
      // A Creality's light switch (the setting printer_light_disabled: by default every Creality has it).
      function parkLightOn(disabled, id) { return !(Array.isArray(disabled) && disabled.indexOf(id) >= 0); }
      // The list with the printer in or out (sorted, no repeats): the PUT /settings body's value.
      function parkLightList(disabled, id, on) {
        var out = (Array.isArray(disabled) ? disabled : []).filter(function (x) { return x !== id; });
        if (!on) out.push(id);
        return out.filter(function (x, i) { return out.indexOf(x) === i; }).sort();
      }

      var USER_ROLE_RU = { admin: "Админ", viewer: "Зритель" };
      // One user's row. ui: {pw: id (the password form open), del: id (the delete asked),
      // role: {id, role} (a role change asked), err: {id: text}}; me: the signed-in login (no
      // own role change, no own delete — one admin must stay).
      function userRowHtml(u, me, ui) {
        ui = ui || {};
        var id = safeText(u.id, ""), name = safeText(u.username, ""), self = u.username === me;
        var asked = ui.role && String(ui.role.id) === String(u.id) ? ui.role.role : null;
        var role = '<select class="hist-in adm-role" data-uid="' + id + '" aria-label="Роль ' + name + '"' +
          (self ? ' disabled data-tip="Свою роль менять нельзя — иначе можно остаться без админов"' : "") + ">" +
          ["admin", "viewer"].map(function (r) {
            return '<option value="' + r + '"' + ((asked || u.role) === r ? " selected" : "") + ">" + USER_ROLE_RU[r] + "</option>";
          }).join("") + "</select>";
        var ll = u.last_login, dev = ll ? uaDevice(ll.user_agent) : "";
        var last = ll ? '<span class="mono dim">' + fmtAuditTime(ll.created_at) + "</span>" +
          '<div class="ll-sub" data-tip="' + safeText(ll.user_agent || "", "") + '"><span class="mono">' + safeText(ll.ip_address, "—") +
          "</span>" + (dev ? " · " + safeText(dev, "") : "") + "</div>" : '<span class="muted">не входил</span>';
        var acts;
        if (asked) {
          acts = '<span class="adm-ask">Роль → «' + USER_ROLE_RU[asked] + '»: сессии пользователя завершатся.</span>' +
            '<button type="button" class="ghost-btn" data-uact="role-yes" data-uid="' + id + '">Сменить</button>' +
            '<button type="button" class="adm-link" data-uact="cancel">Отмена</button>';
        } else if (String(ui.pw) === String(u.id)) {
          acts = '<input type="password" class="hist-in" data-pw-for="' + id + '" placeholder="новый пароль, от 8 символов"' +
            ' autocomplete="new-password" aria-label="Новый пароль ' + name + '">' +
            '<button type="button" class="ghost-btn" data-uact="pw-save" data-uid="' + id + '">Сохранить</button>' +
            '<button type="button" class="adm-link" data-uact="cancel">Отмена</button>';
        } else if (String(ui.del) === String(u.id)) {
          acts = '<span class="adm-ask">Удалить «' + name + '»?</span>' +
            '<button type="button" class="ghost-btn danger" data-uact="del-yes" data-uid="' + id + '">Удалить</button>' +
            '<button type="button" class="adm-link" data-uact="cancel">Отмена</button>';
        } else {
          acts = '<button type="button" class="adm-link" data-uact="pw" data-uid="' + id + '">Сменить пароль</button>' +
            (self ? "" : '<button type="button" class="adm-link danger" data-uact="del" data-uid="' + id + '">Удалить</button>');
        }
        var err = ui.err && ui.err[u.id] ? '<div class="adm-err-row">' + safeText(ui.err[u.id], "") + "</div>" : "";
        return '<tr data-uid="' + id + '"><td><b>' + name + "</b>" + (self ? ' <span class="muted">(вы)</span>' : "") + "</td>" +
          "<td>" + role + "</td><td class=\"ll-cell\">" + last + "</td>" +
          '<td class="adm-actions"><div class="adm-inrow">' + acts + "</div>" + err + "</td></tr>";
      }
      // A typed auto-ignore code -> "0500_8062", or null: case, spaces and the separator do not matter.
      var AI_CODE_RE = /^[0-9A-F]{4}_[0-9A-F]{4}$/;
      var AI_MAX = 32;                         // the server's limit (auto_ignore_print_errors)
      function aiNormCode(s) {
        var t = String(s == null ? "" : s).toUpperCase().replace(/[\s\-_.]/g, "");
        if (!/^[0-9A-F]{8}$/.test(t)) return null;
        return t.slice(0, 4) + "_" + t.slice(4);
      }
      // {codes, error}: the list with a typed code added (or why it is not).
      function aiAdd(codes, typed) {
        var c = aiNormCode(typed), list = (codes || []).slice();
        if (!c) return { codes: list, error: "Код вида 0500_8062 — восемь цифр 0–9 и A–F" };
        if (list.indexOf(c) >= 0) return { codes: list, error: "Код " + c + " уже в списке" };
        if (list.length >= AI_MAX) return { codes: list, error: "Не больше " + AI_MAX + " кодов" };
        list.push(c);
        return { codes: list, error: null };
      }
      function aiSame(a, b) { return JSON.stringify(a || []) === JSON.stringify(b || []); }
      // The list (HTML): each code with Bambu's text (texts: {code: text | null}) and «убрать».
      function aiCodesHtml(codes, texts) {
        if (!codes || !codes.length) return '<div class="ai-empty">Список пуст — автопропуск ничего не нажимает.</div>';
        return codes.map(function (c) {
          var t = texts ? texts[c] : undefined;
          return '<div class="ai-code"><code>' + safeText(c, "") + '</code><span class="ai-text">' +
            (t ? safeText(t, "") : t === null ? '<span class="muted">текста у Bambu нет</span>' : '<span class="muted">…</span>') +
            '</span><button type="button" class="adm-link danger" data-ai-del="' + safeText(c, "") + '">убрать</button></div>';
        }).join("");
      }
      /* adm-ui:end */
      /* acts-ui:begin */
      // История → Действия (IA §7, plan step 12): the audit journal as people read it — the
      // groups and their chips with counts, the words of each action and its printer, the
      // request strings. Pure helpers only (no DOM, no globals besides safeText, amsUnitName,
      // uaDevice and fmtAuditTime), run in node by tests/test_web_acts_ui.py. Rows are
      // /api/admin/audit's {id, user_id, action, ip_address, user_agent, created_at}, counts
      // /api/admin/audit/counts' [{user, key, n}]; every server string goes through safeText,
      // the returned texts are HTML.
      var AUDIT_LIMIT = 50;                // rows per request and «Показать ещё» (IA §10; the API allows le=500)
      // A group is a few action starts (the API matches them, prefix=a,b); «служебные» apart.
      var ACT_GROUPS = [
        { id: "cmd", label: "Команды", prefixes: ["printer_command", "printer_bulk"] },
        { id: "auto", label: "Автоматика", prefixes: ["autodry", "auto_ignore"] },
        { id: "park", label: "Парк и настройки", prefixes: ["printer_create", "printer_update", "printer_delete",
          "printer_restore", "printer_maintenance", "printers_", "settings_", "notif_mute", "job_verdict"] },
        { id: "access", label: "Доступ и входы", prefixes: ["login", "logout", "user_", "rate_limit"] }
      ];
      var ACT_SERVICE = ["token_refresh"];   // «служебные» (~90 % of the rows): hidden by default
      var ACT_WHO = { "auto-dry": "автосушка", "auto-ignore": "автопропуск" };
      var ACT_CMD = {
        light_on: "Свет вкл", light_off: "Свет выкл", pause: "Пауза", resume: "Продолжить", stop: "Стоп",
        cooldown: "Остудить", eject: "Опустить стол", speed: "Скорость", fans: "Вентиляторы", preheat: "Нагрев",
        chamber: "Нагрев камеры", ams_load: "Загрузить филамент", ams_unload: "Выгрузить филамент",
        dry_start: "Сушить", dry_stop: "Остановить сушку", reset_errors: "Сбросить ошибки"
      };
      var ACT_NOTIF = {
        resume: "Продолжить печать", ignore: "Игнорировать и продолжить", stop: "Остановить печать",
        ams_done: "AMS: готово", ams_retry: "AMS: повторить", ams_abort: "Прервать операцию AMS",
        clean: "Убрать отметку отмены", idle_ignore: "Не напоминать", buzzer_off: "Выключить сирену",
        dry_stop: "Остановить сушку", refresh_nozzle: "Перепроверить хотэнд", purifier_off: "Выключить очистку воздуха",
        fw_restart: "Перезапуск прошивки", klippy_restart: "Перезапуск Klipper", close_dialog: "Закрыть диалог"
      };
      var ACT_STATUS = {
        confirmed: ["подтверждено", "ok"], sent: ["отправлено", ""], rejected: ["отклонено", "warn"],
        refused: ["отказ", "warn"], failed: ["ошибка", "err"], timeout: ["без ответа", "warn"], skipped: ["пропущен", ""]
      };
      var ACT_PLAIN = {
        login_ok: ["Вход", "ok"], login_fail: ["Неудачный вход", "err"], rate_limit_block: ["Вход заблокирован", "err"],
        logout: ["Выход", ""], token_refresh: ["Обновление токена", ""], user_create: ["Создан пользователь", ""],
        user_update: ["Изменён пользователь", ""], user_delete: ["Удалён пользователь", "warn"],
        printer_create: ["Добавлен принтер", ""], printer_update: ["Изменён принтер", ""],
        printer_delete: ["Удалён принтер", "warn"], printer_restore: ["Возвращён принтер", ""],
        printers_apply: ["Применена конфигурация", "ok"], printers_discard: ["Правки принтеров отменены", ""],
        settings_update: ["Изменены настройки", ""], autodry_config: ["Изменены правила автосушки", ""]
      };
      var ACT_AUTO_SKIP = { maintenance: "принтер на обслуживании", tries_used: "попытки кончились",
                            no_job_id: "нет номера задания" };
      var ACT_UNIT = {
        autodry_run_now: "Сушить сейчас", autodry_start: "Автосушка: старт", autodry_stop: "Стоп сушки",
        autodry_release: "Пауза снята", autodry_disable: "Автосушка: блок отключён"
      };
      function actStarts(action, prefixes) {
        var s = String(action == null ? "" : action);
        return prefixes.some(function (p) { return s.slice(0, p.length) === p; });
      }
      // "svc", a group's id, or "" (an action no group knows: only under «Все»)
      function actGroupOf(action) {
        if (actStarts(action, ACT_SERVICE)) return "svc";
        for (var i = 0; i < ACT_GROUPS.length; i++) if (actStarts(action, ACT_GROUPS[i].prefixes)) return ACT_GROUPS[i].id;
        return "";
      }
      function actGroup(id) {
        for (var i = 0; i < ACT_GROUPS.length; i++) if (ACT_GROUPS[i].id === id) return ACT_GROUPS[i];
        return null;
      }
      function actWho(user) { return user == null || user === "" ? "—" : safeText(ACT_WHO[user] || user, "—"); }
      function actTag(text, cls) { return '<span class="act-tag' + (cls ? " " + cls : "") + '">' + text + "</span>"; }
      // "ams=0,slot=2,st=confirmed" -> {ams: "0", slot: "2", st: "confirmed"}
      function actParams(s) {
        var out = {};
        String(s || "").split(",").forEach(function (kv) {
          var i = kv.indexOf("=");
          if (i > 0) out[kv.slice(0, i)] = kv.slice(i + 1);
        });
        return out;
      }
      // "55/12/PETG" -> "55° · 12 ч · PETG"
      function actPlan(s) {
        var p = String(s || "").split("/");
        return p.length === 3 ? safeText(p[0], "") + "° · " + safeText(p[1], "") + " ч · " + safeText(p[2], "") : safeText(s, "");
      }
      function actUnit(idx) {
        var n = parseInt(idx, 10);
        return isFinite(n) && String(n) === String(idx) ? amsUnitName(n) : safeText(idx, "");
      }
      // An audit action in words: {what (HTML), printer (its stable id or null)}.
      function actText(action) {
        var s = String(action == null ? "" : action), p = s.split(":"), k = p[0], st;
        if (k === "printer_command" && p.length >= 3) {
          var a = p[1], prm = actParams(p.slice(3).join(":"));
          var name = a.indexOf("notif_") === 0 ? ACT_NOTIF[a.slice(6)] || safeText(a.slice(6), "") : ACT_CMD[a] || safeText(a, "");
          var bits = [];
          if (prm.code) bits.push('<span class="mono">' + safeText(prm.code, "") + "</span>");
          if (prm.ams != null) bits.push(actUnit(prm.ams) + (prm.slot != null && isFinite(prm.slot) ? " · слот " + (Number(prm.slot) + 1) : ""));
          if (prm.temp) bits.push(safeText(prm.temp, "") + "°" + (prm.hours ? " · " + safeText(prm.hours, "") + " ч" : "") +
                                  (prm.filament ? " · " + safeText(prm.filament, "") : ""));
          st = ACT_STATUS[prm.st];
          return { printer: p[2] || null,
                   what: name + (bits.length ? " · " + bits.join(" · ") : "") + (prm.bulk ? ' <span class="dim">(группой)</span>' : "") +
                     (prm.st ? " " + actTag(st ? st[0] : safeText(prm.st, ""), st ? st[1] : "") : "") };
        }
        if (k === "printer_bulk" && p.length >= 4) {
          var c = actParams(p.slice(3).join(":")), parts = [];
          [["confirmed", "подтвердили"], ["sent", "отправлено"], ["rejected", "отказ"], ["skipped", "пропущено"]].forEach(function (x) {
            if (Number(c[x[0]]) > 0) parts.push(x[1] + " " + Number(c[x[0]]));
          });
          return { printer: null, what: (ACT_CMD[p[1]] || safeText(p[1], "")) + " — всем" + (parts.length ? " · " + parts.join(" · ") : "") };
        }
        if (k === "printer_maintenance" && p.length >= 3) {
          var m = { on: "Обслуживание: вкл", off: "Обслуживание: снято", note: "Обслуживание: заметка" }[p[2]];
          return { printer: p[1], what: m || "Обслуживание · " + safeText(p[2], "") };
        }
        if (k === "notif_mute" && p.length >= 4) {
          var key = p.slice(2, -1).join(":"), code = key.indexOf(":") >= 0 ? key.slice(key.indexOf(":") + 1) : key;
          return { printer: p[1], what: (p[p.length - 1] === "on" ? "Скрыто уведомление" : "Возвращено уведомление") +
            ' <span class="mono">' + safeText(code, "") + "</span>" };
        }
        if (ACT_UNIT[k] && p.length >= 2) {
          var pid = p[1], unit = p[2];
          if (p.length === 2 && pid.indexOf("__") > 0) { unit = pid.slice(pid.lastIndexOf("__") + 2); pid = pid.slice(0, pid.lastIndexOf("__")); }
          return { printer: pid, what: ACT_UNIT[k] + (unit != null ? " · " + actUnit(unit) : "") +
            (p.length >= 4 ? " · " + actPlan(p.slice(3).join(":")) : "") };
        }
        if (k === "auto_ignore" && p.length >= 4) {
          var head = 'Автопропуск <span class="mono">' + safeText(p[2], "") + "</span> · ";
          if (p[3] === "skip") {
            var why = p.slice(4).join(":");
            return { printer: p[1], what: head + actTag("не нажал: " + (ACT_AUTO_SKIP[why] ||
              (why.indexOf("state_") === 0 ? "принтер не на паузе" : safeText(why, "—"))), "warn") };
          }
          st = ACT_STATUS[p[3]];
          return { printer: p[1], what: head + (p[3] === "confirmed" ? actTag("нажал ✓", "ok") : actTag(st ? st[0] : safeText(p[3], ""), st ? st[1] : "")) };
        }
        if (k === "job_verdict" && p.length >= 2) return { printer: null, what: "Оценка печати №" + safeText(p[1], "") };
        var plain = ACT_PLAIN[s];
        if (plain) return { printer: null, what: plain[1] ? actTag(plain[0], plain[1]) : plain[0] };
        return { printer: null, what: '<span class="mono">' + safeText(s, "—") + "</span>" };
      }
      // The counts of a window ([{user, key, n}]) for the chips: {all, svc, cmd, auto, park, access}
      // — of one user when who is set (a «Кто» choice); all: every row but the service ones.
      function actCounts(rows, who) {
        var c = { all: 0, svc: 0 };
        ACT_GROUPS.forEach(function (g) { c[g.id] = 0; });
        (rows || []).forEach(function (r) {
          if (!r || (who && r.user !== who)) return;
          var n = Number(r.n) || 0, g = actGroupOf(r.key);
          if (g === "svc") { c.svc += n; return; }
          c.all += n;
          if (g) c[g] += n;
        });
        return c;
      }
      // How many rows the list of a filter has (its «показано N из M»).
      function actTotal(c, f) {
        if (!c) return null;
        return (f.group ? c[f.group] || 0 : c.all) + (f.svc ? c.svc : 0);
      }
      // The «Кто» choice: the users of the window by name (a chosen one kept), the automation in words.
      function actWhoOptionsHtml(rows, who) {
        var seen = {}, users = [];
        (rows || []).forEach(function (r) {
          if (r && r.user != null && r.user !== "" && !seen[r.user]) { seen[r.user] = true; users.push(r.user); }
        });
        if (who && !seen[who]) users.push(who);
        users.sort(function (a, b) { return String(a).localeCompare(String(b), "ru", { numeric: true }); });
        return '<option value="">все</option>' + users.slice(0, 50).map(function (u) {
          return '<option value="' + safeText(u, "") + '"' + (u === who ? " selected" : "") + ">" + actWho(u) + "</option>";
        }).join("");
      }
      // The chips (as the farm's and the journal's, .farm-chip): «Все» and the groups with their counts;
      // one group at a time, the chosen one with «×».
      function actChipsHtml(c, f) {
        var chips = [{ id: "", label: "Все", n: c ? c.all + (f.svc ? c.svc : 0) : null }].concat(ACT_GROUPS.map(function (g) {
          return { id: g.id, label: g.label, n: c ? c[g.id] : null };
        }));
        return chips.map(function (x) {
          var on = (f.group || "") === x.id;
          var tip = !x.id ? "Все действия периода" : on ? "Повторный клик — все действия" : "Показать только «" + x.label + "»";
          return '<button type="button" class="farm-chip' + (x.n === 0 ? " zero" : "") + (on ? " on" : "") + '" data-act-group="' + x.id +
            '" aria-pressed="' + (on ? "true" : "false") + '" data-tip="' + tip + '">' + x.label + (x.n == null ? "" : " <b>" + x.n + "</b>") +
            (on && x.id ? '<span class="farm-x" aria-hidden="true">×</span>' : "") + "</button>";
        }).join("");
      }
      // «Служебные N · скрыты» and its tip (their share of the window's rows).
      function actSvcHtml(c, shown) {
        var n = c ? c.svc : null, all = c ? c.all + c.svc : 0;
        return { text: "Служебные" + (n == null ? "" : " " + n) + " · " + (shown ? "показаны" : "скрыты"),
                 tip: "«Обновление токена»" + (all ? " — " + Math.round(n / all * 100) + " % строк за период" : "") +
                   (shown ? ". Клик — скрыть" : ". Клик — показать") };
      }
      // A group click: only this group; the chosen one again, or «Все» — all.
      function actGroupClick(cur, id) { return !id || id === cur || !actGroup(id) ? "" : id; }
      // The list request: the head's window and printer, the chip (its prefixes, «служебные» with them
      // when shown), «Кто»; without a chip the service rows are excluded unless shown.
      function actsQuery(f, w, printer, offset) {
        var q = ["limit=" + AUDIT_LIMIT, "offset=" + (offset || 0), "fr=" + Math.floor(w.fr), "to=" + Math.ceil(w.to)];
        if (printer) q.push("printer=" + encodeURIComponent(printer));
        if (f.who) q.push("user=" + encodeURIComponent(f.who));
        var g = actGroup(f.group);
        if (g) q.push("prefix=" + g.prefixes.concat(f.svc ? ACT_SERVICE : []).join(","));
        else if (!f.svc) q.push("exclude=" + ACT_SERVICE.join(","));
        return q.join("&");
      }
      function actsCountsQuery(w, printer) {
        return "fr=" + Math.floor(w.fr) + "&to=" + Math.ceil(w.to) + (printer ? "&printer=" + encodeURIComponent(printer) : "");
      }
      // «⇩ Выгрузить» on «Действия»: the list's filter without the paging.
      function actsCsvUrl(f, w, printer) {
        return "/api/admin/audit.csv?" + actsQuery(f, w, printer, 0).replace(/^limit=\d+&offset=\d+&/, "");
      }
      // One row (and its unfolded details: the IP, the whole browser line, the raw action).
      function actRowHtml(e, labels, open) {
        var t = actText(e.action), dev = uaDevice(e.user_agent), id = Number(e.id) || 0;
        var pr = t.printer ? safeText((labels && labels[t.printer]) || t.printer, "") : "";
        var row = '<tr class="act-row' + (open ? " open" : "") + '" data-act-id="' + id + '" tabindex="0" aria-expanded="' + (open ? "true" : "false") + '">' +
          '<td class="act-t mono">' + fmtAuditTime(e.created_at) + "</td>" +
          '<td class="act-who">' + actWho(e.user_id) + "</td>" +
          '<td class="act-what">' + t.what + "</td>" +
          '<td class="act-pr">' + pr + "</td>" +
          '<td class="act-dev">' + safeText(dev || e.ip_address || "—", "—") + "</td></tr>";
        if (!open) return row;
        return row + '<tr class="act-xrow" data-xfor="' + id + '"><td colspan="5"><div class="act-x">' +
          "<span>IP <b class=\"mono\">" + safeText(e.ip_address, "—") + "</b></span>" +
          "<span>Браузер " + safeText(e.user_agent, "—") + "</span>" +
          '<span>Запись <b class="mono">' + safeText(e.action, "—") + "</b></span></div></td></tr>";
      }
      /* acts-ui:end */
      /* hist-ui:begin */
      // «История» (IA §7): the one head of every sub-tab — the period, «Свой…» and the
      // printer, kept in the address (#/history/<sub>?period=7d&printer=<id>) — and the
      // merged «Статистика»: one row per printer with the time shares of
      // /api/history/states-summary and the prints of /api/history/jobs/stats. Pure helpers
      // only (no DOM, no globals besides safeText, timeFmt and the jobs-ui block), run in
      // node by tests/test_web_hist_ui.py. Times are epoch seconds, days "YYYY-MM-DD" in
      // browser time; the returned texts are HTML unless said otherwise.
      var HIST_SUBS = ["jobs", "chrono", "stats", "actions"];   // the first: a fresh tab's
      var HIST_PERIODS = [
        { id: "24h", label: "24 ч", days: 1 }, { id: "7d", label: "7 дн", days: 7 },
        { id: "30d", label: "30 дн", days: 30 }, { id: "90d", label: "90 дн", days: 90 }
      ];
      var HIST_DEFAULT_PERIOD = "24h";      // «что было за ночь»
      var HIST_CHRONO_DAYS = 7;             // «Хронология» draws a week at most
      // forge-history-sub (the old switch's sub-view) → today's sub: read once, then removed
      var HIST_SUB_FROM_SAVED = { timeline: "chrono", jobs: "jobs" };
      // The one dictionary of states (IA §10) in the bars' order. «Нет данных»: a gap in
      // the history (and «unknown»), only in «Хронология» and «Статистика».
      var HIST_SEGS = [
        { id: "printing", label: "Печатает" }, { id: "idle", label: "Простой" }, { id: "paused", label: "Пауза" },
        { id: "error", label: "Ошибка" }, { id: "cancelled", label: "Отменено" }, { id: "finished", label: "Завершено" },
        { id: "offline", label: "Офлайн" }, { id: "maintenance", label: "Обслуживание" }, { id: "nodata", label: "Нет данных" }
      ];
      function histOwn(o, k) { return !!o && typeof k === "string" && Object.prototype.hasOwnProperty.call(o, k); }
      function histPeriodOf(id) {
        return HIST_PERIODS.filter(function (p) { return p.id === id; })[0] || null;
      }
      function hbPad(n) { return (n < 10 ? "0" : "") + n; }
      // "2026-10-21" → its local midnight (plusDays later), null when it is no real day
      function histDayStart(day, plusDays) {
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof day === "string" ? day : "");
        if (!m) return null;
        var y = +m[1], mo = +m[2] - 1, d = +m[3], t = new Date(y, mo, d);
        if (t.getFullYear() !== y || t.getMonth() !== mo || t.getDate() !== d) return null;
        return new Date(y, mo, d + (plusDays || 0)).getTime() / 1000;
      }
      function histDayOf(ts) {
        var d = new Date(ts * 1000);
        return d.getFullYear() + "-" + hbPad(d.getMonth() + 1) + "-" + hbPad(d.getDate());
      }
      // The head's filter {period, from, to, printer} from the address' query (or a filter):
      // what it does not know falls back — 24 ч, all printers; «Свой…» wants both days, С ≤ По.
      function histFilterFrom(q) {
        q = q || {};
        var f = { period: HIST_DEFAULT_PERIOD, from: null, to: null, printer: null };
        if (histOwn(q, "printer") && typeof q.printer === "string" && q.printer !== "") f.printer = q.printer;
        var period = histOwn(q, "period") ? q.period : null;
        if (period === "custom") {
          var a = histDayStart(q.from), b = histDayStart(q.to);
          if (a != null && b != null && a <= b) { f.period = "custom"; f.from = q.from; f.to = q.to; }
        } else if (histPeriodOf(period)) {
          f.period = period;
        }
        return f;
      }
      // ... and back: the address' query, the defaults left out (#/history/jobs is 24 ч, all printers)
      function histQueryOf(f) {
        f = histFilterFrom(f);
        var q = {};
        if (f.period === "custom") { q.period = "custom"; q.from = f.from; q.to = f.to; }
        else if (f.period !== HIST_DEFAULT_PERIOD) q.period = f.period;
        if (f.printer) q.printer = f.printer;
        return q;
      }
      function histSameFilter(a, b) { return JSON.stringify(histQueryOf(a)) === JSON.stringify(histQueryOf(b)); }
      // The filter's window {fr, to} at `now`: the last N days, or «Свой…» from the start
      // of its first day to the end of its last one, never past now.
      function histWindow(f, now) {
        f = histFilterFrom(f);
        if (f.period === "custom") {
          var to = Math.min(histDayStart(f.to, 1), now);
          return { fr: Math.floor(Math.min(histDayStart(f.from), to)), to: Math.ceil(to) };
        }
        return { fr: Math.floor(now - histPeriodOf(f.period).days * 86400), to: Math.ceil(now) };
      }
      // «Статистика»'s time shares (states-summary — a window scan on the Pi, ~17 s for 30 дн): the
      // window in whole minutes (a repeat asks the same and the server's short cache serves it) and
      // its key — the period only: the shares do not depend on the printer, the rows filter them.
      function histSummaryWindow(f, now) {
        f = histFilterFrom(f);
        var w = histWindow(f, now);
        return { fr: Math.floor(w.fr / 60) * 60, to: Math.floor(w.to / 60) * 60,
                 key: JSON.stringify(histQueryOf({ period: f.period, from: f.from, to: f.to, printer: null })) };
      }
      // «Хронология»: the window's last week at most {fr, to, clipped, days (of the whole window)};
      // «7 дн» itself is a week (its rounding to whole seconds is no clipping)
      function histChronoWindow(f, now) {
        var w = histWindow(f, now), max = HIST_CHRONO_DAYS * 86400;
        var days = Math.max(1, Math.round((w.to - w.fr) / 86400));
        return w.to - w.fr > max + 60 ? { fr: w.to - max, to: w.to, clipped: true, days: days }
          : { fr: w.fr, to: w.to, clipped: false, days: days };
      }
      // Plain text (textContent): "24 ч" | "7 дн" | "21.10–28.10" | "21.10" (a single day)
      function histPeriodText(f) {
        f = histFilterFrom(f);
        if (f.period !== "custom") return histPeriodOf(f.period).label;
        function dm(day) { return day.slice(8, 10) + (typeof I18N === "object" ? I18N.dateSep() : ".") + day.slice(5, 7); }
        return f.from === f.to ? dm(f.from) : dm(f.from) + "–" + dm(f.to);
      }
      // The loaders' text: «Считаем за 7 дн…» (plain text)
      function histLoadingText(f) { return "Считаем за " + histPeriodText(f) + "…"; }
      // A phone's folded head: «24 ч · все принтеры» (plain text)
      function histSummaryText(f, printerLabel) {
        f = histFilterFrom(f);
        return histPeriodText(f) + " · " + (f.printer ? String(printerLabel || f.printer) : "все принтеры");
      }
      // Over «Хронология» when it shows less than the period: plain text, "" otherwise
      function histChronoNote(cw) {
        return cw && cw.clipped ? "Хронология — до " + HIST_CHRONO_DAYS + " дн: показаны последние " + HIST_CHRONO_DAYS +
          " из " + cw.days + "." : "";
      }
      // The legend of «Хронология» and «Статистика»: one, from the dictionary
      function histLegendHtml() {
        return HIST_SEGS.map(function (s) { return '<span><i class="seg-' + s.id + '"></i>' + s.label + '</span>'; }).join("");
      }
      // A printer's seconds per state (a states-summary row's states) → the bar's parts in the
      // legend's order: {segs: [{id, label, sec, pct}], total, printing, known, maint}; unknown
      // counts as «Нет данных». known: the time with data and not in maintenance mode — «Печать»
      // and «Загрузка» are shares of it (a history gap is no idle time, maintenance no work).
      function histShares(states) {
        states = states || {};
        var sec = {}, total = 0, segs = [];
        HIST_SEGS.forEach(function (s) { sec[s.id] = Math.max(0, Number(states[s.id]) || 0); });
        sec.nodata += Math.max(0, Number(states.unknown) || 0);
        HIST_SEGS.forEach(function (s) { total += sec[s.id]; });
        HIST_SEGS.forEach(function (s) {
          if (total > 0 && sec[s.id] > 0) segs.push({ id: s.id, label: s.label, sec: sec[s.id], pct: sec[s.id] / total * 100 });
        });
        return { segs: segs, total: total, printing: sec.printing, known: total - sec.nodata - sec.maintenance,
                 maint: sec.maintenance };
      }
      function histPctText(pct) { return pct > 0 && pct < 1 ? "<1%" : Math.round(pct) + "%"; }
      function histBarHtml(sh) {
        return '<span class="dash-stats-bar">' + sh.segs.map(function (g) {
          var t = timeFmt(g.sec);
          return '<i class="dash-seg seg-' + g.id + '" style="width:' + g.pct.toFixed(2) + '%" data-tip="' + g.label + " · " +
            (t === "—" ? "< 1м" : t) + " (" + histPctText(g.pct) + ')"></i>';
        }).join("") + '</span>';
      }
      // «Загрузка»: the printing share of the known time over the rows (pid: one printer's),
      // whole percent; null without known time
      function histUtil(ss, pid) {
        var printing = 0, known = 0;
        (ss || []).forEach(function (r) {
          if (!r || (pid && r.printer_id !== pid)) return;
          var sh = histShares(r.states);
          printing += sh.printing;
          known += Math.max(0, sh.known);
        });
        return known > 0 ? Math.round(printing / known * 100) : null;
      }
      // One row per printer, by name (P2 before P10; IA §10): the live printers (one without
      // data in the window has its row too), the states-summary rows (ss: null while it loads)
      // and the journal's (st.printers); pid: that printer only.
      // -> [{id, label, model, maint, states (null: none), jobs (null: no prints)}]
      function histStatsRows(ss, st, printers, pid) {
        var by = {}, order = [];
        function row(id) {
          id = String(id);
          if (!histOwn(by, id)) {
            by[id] = { id: id, label: "", model: "", maint: false, states: null, jobs: null };
            order.push(id);
          }
          return by[id];
        }
        (printers || []).forEach(function (p) {
          if (!p || p.id == null) return;
          var r = row(p.id);
          r.label = p.label ? String(p.label) : r.label;
          r.model = p.model ? String(p.model) : r.model;
          r.maint = !!p.maintenance;
        });
        (ss || []).forEach(function (x) {
          if (!x || x.printer_id == null) return;
          var r = row(x.printer_id);
          r.label = r.label || (x.label ? String(x.label) : "");
          r.maint = r.maint || !!x.maintenance;
          r.states = x.states || {};
        });
        ((st && st.printers) || []).forEach(function (j) {
          if (!j || j.printer_id == null) return;
          var r = row(j.printer_id);
          r.label = r.label || (j.label ? String(j.label) : "");
          r.model = r.model || (j.model ? String(j.model) : "");
          r.jobs = j;
        });
        var rows = order.map(function (id) { var r = by[id]; if (!r.label) r.label = r.id; return r; })
          .filter(function (r) { return !pid || r.id === String(pid); });
        rows.sort(function (a, b) {
          return a.label.localeCompare(b.label, "ru", { numeric: true }) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
        });
        return rows;
      }
      // A table row, <tr data-pid> (a click picks the printer); the cells' data-l is their label
      // on a phone. ssLoaded: false while states-summary is on its way — the bar waits, «…».
      function histStatsRowHtml(r, ssLoaded) {
        var j = r.jobs || {}, jobs = Number(j.jobs) || 0, accN = Number(j.acc_n) || 0;
        var sh = histShares(r.states), print = "…", printTip = "";
        if (ssLoaded) {
          print = sh.known > 0 ? histPctText(sh.printing / sh.known * 100) : "—";
          printTip = sh.known <= 0 && sh.maint > 0 ? "всё окно — на обслуживании"
            : sh.known > 0 ? "доля «Печатает» во времени с данными, без обслуживания" : "нет данных за период";
        }
        var nameTip = (r.model ? safeText(r.model, "") + " · " : "") + "печатей: " + jobs +
          (j.avg_sec ? " · в среднем " + timeFmt(j.avg_sec) : "");
        var ok = j.success_pct == null ? "—" : Math.round(j.success_pct) + "%";
        var top = j.top_code
          ? '<td class="hs-top" data-l="причина" data-tip="' + jobReasonText(j.top_code, j.top_code_text) + '">' + safeText(j.top_code, "") + '</td>'
          : '<td class="hs-top" data-l="причина">—</td>';
        return '<tr data-pid="' + safeText(r.id, "") + '">' +
          '<td class="hs-pr" data-tip="' + nameTip + '">' + safeText(r.label, "—") +
          (r.maint ? ' <span class="hs-maint">обсл.</span>' : '') + '</td>' +
          '<td class="hs-bar">' + (ssLoaded ? histBarHtml(sh) : '<span class="dash-stats-bar hs-wait"></span>') + '</td>' +
          '<td class="hs-num hs-print" data-l="печать"' + (printTip ? ' data-tip="' + printTip + '"' : '') + '>' + print + '</td>' +
          '<td class="hs-num hs-jobs">' + jobs + ' <span class="hs-dim">печ.</span></td>' +
          '<td class="hs-num hs-ok"' + (jobs ? ' data-tip="завершённые среди закончившихся"' : '') + '>' + ok + '</td>' +
          '<td class="hs-num hs-acc" data-l="факт/план"' + (accN ? ' data-tip="медиана по завершённым с прогнозом, печатей: ' + accN + '"' : '') +
          '>' + (accN ? "×" + jobNum(j.acc_median, 2) : "—") + '</td>' + top + '</tr>';
      }
      // The totals over the window (the prints in maintenance mode left out, as /jobs/stats
      // counts them). «Печатей» and «Успешно» open «Печати» with that filter (data-kpi);
      // util: histUtil's, undefined while states-summary loads.
      function histKpiHtml(st, util) {
        var t = (st && st.total) || {};
        function tile(kpi, label, value, tip) {
          var btn = kpi != null;
          return (btn ? '<button type="button" class="hist-kpi" data-kpi="' + kpi + '"' : '<div class="hist-kpi static"') +
            (tip ? ' data-tip="' + tip + '"' : '') + '><span>' + label + '</span><b>' + value + '</b>' + (btn ? '</button>' : '</div>');
        }
        var open = Number(t.open) || 0, maint = Number(t.maint) || 0, accN = Number(t.acc_n) || 0, hours = Number(t.print_hours) || 0;
        var tips = ["открыть «Печати» за период"].concat(open ? ["из них идут: " + open] : [],
          maint ? ["без " + maint + " в режиме обслуживания — они есть в «Печатях», но не в статистике"] : []);
        return tile("", "Печатей", String(Number(t.jobs) || 0), tips.join("; ")) +
          tile("finished", "Успешно", t.success_pct == null ? "—" : Math.round(t.success_pct) + "%",
            "открыть успешные; завершённые среди закончившихся, «Нет связи» и идущие не считаются") +
          tile(null, "Часы печати", jobNum(hours, hours >= 100 ? 0 : 1), "без пауз, по закончившимся печатям") +
          tile(null, "Загрузка", util === undefined ? "…" : util == null ? "—" : util + "%",
            "доля «Печатает» во времени с данными, без обслуживания") +
          tile(null, "Факт / план", accN ? "×" + jobNum(t.acc_median, 2) : "—", accN
            ? "медиана по завершённым с прогнозом принтера, печатей: " + accN + "; p90 ×" + jobNum(t.acc_p90, 2)
            : "нет завершённых печатей с прогнозом и точным временем");
      }
      /* hist-ui:end */
      /* ctl-ui:begin */
      // Card control row, ⋯ menu, command results and adaptive chip rows: pure
      // helpers only (no DOM, no globals besides safeText, icon, amsUnitName,
      // amsFmtMin, isOffline and the ams-ui block), run in node by tests/test_web_ctl_ui.py.
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
      // The card's own answer under its row (not a global toast). dismissAttr:
      // the × button's attribute (default data-cmd="dismiss"; the notification
      // lists and the card's notification line have handlers of their own).
      function cmdLineHtml(line, dismissAttr) {
        if (!line) return "";
        var lead = line.k === "wait" ? '<span class="spin-sm"></span>' : line.k === "ok" ? "✓"
          : line.k === "bad" ? "✕" : line.k === "unk" ? "?" : "ⓘ";
        return '<div class="pc-cmd ' + line.k + '" role="status"><span class="lead">' + lead + '</span><span class="t">'
          + safeText(line.t, "") + '</span>'
          + (line.k === "bad" ? '<button type="button" class="x" ' + (dismissAttr || 'data-cmd="dismiss"') + ' aria-label="Закрыть">×</button>' : "")
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
      // eject — each only where its cap is true; state blocks it. No 🧵 (the AMS
      // chip opens the AMS popover) and no ⋯ (the card's one ⋯ is in its head):
      // five buttons at most (IA §5).
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
        return head + b.join("") + '</div>' + cmdLineHtml(ui.line);
      }
      // The card's one ⋯, in its head on every card and for both roles (IA §5): never
      // greyed out — the menu sends no printer command. opts: {admin}.
      function headMoreHtml(p, ui, opts) {
        ui = ui || {};
        var tip = opts && opts.admin ? "Обслуживание и ссылки: печати, хронология, параметры" : "Печати и хронология этого принтера";
        return ctlBtnHtml("more", "more", "dots", tip, null, ui, "pc-ctl-head" + (ui.pop === "menu" ? " open" : ""));
      }
      // The ⋯ menu (IA §5): an admin's maintenance section, then the links — «Печати
      // принтера ›», «Хронология принтера ›» (both roles) and «Параметры принтера ›» (an
      // admin's: Админ → Парк, its form open). Cooldown, eject and the print speed are the
      // row's ❄ / ⤓ buttons and the speed chip — the menu never repeats them (owner, 19.10).
      function ctlMenuHtml(p, ui, now, opts) {
        var admin = !!(opts && opts.admin);
        return (admin ? maintMenuHtml(p) + '<div class="pm-sep"></div>' : "")
          + '<button type="button" class="pm-item" data-menu="jobs">' + icon("file") + '<span class="lbl">Печати принтера ›</span></button>'
          + '<button type="button" class="pm-item" data-menu="chrono">' + icon("clock") + '<span class="lbl">Хронология принтера ›</span></button>'
          + (admin ? '<button type="button" class="pm-item" data-menu="params">' + icon("tool")
            + '<span class="lbl">Параметры принтера ›<span class="sub">Админ → Парк, откроется форма</span></span></button>' : "");
      }
      // The ⋯ menu's maintenance section (spec_maintenance §6.3); «…» opens maintPopHtml.
      function maintMenuHtml(p) {
        if (!inMaint(p)) {
          return '<button type="button" class="pm-item" data-menu="maint-open">' + icon("tool")
            + '<span class="lbl">На обслуживание…<span class="sub">убрать из сводки, уведомлений и групповых команд</span></span></button>';
        }
        return '<button type="button" class="pm-item" data-menu="maint-open">' + icon("tool")
          + '<span class="lbl">Заметка обслуживания…</span></button>'
          + '<button type="button" class="pm-item" data-menu="maint-off"><span class="pc-ic pm-check" aria-hidden="true">✓</span>'
          + '<span class="lbl">Снять с обслуживания</span></button>';
      }
      // «Снять с обслуживания» asked in the ⋯ popover itself (IA §10: no native dialog): what
      // changes, «Снять» (data-menu="maint-off-yes") and «Отмена» back to the menu.
      function maintOffAskHtml(p) {
        return '<div class="pc-pop-ask" role="group" aria-label="Подтверждение"><div class="pc-pop-note ask">Снять «'
          + safeText(p && p.label, "") + '» с обслуживания? Принтер вернётся в сетку, уведомления и групповые команды.</div>'
          + '<div class="pc-pop-row"><button type="button" class="pc-pop-preset" data-menu="back">Отмена</button>'
          + '<button type="button" class="pc-pop-ok" data-menu="maint-off-yes">Снять</button></div></div>';
      }
      // The maintenance form in the same popover: an optional one-line note
      // (the server folds line breaks, keeps 200 characters at most).
      function maintPopHtml(p) {
        var m = p && p.maintenance;
        return '<div class="pc-pop-h">' + (m ? "Заметка обслуживания" : "На обслуживание") + '</div>'
          + '<div class="pc-pop-row"><input type="text" class="pc-pop-in" maxlength="200" placeholder="что делаем (необязательно)"'
          + ' aria-label="Заметка обслуживания" value="' + safeText(m && m.note, "") + '">'
          + '<button type="button" class="pc-pop-ok" data-maint-save>' + (m ? "Сохранить" : "Поставить") + '</button></div>';
      }
      function maint2(n) {
        return (n < 10 ? "0" : "") + n;
      }
      // Since when (m.since: unix seconds; nowMs: Date.now(), local time):
      // today «с 14:32», this year «с 18.10», earlier «с 18.10.2025».
      function maintSinceText(since, nowMs) {
        var d = new Date(Number(since) * 1000), n = new Date(nowMs);
        if (since == null || !isFinite(d.getTime())) return "";
        if (d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()) {
          return "с " + maint2(d.getHours()) + ":" + maint2(d.getMinutes());
        }
        var ds = (typeof I18N === "object" ? I18N.dateSep() : ".");
        return "с " + maint2(d.getDate()) + ds + maint2(d.getMonth() + 1) + (d.getFullYear() === n.getFullYear() ? "" : ds + d.getFullYear());
      }
      // The whole story in plain text (the plaque's tooltip, the admin table's badge):
      // «На обслуживании с 18.10.2026 14:32 · поставил admin · меняю хотенд».
      function maintTipText(m) {
        if (!m) return "";
        var d = new Date(Number(m.since) * 1000);
        var ds = (typeof I18N === "object" ? I18N.dateSep() : ".");
        var t = "На обслуживании" + (m.since != null && isFinite(d.getTime()) ? " с " + maint2(d.getDate()) + ds + maint2(d.getMonth() + 1)
          + ds + d.getFullYear() + " " + maint2(d.getHours()) + ":" + maint2(d.getMinutes()) : "");
        if (m.by) t += " · поставил " + m.by;
        if (m.note) t += " · " + m.note;
        return t;
      }
      // The card's dim maintenance line under its header (the status badge stays the
      // real one): «обслуживание · с 19.10 · заметка», the whole story in its tooltip.
      // No «снять» on it: maintenance mode is taken off through the card's ⋯ (IA §5,
      // one way to do it) — the tooltip says so to an admin.
      function maintPlaqueHtml(p, admin, nowMs) {
        var m = p && p.maintenance;
        if (!m) return "";
        var since = maintSinceText(m.since, nowMs);
        var tip = maintTipText(m) + (admin ? " · снять — в меню ⋯" : "");
        return '<div class="pc-maint">' + icon("tool") + '<span class="t" data-tip="' + safeText(tip, "") + '">обслуживание'
          + (since ? " · " + since : "") + (m.note ? " · " + safeText(m.note, "") : "") + '</span></div>';
      }
      // The model in the card's head, always (IA §5: «P3 · P2S», it used to go with an
      // AMS): the printer form's names for Creality, else the reported type in capitals;
      // "" when it only repeats the name (a Klipper or MKS printer is named after its model).
      var CARD_MODEL_NAMES = { k1max: "K1 Max", k1c: "K1C", ender5max: "Ender-5 Max" };   // = the printer form's options
      function cardModelText(p) {
        var raw = String((p && p.device_type) || "").trim();
        var text = CARD_MODEL_NAMES[raw.toLowerCase()] || raw.toUpperCase();
        function norm(v) { return String(v == null ? "" : v).toLowerCase().replace(/[\s_-]+/g, ""); }
        return text && norm(text) !== norm(p && p.label) ? text : "";
      }
      // A printer without an AMS with filament in its extruder: the external spool's chip
      // in the AMS chip's place (editable: an admin's live card that takes AMS commands)
      // opens its popover — the row's 🧵, which did that, is gone (IA §5).
      function extSpoolChip(p, editable) {
        if (!editable || amsUnits(p).length || p.filament_loaded !== true) return "";
        var tip = externalUnloadOk(p) ? "Внешняя катушка: выгрузка" : "Внешняя катушка: " + (filamentOpsBlock(p) || "выгрузка недоступна");
        return '<span class="pc-chip pc-ext pc-editable" data-edit="ext" data-tip="' + safeText(tip, "") + '">' + icon("spool")
          + 'внешняя катушка</span>';
      }
      // The external spool's popover (its chip on a printer without an AMS): the unload.
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
      // A pause on an error (a "pause" notification) is the printer's whatever
      // the stage says: a plain resume after such a dialog pauses it again.
      function resumeHold(p) {
        if (resumeErrCode(p)) return "printer";
        var s = String(p.stage || "").toLowerCase();
        if (!s || s === "paused" || s === "paused_user") return null;
        if (s === "paused_user_gcode" || s === "m400_pause") return "gcode";
        return s.indexOf("paused_") === 0 ? "printer" : null;
      }
      // The code of the printer's pause on an error (p.notifications), or null.
      function resumeErrCode(p) {
        var n = (p && Array.isArray(p.notifications) ? p.notifications : []).filter(function (x) { return x && x.severity === "pause"; })[0];
        return n ? String(n.code || "") || null : null;
      }
      // "пауза принтером: закончился филамент" (stageText: the card's stage label;
      // errCode: resumeErrCode, its own text = notification_actions RESET_PAUSE; plain text).
      function resumeHoldText(code, stageText, errCode) {
        if (code === "gcode") return RESUME_HOLD.gcode;
        if (code !== "printer") return "";
        if (errCode) return "пауза-ошибка " + errCode + " — решается кнопками в уведомлении";
        var t = String(stageText || "").replace(/^Пауза(:\s*|\s+)/, "");
        return RESUME_HOLD.printer + (t ? ": " + t.charAt(0).toLowerCase() + t.slice(1) : "");
      }
      // An admin keeps the printer in maintenance mode (p.maintenance: the
      // /api/printers object, null otherwise): no fleet counts, bulk actions or
      // notifications for it; single commands on its card still work.
      function inMaint(p) {
        return !!(p && p.maintenance);
      }
      // Why a bulk action leaves this printer out (lower case), or null — the
      // mirror of fleet_commands.skip_reason on the server.
      function bulkSkipReason(action, p) {
        var cap = action === "light_on" || action === "light_off" ? "light" : action;
        var c = ctlCaps(p)[cap];
        if (inMaint(p)) return "на обслуживании";        // first, as on the server: not «офлайн»
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
      // Lights over the printers whose light the dashboard can switch (not in maintenance).
      function fleetLightStats(printers) {
        var s = { total: 0, lit: 0, offline: 0 };
        (printers || []).forEach(function (p) {
          if (ctlCaps(p).light !== true || inMaint(p)) return;
          s.total++;
          if (!p.online || p.state === "offline") s.offline++;
          else if (p.light_on === true) s.lit++;
        });
        return s;
      }
      // --- «Принтеры»: the farm's row over the grid (IA §5, plan step 6) ---
      // The one dictionary of states (IA §10), in the chips' order; short: a phone's label.
      var FLEET_CHIPS = [
        { id: "printing", label: "Печатает" }, { id: "paused", label: "Пауза" }, { id: "error", label: "Ошибка" },
        { id: "finished", label: "Завершено" }, { id: "cancelled", label: "Отменено" }, { id: "idle", label: "Простой" },
        { id: "offline", label: "Офлайн" }, { id: "maintenance", label: "Обслуживание", short: "Обсл." }
      ];
      function fleetChipKnown(id) {
        return FLEET_CHIPS.some(function (c) { return c.id === id; });
      }
      // A printer's chip: maintenance mode first (any state), then offline (isOffline, as
      // its card), else its state; a state without a word (unknown, with telemetry) — «Простой».
      function fleetBucket(p) {
        if (inMaint(p)) return "maintenance";
        if (isOffline(p)) return "offline";
        return p.state !== "maintenance" && fleetChipKnown(p.state) ? p.state : "idle";
      }
      function fleetCounts(printers) {
        var c = {};
        FLEET_CHIPS.forEach(function (x) { c[x.id] = 0; });
        (printers || []).forEach(function (p) { c[fleetBucket(p)]++; });
        return c;
      }
      // The saved choice (forge-ops-state-filters, any version) as today's chips: «Онлайн»
      // is gone (IA §11), the rest is taken as it is; unknown and repeated values dropped.
      // old: written by the old «Фильтры ▾», whose «Простаивают» (idle) also held the finished,
      // the cancelled and the unknown — now «Простой», «Завершено» and «Отменено» (unknown is «Простой»).
      function fleetFiltersFrom(saved, old) {
        var out = [];
        (Array.isArray(saved) ? saved : []).forEach(function (v) {
          (old && v === "idle" ? ["idle", "finished", "cancelled"] : [v]).forEach(function (x) {
            if (typeof x === "string" && fleetChipKnown(x) && out.indexOf(x) < 0) out.push(x);
          });
        });
        return out;
      }
      // A chip's click: only this state; the same single chip again — everyone; add (Ctrl): in or out.
      function fleetFilterClick(filter, id, add) {
        var f = (filter || []).slice(), i = f.indexOf(id);
        if (!fleetChipKnown(id)) return f;
        if (add) {
          if (i >= 0) f.splice(i, 1); else f.push(id);
          return f;
        }
        return f.length === 1 && i === 0 ? [] : [id];
      }
      // The chips are the filters (IA §5): every one stays, a zero one dim («Пауза 0» is an
      // answer too), the chosen ones with ×; on a phone only the non-zero ones (CSS).
      function fleetChipsHtml(counts, filter) {
        filter = filter || [];
        return FLEET_CHIPS.map(function (c) {
          var n = (counts && counts[c.id]) || 0, on = filter.indexOf(c.id) >= 0;
          var tip = on ? (filter.length > 1 ? "Ctrl+клик — убрать из выбора · клик — только «" + c.label + "»" : "Повторный клик — все принтеры")
            : "Показать только «" + c.label + "» · Ctrl+клик — добавить к выбору";
          return '<button type="button" class="farm-chip st-' + c.id + (n ? "" : " zero") + (on ? " on" : "") + '" data-filter="' + c.id
            + '" aria-pressed="' + (on ? "true" : "false") + '" data-tip="' + tip + '"><i class="farm-dot" aria-hidden="true"></i>'
            + '<span class="fc-l">' + c.label + '</span>' + (c.short ? '<span class="fc-s">' + c.short + '</span>' : "")
            + ' <b>' + n + '</b>' + (on ? '<span class="farm-x" aria-hidden="true">×</span>' : "") + '</button>';
        }).join("");
      }
      // How long each printer has stood in its state — since: {id: {s, t}}: s its state key
      // (fleetStateKey), t the unix seconds it entered it, null: no change during the last
      // day's state-change events (a day and more), undefined: not known yet. From the
      // events (/api/history/events, newest first: fleetSinceEvents) and from the polls
      // (fleetSinceTrack: a change seen now started now).
      var FLEET_DAY_S = 86400;
      function fleetStateKey(p) {
        return isOffline(p) ? "offline" : String((p && p.state) || "unknown");
      }
      function fleetSinceEvents(since, printers, rows, at) {
        var newest = {}, out = {};
        (rows || []).forEach(function (r) {
          var id = r && r.printer_id != null ? String(r.printer_id) : null;
          if (id !== null && !Object.prototype.hasOwnProperty.call(newest, id)) newest[id] = r;
        });
        (printers || []).forEach(function (p) {
          var id = String(p.id), k = fleetStateKey(p), ev = newest[id], had = since ? since[id] : null;
          if (ev && ev.new_state === k && typeof ev.time === "number" && isFinite(ev.time)) out[id] = { s: k, t: ev.time };
          else if (had && had.s === k && typeof had.t === "number") out[id] = { s: k, t: had.t };   // seen here, history not yet
          else if (!ev) out[id] = { s: k, t: null };                                                   // no change all day
          else out[id] = { s: k, t: at };                                                              // changed after its last row
        });
        return out;
      }
      function fleetSinceTrack(since, printers, now) {
        (printers || []).forEach(function (p) {
          var id = String(p.id), k = fleetStateKey(p), had = since[id];
          if (!had) since[id] = { s: k, t: undefined };
          else if (had.s !== k) since[id] = { s: k, t: now };
        });
        return since;
      }
      // Seconds in the present state: Infinity — a day and more, null — not known.
      function fleetSinceAge(since, p, now) {
        var x = since ? since[String(p.id)] : null;
        if (!x || x.s !== fleetStateKey(p)) return null;
        if (x.t === null) return Infinity;
        return typeof x.t === "number" && isFinite(x.t) ? Math.max(0, now - x.t) : null;
      }
      // «1м» (at least), «40м», «2ч 5м», «3 дн» (from two days on); Infinity — «> 1 дн».
      function fleetDur(sec) {
        if (sec === Infinity) return "> 1 дн";
        if (sec == null || !isFinite(sec)) return "";
        var mins = Math.max(1, Math.round(sec / 60)), h = Math.floor(mins / 60), m = mins % 60;
        if (h >= 48) return Math.floor(h / 24) + " дн";
        return (h ? h + "ч" + (m ? " " : "") : "") + (m || !h ? m + "м" : "");
      }
      // Not at work (IA §5): maintenance mode (any state) or offline for a day and more.
      // A fresh drop is a signal and stays in the grid, so is one not known yet.
      function fleetNotWorking(p, since, now) {
        if (inMaint(p)) return true;
        if (!isOffline(p)) return false;
        var a = fleetSinceAge(since, p, now);
        return a != null && a >= FLEET_DAY_S;
      }
      function fleetByName(a, b) {
        return String(a.label || "").localeCompare(String(b.label || ""), "ru", { numeric: true });
      }
      // The grid and «Не в работе» (IA §5): work — the filter's printers at work (in the
      // given order); rest — all those not at work, by name; open — the group unfolded:
      // asked for, «Обслуживание» chosen, or only its cards answer the filter; shown —
      // its cards on screen: the filter's, or all of them when none answers it.
      function fleetSplit(printers, filter, since, now, restOpen) {
        filter = filter || [];
        var work = [], rest = [];
        function pass(p) { return !filter.length || filter.indexOf(fleetBucket(p)) >= 0; }
        (printers || []).forEach(function (p) { (fleetNotWorking(p, since, now) ? rest : work).push(p); });
        work = work.filter(pass);
        rest.sort(fleetByName);
        var hit = rest.filter(pass);
        var open = !!restOpen || filter.indexOf("maintenance") >= 0 || (filter.length > 0 && hit.length > 0 && !work.length);
        return { work: work, rest: rest, open: open, shown: open ? (hit.length ? hit : rest) : [] };
      }
      // «Не в работе · N» (IA §5): one quiet line of mini chips — the maintenance note only
      // in the tooltip — and «Показать» / «Скрыть»; "" when every printer is at work.
      function fleetRestHtml(rest, open, since, now) {
        if (!rest || !rest.length) return "";
        return '<div class="fleet-rest" role="group" aria-label="Не в работе"><b class="fr-title">Не в работе · ' + rest.length + '</b>'
          + rest.map(function (p) {
            var m = p.maintenance;
            if (m) {
              return '<span class="fr-mini" data-tip="' + safeText(maintTipText(m), "") + '">' + safeText(p.label, "") + icon("tool") + '</span>';
            }
            return '<span class="fr-mini" data-tip="Офлайн больше суток">' + safeText(p.label, "") + ' · офлайн '
              + safeText(fleetDur(fleetSinceAge(since, p, now)), "") + '</span>';
          }).join("")
          + '<span class="fr-sep"></span><button type="button" class="bd-link fr-toggle" data-rest-toggle aria-expanded="'
          + (open ? "true" : "false") + '">' + (open ? "Скрыть" : "Показать") + '</button></div>';
      }
      // No card at work answers the chips (IA §5): say so, with a way back (any: printers at all).
      function fleetEmptyHtml(filtered, any) {
        if (filtered) {
          return '<div class="empty-state fleet-empty">Нет принтеров с таким состоянием · '
            + '<button type="button" class="bd-link" data-filter-clear>Показать все</button></div>';
        }
        return '<div class="empty-state fleet-empty">' + (any ? "Сейчас в работе нет ни одного принтера" : "Принтеров пока нет") + '</div>';
      }
      // --- «Финиши» (IA §5, plan step 7): who is ready to unload and who finishes next ---
      // ready: finished printers (not in maintenance, online) with how long they stand so,
      // the longest first; next: the prints with an ETA, the nearest first; noEta: the
      // prints without one, by name. The window (renderForecast) draws next and noEta.
      function fleetFinishes(printers, since, now) {
        var ready = [], next = [], noEta = [];
        (printers || []).forEach(function (p) {
          if (inMaint(p) || isOffline(p)) return;
          if (p.state === "finished") ready.push({ p: p, age: fleetSinceAge(since, p, now) });
          else if (p.state === "printing") (p.eta_seconds > 0 ? next : noEta).push(p);
        });
        ready.sort(function (a, b) {
          var x = a.age == null ? -1 : a.age, y = b.age == null ? -1 : b.age;
          return x !== y ? y - x : fleetByName(a.p, b.p);
        });
        next.sort(function (a, b) { return a.eta_seconds - b.eta_seconds || fleetByName(a, b); });
        noEta.sort(fleetByName);
        return { ready: ready, next: next, noEta: noEta };
      }
      // «11:25»: the farm's local clock at unix seconds t.
      function finClock(t) {
        var d = new Date(t * 1000);
        return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
      }
      var FIN_NEXT_SHOWN = 5;
      // The row under the farm's chips (o: {open, hours, phone, now}). A desktop: «Готовы:
      // P2 · 40м», «Дальше:» the nearest five by their clock, «ещё N», «Окно 12 ч ▾»
      // unfolding the window on the spot. A phone: one line, a tap opens the list.
      function finishRowHtml(f, o) {
        var now = o.now;
        function readyTip(x) {
          return "Завершено — снять пластину" + (x.age != null ? " · готово " + (x.age === Infinity ? "больше суток" : fleetDur(x.age) + " назад") : "");
        }
        if (o.phone) {
          var first = f.next[0];
          return '<button type="button" class="fin-row fin-phone" data-fin-sheet aria-haspopup="dialog" aria-controls="finSheet">'
            + '<span class="fin-it"><span class="fin-lbl">Готовы:</span> ' + (f.ready.length ? '<b class="fin-ready">'
              + f.ready.map(function (x) { return safeText(x.p.label, ""); }).join(", ") + '</b>' : '<span class="fin-dim">нет</span>') + '</span>'
            + '<span class="fin-it"><span class="fin-lbl">Дальше:</span> ' + (first ? '<b>' + safeText(first.label, "") + '</b> <span class="fin-t">'
              + finClock(now + first.eta_seconds) + '</span>' : '<span class="fin-dim">—</span>') + '</span>'
            + '<span class="fin-go" aria-hidden="true">›</span></button>';
        }
        var ready = f.ready.length ? f.ready.map(function (x) {
          return '<b class="fin-ready" data-tip="' + readyTip(x) + '">' + safeText(x.p.label, "") + '</b>'
            + (x.age != null ? ' · ' + safeText(fleetDur(x.age), "") : "");
        }).join(", ") : '<span class="fin-dim">нет — снимать нечего</span>';
        var next = f.next.slice(0, FIN_NEXT_SHOWN).map(function (p) {
          return '<span class="fin-it" data-tip="' + safeText(p.label, "") + ' · через ' + fleetDur(p.eta_seconds)
            + (p.progress_pct != null && isFinite(p.progress_pct) ? " · " + Math.round(p.progress_pct) + "%" : "") + '">'
            + '<span class="fin-t">' + finClock(now + p.eta_seconds) + '</span><b>' + safeText(p.label, "") + '</b></span>';
        }).join('<span class="fin-dim">·</span>');
        var more = f.next.length - FIN_NEXT_SHOWN;
        return '<div class="fin-row"><span class="fin-it"><span class="fin-lbl">Готовы:</span> ' + ready + '</span>'
          + '<span class="fin-it"><span class="fin-lbl">Дальше:</span></span>'
          + (next || '<span class="fin-dim">' + (f.noEta.length ? "без оценки времени" : "ничего не печатается") + '</span>')
          + (more > 0 ? '<span class="fin-dim">· ещё ' + more + '</span>' : "")
          + '<span class="farm-sep"></span><button type="button" class="bd-link fin-win-btn" data-fin-win aria-controls="finishWin" aria-expanded="'
          + (o.open ? "true" : "false") + '">Окно ' + o.hours + ' ч ' + (o.open ? "▴" : "▾") + '</button></div>';
      }
      // The phone's list (IA §5: no scale, its labels would overlap at 375 px): «Готовы»,
      // «Ближайшие N ч», «Позже», «Без оценки»; a tap on a line goes to the card.
      function finishListHtml(f, hours, now) {
        var H = hours * 3600;
        function li(p, t, sub) {
          return '<button type="button" class="fin-li" data-fin-goto="' + safeText(p.id, "") + '"><span class="fin-t">' + t + '</span><b>'
            + safeText(p.label, "") + '</b><span class="fin-sub">' + sub + '</span></button>';
        }
        function pct(p) { return p.progress_pct != null && isFinite(p.progress_pct) ? " · " + Math.round(p.progress_pct) + "%" : ""; }
        function grp(title, rows) {
          return '<div class="fin-grp">' + title + '</div>' + (rows.length ? rows.join("") : '<div class="fin-li fin-none">—</div>');
        }
        var soon = f.next.filter(function (p) { return p.eta_seconds <= H; }), later = f.next.filter(function (p) { return p.eta_seconds > H; });
        return '<div class="fin-list">'
          + grp("Готовы · снять пластину", f.ready.map(function (x) {
            return li(x.p, "✓", x.age == null ? "завершено" : x.age === Infinity ? "готово больше суток" : "готово " + fleetDur(x.age) + " назад");
          }))
          + grp("Ближайшие " + hours + " ч", soon.map(function (p) { return li(p, finClock(now + p.eta_seconds), "через " + fleetDur(p.eta_seconds) + pct(p)); }))
          + grp("Позже", later.map(function (p) { return li(p, finClock(now + p.eta_seconds), "через " + fleetDur(p.eta_seconds) + pct(p)); }))
          + grp("Без оценки", f.noEta.map(function (p) { return li(p, "—", "печатает, время неизвестно" + pct(p)); }))
          + '</div>';
      }
      // The farm's commands (admins; bulk: {busy, msg, failed}), the row under the chips
      // (IA §5): «Свет: вкл · выкл» — how many are lit in its tooltip; «Пауза всех…» with
      // its dialog; «Продолжить N» only while N > 0; the unconfirmed strip with «Повторить».
      // «Сбросить ошибки» is at the foot of the bell's list. "" when nothing in the farm
      // can be switched or paused from here.
      function fleetBulkHtml(printers, bulk) {
        bulk = bulk || {};
        printers = printers || [];
        var anyPrint = printers.some(function (p) { return ctlCaps(p).pause === true; });
        var ls = fleetLightStats(printers);
        if (!ls.total && !anyPrint) return "";
        var lit = "горит " + ls.lit + " из " + ls.total + (ls.offline > 0 ? " · " + ls.offline + " офлайн" : "");
        function lightBtn(action, tip, text) {
          var busy = bulk.busy === action;
          return '<button type="button"' + (busy ? ' class="is-wait"' : "") + ' data-bulk="' + action + '" data-tip="' + tip + '">'
            + (busy ? '<span class="spin-sm"></span>' : "") + text + '</button>';
        }
        var html = '<div class="fleet-bulk" role="toolbar" aria-label="Вся ферма">';
        if (ls.total) {
          html += '<span class="fb-light-lbl" data-tip="' + lit + '">' + icon("bulb") + 'Свет:</span><span class="farm-seg fb-light">'
            + lightBtn("light_on", "Включить на всех онлайн-принтерах со светом · " + lit, "вкл")
            + lightBtn("light_off", "Выключить на всех онлайн-принтерах со светом · " + lit, "выкл") + '</span>';
        }
        if (anyPrint) {
          var n = bulkSplit("pause", printers).go.length, m = bulkSplit("resume", printers).go.length;
          html += '<button type="button" class="fb-btn pause" data-bulk="pause"' + (n ? "" : ' aria-disabled="true"') + ' data-tip="'
            + (n ? "Пауза " + n + " " + plural(n, "печатающему", "печатающим", "печатающим") + " — сначала выбрать, каким" : "Никто не печатает")
            + '">' + icon("pause") + 'Пауза всех…</button>';
          if (m) {
            html += '<button type="button" class="fb-btn" data-bulk="resume" data-tip="Выбрать, какие продолжить">' + icon("play")
              + 'Продолжить <span class="fb-n">' + m + '</span></button>';
          }
        }
        if (bulk.msg) html += '<span class="fb-meta ' + (bulk.msg.k === "ok" ? "ok" : "bad") + '" role="status">' + safeText(bulk.msg.t, "") + '</span>';
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
      /* nf-ui:begin */
      // Printer notifications (p.notifications, app/services/notifications.py):
      // the card's badge and cause strip, the popover / bottom sheet, the farm
      // list under the header bell and its «Сбросить ошибки» dialog.
      // Pure helpers only (no DOM, no globals besides safeText, icon, plural and
      // cmdLineHtml), run in node by tests/test_web_nf_ui.py. Server and device
      // strings go through safeText, in data-tip and aria-label too. The buttons
      // are the server's (n.actions, filtered by notification_actions) and are
      // drawn only for an admin on a printer that can act (nfCanAct).
      var NF_SEV = {   // ranks = notifications.RANK (parity test)
        fatal: { r: 4, label: "Критично" },
        pause: { r: 3, label: "Пауза-ошибка" },
        serious: { r: 2, label: "Серьёзно" },
        common: { r: 1, label: "Уведомление" },
        info: { r: 1, label: "Инфо", cls: "common" }
      };
      var NF_SEV_ORDER = ["fatal", "pause", "serious", "common", "info"];
      // Model -> Bambu serial prefix, for the popover's footnote only: the server
      // never sends it (= hms_catalog.MODEL_PREFIX, parity test).
      var NF_PREFIX = { X1C: "00M", X1: "00W", X1E: "03W", P1P: "01S", P1S: "01P", A1M: "030", A1MINI: "030",
                        A1: "039", P2S: "22E", H2S: "093", H2D: "094", H2C: "31B", X2D: "20P" };
      var NF_SRC = {   // where a record comes from (the code's tooltip)
        hms: "HMS (MQTT print.hms[])", print_error: "print_error (диалог на экране принтера)",
        klipper: "Moonraker: webhooks.state / print_stats", creality: "Creality: поле err в статусе WS :9999"
      };
      var NF_UNKNOWN = "Описание кода Bambu не публикует (в таблице пусто)";
      var NF_REQUIRED = "Выбор обязателен: принтер ждёт ответа, просто закрыть этот диалог нельзя";
      var NF_NET_LOST = "связь прервалась — итог покажет статус принтера";   // = NET_LOST (parity test)
      var NF_CANCEL_KEYS = ["pe:0300_400C", "pe:0500_400E"];                   // = bambu_raw.CANCEL_CODES
      var NF_RESET = {   // = notification_actions.RESET_* / fleet_commands.MSG_* / NOTE_NOT_WIRED (parity test)
        maintenance: "на обслуживании", offline: "офлайн", grace: "нет связи — данные устарели", not_wired: "управление не подключено",
        empty: "уведомлений нет", hms_only: "только HMS — сбрасывать нечего, принтер снимет их сам",
        hms_fatal: "HMS не сбрасываются командой — принтер снимет сам, когда причину устранят",
        pe: "ошибка %s — команды сброса нет, решите на экране принтера",
        pause: "пауза-ошибка %s — решается кнопками в уведомлении",
        blocking: "%s — выбор обязателен: решите кнопками в уведомлении",
        auto_tail: ", автопропуск уже пробует",
        klipper: "Klipper: перезапуск прошивки — только по одному, с подтверждением",
        klipper_none: "Klipper: команды сброса нет — устраните причину на принтере",
        creality: "Creality: команды сброса нет — только на экране принтера"
      };
      var NF_RESET_BLOCKING = { "0300_8007": "0300_8007 после отключения питания — выбор обязателен: продолжить или остановить" };
      var NF_MUTE_TIP = {   // «Скрыть» / «Вернуть» (nfMuteHtml)
        on: "Скрыть у всех: без счётчика, полосы и колокольчика; из-за него карточка не поднимается. Принтеру ничего не отправляется",
        off: "Вернуть: снова в счётчике, полосе и колокольчике"
      };
      // A fatal record younger than this keeps the card's badge loud while the printer prints
      // (IA §5); after it the badge greys out (C1's lidar HMS for weeks, owner 27.10).
      var NF_FRESH_S = 1800;
      // Why the badge is loud or grey: the end of its tooltip.
      var NF_LOUD_TIP = { attention: " · нужно решение", fresh: " · критичное за последние 30 мин",
                          quiet: " · решения не ждут" };
      var NF_RESET_TIP = "Закрыть диалоги «Задача отменена» на экранах — сначала покажет, кого заденет";

      function nfItems(p) {
        return p && Array.isArray(p.notifications) ? p.notifications : [];
      }
      // The printer is in maintenance mode (= ctl-ui's inMaint; this block runs on its own in the tests):
      // its records are still sent (debug), but no badge, strip, bell or farm list shows them.
      function nfMaint(p) {
        return !!(p && p.maintenance);
      }
      // A record an admin hid («Скрыть»: n.muted, the server's notification_mutes, owner 27.10:
      // C1's lidar HMS for weeks). It draws no attention: no badge count or colour, strip,
      // bell, tab «(N⚠)», order or push reason; it waits under «скрытые · N» in its list.
      function nfMuted(n) {
        return !!(n && n.muted === true);
      }
      function nfLive(p) {
        return nfItems(p).filter(function (n) { return !nfMuted(n); });
      }
      function nfHidden(p) {
        return nfItems(p).filter(nfMuted);
      }
      function nfRank(sev) {
        return NF_SEV[sev] ? NF_SEV[sev].r : 0;
      }
      // The CSS class of a level: info (and a level this code does not know) looks like common.
      function nfCls(sev) {
        return NF_SEV[sev] ? NF_SEV[sev].cls || sev : "common";
      }
      function nfSevLabel(sev) {
        return (NF_SEV[sev] || NF_SEV.common).label;
      }
      // Worst level first, the printer's order within a level (a stable sort).
      function nfSort(list) {
        return (list || []).map(function (n, i) { return { n: n, i: i }; })
          .sort(function (a, b) { return (nfRank(b.n.severity) - nfRank(a.n.severity)) || (a.i - b.i); })
          .map(function (x) { return x.n; });
      }
      // The worst level of the records that are not hidden (null without one).
      function nfWorst(p) {
        var w = null;
        nfLive(p).forEach(function (n) { if (w === null || nfRank(n.severity) > nfRank(w)) w = n.severity; });
        return w;
      }
      // A record holds the printer only while it is paused or in error. A «fatal» HMS on a
      // printer that keeps printing (a lidar lens HMS that lasts for weeks) stays in its badge
      // and list but is no decision to make now.
      function nfHolds(p) {
        return !!p && (p.state === "paused" || p.state === "error");
      }
      // Wants a decision now: a fatal error or a pause on an error that holds the printer
      // (never in maintenance mode).
      function nfAttention(p) {
        return !nfMaint(p) && nfHolds(p) && nfRank(nfWorst(p)) >= 3;
      }
      // «Проблемные сверху»'s first key: the rank of a fatal / pause that holds the printer,
      // else 0 — a lasting HMS (H2's «drying on printer power», C1's lidar) keeps no card on top.
      function nfUrgency(p) {
        var r = nfMaint(p) || !nfHolds(p) ? 0 : nfRank(nfWorst(p));
        return r >= 3 ? r : 0;
      }
      // A fatal record that is not hidden and appeared less than NF_FRESH_S ago (now: unix
      // seconds); never without a time or a now, never in maintenance mode.
      function nfFreshFatal(p, now) {
        if (nfMaint(p) || typeof now !== "number" || !isFinite(now)) return false;
        return nfLive(p).some(function (n) {
          return n.severity === "fatal" && typeof n.since === "number" && isFinite(n.since) && now - n.since < NF_FRESH_S;
        });
      }
      // The badge's loudness, not its level (IA §5, §10): its level's colour only when a
      // decision is wanted (nfAttention, the bell's predicate) or a fatal record is fresh;
      // otherwise the grey «ⓘ N». The level's colour stays inside the list.
      function nfLoud(p, now) {
        return nfAttention(p) || nfFreshFatal(p, now);
      }
      function nfCodeLabel(n) {
        return n.src === "hms" ? "HMS_" + n.code : String(n.code || "");
      }
      function nfPad(n) {
        return (n < 10 ? "0" : "") + n;
      }
      function nfTs(ts) {
        return typeof ts === "number" && isFinite(ts) && ts > 0 ? new Date(ts * 1000) : null;
      }
      // "18:27" (local time), "" without a time.
      function nfClock(ts) {
        var d = nfTs(ts);
        return d ? nfPad(d.getHours()) + ":" + nfPad(d.getMinutes()) : "";
      }
      // "с 17.10 18:27" (local time), "" without a time.
      function nfSinceText(ts) {
        var d = nfTs(ts);
        return d ? "с " + nfPad(d.getDate()) + (typeof I18N === "object" ? I18N.dateSep() : ".") + nfPad(d.getMonth() + 1) + " " + nfClock(ts) : "";
      }
      function nfModel(p) {
        return String(p.device_type || p.kind || "").toUpperCase();
      }
      function nfPrefix(model) {
        return NF_PREFIX[String(model || "").toUpperCase().replace(/[\s-]/g, "")] || null;
      }
      function nfNoun(n) {
        return plural(n, "уведомление", "уведомления", "уведомлений");
      }
      function nfCounts(items) {
        var c = {};
        (items || []).forEach(function (n) { var k = NF_SEV[n.severity] ? n.severity : "common"; c[k] = (c[k] || 0) + 1; });
        return c;
      }
      // "критично 1, серьёзно 2" (sep between the level and its count).
      function nfCountsText(c, sep) {
        return NF_SEV_ORDER.filter(function (k) { return c[k]; })
          .map(function (k) { return NF_SEV[k].label.toLowerCase() + sep + c[k]; }).join(", ");
      }
      // Buttons are drawn for this printer: online with fresh data and the
      // commands reach it (Bambu: the print class answers, caps.resume; Klipper: always).
      function nfCanAct(p) {
        if (!p || !p.online || p.state === "offline" || p.grace_period_active) return false;
        if (p.kind === "bambu") return !!(p.caps && p.caps.resume === true);
        return p.kind === "klipper";
      }
      // The auto-ignore line of a pause (n.auto = auto_ignore.view): {t: the
      // list's line, s: the strip's short one}, or null.
      function nfAutoText(auto) {
        if (!auto || auto.enabled === false) return null;
        var tries = auto.tries || 0, max = auto.max || 3;
        if (auto.skip === "maintenance") return { t: "Автопропуск выключен: принтер на обслуживании", s: "автопропуск выключен" };
        if (auto.skip === "no_job_id") return { t: "Автопропуск не сработает: в отчёте нет номера задания", s: "автопропуск не сработает" };
        if (tries >= max || auto.skip === "tries_used") {
          return { t: "Автопропуск: " + max + " " + plural(max, "попытка", "попытки", "попыток") + " не помогли — решите вручную",
                   s: "автопропуск не помог" };
        }
        if (!tries) return { t: "Автопропуск включён · ждём первой попытки", s: "автопропуск: ждём первой попытки" };
        var next = auto.next_in_s;
        var when = typeof next !== "number" ? "" : next <= 0 ? " · следующая сейчас" : " · следующая через " + Math.ceil(next / 60) + " мин";
        return { t: "Автопропуск включён · попытка " + tries + " из " + max + when, s: "автопропуск: попытка " + tries + " из " + max };
      }
      function nfWaitLine(label) {
        return { k: "wait", t: label + " — ждём принтер…" };
      }
      // An action's answer ({status, detail}: httpOutcome) as a status line;
      // card: the card's line, which names the action in a refusal too.
      function nfActLine(label, out, card) {
        out = out || {};
        if (out.status === "confirmed") return { k: "ok", t: label + " — принято принтером" };
        if (out.status === "sent") return { k: "unk", t: label + " — " + (out.detail === NF_NET_LOST ? NF_NET_LOST : "отправлено, ответа нет") };
        var why = out.detail ? ": " + out.detail : "";
        return { k: "bad", t: card ? label + " — отказ" + why : "Отказ" + why };
      }
      // A card's refusal (line.key: its record) once the record is gone and the
      // printer prints: resumed on its own screen (spec §8.4) — the info line, else null.
      function nfResumedLine(line, p) {
        if (!line || line.k !== "bad" || line.key == null || !p || p.state !== "printing") return null;
        var key = String(line.key);
        if (nfItems(p).some(function (n) { return String(n.key) === key; })) return null;
        return { k: "info", t: "Печать уже идёт — продолжили на экране принтера" };
      }
      function nfHiddenText(n) {
        return n + " " + plural(n, "скрытое уведомление", "скрытых уведомления", "скрытых уведомлений");
      }
      // "5 уведомлений: критично 1, серьёзно 2, уведомление 2" (plain text; the hidden ones
      // are not counted, «· скрыто N» says they are there).
      function nfTipText(p) {
        var items = nfLive(p), hid = nfHidden(p).length;
        return items.length + " " + nfNoun(items.length) + ": " + nfCountsText(nfCounts(items), " ")
          + (hid ? " · скрыто " + hid : "");
      }
      // The card's badge (open: its popover is open) or, asPill, the same as a plain pill;
      // now: unix seconds (a fresh fatal, nfLoud). N: the records that are not hidden. Loud:
      // «⚠ N» in the worst level's colour; otherwise the grey «ⓘ N». All of them hidden: a
      // quiet grey bell-off pill with their count, so the list stays a click away.
      function nfBadgeHtml(p, open, asPill, now) {
        var items = nfLive(p), hid = nfHidden(p).length;
        if ((!items.length && !hid) || nfMaint(p)) return "";
        var loud = items.length > 0 && nfLoud(p, now);
        var cls = "nf-badge " + (!items.length ? "muted" : loud ? nfCls(nfWorst(p)) : "quiet");
        var why = !items.length ? "" : !loud ? NF_LOUD_TIP.quiet : nfAttention(p) ? NF_LOUD_TIP.attention : NF_LOUD_TIP.fresh;
        var tip = safeText(items.length ? nfTipText(p) + why : nfHiddenText(hid), "");
        var face = !items.length ? icon("bell-off") + hid : loud ? "⚠ " + items.length : icon("info") + items.length;
        if (asPill) return '<span class="' + cls + ' as-pill" data-tip="' + tip + '">' + face + '</span>';
        var id = safeText(p.id, "");
        return '<button type="button" class="' + cls + '" data-nf-open="' + id + '" data-fk="badge-' + id + '" aria-haspopup="dialog" aria-expanded="'
          + (open ? "true" : "false") + '" data-tip="' + tip + ' — открыть список" aria-label="' + tip + '. Открыть список">' + face + '</button>';
      }
      // The card's cause strip under the progress: the worst record, when it is
      // fatal or a pause on an error and holds the printer (nfHolds); never a hidden one.
      // «Решить ›» when its buttons are drawn for this viewer, else «Подробнее ›».
      function nfStripHtml(p, admin) {
        var items = nfLive(p), top = nfSort(items)[0];
        if (!top || nfRank(top.severity) < 3 || nfMaint(p) || !nfHolds(p)) return "";
        var id = safeText(p.id, ""), code = nfCodeLabel(top), text = top.text ? String(top.text) : NF_UNKNOWN;
        var solve = !!admin && nfCanAct(p) && (top.actions || []).length > 0;
        var auto = nfAutoText(top.auto), more = items.length - 1;
        return '<button type="button" class="nf-strip ' + nfCls(top.severity) + '" data-nf-open="' + id + '" data-fk="strip-' + id
          + '" aria-haspopup="dialog" aria-label="' + safeText(nfSevLabel(top.severity) + ": " + code + " — " + text, "") + '">'
          + '<span class="ic">' + icon(top.severity === "pause" ? "pause" : "alert") + '</span><span class="body"><span class="t"><code>'
          + safeText(code, "") + '</code> ' + safeText(text, "") + '</span>'
          + (auto ? '<span class="sub">' + icon("reset") + safeText(auto.s, "") + '</span>' : "")
          + (top.blocking ? '<span class="sub req">' + icon("lock") + 'выбор обязателен</span>' : "")
          + (more > 0 && !auto && !top.blocking ? '<span class="sub">ещё ' + more + " " + nfNoun(more) + '</span>' : "")
          + '</span><span class="go">' + (solve ? "Решить" : "Подробнее") + ' ›</span></button>';
      }
      function nfAction(n, key) {
        return (n.actions || []).filter(function (a) { return a && a.key === key; })[0] || null;
      }
      // One record of a list. st: its runtime state nfUi[pid + "|" + key]
      // ({busy, confirm, line, done, muting}, may be undefined); surf: "pop" | "farm"
      // (the two lists' focus keys stay apart); opts: {admin, canAct} — canAct
      // defaults to nfCanAct(p). A dangerous action asks first (st.confirm). An admin gets
      // «Скрыть» on every record (nfMuteHtml); a hidden one is dim, with «Вернуть» next to
      // the printer's buttons — a hidden pause still holds the printer and is solved here.
      function nfItemHtml(p, n, surf, st, opts) {
        st = st || {};
        opts = opts || {};
        var cls = nfCls(n.severity), key = String(p.id) + "|" + String(n.key), fk = safeText(key + "|" + surf, "");
        var muted = nfMuted(n), can = opts.admin && (opts.canAct != null ? opts.canAct : nfCanAct(p));
        var acts = can ? (n.actions || []).filter(function (a) { return a && a.key; }) : [];
        var since = nfSinceText(n.since), auto = nfAutoText(n.auto);
        var wiki = typeof n.wiki === "string" && /^https:\/\//.test(n.wiki) ? n.wiki : null;
        var h = '<div class="nf-item ' + cls + (muted ? " muted" : "") + '"><div class="nf-top"><span class="nf-sev ' + cls + '">' + nfSevLabel(n.severity) + '</span>'
          + '<code class="nf-code" data-tip="Источник: ' + safeText(NF_SRC[n.src] || n.src, "") + '">' + safeText(nfCodeLabel(n), "") + '</code>'
          + (since ? '<span class="nf-since" data-tip="Когда появилось">' + since + '</span>' : "") + '</div>'
          + '<div class="nf-text">' + (n.text ? safeText(n.text, "") : '<span class="nf-unk">' + NF_UNKNOWN + '</span>') + '</div>';
        if (n.blocking) h += '<div class="nf-req">' + icon("lock") + '<span>' + NF_REQUIRED + '</span></div>';
        if (auto) h += '<div class="nf-auto">' + icon("reset") + '<span>' + safeText(auto.t, "") + '</span></div>';
        if (n.hint) h += '<div class="nf-hint">' + icon(n.routine ? "tool" : "info") + '<span>' + safeText(n.hint, "") + '</span></div>';
        var attrs = ' data-pid="' + safeText(p.id, "") + '" data-key="' + safeText(n.key, "") + '"';
        var tog = opts.admin ? nfMuteHtml(n, attrs, fk, st) : "";
        var ask = st.confirm ? nfAction(n, st.confirm) : null;
        if (ask && acts.indexOf(ask) >= 0) {
          h += '<div class="pc-confirm" role="group" aria-label="Подтверждение"><span class="q">' + icon(ask.key === "stop" ? "stop" : "reset")
            + safeText(ask.confirm || ask.label, "") + '</span><button type="button" class="' + (ask.style === "danger" ? "cf-stop" : "cf-go")
            + '" data-nf-act="' + safeText(ask.key, "") + '"' + attrs + ' data-yes="1" data-fk="yes-' + fk + '">' + safeText(ask.yes || ask.label, "") + '</button>'
            + '<button type="button" class="cf-cancel" data-nf-cancel="' + safeText(key, "") + '" data-fk="no-' + fk + '">Отмена</button></div>';
        } else if (acts.length || wiki) {
          var lock = !!(st.busy || st.done);
          h += '<div class="nf-acts">' + acts.map(function (a) {
            var busy = st.busy === a.key, style = a.style === "primary" || a.style === "danger" ? " " + a.style : "";
            return '<button type="button" class="nf-btn' + style + (busy ? " is-wait" : "") + '" data-nf-act="' + safeText(a.key, "") + '"' + attrs
              + ' data-fk="act-' + safeText(a.key, "") + "-" + fk + '"' + (a.bambu_id != null ? ' data-tip="Кнопка Bambu №' + safeText(a.bambu_id, "") + '"' : "")
              + (lock ? ' aria-disabled="true"' : "") + '>' + (busy ? '<span class="spin-sm"></span>' : "") + safeText(a.label, "") + '</button>';
          }).join("")
            + (wiki ? '<a class="nf-wiki" href="' + safeText(wiki, "") + '" target="_blank" rel="noopener noreferrer" data-fk="wiki-' + fk
              + '" data-tip="' + safeText(wiki, "") + '">Подробнее' + icon("ext") + '</a>' : "") + tog + '</div>';
        } else if (tog) {
          h += '<div class="nf-mrow">' + tog + '</div>';
        }
        return h + cmdLineHtml(st.line, 'data-nf-dismiss="' + safeText(key, "") + '"') + '</div>';
      }
      // «Скрыть» / «Вернуть» of one record (admins). Not a printer command: a busy card, an
      // offline printer or one without print control does not hold it. st.muting: its PUT runs.
      function nfMuteHtml(n, attrs, fk, st) {
        var on = nfMuted(n), wait = !!(st && st.muting);
        return '<button type="button" class="nf-mute' + (wait ? " is-wait" : "") + '" data-nf-mute="' + (on ? "off" : "on") + '"' + attrs
          + ' data-fk="mute-' + fk + '" data-tip="' + (on ? NF_MUTE_TIP.off : NF_MUTE_TIP.on) + '"' + (wait ? ' aria-disabled="true"' : "") + '>'
          + (wait ? '<span class="spin-sm"></span>' : icon(on ? "bell" : "bell-off")) + (on ? "Вернуть" : "Скрыть") + '</button>';
      }
      // A printer's list, worst first. Routine notes (n.routine: the lesser
      // HMS) fold under «ещё N плановых»; when there is nothing else, the
      // first one stays in view. The hidden ones (nfMuted) fold under «скрытые · N»
      // at the bottom. ui: nfUi; openCom: {surf + "|" + pid: true} for an unfolded
      // group, {"hid|" + surf + "|" + pid: true} for the hidden one.
      function nfListHtml(p, surf, ui, openCom, opts) {
        ui = ui || {};
        var all = nfSort(nfItems(p)), arr = all.filter(function (n) { return !nfMuted(n); }), hid = all.filter(nfMuted);
        var imp = arr.filter(function (n) { return !n.routine; }), com = arr.filter(function (n) { return !!n.routine; });
        if (!imp.length) { imp = com.slice(0, 1); com = com.slice(1); }
        function item(n) { return nfItemHtml(p, n, surf, ui[String(p.id) + "|" + String(n.key)], opts); }
        var h = imp.map(item).join("");
        if (com.length) {
          var ok = surf + "|" + String(p.id), sok = safeText(ok, ""), open = !!(openCom && openCom[ok]);
          h += '<button type="button" class="nf-more" data-nf-more="' + sok + '" aria-expanded="' + open + '" data-fk="more-' + sok + '"><span>'
            + (open ? "Свернуть плановые" : "ещё " + com.length + " " + plural(com.length, "плановое", "плановых", "плановых")) + '</span>'
            + (open ? "" : '<span class="codes">' + safeText(com.map(function (n) { return n.code; }).join(" · "), "") + '</span>')
            + icon("chev") + '</button>' + (open ? '<div class="nf-coms">' + com.map(item).join("") + '</div>' : "");
        }
        if (hid.length) {
          var hk = "hid|" + surf + "|" + String(p.id), shk = safeText(hk, ""), hopen = !!(openCom && openCom[hk]);
          h += '<button type="button" class="nf-more hid" data-nf-more="' + shk + '" aria-expanded="' + hopen + '" data-fk="more-' + shk + '"><span>'
            + (hopen ? "Свернуть скрытые" : "скрытые · " + hid.length) + '</span>'
            + (hopen ? "" : '<span class="codes">' + safeText(hid.map(function (n) { return n.code; }).join(" · "), "") + '</span>')
            + icon("chev") + '</button>' + (hopen ? '<div class="nf-coms">' + hid.map(item).join("") + '</div>' : "");
        }
        return h;
      }
      // The printer's own answer line (line: the card's, cardUi.nfLine) unless
      // one of its records already shows its own.
      function nfPrinterLine(p, ui, line) {
        ui = ui || {};
        var own = nfItems(p).some(function (n) { var s = ui[String(p.id) + "|" + String(n.key)]; return !!(s && s.line); });
        return own || !line ? "" : cmdLineHtml(line, 'data-nf-dismiss-card="' + safeText(p.id, "") + '"');
      }
      // now: unix seconds — a list whose badge is grey says why (nfLoud).
      function nfSummaryHtml(p, now) {
        var st = p.state;
        var lead = !p.online || st === "offline" ? "Принтер не на связи" : st === "paused" ? "Печать стоит и ждёт решения"
          : st === "error" ? "Печать остановлена" : st === "printing" ? "Печать идёт" : st === "cancelled" ? "Задание отменено"
          : st === "idle" || st === "finished" ? "Принтер свободен" : "Состояние неизвестно";
        var c = nfCountsText(nfCounts(nfLive(p)), " — "), hid = nfHidden(p).length;
        if (hid) c += (c ? ", " : "") + "скрыто — " + hid;
        var quiet = nfLive(p).length > 0 && !nfLoud(p, now) ? ' · <span class="nf-dim">решения не ждут — значок серый</span>' : "";
        return "<b>" + lead + "</b>" + (c ? " · " + c : "") + quiet;
      }
      function nfFootText(p) {
        if (p.kind === "bambu") {
          var m = nfModel(p), pre = nfPrefix(m);
          return "Тексты — из таблицы Bambu (ru), кнопки — из таблицы действий Bambu для " + (m || "этой модели") + (pre ? " (" + pre + ")" : "")
            + ", ответы — подтверждение принтера по MQTT.";
        }
        if (p.kind === "klipper") {
          return "Источник — Moonraker: webhooks.state_message / notify_klippy_shutdown. Принтер остаётся на связи в «ОШИБКЕ», а не уходит в офлайн.";
        }
        if (p.kind === "creality") return "Источник — поле err в статусе WebSocket :9999. Своей команды подтверждения ошибки у Creality нет.";
        return "";
      }
      // The printer's status pill: opts.status(p) -> {cls, label} (the card's
      // computeUiStatus / statusLabel live outside this block); none without it.
      function nfStatusHtml(p, opts) {
        var s = opts && typeof opts.status === "function" ? opts.status(p) : null;
        return s && s.label ? '<span class="status-badge ' + safeText(s.cls, "") + '">' + safeText(s.label, "") + '</span>' : "";
      }
      // The printer's popover / bottom sheet. opts: {admin, canAct, status,
      // lines: {pid: the card's line}, now: unix seconds}.
      function nfPopPrinterHtml(p, ui, openCom, opts) {
        opts = opts || {};
        var items = nfItems(p), line = nfPrinterLine(p, ui, opts.lines && opts.lines[p.id]), foot = nfFootText(p);
        return '<div class="nf-pop-h">' + nfBadgeHtml(p, false, true, opts.now)
          + '<div class="nf-pop-ttl"><b id="nfPopTitle">' + safeText(p.label, "") + '</b><span class="bd-model">' + safeText(nfModel(p), "") + '</span></div>'
          + nfStatusHtml(p, opts) + '<button type="button" class="nf-x" data-nf-close data-fk="close" aria-label="Закрыть">×</button></div>'
          + '<div class="nf-sum">' + nfSummaryHtml(p, opts.now) + '</div>' + (line ? '<div class="nf-line">' + line + '</div>' : "")
          + '<div class="nf-body">' + (nfLive(p).length ? "" : '<div class="nf-empty">✓ Уведомлений нет</div>')
          + (items.length ? nfListHtml(p, "pop", ui, openCom, opts) : "") + '</div>'
          + (foot ? '<div class="nf-foot">' + safeText(foot, "") + '</div>' : "");
      }
      function nfByName(a, b) {
        return String(a.label || "").localeCompare(String(b.label || ""), "ru", { numeric: true });
      }
      // The farm's list under the header bell (IA §5, «Нужно решение / Прочие»), printers
      // in maintenance mode and hidden records left out:
      // - «Нужно решение · N»: the printers the bell counts (nfAttention), by name, each
      //   with its records that want the decision (fatal / pause) and their buttons;
      // - «Прочие · X на Y принтерах», folded (openCom["farm-other"]): one line per
      //   printer — its badge and worst record — the loud ones first, then the worst
      //   level, then the name; a line leads to the card and its own list;
      // - at the bottom, an admin's «Сбросить ошибки (N)» when there is something to
      //   reset (nfResetSplit: the same dry-run dialog as before).
      // opts: as nfPopPrinterHtml (canAct per printer).
      function nfPopFarmHtml(printers, ui, openCom, opts) {
        opts = opts || {};
        openCom = openCom || {};
        var all = printers || [], now = opts.now;
        var act = all.filter(function (p) { return !nfMaint(p) && nfLive(p).length; });
        var att = act.filter(nfAttention).sort(nfByName);
        var other = act.filter(function (p) { return !nfAttention(p); }).sort(function (a, b) {
          return (nfLoud(b, now) - nfLoud(a, now)) || (nfRank(nfWorst(b)) - nfRank(nfWorst(a))) || nfByName(a, b);
        });
        var reset = opts.admin ? nfResetSplit(all).go.length : 0;
        var h = '<div class="nf-pop-h">' + icon("bell") + '<div class="nf-pop-ttl"><b id="nfPopTitle">Уведомления фермы</b></div>'
          + '<button type="button" class="nf-x" data-nf-close data-fk="close" aria-label="Закрыть">×</button></div>';
        var foot = reset ? '<div class="nf-pop-foot"><button type="button" class="fb-btn reset" data-nf-reset data-fk="reset" data-tip="'
          + NF_RESET_TIP + '">' + icon("reset") + "Сбросить ошибки (" + reset + ')</button></div>' : "";
        if (!act.length) return h + '<div class="nf-body"><div class="nf-empty">✓ На ферме уведомлений нет</div></div>' + foot;
        var po = { admin: opts.admin, status: opts.status };   // canAct: each printer's own
        h += '<div class="nf-sec">Нужно решение · ' + att.length + '</div><div class="nf-body">' + (att.length ? att.map(function (p) {
          var id = safeText(p.id, "");
          var want = nfSort(nfLive(p)).filter(function (n) { return nfRank(n.severity) >= 3; });
          return '<section class="nf-grp" aria-label="' + safeText(p.label, "") + '"><button type="button" class="nf-grp-h" data-nf-goto="' + id
            + '" data-fk="goto-' + id + '">' + nfBadgeHtml(p, false, true, now) + '<b>' + safeText(p.label, "") + '</b><span class="bd-model">'
            + safeText(nfModel(p), "") + '</span>' + nfStatusHtml(p, opts) + '<span class="nf-grp-go">к карточке ›</span></button>'
            + nfPrinterLine(p, ui, opts.lines && opts.lines[p.id])
            + want.map(function (n) { return nfItemHtml(p, n, "farm", ui && ui[String(p.id) + "|" + String(n.key)], po); }).join("")
            + '</section>';
        }).join("") : '<div class="nf-empty">✓ Решений не требуется</div>') + '</div>';
        if (other.length) {
          var n = 0, open = !!openCom["farm-other"];
          other.forEach(function (p) { n += nfLive(p).length; });
          h += '<button type="button" class="nf-other" data-nf-more="farm-other" aria-expanded="' + open + '" data-fk="more-farm-other"><span>Прочие · '
            + n + " на " + other.length + " " + plural(other.length, "принтере", "принтерах", "принтерах") + '</span>'
            + '<span class="nf-dim">долгие HMS, плановое</span>' + icon("chev") + '</button>';
          if (open) {
            h += '<div class="nf-orows">' + other.map(function (p) {
              var id = safeText(p.id, ""), top = nfSort(nfLive(p))[0];
              return '<button type="button" class="nf-orow" data-nf-goto="' + id + '" data-fk="goto-' + id + '">' + nfBadgeHtml(p, false, true, now)
                + '<b>' + safeText(p.label, "") + '</b><span class="t">' + safeText(top.text || nfCodeLabel(top), "") + '</span>'
                + '<span class="nf-grp-go">›</span></button>';
            }).join("") + '</div>';
          }
        }
        return h + foot;
      }
      // The header bell: how many printers want a decision (nfAttention).
      function nfBellHtml(printers, open) {
        var att = (printers || []).filter(nfAttention), w = null;
        att.forEach(function (p) { var x = nfWorst(p); if (w === null || nfRank(x) > nfRank(w)) w = x; });
        var label = att.length ? att.length + " " + plural(att.length, "принтер требует", "принтера требуют", "принтеров требуют") + " внимания"
          : "Срочных уведомлений нет";
        return '<button type="button" class="nf-bell' + (att.length ? "" : " calm") + '" id="nfBell" data-nf-bell data-fk="bell" aria-haspopup="dialog" aria-expanded="'
          + (open ? "true" : "false") + '" data-tip="Уведомления фермы: ' + label + '" aria-label="Уведомления фермы: ' + label + '">' + icon("bell")
          + (att.length ? '<span class="nf-badge ' + nfCls(w) + ' as-pill">⚠ ' + att.length + '</span>' : "") + '</button>';
      }
      // "<code> — <text>" of the worst record that is neither routine nor hidden (a
      // browser notification's body; plain text), "" without one.
      function nfTopText(p) {
        var top = nfSort(nfLive(p)).filter(function (n) { return !n.routine; })[0];
        return top ? nfCodeLabel(top) + (top.text ? " — " + top.text : "") : "";
      }
      // A cancelled card's stage line: «Задание отменено в 17:03 на 23%» (the
      // time of the cancel echo, while the printer still reports it).
      function nfCancelText(p) {
        var echo = nfItems(p).filter(function (n) { return NF_CANCEL_KEYS.indexOf(n.key) >= 0; })[0];
        var at = echo ? nfClock(echo.since) : "", pct = p.progress_pct;
        return "Задание отменено" + (at ? " в " + at : "")
          + (typeof pct === "number" && isFinite(pct) ? " на " + Math.round(pct) + "%" : "");
      }
      // nfUi without the records the latest printers no longer report.
      function nfPruneUi(ui, printers) {
        var live = {}, out = {};
        (printers || []).forEach(function (p) {
          nfItems(p).forEach(function (n) { live[String(p.id) + "|" + String(n.key)] = true; });
        });
        Object.keys(ui || {}).forEach(function (k) { if (live[k]) out[k] = ui[k]; });
        return out;
      }

      // --- «Сбросить ошибки» at the foot of the bell's list ---
      // The record the fleet reset closes («Закрыть диалог» on a print error that
      // is neither a pause nor a forced choice), or null — the mirror of
      // notification_actions.resettable.
      function nfResettable(p) {
        return nfItems(p).filter(function (n) {
          return n.src === "print_error" && !n.blocking && n.severity !== "pause" && !!nfAction(n, "clean");
        })[0] || null;
      }
      function nfHeldSkip(n) {
        var code = String(n.code || "");
        var t = n.blocking ? NF_RESET_BLOCKING[code] || NF_RESET.blocking.replace("%s", code) : NF_RESET.pause.replace("%s", code);
        var a = n.auto;
        return t + (a && a.skip == null && (a.tries || 0) < (a.max || 0) ? NF_RESET.auto_tail : "");
      }
      // Why the fleet reset leaves this printer out (lower case), or null — the
      // mirror of fleet_commands.skip_reason("reset_errors"): maintenance mode
      // first, then notification_actions.reset_skip_reason (parity tests).
      function nfResetSkipWhy(p) {
        if (nfMaint(p)) return NF_RESET.maintenance;
        if (!p.online || p.state === "offline") return NF_RESET.offline;
        if (p.grace_period_active) return NF_RESET.grace;
        var items = nfItems(p);
        if (p.kind === "klipper" && items.length) {
          return items.some(function (n) { return (n.actions || []).length > 0; }) ? NF_RESET.klipper : NF_RESET.klipper_none;
        }
        if (p.kind === "creality" && items.length) return NF_RESET.creality;
        if (!p.caps || p.caps.resume !== true) return p.caps_note || NF_RESET.not_wired;
        if (!items.length) return NF_RESET.empty;
        if (nfResettable(p)) return null;
        var held = items.filter(function (n) { return n.blocking || n.severity === "pause"; })[0];
        if (held) return nfHeldSkip(held);
        var pe = items.filter(function (n) { return n.src === "print_error"; })[0];
        if (pe) return NF_RESET.pe.replace("%s", String(pe.code));
        return items.some(function (n) { return n.severity === "fatal"; }) ? NF_RESET.hms_fatal : NF_RESET.hms_only;
      }
      // {go: [{p, n: the record it closes}], skip: [{p, why}]}, both by name.
      function nfResetSplit(printers) {
        var go = [], skip = [];
        (printers || []).forEach(function (p) {
          var why = nfResetSkipWhy(p);
          if (why) skip.push({ p: p, why: why });
          else go.push({ p: p, n: nfResettable(p) });
        });
        function byName(a, b) { return nfByName(a.p, b.p); }
        go.sort(byName);
        skip.sort(byName);
        return { go: go, skip: skip };
      }
      // The reset dialog's state, step 1 (the client's dry run, everything picked):
      // {action, step, go: [{id, label, model, key, code, severity, text, since,
      // hms: the HMS that stay}], skip: [{id, label, model, why}], sel, rows, busy}.
      function nfResetDialog(printers) {
        var s = nfResetSplit(printers), sel = {};
        return {
          action: "reset_errors", step: 1, sel: sel, rows: [], busy: false,
          go: s.go.map(function (x) {
            sel[x.p.id] = true;
            return { id: x.p.id, label: x.p.label, model: nfModel(x.p), key: x.n.key, code: x.n.code, severity: x.n.severity,
                     text: x.n.text, since: x.n.since, hms: nfItems(x.p).filter(function (n) { return n.src === "hms"; }).length };
          }),
          skip: s.skip.map(function (x) { return { id: x.p.id, label: x.p.label, model: nfModel(x.p), why: x.why }; })
        };
      }
      // «Сбросить выбранные»: each picked row against the latest printers ->
      // {ids: to send, rows: step 2}. Not sent: a printer that is gone, left out
      // by now or about to close another record than the one shown.
      function nfResetRevalidate(dlg, printers) {
        var byId = {}, ids = [];
        (printers || []).forEach(function (p) { byId[String(p.id)] = p; });
        var rows = dlg.go.filter(function (x) { return dlg.sel[x.id]; }).map(function (x) {
          var p = byId[String(x.id)], n = p && nfResetSkipWhy(p) === null ? nfResettable(p) : null;
          var ok = !!n && n.key === x.key;
          if (ok) ids.push(x.id);
          return { id: x.id, label: x.label, model: x.model, code: x.code, text: x.text, status: ok ? "wait" : "skipped",
                   detail: ok ? "" : "состояние изменилось, не отправлено" };
        });
        return { ids: ids, rows: rows };
      }
      // {id: the record shown in the dialog} for these ids: sent along, the server
      // refuses a printer whose live record is another one by then (reset_one).
      function nfResetKeys(dlg, ids) {
        var want = {}, keys = {};
        (ids || []).forEach(function (id) { want[String(id)] = true; });
        (dlg.go || []).forEach(function (x) { if (want[String(x.id)] && x.key) keys[String(x.id)] = String(x.key); });
        return keys;
      }
      // The reset dialog, drawn into #bulkModal like the bulk one (the same
      // data-bd controls); step 2's rows take the bulk response (mergeBulkResults).
      function nfResetDialogHtml(dlg) {
        var head = '<div class="bd" role="dialog" aria-modal="true" aria-labelledby="bdTitle"><div class="bd-h"><div class="bd-ic reset">'
          + icon("reset") + '</div><div>';
        function model(x) {
          return '<span class="bd-l1"><span class="bd-name">' + safeText(x.label, "") + '</span><span class="bd-model">' + safeText(x.model, "") + '</span></span>';
        }
        function what(x, since) {
          return '<span class="bd-job"><span>' + safeText([x.code, x.text, since].filter(function (s) { return s; }).join(" · "), "") + '</span></span>';
        }
        if (dlg.step === 2) {
          var c = { confirmed: 0, wait: 0, sent: 0, rejected: 0, skipped: 0 };
          dlg.rows.forEach(function (r) { c[r.status] = (c[r.status] || 0) + 1; });
          var busy = !!dlg.busy || c.wait > 0;
          var sum = (c.confirmed ? '<span class="bd-st ok">✓ ' + c.confirmed + '</span>' : "")
            + (c.wait ? '<span class="bd-st"><span class="spin-sm"></span>' + c.wait + '</span>' : "")
            + (c.sent ? '<span class="bd-st unk">? ' + c.sent + '</span>' : "")
            + (c.rejected ? '<span class="bd-st bad">✕ ' + c.rejected + '</span>' : "")
            + (c.skipped ? '<span class="bd-st">— ' + c.skipped + '</span>' : "");
          var rows = dlg.rows.map(function (r) {
            var chip = r.status === "wait" ? '<span class="bd-st"><span class="spin-sm"></span>ждём принтер…</span>'
              : r.status === "confirmed" ? '<span class="bd-st ok">✓ подтверждено</span>'
              : r.status === "sent" ? '<span class="bd-st unk">? отправлено, ответа нет</span>'
              : r.status === "rejected" ? '<span class="bd-st bad">✕ ' + safeText(r.detail, "ошибка") + '</span>'
              : '<span class="bd-st">— ' + safeText(r.detail, "") + '</span>';
            return '<div class="bd-row bd-res">' + model(r) + chip + what(r, "") + '<span></span></div>';
          }).join("");
          return head + '<h3 id="bdTitle">Сброс: ' + (busy ? "ждём ответа принтеров…" : "подтверждено " + c.confirmed + " из " + dlg.rows.length)
            + '</h3><p>Каждый принтер отвечает сам, до 8 с. Карточки обновятся через несколько секунд.</p></div></div>'
            + '<div class="bd-sum">' + sum + '</div><div class="bd-list bd-results">' + rows + '</div>'
            + '<div class="bd-foot"><button type="button" class="bd-btn" data-bd="close"' + (busy ? ' aria-disabled="true"' : "") + '>Готово</button></div></div>';
        }
        var n = dlg.go.filter(function (x) { return dlg.sel[x.id]; }).length;
        var list = dlg.go.map(function (x) {
          var on = !!dlg.sel[x.id];
          return '<label class="bd-row' + (on ? "" : " unchecked") + '"><input type="checkbox" data-bd="toggle" data-id="' + safeText(x.id, "") + '"'
            + (on ? " checked" : "") + '>' + model(x) + '<span class="nf-sev ' + nfCls(x.severity) + '">' + nfSevLabel(x.severity) + '</span>'
            + what(x, nfSinceText(x.since)) + '<span class="bd-eta">' + (x.hms > 0 ? "HMS останутся: " + x.hms : "") + '</span></label>';
        }).join("");
        var skip = dlg.skip.map(function (x) {
          return '<div class="bd-off"><b>' + safeText(x.label, "") + '</b><span>' + safeText(x.model, "") + ' — ' + safeText(x.why, "") + '</span></div>';
        }).join("");
        return head + '<h3 id="bdTitle">Сбросить ошибки на ' + n + " " + plural(n, "принтере", "принтерах", "принтерах") + '?</h3>'
          + '<p>Пробный прогон: ещё ничего не отправлено. Выбранным уйдёт «ОК» (clean_print_error): закроется диалог «Задача отменена» на экране, '
          + 'карточка вернётся в «Простой». Печать никто не продолжит и не остановит.</p></div></div>'
          + '<div class="bd-dry">Попадают только диалоги, где решать нечего. <b>Паузы-ошибки, выбор после отключения питания, HMS, Klipper и Creality</b> '
          + 'не трогаются — у них своя кнопка в уведомлении или её нет вовсе.</div>'
          + '<div class="bd-sec"><span>Попадут · ' + dlg.go.length + '</span>' + (dlg.go.length ? '<button type="button" class="bd-link" data-bd="all">'
            + (n === dlg.go.length ? "Снять все" : "Выбрать все") + '</button>' : "") + '</div><div class="bd-list">' + list + '</div>'
          + (dlg.skip.length ? '<div class="bd-sec"><span>Не попадут · ' + dlg.skip.length + '</span></div><div class="bd-skip">' + skip + '</div>' : "")
          + '<div class="bd-foot"><button type="button" class="bd-btn" data-bd="cancel">Отмена</button>'
          + '<button type="button" class="bd-btn go" data-bd="go"' + (n ? "" : ' aria-disabled="true"') + '>' + icon("reset")
          + "Сбросить выбранные (" + n + ')</button></div></div>';
      }
      /* nf-ui:end */
      function renderPrinterCard(p) {
        const uiStatus = computeUiStatus(p);
        const offlineFlag = isOffline(p);
        const isPrinting = p.state === "printing";
        const progress = Math.max(0, Math.min(100, Number(p.progress_pct || 0)));
        // A cancelled job: only its line «Задание отменено в … на N%», no bar, no ring.
        const showProgress = p.state !== "cancelled" && (isPrinting || p.progress_pct != null);
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
          ? '<span class="pc-job" data-tip="' + safeText(p.job_name, "") + '">' + icon("file") + '<span class="pc-job-text">' + safeText(jobShortName(p.job_name), "\u2014") + '</span></span>'
          : "";
        // A running filament change (load / unload step) replaces the print stage.
        var changeText = filamentChangeText(p.filament_change);
        // A cancelled job says when and where it stopped (nf-ui) instead of the printer's stage.
        var cancelText = p.state === "cancelled" ? nfCancelText(p) : "";
        var stage = changeText || cancelText || stageLabel(p.stage);
        // What this printer can do comes from the server (p.caps, printer_commands.caps_for):
        // tune = temperatures, fans, speed; ams = AMS / external spool commands.
        var caps = ctlCaps(p);
        var canEdit = isAdmin() && !offlineFlag && caps.tune === true;
        // AMS commands are checked on live telemetry: a card in its grace period
        // shows the last good data and the backend would refuse them.
        var canAms = isAdmin() && !offlineFlag && caps.ams === true && !p.grace_period_active;
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
          speedChip(p, canEdit),
          nz && !nz.standard ? '<span class="pc-chip" data-tip="' + nz.tip + '">' + nz.text + '</span>' : "",
          p.door_open === true ? '<span class="pc-chip pc-warn" data-tip="Дверца открыта">' + icon("door") + 'открыта</span>' : "",
          p.sdcard === "missing" ? '<span class="pc-chip pc-warn" data-tip="SD-карта не вставлена">' + icon("sd") + 'нет SD</span>'
            : p.sdcard === "abnormal" ? '<span class="pc-chip pc-warn" data-tip="SD-карта: ошибка или только чтение">' + icon("sd") + 'SD: ошибка</span>' : "",
        ].filter(Boolean).join("");

        // The control row: only for admins on online cards and only the controls
        // whose cap is true (no row at all otherwise — the card does not grow).
        var controls = ctlRowHtml(p, cardUi[p.id], { admin: isAdmin(), offline: offlineFlag, now: Date.now() });
        // The card's one ⋯ (IA §5): in the head of every card, for both roles.
        var headMore = headMoreHtml(p, cardUi[p.id], { admin: isAdmin(), offline: offlineFlag });
        // Under the name: the AMS chip (an admin's opens the commands, anyone else's the
        // read-only popover) or, without an AMS, the external spool's chip (admins).
        var model = cardModelText(p);
        var sub = amsChip(p, canAms, true) || extSpoolChip(p, canAms);

        return `
          <article class="printer-card${offlineFlag ? " offline" : ""}${" " + uiStatus}${inMaint(p) ? " maint" : ""}" data-id="${p.id}" data-state="${uiStatus}" data-kind="${p.kind}" data-label="${safeText(p.label, "")}">
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
                  <div class="printer-name" data-tip="${safeText(p.label, "")}"><span class="pc-nm">${safeText(p.label, "Без имени")}</span>${model ? '<span class="pc-model">' + safeText(model, "") + '</span>' : ""}${p.grace_period_active ? '<span class="grace-dot" data-tip="Нет связи, данные устарели"></span>' : ''}</div>
                  <div class="pc-toprow-right">
                    ${wifi}
                    ${nfBadgeHtml(p, nfPopOpenFor(p.id), false, Date.now() / 1000)}
                    <div class="status-badge ${uiStatus}">${statusLabel(uiStatus)}</div>
                    ${headMore}
                  </div>
                </div>
                ${sub ? '<div class="pc-subrow">' + sub + '</div>' : ""}
              </div>
            </div>
            ${maintPlaqueHtml(p, isAdmin(), Date.now())}
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
            ${!showProgress && !changeText && cancelText ? '<div class="stage-badge">' + safeText(cancelText, "") + '</div>' : ""}
            ${nfStripHtml(p, isAdmin()) + cmdLineHtml(cardUi[p.id] && cardUi[p.id].nfLine, 'data-nf-dismiss-card="' + safeText(p.id, "") + '"')}
            ${chips ? '<div class="pc-chips">' + chips + '</div>' : ""}
            ${controls}
          </article>
        `;
      }
      // «Не в работе» unfolded (this page only: a reload starts folded, IA §5) and how long
      // each printer has stood in its state (fleetSince, ctl-ui: the last day's events and the polls).
      var fleetRestOpen = false;
      var fleetSince = {};
      var fleetEv = null;            // {rows, at, used}: the last answer of /api/history/events
      // The grid's items in order: the cards at work, the empty line when none answers the
      // chips, «Не в работе · N», and its cards when unfolded — all inside #printerGrid, so
      // the cards' own listeners (chips, buttons, tooltips) serve both parts.
      function fleetItems(split, filtered, any, now) {
        var items = split.work.map(function (p) { return { key: "p:" + p.id, card: true, html: renderPrinterCard(p) }; });
        if (!split.work.length) items.push({ key: "empty", html: fleetEmptyHtml(filtered, any) });
        var bar = fleetRestHtml(split.rest, split.open, fleetSince, now);
        if (bar) items.push({ key: "rest", html: bar });
        split.shown.forEach(function (p) { items.push({ key: "p:" + p.id, card: true, html: renderPrinterCard(p) }); });
        return items;
      }
      // Keyed reconcile: only the items whose markup changed are replaced, the others keep
      // their DOM nodes (hover, transitions) between polls; a card moving between the
      // parts is the same node moved.
      var fleetNodes = new Map();       // item key -> its node in #printerGrid
      var fleetCardCache = new Map();   // item key -> the markup it was built from
      function renderFleet(items) {
        var keep = new Set(), prev = null, freshCards = [];
        items.forEach(function (it) {
          var el = fleetNodes.get(it.key);
          if (el && el.parentNode !== grid) el = null;
          if (!el || fleetCardCache.get(it.key) !== it.html) {
            var tpl = document.createElement("template");
            tpl.innerHTML = it.html.trim();
            var fresh = tpl.content.firstElementChild;
            if (el) nfCarryFlash(el, fresh);
            if (el) el.replaceWith(fresh); else grid.appendChild(fresh);
            el = fresh;
            fleetNodes.set(it.key, el);
            fleetCardCache.set(it.key, it.html);
            if (it.card) freshCards.push(fresh);
          }
          keep.add(el);
          var expected = prev ? prev.nextElementSibling : grid.firstElementChild;
          if (expected !== el) grid.insertBefore(el, expected);
          prev = el;
        });
        Array.from(grid.children).forEach(function (el) { if (!keep.has(el)) el.remove(); });
        fleetNodes.forEach(function (el, k) {
          if (!keep.has(el)) { fleetNodes.delete(k); fleetCardCache.delete(k); }
        });
        balanceChips(freshCards);
      }
      // The last day's state changes (/api/history/events, newest first, pages of 500):
      // how long a printer has stood offline («Не в работе» from a day on) or finished.
      // At start (the first paint waits for it a moment), every 5 min and back in view;
      // a failure keeps what was known — an unknown time leaves a card in the grid.
      var FLEET_EV_PAGE = 500, FLEET_EV_PAGES = 4, FLEET_EV_EVERY_MS = 5 * 60 * 1000;
      var fleetEvBusy = null, fleetEvAt = 0, fleetPainted = false;
      function fleetLoadSince() {
        if (fleetEvBusy) return fleetEvBusy;
        fleetEvBusy = (async function () {
          try {
            var to = Date.now() / 1000, fr = to - FLEET_DAY_S, rows = [];
            for (var page = 0; page < FLEET_EV_PAGES; page++) {
              var res = await apiFetch("/api/history/events?fr=" + fr + "&to=" + to + "&limit=" + FLEET_EV_PAGE
                + "&offset=" + page * FLEET_EV_PAGE);
              if (!res.ok) throw new Error("HTTP " + res.status);
              var data = await res.json();
              rows = rows.concat((data && data.rows) || []);
              if (!data || !data.has_more) break;
            }
            fleetEv = { rows: rows, at: to, used: false };
            fleetEvAt = Date.now();
            if (fleetPainted && cachedAllPrinters.length) applyAndRender();
          } catch (e) {
            console.debug("state-change events not loaded:", e);
          } finally {
            fleetEvBusy = null;
          }
        })();
        return fleetEvBusy;
      }
      function fleetSinceApply(printers, now) {
        if (fleetEv && !fleetEv.used) {
          fleetSince = fleetSinceEvents(fleetSince, printers, fleetEv.rows, fleetEv.at);
          fleetEv.used = true;
        }
        fleetSinceTrack(fleetSince, printers, now);
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
      // pid -> {busy, res, line, confirm, pop, ejectUntil}: what the
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
      // The head's ⋯ opens its menu; a second click on it closes it.
      function togglePop(btn, pid, kind) {
        var same = pcPopPid === pid && cardUiOf(pid).pop === kind;
        closePop(true);
        var p = findPrinter(pid);
        if (!same && p) {
          var ui = cardUiOf(pid);
          ui.pop = kind;
          openPop(btn, pid, ctlMenuHtml(p, ui, Date.now(), menuOpts(p)), kind);
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
        // a phone: the commands are in the «Ферма» sheet — what did not go through marks its button
        var fb = document.getElementById("farmSheetBtn");
        if (fb) fb.classList.toggle("warn", !!(isAdmin() && bulkUi.failed));
      }
      function flashBulkMsg(msg) {
        bulkUi.msg = msg;
        // a phone with the «Ферма» sheet closed: the answer has no place on screen — a toast
        if (msg && sheetPhone() && !farmSheetOn) showToast(msg.t, msg.k !== "bad");
        renderFleetBulk();
        setTimeout(function () { if (bulkUi.msg === msg) { bulkUi.msg = null; renderFleetBulk(); } }, BULK_MSG_MS);
      }
      // One bulk request -> a result row per id. Nothing came back: 4xx = nothing
      // ran (rejected, «Повторить» possible); lost / 5xx = the server may have sent them.
      // keys (reset_errors): {id: the record confirmed in the dialog}.
      async function postBulk(action, ids, keys) {
        var body = { action: action, printer_ids: ids };
        if (keys) body.keys = keys;
        var out = await postJson("/api/admin/printers/bulk-command", body);
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
        // «Сбросить ошибки» (nf-ui) shares the modal and its data-bd controls
        m.innerHTML = bulkDlg.action === "reset_errors" ? nfResetDialogHtml(bulkDlg) : bulkDialogHtml(bulkDlg);
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
                   holdText: x.hold ? resumeHoldText(x.hold, stageLabel(p.stage), resumeErrCode(p)) : null };
        });
        var skip = split.skip.map(function (x) { return { id: x.p.id, label: x.p.label, model: model(x.p), why: x.why }; });
        bulkDlg = { action: action, step: 1, go: go, skip: skip, sel: sel, rows: [], busy: false };
        renderBulkDialog('.bd input[type="checkbox"]');
      }
      // «Сбросить ошибки» (the foot of the bell's list): the client's dry run first (nfResetDialog),
      // nothing is sent until «Сбросить выбранные».
      function openResetDialog() {
        if (bulkDlg) return;
        var d = nfResetDialog(cachedAllPrinters);
        if (!d.go.length) return;
        nfClose(false);
        closePop(true);
        bulkDlg = d;
        renderBulkDialog('[data-bd="go"]');
      }
      // Closing never loses results: not while the request runs; afterwards
      // the failures stay in the bar's strip for a later «Повторить» (a reset's
      // stay on its cards: the strip would resend it without the dry run).
      // The focus goes back to the bar's button that opened the dialog.
      function closeBulkDialog() {
        if (!bulkDlg || bulkDlg.busy) return;
        var action = bulkDlg.action;
        if (action !== "reset_errors") bulkUi.failed = bulkDialogFailed(bulkDlg, bulkUi.failed);
        bulkDlg = null;
        renderBulkDialog();
        renderFleetBulk();
        // «Сбросить ошибки» opens from the bell's list (closed by then): the focus goes back to the bell
        var opener = action === "reset_errors" ? document.getElementById("nfBell")
          : fleetBulkEl && fleetBulkEl.querySelector('[data-bulk="' + action + '"]');
        if (opener) opener.focus();
      }
      // «Пауза выбранных» / «Продолжить выбранные»: each chosen printer is checked
      // against the latest data first (bulkRevalidate) — what changed is not sent.
      async function bulkDialogGo() {
        var d = bulkDlg;
        var chosen = d.go.filter(function (x) { return d.sel[x.id]; });
        if (!chosen.length) return;
        var plan = d.action === "reset_errors" ? nfResetRevalidate(d, cachedAllPrinters)
          : bulkRevalidate(d.action, chosen, cachedAllPrinters);
        d.rows = plan.rows;
        d.step = 2;
        await bulkDialogSend(d, plan.ids);
      }
      async function bulkDialogSend(d, ids) {
        d.busy = ids.length > 0;
        renderBulkDialog();
        if (ids.length) {
          mergeBulkResults(d, await postBulk(d.action, ids, d.action === "reset_errors" ? nfResetKeys(d, ids) : null));
          d.busy = false;
          if (d.action === "reset_errors") nfResetLines(d.rows, ids);
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
        var now = Date.now() / 1000;
        var focused = document.activeElement;
        fleetSinceApply(printers, now);
        // the chips' filter over the printers at work, «Не в работе» apart (IA §5)
        var split = fleetSplit(sortPrinters(printers), fleetFilter, fleetSince, now, fleetRestOpen);
        renderFilters();       // the chips: counts over all printers, the filter ignored
        renderFleetBulk();     // so are the farm's commands
        renderFleet(fleetItems(split, fleetFilter.length > 0, printers.length > 0, now));
        renderFinish(now);     // «Финиши» and, unfolded, the window (IA §5)
        tempPopTick();         // an open temperature chart's header value
        renderNf(focused);     // the header bell and the notifications' popover
      }

      // --- «Финиши» on «Принтеры» (IA §5, plan step 7): the row, «Окно ▾» unfolding the
      // window (renderForecast) on a desktop, the list sheet on a phone (< 900 px) ---
      // finOpen: the window unfolded — in the address (#/printers?finish=open, routeNow),
      // so a reload and the tabs keep it; the phone's list sheet is not (it closes itself).
      var finOpen = false, finRowMarkup = null, finSheetOn = false, finSheetMarkup = null;
      var finMq = window.matchMedia ? window.matchMedia("(max-width: 899px)") : null;
      function finPhone() {
        return !!(finMq && finMq.matches);
      }
      if (finMq) {
        var finMqChange = function () { if (!finPhone()) finSheetSet(false); applyAndRender(); };
        if (finMq.addEventListener) finMq.addEventListener("change", finMqChange);
        else if (finMq.addListener) finMq.addListener(finMqChange);
      }
      function renderFinish(now) {
        var row = document.getElementById("finishRow"), win = document.getElementById("finishWin");
        if (!row) return;
        var f = fleetFinishes(cachedAllPrinters, fleetSince, now);
        var html = finishRowHtml(f, { open: finOpen, hours: forecastHours, phone: finPhone(), now: now });
        if (html !== finRowMarkup) { row.innerHTML = html; finRowMarkup = html; }
        var show = finOpen && !finPhone();
        if (win && win.hidden === show) win.hidden = !show;
        if (show && routeActiveView() === "fleet") renderForecast();
        if (finSheetOn) renderFinSheet(f, now);
      }
      // fromAddress: the router shows what the address says, nothing is written back.
      function finSetOpen(open, fromAddress) {
        open = !!open;
        if (open === finOpen) return;
        finOpen = open;
        if (!fromAddress) routeSync(false);       // no history entry: unfolding is no move
        if (cachedAllPrinters.length) renderFinish(Date.now() / 1000);
      }
      function renderFinSheet(f, now) {
        var body = document.getElementById("finSheetBody");
        if (!body) return;
        var html = finishListHtml(f || fleetFinishes(cachedAllPrinters, fleetSince, now), forecastHours, now);
        if (html !== finSheetMarkup) { body.innerHTML = html; finSheetMarkup = html; }
      }
      function finSheetSet(open) {
        var sheet = document.getElementById("finSheet"), back = document.getElementById("finBack");
        if (!sheet || !back || open === finSheetOn) return;
        var inside = sheet.contains(document.activeElement);
        finSheetOn = open;
        sheet.hidden = !open;
        back.hidden = !open;
        if (open) sheetOpened(finSheetClose); else sheetClosed(finSheetClose);   // «Назад» closes it
        if (open) {
          finSheetMarkup = null;
          renderFinSheet(null, Date.now() / 1000);
          sheet.focus({ preventScroll: true });
        } else if (inside) {
          var b = document.querySelector("#finishRow [data-fin-sheet]");
          if (b) b.focus({ preventScroll: true });
        }
      }
      function finSheetClose() { finSheetSet(false); }
      // A line of the list: the sheet closes, the card is scrolled into view and flashed
      // («Не в работе» unfolds for it, as for «к карточке ›»).
      function finGoto(pid) {
        finSheetSet(false);
        var p = findPrinter(pid);
        if (p && fleetNotWorking(p, fleetSince, Date.now() / 1000)) fleetRestOpen = true;
        applyAndRender();
        var card = grid.querySelector('.printer-card[data-id="' + nfCss(pid) + '"]');
        if (!card) { showToast("Карточка скрыта фильтром — снимите фильтры", false); return; }
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.remove("nf-flash");
        card.style.animationDelay = "";
        void card.offsetWidth;
        card.classList.add("nf-flash");
      }
      (function initFinish() {
        var row = document.getElementById("finishRow"), sheet = document.getElementById("finSheet");
        var back = document.getElementById("finBack");
        if (row) {
          row.addEventListener("click", function (e) {
            if (e.target.closest("[data-fin-sheet]")) { finSheetSet(true); return; }
            if (!e.target.closest("[data-fin-win]")) return;
            finSetOpen(!finOpen);
            var again = row.querySelector("[data-fin-win]");
            if (again) again.focus({ preventScroll: true });
          });
        }
        if (sheet) {
          sheet.addEventListener("click", function (e) {
            var go = e.target.closest("[data-fin-goto]");
            if (go) { finGoto(go.getAttribute("data-fin-goto")); return; }
            if (e.target.closest("[data-fin-close]")) finSheetSet(false);
          });
        }
        if (back) back.addEventListener("click", function () { finSheetSet(false); });
        document.addEventListener("keydown", function (e) { if (e.key === "Escape" && finSheetOn) finSheetSet(false); });
      })();

      // --- «Ферма ▾» (a phone, IA §5): «Порядок» (both roles) and the farm's commands
      // (admins) in a sheet. The farm row's own controls are moved in while it is open — their
      // handlers, answers and dialogs stay the same — and put back when it closes. ---
      var farmSheetOn = false;
      function farmSheetSet(open) {
        var sheet = document.getElementById("farmSheet"), back = document.getElementById("farmBack");
        var btn = document.getElementById("farmSheetBtn"), order = document.getElementById("farmOrder");
        var top = document.querySelector("#fleetFarm .farm-top"), cmds = document.getElementById("farmSheetCmds");
        if (!sheet || !back || !btn || !order || !top || !cmds || !fleetBulkEl || open === farmSheetOn) return;
        var inside = sheet.contains(document.activeElement);
        farmSheetOn = open;
        if (open) {
          document.getElementById("farmSheetOrder").appendChild(order);
          cmds.appendChild(fleetBulkEl);
        } else {
          btn.parentNode.insertBefore(order, btn);
          top.appendChild(fleetBulkEl);
        }
        cmds.hidden = !isAdmin();
        sheet.hidden = !open;
        back.hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
          sheetOpened(farmSheetClose);
          sheet.focus({ preventScroll: true });
        } else {
          sheetClosed(farmSheetClose);
          if (inside) btn.focus({ preventScroll: true });
        }
      }
      function farmSheetClose() { farmSheetSet(false); }
      (function initFarmSheet() {
        var btn = document.getElementById("farmSheetBtn"), sheet = document.getElementById("farmSheet");
        var back = document.getElementById("farmBack");
        if (!btn || !sheet || !back) return;
        btn.addEventListener("click", function () { farmSheetSet(!farmSheetOn); });
        back.addEventListener("click", farmSheetClose);
        // capture: «Пауза всех…» / «Продолжить» open their dialog over the page, so the sheet
        // goes first (the click still reaches the bar's own handler)
        sheet.addEventListener("click", function (e) {
          if (e.target.closest("[data-farm-close]") || e.target.closest('[data-bulk="pause"]:not([aria-disabled="true"]),[data-bulk="resume"]')) farmSheetSet(false);
        }, true);
        document.addEventListener("keydown", function (e) { if (e.key === "Escape" && farmSheetOn && !bulkDlg) farmSheetSet(false); });
        if (sheetMq) {
          var change = function () { if (!sheetPhone()) farmSheetSet(false); };
          if (sheetMq.addEventListener) sheetMq.addEventListener("change", change);
          else if (sheetMq.addListener) sheetMq.addListener(change);
        }
      })();

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

      // --- «Окно» under «Финиши» (the former tab): one time axis, a flag per print ---
      // forge-ops-forecast-hours: the horizon, 12 / 24 h (kept as it was).
      var FORECAST_HOURS_KEY = "forge-ops-forecast-hours";
      var forecastHours = 12;
      try {
        var savedForecastHours = parseInt(localStorage.getItem(FORECAST_HOURS_KEY), 10);
        if (savedForecastHours === 12 || savedForecastHours === 24) forecastHours = savedForecastHours;
      } catch (e) {}

      document.querySelectorAll(".forecast-controls .time-btn").forEach(function (btn) {
        btn.classList.toggle("active", parseInt(btn.dataset.hours, 10) === forecastHours);
        btn.setAttribute("aria-pressed", parseInt(btn.dataset.hours, 10) === forecastHours ? "true" : "false");
        btn.addEventListener("click", function () {
          forecastHours = parseInt(btn.dataset.hours, 10) || 12;
          try { localStorage.setItem(FORECAST_HOURS_KEY, String(forecastHours)); } catch (e) {}
          document.querySelectorAll(".forecast-controls .time-btn").forEach(function (b) {
            b.classList.toggle("active", b === btn);
            b.setAttribute("aria-pressed", b === btn ? "true" : "false");
          });
          renderFinish(Date.now() / 1000);   // «Окно 24 ч ▾» and the window
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
        if (meta) meta.innerHTML = 'сейчас <span class="forecast-now-time">' + hhmm(now) + '</span> → ' + hhmm(end);

        var printing = cachedAllPrinters.filter(function (p) { return p.state === "printing" && !inMaint(p); });
        var withEta = printing.filter(function (p) { return p.eta_seconds != null && p.eta_seconds > 0; });
        var withoutEta = printing.filter(function (p) { return p.eta_seconds == null || p.eta_seconds <= 0; });

        if (noEta) {
          if (withoutEta.length) {
            noEta.style.display = "";
            noEta.textContent = "Без оценки: " + withoutEta.map(function (p) { return shortPrinterName(p.label); }).join(", ");
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
        // previous flag on that level — at least 14% of the axis, more on a narrow
        // one: the widest label (12 px text, 7 px a character, 16 px padding) plus a gap.
        var widest = flags.reduce(function (w, f) {
          return Math.max(w, (shortPrinterName(f.p.label) + " · 00:00").length);
        }, 0);
        var MIN_GAP_PCT = Math.max(14, (widest * 7 + 16 + 8) / Math.max(area.clientWidth || 0, 300) * 100);
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
          // the details in the floating tooltip (data-tip, IA §10), one line
          var tip = f.p.label +
            (f.p.job_name ? " · " + jobShortName(f.p.job_name) : "") +
            (f.p.progress_pct != null ? " · " + Math.round(f.p.progress_pct) + "%" : "") +
            " · осталось " + timeFmt(f.p.eta_seconds) +
            " · закончит " + hhmm(f.finish) + (f.over ? " (за пределами окна)" : "");
          var edge = f.pct < 6 ? " forecast-flag-left" : (f.pct > 94 ? " forecast-flag-right" : "");
          html += '<div class="forecast-flag' + (f.over ? " forecast-flag-over" : "") + edge +
            '" style="left:' + f.pct.toFixed(2) + '%;top:' + top + 'px" data-tip="' + escHtml(tip) + '">' +
            '<span class="forecast-flag-label">' + escHtml(text) + '</span>' +
            '<span class="forecast-flag-stem" style="height:' + Math.max(stemH, 4) + 'px"></span>' +
            '</div>';
        });

        area.style.height = (axisTop + 44) + "px";
        area.innerHTML = html;
      }

      /* ── Ответ на команду (IA §10): там, где нажали; тост — только когда этого места нет ── */
      // box: the place a command (or a change) was sent from — a popover, an AMS block, a row,
      // a bar. While box is on screen its reply line (the .reply-slot child, made at its end
      // when missing) says the outcome, as a card's .pc-cmd does; once box is gone (a closed
      // popover or sheet, another tab, a folded block) a toast says it. A refusal stays until
      // its × (or until the place is redrawn), the rest fade after CMD_RESULT_MS; a wait line
      // is never a toast. line: {k: wait|ok|unk|bad|info, t}. -> true when said in place.
      function replyIn(box, line) {
        if (!line) return false;
        if (box && box.isConnected && box.getClientRects().length) {
          var slot = null;
          for (var i = 0; i < box.children.length; i++) {
            if (box.children[i].classList.contains("reply-slot")) slot = box.children[i];
          }
          if (!slot) {
            slot = document.createElement("div");
            slot.className = "reply-slot";
            box.appendChild(slot);
          }
          slot.innerHTML = cmdLineHtml(line, "data-reply-x");
          if (box.scrollHeight > box.clientHeight + 1) slot.scrollIntoView({ block: "nearest" });   // a long sheet
          var token = {};
          slot.replyToken = token;
          if (line.k !== "bad" && line.k !== "wait") {
            setTimeout(function () { if (slot.replyToken === token) slot.innerHTML = ""; }, CMD_RESULT_MS);
          }
          return true;
        }
        if (line.k !== "wait") showToast(line.t, line.k === "ok" || line.k === "info");
        return false;
      }
      // × of a reply line: the line goes (an AMS block's one is forgotten too)
      document.addEventListener("click", function (e) {
        var x = e.target.closest && e.target.closest("[data-reply-x]");
        var slot = x && x.closest(".reply-slot");
        if (!slot) return;
        slot.replyToken = null;
        slot.innerHTML = "";
        if (slot.dataset.amsReply) delete amsReply[slot.dataset.amsReply];
      });

      /* ── Команды с вкладки AMS (ответ — на блоке; карточки — runCardCmd) ────── */
      // -> {ok: sent or confirmed, line: what the block / the popover says (cmdResult, as a card)}
      async function sendPrinterCmd(pid, action, extra) {
        var out = await postCommand(pid, action, extra);
        var ok = out.status === "confirmed" || out.status === "sent";
        if (ok) {
          setTimeout(load, 1500);
          if (amsPanelActive()) setTimeout(loadAms, 1500);
        }
        return { ok: ok, line: cmdResult(action, out) };
      }
      // ── Поповер смены параметров (клик по чипу сопла/стола/скорости/вентилятора, AMS и ⋯) ──
      // A phone (≤ 899 px, plan step 15): the popover is a bottom sheet over a dim backdrop
      // (pcPopBack; a tap on it is a click outside), «Назад» closes it (sheetOpened).
      var pcPop = null, pcPopPid = null, pcPopAnchorRect = null, pcPopPressed = null, pcPopBack = null;
      function ensurePop() {
        if (pcPop) return pcPop;
        pcPopBack = document.createElement("div");
        pcPopBack.className = "pc-pop-back";
        pcPopBack.hidden = true;
        document.body.appendChild(pcPopBack);
        pcPop = document.createElement("div");
        pcPop.className = "pc-pop";
        pcPop.style.display = "none";
        document.body.appendChild(pcPop);
        pcPop.addEventListener("click", onPopClick);
        pcPop.addEventListener("input", function (e) {
          var r = e.target.closest("[data-fan]");
          if (r) { var b = r.parentNode.querySelector("b"); if (b) b.textContent = r.value; }
        });
        // the maintenance note: Enter = its button
        pcPop.addEventListener("keydown", function (e) {
          if (e.key !== "Enter" || pcPop.dataset.kind !== "maint" || !e.target.matches("input.pc-pop-in")) return;
          e.preventDefault();
          var ok = pcPop.querySelector("[data-maint-save]");
          if (ok) ok.click();
        });
        return pcPop;
      }
      // Close the popover and clear the highlight of the card button that opened
      // it (re-rendered unless noRender: the caller renders itself right after).
      function closePop(noRender) {
        var ui = pcPopPid != null ? cardUi[pcPopPid] : null;
        if (pcPop) pcPop.style.display = "none";
        if (pcPopBack) pcPopBack.hidden = true;
        sheetClosed(pcPopClose);
        pcPopPid = null;
        if (ui && ui.pop) {
          ui.pop = null;
          if (!noRender) applyAndRender();
        }
      }
      // A command from a popover the AMS tab opened (the drying form): its buttons stay
      // disabled until the reply (a second click would send it twice). Sent — the popover
      // closes and the block it came from says so (amsReplySet); refused — the form stays as
      // filled and says why itself (IA §10). Opened from a card, the card's own command path
      // answers (cardPopCmd).
      async function popCmd(pid, action, extra) {
        if (pcPop && pcPop.dataset && pcPop.dataset.origin === "card") return cardPopCmd(pid, action, extra);
        var btns = pcPop ? Array.prototype.filter.call(pcPop.querySelectorAll("button"), function (b) { return !b.disabled; }) : [];
        btns.forEach(function (b) { b.disabled = true; });
        replyIn(pcPop, cmdWaitLine(action));
        var res = { ok: false, line: null };
        try { res = await sendPrinterCmd(pid, action, extra); } finally { btns.forEach(function (b) { b.disabled = false; }); }
        if (res.ok) {
          closePop();
          if (extra && extra.ams != null) amsReplySet(pid + "__" + extra.ams, res.line);
          else replyIn(null, res.line);
        } else {
          replyIn(pcPop, res.line);
        }
        return res.ok;
      }
      // A command from a card's AMS popover: the popover stays open while the
      // printer answers (the pressed button waits), closes on success and keeps
      // the filled form on a refusal; the card's AMS button and status line show it too.
      async function cardPopCmd(pid, action, extra) {
        var btn = pcPopPressed;
        if (btn) { btn.setAttribute("aria-busy", "true"); btn.classList.add("is-wait"); }
        var out = await runCardCmd(pid, action, extra, "ams", { after: function (o) {
          if (o.status === "confirmed" || o.status === "sent") closePop(true);
          else replyIn(pcPop, cmdResult(action, o));       // the popover stays open: it says why itself
        } });
        if (btn) { btn.removeAttribute("aria-busy"); btn.classList.remove("is-wait"); }
        return !!out && (out.status === "confirmed" || out.status === "sent");
      }
      function pcPopClose() { closePop(); }
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
        var rect = anchor.getBoundingClientRect();   // read first: closing the notifications re-renders the card
        nfClose(false);
        var pop = ensurePop();
        pcPopPid = pid;
        pop.dataset.kind = kind || "";
        pop.dataset.origin = origin || "card";
        pop.innerHTML = html;
        pop.style.display = "block";
        pop.style.visibility = "hidden";
        pcPopAnchorRect = rect;
        placePop(pop, pcPopAnchorRect);              // a phone's sheet ignores the place (CSS)
        pop.style.visibility = "";
        pop.scrollTop = 0;
        pcPopBack.hidden = !sheetPhone();
        sheetOpened(pcPopClose);
        var inp = pop.querySelector("input.pc-pop-in");
        // not on a touch screen under the temperature setter: its keyboard would cover the chart
        // (a tap on the field still opens it)
        var keys = inp && !(TEMP_KINDS[kind] && window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
        if (keys) { inp.focus(); inp.select && inp.select(); }
      }
      // hours (optional): the temperature chart of that window goes under the setter.
      function tempPopHtml(kind, cur, presets, hours) {
        var chips = presets.map(function (v) {
          return '<button type="button" class="pc-pop-preset" data-set="' + v[1] + '">' + v[0] + '</button>';
        }).join("");
        var title = kind === "nozzle" ? "Сопло" : kind === "chamber" ? "Камера" : "Стол";
        return '<div class="pc-pop-h">' + title + ', °C</div>'
          + '<div class="pc-pop-presets">' + chips + '</div>'
          + '<div class="pc-pop-row"><input type="text" inputmode="numeric" class="pc-pop-in" value="' + cur + '">'
          + '<button type="button" class="pc-pop-ok" data-ok="preheat">OK</button></div>'
          + (hours != null ? tempChartHtml(kind, hours) : '');
      }
      /* temp-chart:begin */
      // The temperature chart of the chip popover (nozzle / bed / chamber): pure
      // helpers only (no DOM, no globals besides the AMS chart code amsChart and
      // amsTipTime), run in node by tests/test_web_temp_chart.py. resp is
      // GET /api/printers/{id}/temps as served: {t: [epoch s], step, series:
      // {kind: {v, target, stats[, lo, hi]}}}; a series without data is left out.
      var TEMP_HOURS = [6, 24, 168];
      var TEMP_KINDS = {
        nozzle: { title: "Сопло", cur: "nozzle_temp", tgt: "target_nozzle_temp", color: "var(--warn)" },
        bed: { title: "Стол", cur: "bed_temp", tgt: "target_bed_temp", color: "var(--info)" },
        chamber: { title: "Камера", cur: "chamber_temp", tgt: "target_chamber_temp", color: "var(--ok)" }
      };
      // viewBox of the chart: 300 wide = the 320 px popover's content box, so the text keeps its shape
      var TC_GEO = { W: 300, H: 112, PL: 28, PR: 8, PT: 8, PB: 16 };
      var TC_GAP_S = 600;          // a hole in the history longer than this (or 2.5 steps) breaks the line
      var TC_CACHE_MS = 60000;     // client cache per (printer, hours): the server caches 60 s too
      function tempHoursLabel(h) { return h >= 168 ? "7д" : h + "ч"; }
      // The response -> points of the curve ({t, v}), the target ({t, v}; null = heater
      // off) and the min..max band ({t, lo, hi}; only for windows coarser than the raw
      // step), all index-aligned. A hole in the history gets a null point (the line
      // breaks: no data is not a flat temperature). live = {cur, tgt} of the card
      // (null when offline) becomes the last point at nowSec when the last bucket is fresh.
      function tempSeries(resp, kind, live, nowSec) {
        var ser = resp && resp.series ? resp.series[kind] : null;
        var t = ser && resp.t ? resp.t : [];
        var gap = Math.max(TC_GAP_S, 2.5 * (resp && resp.step || 90));
        var cur = [], tgt = [], band = ser && ser.lo && ser.hi ? [] : null, lastV = -1;
        for (var i = 0; i < t.length; i++) {
          if (i && t[i] - t[i - 1] > gap) {
            cur.push({ t: t[i - 1], v: null });
            tgt.push({ t: t[i - 1], v: null });
            if (band) band.push({ t: t[i - 1], lo: null, hi: null });
          }
          cur.push({ t: t[i], v: ser.v[i] });
          tgt.push({ t: t[i], v: ser.target[i] != null && ser.target[i] > 0 ? ser.target[i] : null });
          if (band) band.push({ t: t[i], lo: ser.lo[i], hi: ser.hi[i] });
          if (ser.v[i] != null) lastV = cur.length - 1;
        }
        var last = lastV >= 0 ? cur[lastV].t : null, hasLive = false;
        // Fresh = no hole between the last value and now (a 7-day bucket's t is the
        // mean of 42 minutes of rows, so a fixed 600 s would almost never pass).
        if (live && live.cur != null && isFinite(live.cur) && last != null && nowSec > last && nowSec - last <= gap) {
          // empty buckets after the last value (this sensor only): the live end joins the line
          cur.length = tgt.length = lastV + 1;
          if (band) band.length = lastV + 1;
          cur.push({ t: nowSec, v: live.cur });
          tgt.push({ t: nowSec, v: live.tgt != null && live.tgt > 0 ? live.tgt : null });
          if (band) band.push({ t: nowSec, lo: live.cur, hi: live.cur });
          hasLive = true;
        }
        return { cur: cur, tgt: tgt, band: band, stats: ser ? ser.stats || null : null, live: hasLive };
      }
      // [ymin, ymax] over the curve, the target and the band: whole tens, at least 20° tall, not below 0.
      function tempYRange(s) {
        var lo = Infinity, hi = -Infinity;
        function take(v) { if (v != null && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; } }
        s.cur.forEach(function (p) { take(p.v); });
        s.tgt.forEach(function (p) { take(p.v); });
        (s.band || []).forEach(function (p) { take(p.lo); take(p.hi); });
        if (!isFinite(lo)) return [0, 100];
        var ymin = Math.max(0, Math.floor(lo / 10) * 10), ymax = Math.ceil(hi / 10) * 10;
        if (ymax - ymin < 20) {
          ymin = Math.max(0, Math.round((lo + hi) / 20) * 10 - 10);
          ymax = ymin + 20;
        }
        return [ymin, ymax];
      }
      function tempStatsText(st) {
        if (!st || st.min == null) return "";
        return "мин " + Math.round(st.min) + "° · ср " + Math.round(st.avg) + "° · макс " + Math.round(st.max) + "°";
      }
      // The read-only header's value, like the chip: the target only while really heating / cooling.
      function tempNowText(cur, target) {
        if (cur == null || !isFinite(cur)) return "";
        var moving = target != null && target > 0 && Math.abs(cur - target) > 3;
        return Math.round(cur) + "°" + (moving ? " → " + Math.round(target) + "°" : "");
      }
      // Everything the popover shows for one answer: the SVG (or the no-data line),
      // the hover's window and Y range, the live dot's place (% of the box), the stats
      // and which legend entries apply.
      function tempChartView(resp, kind, hours, live, nowSec) {
        var K = TEMP_KINDS[kind], g = TC_GEO;
        var s = tempSeries(resp, kind, live, nowSec);
        var yr = tempYRange(s), fr = nowSec - hours * 3600, to = nowSec;
        var html = amsChart(s.cur, fr, to, yr[0], yr[1], K.color, {
          geo: g, span: "-" + tempHoursLabel(hours), gid: "tcg-" + kind, lone: true,
          band: s.band, bandColor: K.color, target: s.tgt, targetColor: K.color });
        var on = s.cur.some(function (p) { return p.v != null; });
        var dot = null;
        if (on && s.live) {
          var lp = s.cur[s.cur.length - 1], ih = g.H - g.PT - g.PB;
          dot = { left: (g.W - g.PR) / g.W * 100, top: (g.PT + ih * (1 - (lp.v - yr[0]) / (yr[1] - yr[0]))) / g.H * 100 };
        }
        return { s: s, fr: fr, to: to, ymin: yr[0], ymax: yr[1], html: html, on: on, dot: dot,
                 stats: on ? tempStatsText(s.stats) : "",
                 tgt: on && s.tgt.some(function (p) { return p.v != null; }), band: on && !!s.band };
      }
      // The hover line: "14:32 · 215° · цель 220°" (with the date over 24 h).
      function tempTipText(s, best, hours) {
        var tg = s.tgt[best.i] ? s.tgt[best.i].v : null;
        return amsTipTime(best.t, hours) + " · " + Math.round(best.v) + "°" + (tg != null ? " · цель " + Math.round(tg) + "°" : "");
      }
      // The chart block: the period switch and the stats, the box (a same-height
      // placeholder until the answer: the popover does not jump) and the legend.
      function tempChartHtml(kind, hours) {
        var K = TEMP_KINDS[kind];
        return '<div class="tc" style="--tc:' + K.color + '">'
          + '<div class="tc-head"><div class="ams-range tc-range" role="group" aria-label="Период">'
          + TEMP_HOURS.map(function (h) {
            return '<button type="button" data-trange="' + h + '"' + (h === hours ? ' class="active" aria-pressed="true"' : ' aria-pressed="false"')
              + '>' + tempHoursLabel(h) + '</button>';
          }).join("")
          + '</div><span class="tc-mm"></span></div>'
          + '<div class="tc-box"><div class="ams-nodata">загрузка…</div></div>'
          + '<div class="tc-legend"><span><i class="cur"></i>текущая</span><span data-lg="tgt" hidden><i class="tgt"></i>цель</span>'
          + '<span data-lg="band" hidden><i class="band"></i>разброс</span></div></div>';
      }
      // The read-only popover (a viewer, or a chip that cannot be set): the chart only,
      // nothing in it sends a command.
      function tempChartPopHtml(kind, hours, nowText) {
        return '<div class="pc-pop-h tc-h"><span>' + TEMP_KINDS[kind].title + ', °C</span><span class="tc-now">'
          + safeText(nowText, "") + '</span></div>' + tempChartHtml(kind, hours);
      }
      /* temp-chart:end */
      // --- the chart's data and drawing (DOM side of the temp-chart block) ---
      var tcCache = {};      // "pid|hours" -> {at, resp}
      var tcPending = {};    // "pid|hours" -> the request in flight (a reopen joins it)
      var tcSeq = 0;         // the popover's current load: an older answer is ignored
      var tcShown = null;    // the drawn chart {pid, kind, hours, resp, key}: a poll redraws its live end
      var tcRefreshAt = 0;   // the last background refresh of the drawn answer (at most one per TC_CACHE_MS)
      var TEMP_HOURS_KEY = "forge-ops-temp-hours";
      function tempHoursGet() {
        try {
          var h = parseInt(localStorage.getItem(TEMP_HOURS_KEY), 10);
          if (TEMP_HOURS.indexOf(h) >= 0) return h;
        } catch (e) {}
        return 6;
      }
      function tempHoursSet(h) { try { localStorage.setItem(TEMP_HOURS_KEY, String(h)); } catch (e) {} }
      // One request per (printer, hours) at a time, its answer reused for TC_CACHE_MS;
      // a failure is not cached.
      function tempChartFetch(pid, hours) {
        var key = pid + "|" + hours, now = Date.now(), hit = tcCache[key];
        if (hit && now - hit.at < TC_CACHE_MS) return Promise.resolve(hit.resp);
        if (tcPending[key]) return tcPending[key];
        var req = apiFetch("/api/printers/" + encodeURIComponent(pid) + "/temps?hours=" + hours)
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          })
          .then(function (resp) {
            Object.keys(tcCache).forEach(function (k) { if (now - tcCache[k].at >= TC_CACHE_MS) delete tcCache[k]; });
            tcCache[key] = { at: Date.now(), resp: resp };
            delete tcPending[key];
            return resp;
          }, function (e) {
            delete tcPending[key];
            throw e;
          });
        tcPending[key] = req;
        return req;
      }
      // The open popover's chart box, replaced as a whole: the hover's listeners go with the old one.
      function tempChartBox(html) {
        var old = pcPop && pcPop.querySelector(".tc-box");
        if (!old) return null;
        var box = document.createElement("div");
        box.className = "tc-box";
        box.innerHTML = html;
        old.replaceWith(box);
        return box;
      }
      function tempChartMark(on, stats) {
        var tc = pcPop && pcPop.querySelector(".tc");
        if (tc) tc.classList.toggle("tc-on", on);
        var mm = pcPop && pcPop.querySelector(".tc-mm");
        if (mm) mm.textContent = stats || "";
      }
      // The popover is open on this chart (the same printer and kind) and still waits for load `seq`.
      function tempChartCurrent(seq, pid, kind) {
        return seq === tcSeq && !!pcPop && pcPop.style.display !== "none" && pcPopPid === pid && pcPop.dataset.kind === kind;
      }
      function tempChartLoad(pid, kind, hours) {
        var seq = ++tcSeq, hit = tcCache[pid + "|" + hours];
        if (hit && Date.now() - hit.at < TC_CACHE_MS) { tempChartDraw(pid, kind, hours, hit.resp); return; }
        tcShown = null;
        tempChartBox('<div class="ams-nodata">загрузка…</div>');
        tempChartMark(false, "");
        tempChartFetch(pid, hours).then(function (resp) {
          if (tempChartCurrent(seq, pid, kind)) tempChartDraw(pid, kind, hours, resp);
        }, function (e) {
          if (!tempChartCurrent(seq, pid, kind)) return;
          console.warn("temps failed", pid, hours, e);
          tempChartBox('<div class="ams-nodata err">не удалось загрузить</div>');
          tempChartMark(false, "");
        });
      }
      // The card's value is the live end of the line, unless it is stale.
      function tempChartLive(p, K) {
        return p && !isOffline(p) && !p.grace_period_active ? { cur: p[K.cur], tgt: p[K.tgt] } : null;
      }
      // What the chart shows of the live value (as rounded on it): a poll redraws only when it changes.
      function tempLiveKey(live) {
        return live && live.cur != null ? Math.round(live.cur) + "/" + Math.round(live.tgt || 0) : "";
      }
      function tempChartDraw(pid, kind, hours, resp) {
        var p = findPrinter(pid), K = TEMP_KINDS[kind];
        var live = tempChartLive(p, K);
        var v = tempChartView(resp, kind, hours, live, Date.now() / 1000);
        var box = tempChartBox(v.html);
        if (!box) return;
        tcShown = { pid: pid, kind: kind, hours: hours, resp: resp, key: tempLiveKey(live) };
        if (v.on) {
          amsAttachHover(box, v.s.cur, v.fr, v.to, v.ymin, v.ymax, K.color, "°", {
            geo: TC_GEO, hours: hours, clamp: true, tip: function (best) { return tempTipText(v.s, best, hours); } });
        }
        if (v.dot) {
          var dot = document.createElement("div");
          dot.className = "tc-live";
          dot.style.background = K.color;
          dot.style.left = v.dot.left + "%";
          dot.style.top = v.dot.top + "%";
          box.appendChild(dot);
        }
        tempChartMark(v.on, v.stats);
        var lgT = pcPop.querySelector('[data-lg="tgt"]'), lgB = pcPop.querySelector('[data-lg="band"]');
        if (lgT) lgT.hidden = !v.tgt;
        if (lgB) lgB.hidden = !v.band;
      }
      function tempChartSwitch(hours) {
        if (!pcPop || TEMP_HOURS.indexOf(hours) < 0) return;
        tempHoursSet(hours);
        pcPop.querySelectorAll("[data-trange]").forEach(function (b) {
          var on = parseInt(b.dataset.trange, 10) === hours;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        tempChartLoad(pcPopPid, pcPop.dataset.kind, hours);
      }
      // The cards re-render every poll: the read-only header follows the chip's value, and
      // so does the chart's live end, redrawn from the answer it has (no request; not under
      // the pointer, the hover stays put). An answer older than TC_CACHE_MS is refreshed in
      // the background, at most once per TC_CACHE_MS: a long look does not grow a false hole.
      function tempPopTick() {
        if (!pcPop || pcPop.style.display === "none" || !TEMP_KINDS[pcPop.dataset.kind]) return;
        var kind = pcPop.dataset.kind, K = TEMP_KINDS[kind], p = findPrinter(pcPopPid);
        var el = pcPop.querySelector(".tc-now");
        if (el && p) el.textContent = isOffline(p) ? "" : tempNowText(p[K.cur], p[K.tgt]);
        var sh = tcShown, box = pcPop.querySelector(".tc-box");
        if (!sh || sh.pid !== pcPopPid || sh.kind !== kind || !box || box.matches(":hover")) return;
        if (tempLiveKey(tempChartLive(p, K)) === sh.key) return;
        var hit = tcCache[sh.pid + "|" + sh.hours], now = Date.now();
        tempChartDraw(sh.pid, kind, sh.hours, hit ? hit.resp : sh.resp);   // the cache holds the newest answer
        if ((hit && now - hit.at < TC_CACHE_MS) || now - tcRefreshAt < TC_CACHE_MS) return;
        tcRefreshAt = now;
        var seq = tcSeq;
        tempChartFetch(sh.pid, sh.hours).then(function (resp) {
          if (tempChartCurrent(seq, sh.pid, kind)) tempChartDraw(sh.pid, kind, sh.hours, resp);
        }, function (e) { console.warn("temps refresh failed", sh.pid, sh.hours, e); });
      }
      // The four modes with the feed rate each one sets (Bambu's print_speed levels
      // report it, Creality gets it as setFeedratePct); the one at `cur` % is marked.
      function speedPopHtml(cur) {
        var lv = [["Тихий", 1, 50], ["Стандарт", 2, 100], ["Спорт", 3, 124], ["Безумный", 4, 166]];
        var btns = lv.map(function (x) {
          return '<button type="button" class="pc-pop-preset' + (Number(cur) === x[2] ? ' on' : '') + '" data-speed="' + x[1] + '">'
            + x[0] + ' · ' + x[2] + '%</button>'; });
        return '<div class="pc-pop-h">Скорость печати</div><div class="pc-pop-col">' + btns.join("") + '</div>';
      }
      // keys: the fans to show (fanKeys, the chip's data-fans); all three when not given.
      function fanPopHtml(part, aux, chamber, keys) {
        keys = keys || ["part", "aux", "chamber"];
        function row(lbl, key, val) {
          if (keys.indexOf(key) < 0) return "";
          return '<label class="pc-pop-fan">' + lbl
            + '<input type="range" min="0" max="100" value="' + val + '" data-fan="' + key + '"><b>' + val + '</b></label>';
        }
        return '<div class="pc-pop-h">Вентиляторы, %</div>'
          + row("Деталь", "part", part) + row("Вспом.", "aux", aux) + row("Камера", "chamber", chamber)
          + '<div class="pc-pop-row"><button type="button" class="pc-pop-ok" data-ok="fans">Применить</button></div>';
      }
      // AMS popover and drying form actions; the printer is read at click time. Load, unload
      // and a drying stop ask first, in the popover itself (amsAskHtml: «…-go» does it,
      // «Отмена» goes back to the slots / the external spool).
      async function onAmsPopClick(pid, t) {
        var p = findPrinter(pid);
        if (!p) { closePop(); return; }
        function unitOf(idx) {
          return amsUnits(p).find(function (u) { return u.index === idx; }) || null;
        }
        if (t.hasAttribute("data-ams-back")) {
          swapPop(amsUnits(p).length ? amsPopHtml(p) : extSpoolPopHtml(p), "ams");
          return;
        }
        if (t.dataset.amsLoad != null) {
          var pair = t.dataset.amsLoad.split(":");
          var ams = parseInt(pair[0], 10), slot = parseInt(pair[1], 10);
          swapPop(amsAskHtml(amsLoadConfirmText(p, ams, slot), "Загрузить", 'data-ams-load-go="' + ams + ":" + slot + '"'), "ams");
          return;
        }
        if (t.dataset.amsLoadGo != null) {
          var go = t.dataset.amsLoadGo.split(":");
          await popCmd(pid, "ams_load", { ams: parseInt(go[0], 10), slot: parseInt(go[1], 10) });
          return;
        }
        if (t.hasAttribute("data-ams-unload")) {
          swapPop(amsAskHtml(amsUnloadConfirmText(p), "Выгрузить", "data-ams-unload-go"), "ams");
          return;
        }
        if (t.hasAttribute("data-ams-unload-go")) {
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
          swapPop(amsAskHtml("Остановить сушку " + amsUnitName(sIdx) + " на «" + p.label + "»?", "Остановить",
                             'data-dry-stop-go="' + sIdx + '"', true), "ams");
          return;
        }
        if (t.dataset.dryStopGo != null) {
          await popCmd(pid, "dry_stop", { ams: parseInt(t.dataset.dryStopGo, 10) });
        }
      }
      async function onPopClick(e) {
        var pid = pcPopPid;
        if (!pid) return;
        var t = e.target.closest("[data-trange],[data-set],[data-speed],[data-ok],[data-menu],[data-maint-save],"
          + "[data-ams-back],[data-ams-load],[data-ams-load-go],[data-ams-unload],[data-ams-unload-go],[data-dry-open],"
          + "[data-dry-mat],[data-dry-start],[data-dry-stop],[data-dry-stop-go]");
        if (!t || t.disabled || t.getAttribute("aria-disabled") === "true") return;
        // the chart's period switch: never a command
        if (t.dataset.trange != null) { tempChartSwitch(parseInt(t.dataset.trange, 10)); return; }
        // A card answers one command at a time: no new one from its popover meanwhile
        // (the ⋯ menu — maintenance mode and links — sends no printer command).
        // (The questions themselves send nothing: only their «…-go» buttons do.)
        var sends = t.matches("[data-speed],[data-ok],[data-ams-load-go],[data-ams-unload-go],[data-dry-start],[data-dry-stop-go]");
        if (sends && pcPop.dataset.origin === "card" && cardUiOf(pid).busy) { replyIn(pcPop, { k: "info", t: CTL_MSG.busy }); return; }
        if (t.hasAttribute("data-maint-save")) {
          if (t.classList.contains("is-wait")) return;
          var mIn = pcPop.querySelector(".pc-pop-in");
          t.classList.add("is-wait");
          try { await setMaintenance(pid, true, mIn ? mIn.value : ""); } finally { t.classList.remove("is-wait"); }
          return;
        }
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
          runCardCmd(pid, "speed", { level: parseInt(t.dataset.speed, 10) }, null);
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
      // ⋯ menu items; a disabled one never gets here (onPopClick). «Снять с обслуживания»
      // asks in the menu itself (maintOffAskHtml, IA §10): «Снять» does it, «Отмена» is the menu again.
      function onMenuClick(pid, what) {
        var p = findPrinter(pid);
        if (!p) { closePop(); return; }
        if (what === "maint-open") {          // the note form in the same popover
          swapPop(maintPopHtml(p), "maint");
          return;
        }
        if (what === "maint-off") {
          if (isAdmin()) swapPop(maintOffAskHtml(p), "menu");
          return;
        }
        if (what === "back") {
          swapPop(ctlMenuHtml(p, cardUiOf(pid), Date.now(), menuOpts(p)), "menu");
          return;
        }
        if (what === "maint-off-yes") {       // the popover closes once the server took it
          if (isAdmin()) setMaintenance(pid, false);
          return;
        }
        closePop(true);
        applyAndRender();
        if (what === "jobs" || what === "chrono") historyOpenFor(what, p);
        else if (what === "params") adminOpenPrinter(p);
      }
      // ⋯ «Печати / Хронология принтера ›»: «История» on that sub-tab, the head's printer set
      // to it by its stable id (IA §10), the period kept: #/history/jobs?printer=<id>.
      function historyOpenFor(what, p) {
        histFilter = histFilterFrom({ period: histFilter.period, from: histFilter.from, to: histFilter.to,
                                      printer: String(p.id) });
        histRenderHead();
        setHistorySub(what === "jobs" ? "jobs" : "chrono");
        setActiveView("history");             // loads the sub-tab, the address follows
        window.scrollTo(0, 0);
      }
      // ⋯ «Параметры принтера ›» (admins): Админ → Парк, the printer's form opened once
      // the table has loaded (loadAdminPrinters).
      function adminOpenPrinter(p) {
        if (!isAdmin()) return;
        prnOpenAfterLoad = String(p.id);
        setAdminSub("park");
        setActiveView("admin");
        window.scrollTo(0, 0);
      }
      // The ⋯ menu's options: printer items only on a live card, the maintenance section for admins.
      function menuOpts(p) {
        return { admin: !!isAdmin(), offline: isOffline(p) };
      }
      // Maintenance mode on / off or its note (PUT, admins only): not a printer command, so a
      // busy card does not hold it. -> true when the server took it.
      async function setMaintenance(pid, on, note) {
        var p = findPrinter(pid), label = p ? p.label : pid, body = null, r;
        try {
          r = await apiFetch("/api/admin/printers/" + encodeURIComponent(pid) + "/maintenance", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(on ? { on: true, note: note || "" } : { on: false })
          });
          try { body = await r.json(); } catch (e) { body = null; }
        } catch (e) {
          // the popover (the form, the question) is still open: it says why (IA §10)
          replyIn(pcPop, { k: "bad", t: e && e.message === "Unauthorized" ? "Сессия истекла — войдите снова" : "Нет связи с сервером" });
          return false;
        }
        if (!r.ok) {
          replyIn(pcPop, { k: "bad", t: detailText(body && body.detail) || "Ошибка" });
          return false;
        }
        var change = body && body.change;
        showToast(change === "note" || (change === "none" && on) ? "Заметка сохранена"
          : on ? label + ": на обслуживании" : label + ": снят с обслуживания", true);
        if (p && body && "maintenance" in body) p.maintenance = body.maintenance;   // until the next poll
        closePop(true);
        applyAndRender();
        load();
        var adm = document.querySelector('.view-panel[data-panel="admin"]');
        if (adm && adm.classList.contains("active")) loadAdminPrinters();
        return true;
      }
      // ❄: cooling a paused print asks first (the bed cools, the part may let go).
      function coolCmd(pid, p) {
        if (p.state === "paused") askConfirm(pid, "cooldown");
        else runCardCmd(pid, "cooldown", null, "cool");
      }

      // The one floating tooltip of every data-tip on the page (IA §10: no native title
      // anywhere): placed at the element and clamped to the window — neither edge cuts it,
      // at any text scale. A mouse shows it after 250 ms over the element; a touch screen on
      // a tap (tipOnTap) and hides it on the next tap, a scroll or after TIP_TAP_MS.
      var TIP_TAP_MS = 4000;
      (function initChipTooltips() {
        var tip = null, timer = null, curEl = null, touchAt = 0;
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
        // One binding for the whole page: the cards, the farm row, «История», AMS, «Админ», the
        // popovers, the sheets and the header alike.
        function bindTips(root) {
          // a touch's pointerdown: the mouse events the browser emulates after the tap are not a hover
          root.addEventListener("pointerdown", function (e) { if (e.pointerType === "touch") touchAt = Date.now(); }, true);
          root.addEventListener("mouseover", function (e) {
            if (Date.now() - touchAt < 1000) return;
            var el = e.target.closest && e.target.closest("[data-tip]");
            if (!el || el === curEl) return;
            curEl = el;
            if (timer) clearTimeout(timer);
            // a click may have re-rendered the card meanwhile: a detached node measures 0,0
            timer = setTimeout(function () { if (curEl === el && el.isConnected) place(el); }, 250);
          });
          root.addEventListener("mouseout", function (e) {
            if (Date.now() - touchAt < 1000) return;
            var el = e.target.closest && e.target.closest("[data-tip]");
            if (!el) return;
            if (e.relatedTarget && el.contains(e.relatedTarget)) return; // ушли на своего ребёнка
            hide();
          });
          // a tap: the tip of what was tapped (a second tap on it closes it), or none
          root.addEventListener("click", function (e) {
            if (Date.now() - touchAt >= 1000) return;
            var el = e.target.closest && e.target.closest("[data-tip]");
            var again = el && el === curEl && tip && tip.classList.contains("show");
            hide();
            if (!el || again || !tipOnTap(el) || !el.getAttribute("data-tip")) return;
            curEl = el;
            place(el);
            timer = setTimeout(hide, TIP_TAP_MS);
          }, true);
        }
        bindTips(document);
        window.addEventListener("scroll", hide, true);
      })();
      // A tap shows the tip of a chip, a badge, a label — and of a blocked control (it says why);
      // a working control answers by doing what it does. The card's buttons and its confirmation
      // say why in the card's own status line instead (setInfoLine).
      function tipOnTap(el) {
        if (el.closest(".pc-ctl, .pc-confirm")) return false;
        var act = el.closest("button, a[href], select, input, textarea, label, summary, [role=button], " +
                             ".pc-editable, .pc-chartable, [data-ams-row]");
        return !act || !!act.disabled || act.getAttribute("aria-disabled") === "true";
      }

      // «Не в работе · N»: «Показать» unfolds the group, «Скрыть» folds it and lets go of
      // the chips that only its cards answer (offline, maintenance); «Показать все» of the
      // empty line clears the chips. The focus stays on the button that was pressed.
      grid.addEventListener("click", function (e) {
        var t = e.target.closest("[data-rest-toggle],[data-filter-clear]");
        if (!t || !grid.contains(t)) return;
        var sel = "[data-filter-clear]";
        if (t.hasAttribute("data-filter-clear")) {
          fleetFilter = [];
        } else {
          sel = "[data-rest-toggle]";
          if (t.getAttribute("aria-expanded") === "true") {
            fleetRestOpen = false;
            fleetFilter = fleetFilter.filter(function (k) { return k !== "offline" && k !== "maintenance"; });
          } else {
            fleetRestOpen = true;
          }
        }
        persistFilters();
        applyAndRender();
        var again = grid.querySelector(sel) || (filtersEl && filtersEl.querySelector("button[data-filter]"));
        if (again) again.focus({ preventScroll: true });
      });

      (function initPrinterControls() {
        grid.addEventListener("click", function (e) {
          var chip = e.target.closest(".pc-editable, .pc-chartable");
          if (chip && grid.contains(chip)) {
            e.stopPropagation();
            var c = chip.closest(".printer-card");
            if (!c) return;
            var pid = c.dataset.id, kind = chip.dataset.edit || chip.dataset.chart;
            // Close first (clears another card's ⋯ / AMS highlight), render after
            // opening: the chip's rect is read while it is still in the page.
            closePop(true);
            if (kind === "nozzle" || kind === "bed" || kind === "chamber") {
              // the setter on top and the chart under it; read-only: the chart only
              var hours = tempHoursGet();
              if (chip.dataset.edit) {
                var presets = kind === "nozzle"
                  ? [["PLA 220", 220], ["PETG 250", 250], ["ABS 260", 260], ["Выкл", 0]]
                  : kind === "chamber"
                  ? [["40", 40], ["50", 50], ["60", 60], ["65", 65], ["Выкл", 0]]
                  : [["60", 60], ["80", 80], ["100", 100], ["Выкл", 0]];
                openPop(chip, pid, tempPopHtml(kind, chip.dataset.cur || "", presets, hours), kind);
              } else {
                var pt = findPrinter(pid), K = TEMP_KINDS[kind];
                openPop(chip, pid, tempChartPopHtml(kind, hours, pt && !isOffline(pt) ? tempNowText(pt[K.cur], pt[K.tgt]) : ""), kind);
              }
              tempChartLoad(pid, kind, hours);
            } else if (kind === "speed") {
              openPop(chip, pid, speedPopHtml(chip.dataset.cur));
            } else if (kind === "fan") {
              openPop(chip, pid, fanPopHtml(+chip.dataset.part || 0, +chip.dataset.aux || 0, +chip.dataset.chamber || 0,
                                            chip.dataset.fans ? chip.dataset.fans.split(",") : null));
            } else if (kind === "ams" || kind === "ext") {
              // the AMS chip: an admin's opens the commands, anyone else's the read-only
              // popover; the external spool's chip its unload
              var pa = findPrinter(pid);
              if (pa) {
                cardUiOf(pid).pop = "ams";
                openPop(chip, pid, kind === "ext" ? extSpoolPopHtml(pa) : chip.dataset.edit ? amsPopHtml(pa) : amsViewPopHtml(pa), "ams");
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
          var btn = e.target.closest(".pc-controls .pc-ctl, .pc-head .pc-ctl[data-cmd=more], .pc-confirm button, .pc-cmd .x");
          if (!btn || !grid.contains(btn)) return;
          var card = btn.closest(".printer-card"), pid2 = card && card.dataset.id, p = pid2 && findPrinter(pid2);
          if (!p) return;
          var ui = cardUiOf(pid2), cmd = btn.dataset.cmd;
          // the head's ⋯: maintenance and links, no printer command — a busy card does not hold it
          if (cmd === "more") { e.stopPropagation(); togglePop(btn, pid2, "menu"); return; }
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
          }
        });
        document.addEventListener("click", function (e) {
          if (!pcPop || pcPop.style.display === "none") return;
          // composedPath() is fixed when the click starts: a button the popover
          // just replaced (a next screen) still counts as inside it.
          var path = e.composedPath ? e.composedPath() : [];
          if (path.indexOf(pcPop) !== -1 || pcPop.contains(e.target)) return;
          if (e.target.closest(".pc-editable, .pc-chartable")
              || e.target.closest(".pc-head [data-cmd=more]")) return;
          closePop();
        });
        document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePop(); });
      })();

      // --- Printer notifications: the popover / bottom sheet, the header bell and
      // the buttons. nf-ui draws the markup; this wires it to the page and the API. ---
      var nfPopEl = document.getElementById("nfPop"), nfBackEl = document.getElementById("nfBack");
      var nfBellSlot = document.getElementById("nfBellSlot");
      var nfPop = null;      // {type: "printer" | "farm", id, opener: the opener's data-fk}
      var nfUi = {};         // pid|key -> {busy, confirm, line, done, muting}: a record's buttons and answer
      var nfOpenCom = {};    // surf|pid -> its routine group is unfolded
      var nfPopMarkup = null, nfBellMarkup = null;
      var NF_LINE_MS = 9000;     // the card's answer line fades (a refusal stays until dismissed)
      var NF_DONE_MS = 60000;    // a confirmed record's buttons stay locked while the printer clears it
      // a phone (≤ 899 px since plan step 15, as every popover): the bottom sheet
      var nfSheetMq = window.matchMedia ? window.matchMedia("(max-width: 899px)") : null;
      function nfIsSheet() {
        return !!(nfSheetMq && nfSheetMq.matches);
      }
      function nfCss(v) {
        return window.CSS && CSS.escape ? CSS.escape(String(v)) : String(v).replace(/["\\]/g, "\\$&");
      }
      function nfPopOpenFor(pid) {
        return !!(nfPop && nfPop.type === "printer" && String(nfPop.id) === String(pid));
      }
      function nfSurf() {
        return nfPop && nfPop.type === "farm" ? "farm" : "pop";
      }
      function nfFocusFk(fk) {
        var el = fk ? document.querySelector('[data-fk="' + nfCss(fk) + '"]') : null;
        if (el) el.focus({ preventScroll: true });
        return !!el;
      }
      function nfStatusOf(p) {
        var s = computeUiStatus(p);
        return { cls: s, label: statusLabel(s) };
      }
      function nfOpts() {
        var lines = {};
        Object.keys(cardUi).forEach(function (pid) { if (cardUi[pid] && cardUi[pid].nfLine) lines[pid] = cardUi[pid].nfLine; });
        return { admin: !!isAdmin(), status: nfStatusOf, lines: lines, now: Date.now() / 1000 };
      }
      // The bell and the open popover (from applyAndRender, after the cards).
      // focused: the element focused before the cards were re-rendered — when a
      // re-render replaced it, the focus moves to its successor (same data-fk).
      function renderNf(focused) {
        if (!nfPopEl || !nfBackEl || !nfBellSlot) return;      // before this section ran (IIFE init)
        nfUi = nfPruneUi(nfUi, cachedAllPrinters);
        if (nfPop && nfPop.type === "printer" && !findPrinter(nfPop.id)) { nfPop = null; sheetClosed(nfSheetClose); }   // the printer is gone
        var bell = nfBellHtml(cachedAllPrinters, !!(nfPop && nfPop.type === "farm"));
        if (bell !== nfBellMarkup) { nfBellSlot.innerHTML = bell; nfBellMarkup = bell; }
        if (!nfPop) {
          if (!nfPopEl.hidden || nfPopMarkup !== null) {
            nfPopEl.hidden = true;
            nfBackEl.hidden = true;
            nfPopEl.innerHTML = "";
            nfPopMarkup = null;
            document.body.style.overflow = "";
          }
        } else {
          var html = nfPop.type === "farm" ? nfPopFarmHtml(cachedAllPrinters, nfUi, nfOpenCom, nfOpts())
            : nfPopPrinterHtml(findPrinter(nfPop.id), nfUi, nfOpenCom, nfOpts());
          var cls = "nf-pop" + (nfPop.type === "farm" ? " farm" : "");
          if (nfPopEl.className !== cls) nfPopEl.className = cls;
          if (html !== nfPopMarkup) {
            var sc = nfPopEl.scrollTop;
            nfPopEl.innerHTML = html;
            nfPopMarkup = html;
            nfPopEl.scrollTop = sc;
          }
          var sheet = nfIsSheet();
          nfPopEl.hidden = false;
          nfBackEl.hidden = !sheet;
          nfPopEl.setAttribute("aria-modal", sheet ? "true" : "false");
          nfPlace();
        }
        if (focused && focused !== document.body && !focused.isConnected) {
          var fk = focused.getAttribute && focused.getAttribute("data-fk");
          // its record resolved: the focus stays inside the open list
          if (!nfFocusFk(fk) && nfPop) { var x = nfPopEl.querySelector("[data-nf-close]"); if (x) x.focus({ preventScroll: true }); }
        }
      }
      function nfAnchor() {
        if (!nfPop) return null;
        if (nfPop.type === "farm") return document.getElementById("nfBell");
        var id = nfCss(nfPop.id);
        return grid.querySelector('[data-nf-open="' + id + '"].nf-badge') || grid.querySelector('.printer-card[data-id="' + id + '"]');
      }
      // Under the anchor when it fits, else above it; when neither, the roomier
      // side with a shorter (scrolling) list. Never off-screen, even when the
      // anchor scrolled away. A phone gets the bottom sheet (CSS) instead.
      function nfPlace() {
        if (!nfPop || nfPopEl.hidden) return;
        nfPopEl.style.maxHeight = "";
        if (nfIsSheet()) { nfPopEl.style.left = ""; nfPopEl.style.top = ""; return; }
        var a = nfAnchor();
        if (!a || !a.getClientRects().length) return;           // the card is filtered out / another tab
        var r = a.getBoundingClientRect(), w = nfPopEl.offsetWidth, h = nfPopEl.offsetHeight, m = 8;
        var vw = document.documentElement.clientWidth, vh = window.innerHeight;
        var left = Math.max(m, Math.min(r.right - w, vw - w - m));
        var below = vh - r.bottom - 8 - m, above = r.top - 8 - m, top;
        if (h <= below) top = r.bottom + 8;
        else if (h <= above) top = r.top - 8 - h;
        else if (below >= 280 || below >= above) { nfPopEl.style.maxHeight = Math.max(200, below) + "px"; top = r.bottom + 8; }
        else { nfPopEl.style.maxHeight = Math.max(200, above) + "px"; top = r.top - 8 - nfPopEl.offsetHeight; }
        top = Math.max(m, Math.min(top, vh - m - nfPopEl.offsetHeight));
        nfPopEl.style.left = Math.round(left) + "px";
        nfPopEl.style.top = Math.round(top) + "px";
      }
      function nfOpen(type, id, opener) {
        closePop(true);                                         // the card's chip / ⋯ / AMS popover
        nfPop = { type: type, id: id, opener: opener && opener.getAttribute ? opener.getAttribute("data-fk") : null };
        sheetOpened(nfSheetClose);                              // a phone's «Назад» closes the list
        if (nfIsSheet()) document.body.style.overflow = "hidden";
        nfPopMarkup = null;
        nfPopEl.scrollTop = 0;
        applyAndRender();                                       // the badge's / bell's aria-expanded too
        var x = nfPopEl.querySelector("[data-nf-close]");
        if (x) x.focus({ preventScroll: true });
      }
      // restore: false — the focus stays where the click put it (outside click, another popover).
      // An open «Остановить печать?» question does not wait for the next opening.
      function nfClose(restore) {
        if (!nfPop) return;
        var op = nfPop.opener;
        nfPop = null;
        sheetClosed(nfSheetClose);
        Object.keys(nfUi).forEach(function (k) { if (nfUi[k]) nfUi[k].confirm = null; });
        applyAndRender();
        if (restore !== false) nfFocusFk(op);
      }
      function nfSheetClose() { nfClose(); }
      // «к карточке ›»: the fleet tab, the card scrolled into view and flashed, then its own popover.
      // cardOnly: no popover after the flash (a finish's push).
      function nfGoto(pid, cardOnly) {
        nfPop = null;
        sheetClosed(nfSheetClose);                              // the move to «Принтеры» takes its «Назад» entry
        var fleet = document.querySelector('.view-panel[data-panel="fleet"]');
        if (fleet && !fleet.classList.contains("active")) setActiveView("fleet");
        var gp = findPrinter(pid);
        if (gp && fleetNotWorking(gp, fleetSince, Date.now() / 1000)) fleetRestOpen = true;   // «Не в работе» unfolds
        applyAndRender();
        var card = grid.querySelector('.printer-card[data-id="' + nfCss(pid) + '"]');
        if (!card) { showToast("Карточка скрыта фильтром — снимите фильтры", false); return; }
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.remove("nf-flash");
        card.style.animationDelay = "";
        void card.offsetWidth;
        card.classList.add("nf-flash");
        if (cardOnly) return;
        setTimeout(function () {
          if (!findPrinter(pid)) return;
          nfOpen("printer", pid, grid.querySelector('[data-nf-open="' + nfCss(pid) + '"].nf-badge'));
        }, 450);
      }
      // A click on a browser push: what is open closes, the printer opens where the event is.
      function pushOpen(pid, notes) {
        sheetCloseAll();
        closePop(true);
        if (!findPrinter(pid)) { setActiveView("fleet"); return; }
        nfGoto(pid, !notes);
      }
      // A re-rendered card (the popover's aria-expanded right after «к карточке ›»,
      // a poll) is a new node: the running flash goes on where it was, so both
      // flashes are seen (from renderFleet, before the old node is replaced).
      function nfCarryFlash(from, to) {
        if (!from.classList.contains("nf-flash") || !from.getAnimations) return;
        var a = from.getAnimations().filter(function (x) { return x.animationName === "nfFlash"; })[0];
        if (!a || a.playState === "finished") return;
        to.style.animationDelay = -Math.round(Number(a.currentTime) || 0) + "ms";
        to.classList.add("nf-flash");
      }
      // A notification's button: a dangerous one asks first (inline, st.confirm),
      // then POST notification-action; the record and the card's line show the answer.
      async function runNfAction(pid, key, action, confirmed) {
        var p = findPrinter(pid);
        var n = p ? nfItems(p).filter(function (x) { return String(x.key) === String(key); })[0] : null;
        var a = n ? nfAction(n, action) : null;
        if (!a || !isAdmin()) return;
        var uk = String(pid) + "|" + String(key), st = nfUi[uk] || (nfUi[uk] = {});
        if (st.busy || st.done) return;
        if (a.confirm && !confirmed) {
          st.confirm = action;
          applyAndRender();
          nfFocusFk("yes-" + uk + "|" + nfSurf());
          return;
        }
        var cu = cardUiOf(pid);
        st.confirm = null;
        st.busy = action;
        st.line = nfWaitLine(a.label);
        cu.nfLine = st.line;
        applyAndRender();
        var out = await postJson("/api/admin/printers/" + encodeURIComponent(pid) + "/notification-action",
                                 { key: String(key), action: action, confirm: !!confirmed });
        st.busy = null;
        st.line = nfActLine(a.label, out);
        st.done = out.status === "confirmed";
        var line = nfActLine(a.label, out, true);
        if (line.k === "bad") line.key = String(key);           // nfResumedLines: resumed on the printer
        cu.nfLine = line;
        applyAndRender();
        if (line.k !== "bad") nfFadeLine(pid, line);
        if (st.done) {
          setTimeout(function () { if (st.done) { st.done = false; applyAndRender(); } }, NF_DONE_MS);
        }
        if (out.status === "confirmed" || out.status === "sent") setTimeout(load, 1500);
      }
      function nfFadeLine(pid, line) {
        setTimeout(function () {
          var cu = cardUi[pid];
          if (cu && cu.nfLine === line) { cu.nfLine = null; applyAndRender(); }
        }, NF_LINE_MS);
      }
      // After a poll (load, before the render): a card's refusal whose record is
      // gone while the printer prints says so instead, and fades.
      function nfResumedLines(printers) {
        printers.forEach(function (p) {
          var cu = cardUi[p.id], next = cu ? nfResumedLine(cu.nfLine, p) : null;
          if (next) { cu.nfLine = next; nfFadeLine(p.id, next); }
        });
      }
      // The fleet reset's answers on the cards (ids: the rows that were sent).
      function nfResetLines(rows, ids) {
        var sent = {};
        ids.forEach(function (id) { sent[String(id)] = true; });
        rows.forEach(function (r) {
          if (!sent[String(r.id)] || r.status === "wait") return;
          var line = nfActLine("Закрыть диалог", { status: r.status, detail: r.detail }, true);
          cardUiOf(r.id).nfLine = line;
          if (line.k !== "bad") nfFadeLine(r.id, line);
        });
        setTimeout(load, 1500);
      }
      function nfCancelConfirm(uk) {
        var st = nfUi[uk], action = st && st.confirm;
        if (st) st.confirm = null;
        applyAndRender();
        if (action) nfFocusFk("act-" + action + "-" + uk + "|" + nfSurf());
      }
      // «Скрыть» / «Вернуть» (PUT notification-mute, admins only): not a printer command, so
      // a busy record or card does not hold it. On success the record is updated in place
      // until the next poll; a refusal shows as the record's line.
      async function runNfMute(pid, key, muted) {
        if (!isAdmin()) return;
        var uk = String(pid) + "|" + String(key), st = nfUi[uk] || (nfUi[uk] = {});
        if (st.muting) return;
        st.muting = true;
        applyAndRender();
        var label = muted ? "Скрыть" : "Вернуть", r = null, body = null;
        try {
          r = await apiFetch("/api/admin/printers/" + encodeURIComponent(pid) + "/notification-mute", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: String(key), muted: !!muted })
          });
          try { body = await r.json(); } catch (e) { body = null; }
        } catch (e) {
          body = { detail: e && e.message === "Unauthorized" ? "Сессия истекла — войдите снова" : "Нет связи с сервером" };
        }
        st.muting = false;
        if (!r || !r.ok || !body || typeof body.muted !== "boolean") {
          st.line = { k: "bad", t: label + " — отказ: " + (detailText(body && body.detail) || "ошибка"), mute: true };
          applyAndRender();
          return;
        }
        if (st.line && st.line.mute) st.line = null;
        nfItems(findPrinter(pid)).forEach(function (n) { if (String(n.key) === String(key)) n.muted = body.muted; });
        applyAndRender();
        load();
      }
      function onNfPopClick(t) {
        var el;
        if ((el = t.closest("[data-nf-mute]"))) {
          if (el.getAttribute("aria-disabled") !== "true") {
            runNfMute(el.getAttribute("data-pid"), el.getAttribute("data-key"), el.getAttribute("data-nf-mute") === "on");
          }
        } else if ((el = t.closest("[data-nf-act]"))) {
          if (el.getAttribute("aria-disabled") !== "true") {
            runNfAction(el.getAttribute("data-pid"), el.getAttribute("data-key"), el.getAttribute("data-nf-act"), el.hasAttribute("data-yes"));
          }
        } else if ((el = t.closest("[data-nf-cancel]"))) {
          nfCancelConfirm(el.getAttribute("data-nf-cancel"));
        } else if ((el = t.closest("[data-nf-dismiss]"))) {
          var st = nfUi[el.getAttribute("data-nf-dismiss")];
          if (st) st.line = null;
          applyAndRender();
          var x = nfPopEl.querySelector("[data-nf-close]");
          if (x) x.focus({ preventScroll: true });
        } else if ((el = t.closest("[data-nf-dismiss-card]"))) {
          var cu = cardUi[el.getAttribute("data-nf-dismiss-card")];
          if (cu) cu.nfLine = null;
          applyAndRender();
        } else if ((el = t.closest("[data-nf-more]"))) {
          var mk = el.getAttribute("data-nf-more");
          nfOpenCom[mk] = !nfOpenCom[mk];
          applyAndRender();
        } else if ((el = t.closest("[data-nf-close]"))) {
          nfClose();
        } else if ((el = t.closest("[data-nf-goto]"))) {
          nfGoto(el.getAttribute("data-nf-goto"));
        } else if ((el = t.closest("[data-nf-reset]"))) {
          openResetDialog();                                   // closes the list itself
        }
      }
      (function initNotifications() {
        if (!nfPopEl || !nfBackEl || !nfBellSlot) return;
        document.addEventListener("click", function (e) {
          var t = e.target && e.target.closest ? e.target : null;
          if (!t) return;
          // composedPath() is fixed when the click starts: a button the
          // popover just re-rendered still counts as inside it
          var path = e.composedPath ? e.composedPath() : [];
          if (path.indexOf(nfPopEl) !== -1 || nfPopEl.contains(t)) { onNfPopClick(t); return; }
          var el;
          if ((el = t.closest("[data-nf-dismiss-card]"))) {
            var cu = cardUi[el.getAttribute("data-nf-dismiss-card")];
            if (cu) cu.nfLine = null;
            applyAndRender();
            return;
          }
          if ((el = t.closest("[data-nf-open]"))) {
            var id = el.getAttribute("data-nf-open");
            if (nfPopOpenFor(id)) nfClose(); else nfOpen("printer", id, el);
            return;
          }
          if ((el = t.closest("[data-nf-bell]"))) {
            if (nfPop && nfPop.type === "farm") nfClose(); else nfOpen("farm", null, el);
            return;
          }
          if (nfPop && path.indexOf(nfBackEl) === -1) nfClose(false);   // a click outside closes it
        });
        nfBackEl.addEventListener("click", function () { nfClose(); });
        document.addEventListener("keydown", function (e) {
          if (e.key !== "Escape" || !nfPop || bulkDlg) return;
          // an open confirmation is cancelled first, then the list closes
          var asks = Object.keys(nfUi).filter(function (k) { return nfUi[k] && nfUi[k].confirm; });
          if (asks.length) {
            asks.slice(1).forEach(function (k) { nfUi[k].confirm = null; });
            nfCancelConfirm(asks[0]);
          } else {
            nfClose();
          }
          e.preventDefault();
        });
        window.addEventListener("resize", function () {
          if (!nfPop) return;
          var sheet = nfIsSheet();
          nfBackEl.hidden = !sheet;
          nfPopEl.setAttribute("aria-modal", sheet ? "true" : "false");
          document.body.style.overflow = sheet ? "hidden" : "";
          nfPlace();
        });
        window.addEventListener("scroll", function () { if (nfPop && !nfIsSheet()) nfPlace(); }, { passive: true });
      })();

      /* tab-ui:begin */
      // The browser tab: «(3⚠) 47% · Кузница» and a favicon with a progress
      // ring and a red dot (drawn by updateTabStatus below). Pure function
      // declarations only (no DOM, no globals: it sits above load(), which the
      // IIFE's init calls before the vars further down are assigned), run in
      // node by tests/test_web_tab_ui.py.
      // attention: printers attentionOf counts — nfAttention, the header
      // bell's predicate, so the two numbers always match (an offline status
      // carries no notifications). pct: the tab title's percent — the
      // printing printer nearest to its ETA; without any ETA the highest
      // progress; floored, never 100 before the finish.
      function tabSummary(printers, statusOf, attentionOf) {
        var attention = 0, next = null, top = null;
        (printers || []).forEach(function (p) {
          if (attentionOf(p)) attention++;
          if (p.state !== "printing" || p.progress_pct == null || statusOf(p) === "offline") return;
          if (top === null || p.progress_pct > top) top = p.progress_pct;
          if (p.eta_seconds != null && p.eta_seconds > 0 && (next === null || p.eta_seconds < next.eta_seconds)) next = p;
        });
        var raw = next ? next.progress_pct : top;
        return { attention: attention, pct: raw == null ? null : Math.min(99, Math.max(0, Math.floor(raw))) };
      }
      function tabTitle(sum, base, stale) {
        if (stale) return "нет связи · " + base;
        return (sum.attention ? "(" + sum.attention + "⚠) " : "") + (sum.pct != null ? sum.pct + "% · " : "") + base;
      }
      // The favicon's signature: "base" (the page's own icon), "r<pct>" (the
      // ring), plus "+dot" while printers want attention.
      function tabIconKey(sum, stale) {
        if (stale) return "base";
        return (sum.pct == null ? "base" : "r" + sum.pct) + (sum.attention > 0 ? "+dot" : "");
      }
      // The header's status chip (IA §4): the dot and the time of the last
      // answer, words only on a failure. state: "ok", "late" (the last poll
      // failed: an amber dot, the time stays) or "down" (the tab title's own
      // «нет связи», after 30 s: red «нет связи · 2 мин»). clock: the time of
      // the last answer ("" before the first one); sinceOk: seconds since it.
      function liveChip(state, clock, sinceOk) {
        var since = clock ? " · данные от " + clock : "";
        if (state === "down") {
          var sec = Math.max(0, Math.floor(sinceOk || 0)), min = Math.floor(sec / 60);
          var ago = sec < 60 ? sec + " с" : min < 60 ? min + " мин" : Math.floor(min / 60) + " ч";
          return { cls: "down", text: "нет связи · " + ago, tip: "Нет связи с сервером" + since };
        }
        if (state === "late") return { cls: "late", text: clock || "—", tip: "Сервер не ответил, повторяем" + since };
        return { cls: "", text: clock || "—", tip: clock ? "Данные с принтеров получены в " + clock : "Данные с принтеров ещё не получены" };
      }
      /* tab-ui:end */

      // The tab's DOM side, driven by the 3-s load() only: no timers, no rAF,
      // no CSS animation. The favicon is redrawn on an offscreen 64x64 canvas
      // only when its key changes — the ring at most once per 10 s (the next
      // poll after that applies a pending change), the dot at once. Its colours
      // are fixed: Chrome's tab strip has its own theme. The installed app has
      // no tab: there only the title (its window caption) and the app badge.
      const tabState = { lastOkAt: Date.now(), link: null, baseHref: null, key: "base", drawnAt: 0,
                         canvas: null, standaloneMq: null, badge: null, stale: false, okClock: "" };
      function tabIsStandalone() {
        if (!tabState.standaloneMq && window.matchMedia) tabState.standaloneMq = window.matchMedia("(display-mode: standalone)");
        return !!(tabState.standaloneMq && tabState.standaloneMq.matches);
      }
      function tabIconUrl(pct, dot) {
        var c = tabState.canvas;
        if (!c) {
          c = tabState.canvas = document.createElement("canvas");
          c.width = 64;
          c.height = 64;
        }
        var x = c.getContext("2d");
        x.clearRect(0, 0, 64, 64);
        x.fillStyle = "#1e293b";
        x.beginPath();
        if (x.roundRect) x.roundRect(0, 0, 64, 64, 14);
        else {
          x.moveTo(14, 0);
          x.arcTo(64, 0, 64, 64, 14);
          x.arcTo(64, 64, 0, 64, 14);
          x.arcTo(0, 64, 0, 0, 14);
          x.arcTo(0, 0, 64, 0, 14);
          x.closePath();
        }
        x.fill();
        // the logo's hexagon (favicon viewBox 32, centred on its dot at 16,16):
        // half size inside the ring, full size without it
        var k = 2, stroke = 5, r = 6;
        if (pct != null) {
          x.lineWidth = 8;
          x.lineCap = "butt";
          x.strokeStyle = "#33475c";
          x.beginPath();
          x.arc(32, 32, 26, 0, 2 * Math.PI);
          x.stroke();
          if (pct > 0) {
            x.strokeStyle = "#60a5fa";
            x.beginPath();
            x.arc(32, 32, 26, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * pct / 100);
            x.stroke();
          }
          k = 1;
          stroke = 3.5;
          r = 4;
        }
        var hex = [[16, 3], [29, 12], [29, 24], [16, 30], [3, 24], [3, 12]];
        x.strokeStyle = "#60a5fa";
        x.fillStyle = "#60a5fa";
        x.lineWidth = stroke;
        x.lineJoin = "round";
        x.beginPath();
        hex.forEach(function (v, i) {
          var px = 32 + (v[0] - 16) * k, py = 32 + (v[1] - 16) * k;
          if (i) x.lineTo(px, py);
          else x.moveTo(px, py);
        });
        x.closePath();
        x.stroke();
        x.beginPath();
        x.arc(32, 32, r, 0, 2 * Math.PI);
        x.fill();
        if (dot) {
          x.beginPath();
          x.arc(51, 13, 11, 0, 2 * Math.PI);
          x.fillStyle = "#ef4444";
          x.fill();
          x.lineWidth = 3;
          x.strokeStyle = "#0f172a";
          x.stroke();
        }
        return c.toDataURL("image/png");
      }
      function tabFavicon(sum, stale) {
        var st = tabState;
        if (tabIsStandalone()) return;
        if (!st.link) {
          st.link = document.querySelector('link[rel~="icon"]');
          if (!st.link) return;
          st.baseHref = st.link.getAttribute("href");
        }
        var key = tabIconKey(sum, stale);
        if (key === st.key) return;
        var now = Date.now(), dot = /\+dot$/.test(key), ring = key.charAt(0) === "r";
        // only the % moved: at most once per 10 s
        if (ring && st.key.charAt(0) === "r" && dot === /\+dot$/.test(st.key) && now - st.drawnAt < 10000) return;
        st.key = key;
        if (key === "base") {
          st.link.setAttribute("href", st.baseHref);
          return;
        }
        st.link.setAttribute("href", tabIconUrl(ring ? sum.pct : null, dot));
        st.drawnAt = now;
      }
      // The installed app's icon badge (Windows; Chrome Android has no API): on change only.
      function tabAppBadge(n) {
        if (!("setAppBadge" in navigator) || !tabIsStandalone() || n === tabState.badge) return;
        tabState.badge = n;
        try {
          var done = n > 0 ? navigator.setAppBadge(n) : navigator.clearAppBadge();
          if (done && done.catch) done.catch(function () {});
        } catch (e) {}
      }
      function updateTabStatus(printers, stale) {
        tabState.stale = !!stale;   // the status chip says «нет связи» exactly when the title does
        try {
          var sum = tabSummary(printers, computeUiStatus, nfAttention);
          var t = tabTitle(sum, "Кузница", stale);
          if (document.title !== t) document.title = t;
          tabFavicon(sum, stale);
          if (!stale) tabAppBadge(sum.attention);
        } catch (e) {
          console.debug("Tab status not updated:", e);
        }
      }
      // ok: a poll just answered; otherwise it failed (after updateTabStatus, whose verdict it shows).
      function renderLiveChip(ok) {
        var el = document.getElementById("liveChip");
        if (!el || !lastUpdate) return;
        if (ok) tabState.okClock = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        var v = liveChip(ok ? "ok" : tabState.stale ? "down" : "late", tabState.okClock || "", (Date.now() - tabState.lastOkAt) / 1000);
        lastUpdate.textContent = v.text;
        el.className = "status-chip" + (v.cls ? " " + v.cls : "");
        el.setAttribute("data-tip", v.tip);
      }

      async function load() {
        try {
          const response = await apiFetch("/api/printers");
          const data = await response.json();
          if (!fleetPainted) {
            // the first paint waits up to 1.5 s for the last day's state changes: a printer
            // offline for days does not show in the grid and then jump into «Не в работе»
            fleetPainted = true;
            await Promise.race([fleetEvBusy || fleetLoadSince(), new Promise(function (r) { setTimeout(r, 1500); })]);
          }
          cachedAllPrinters = data.map((p) => Object.assign({}, p));
          nfResumedLines(cachedAllPrinters);
          applyAndRender();
          tabState.lastOkAt = Date.now();
          updateTabStatus(cachedAllPrinters, false);
          jobsTick();
          histPrinterOptions();   // «История»'s printer choice follows the farm
          // «Парк» drawn before the first answer (a reload on #/admin/park): its firmware and
          // «Управление» come from these rows — once, not every poll
          if (prnNeedLive) { prnNeedLive = false; loadAdminPrinters(); }
          const settings = loadNotificationSettings();
          cachedAllPrinters.forEach(function(p) {
            var prev = prevPrinterStates[p.id];
            // no push for a printer in maintenance mode; its state is still tracked, so after
            // the mode only a change from then on notifies
            if (prev && prev !== p.state && !inMaint(p)) {
              if (settings.enabled && ("Notification" in window) && Notification.permission === "granted") {
                if (settings.finish && prev === "printing" && p.state === "finished") {
                  showBrowserNotification("✅ Печать завершена", p.label, p.id, false);
                }
                // the reason: the worst notification that is not routine (a cancel is
                // its own state and never notifies); never a hidden one — the server's
                // last_error joins every record, the hidden ones too, so not with those
                var why = nfTopText(p);
                if (settings.error && p.state === "error" && prev !== "error" && prev !== "offline") {
                  why = why || (nfHidden(p).length ? "" : p.last_error);
                  showBrowserNotification("🔴 Ошибка принтера", p.label + (why ? ": " + why : ""), p.id, true);
                }
                if (settings.paused && prev === "printing" && p.state === "paused") {
                  showBrowserNotification("⚠️ Пауза печати", p.label + (why ? ": " + why : ""), p.id, !!why);
                }
              }
            }
            prevPrinterStates[p.id] = p.state;
          });
          renderLiveChip(true);
        } catch (error) {
          console.error("Load failed:", error);
          updateTabStatus(cachedAllPrinters, Date.now() - tabState.lastOkAt > 30000);
          renderLiveChip(false);
          // Единичный сбой поллинга (каждые 3с) не должен стирать сетку —
          // если есть последние данные, оставляем их (статус-чип говорит, что они не свежие).
          if (!(cachedAllPrinters && cachedAllPrinters.length)) {
            grid.innerHTML = `
            <div class="empty-state">
              <strong>Не удалось загрузить телеметрию</strong>
              Проверьте доступность API <code>/api/printers</code> и повторите попытку.
            </div>
          `;
          }
        }
      }
      // A tab (and the brand, «Принтеры») opens its screen from the top: the
      // header stays on screen while the page scrolls under it.
      // A phone (the tabs are the bottom navigation): a second tap on the screen's own item
      // goes back to its top and folds what is unfolded (IA §4).
      viewTabs.forEach((tab) => {
        tab.addEventListener("click", () => {
          if (navPhone() && routeActiveView() === tab.dataset.view) { phoneRetap(tab.dataset.view); return; }
          setActiveView(tab.dataset.view);
          window.scrollTo(0, 0);
        });
      });
      document.getElementById("brandBtn").addEventListener("click", () => { setActiveView("fleet"); window.scrollTo(0, 0); });
      // The router starts once the whole IIFE has run (appjs-iife-var-hoisting-trap).
      Promise.resolve().then(routeStart);
      // Another language (app/web/i18n.js): the screen is drawn again, so its hand-made dates
      // and numbers («01.11», «×1,00») follow the language (the translator leaves them alone).
      document.addEventListener("forge-lang", function () { routeApply(routeNow(), true); });
      loadPersistedFilters();
      loadSortPrefs();
      renderFilters();
      fleetLoadSince();
      load();
      setInterval(load, 3000);
      setInterval(fleetLoadSince, FLEET_EV_EVERY_MS);
      // back in view after a while (a sleeping tab saw no changes): the times again
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible" && Date.now() - fleetEvAt > 2 * 60 * 1000) fleetLoadSince();
      });
      initScrollTop();
      initTheme();
      syncNotificationControls();
      var themeBtns = document.querySelectorAll(".theme-seg-btn");
      themeBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
          setTheme(btn.getAttribute("data-theme") || "");
        });
      });
      var glassPhotoInput = document.getElementById("glassPhotoInput");
      document.getElementById("glassPhotoPick").addEventListener("click", function () { glassPhotoInput.click(); });
      glassPhotoInput.addEventListener("change", function () {
        glassPhotoPick(glassPhotoInput.files && glassPhotoInput.files[0]);
        glassPhotoInput.value = "";
      });
      document.getElementById("glassPhotoClear").addEventListener("click", glassPhotoClear);
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

      // The name column of «Хронология»: 200 px, a phone's 64 (plan step 15: the bars get the width).
      function tlLabelW() { return sheetPhone() ? 64 : 200; }
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
      var stateOrder = { printing: 0, paused: 1, error: 2, finished: 3, cancelled: 3.5, idle: 4, unknown: 5, offline: 6 };

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
        // the head's window — its last week at most (IA §7) — and its printer
        var cw = histChronoWindow(histFilter, Date.now() / 1000);
        timelineRange = { from: cw.fr, to: cw.to };
        timelinePrinter = histFilter.printer;
        var note = document.getElementById('chronoNote');
        if (note) { note.textContent = histChronoNote(cw); note.hidden = !cw.clipped; }
        if (loader) {
          loader.textContent = histLoadingText(cw.clipped ? { period: "7d" } : histFilter);
          loader.style.display = '';
        }
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
      // Maintenance mode: the same hatching in yellow (--warn-5 / --warn-7, as .seg-maintenance);
      // a solid --warn is the pause already.
      function makeMaintPattern(ctx) {
        var pc = document.createElement('canvas');
        pc.width = 8;
        pc.height = 8;
        var p = pc.getContext('2d');
        var st = getComputedStyle(document.documentElement);
        p.fillStyle = (st.getPropertyValue('--warn-5') || '#56401e').trim() || '#56401e';
        p.fillRect(0, 0, 8, 8);
        p.strokeStyle = (st.getPropertyValue('--warn-7') || '#7a5c2e').trim() || '#7a5c2e';
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
        var maintPattern = makeMaintPattern(ctx);
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

        var labelW = tlLabelW();
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
            tl = td.getDate().toString().padStart(2,'0') + (typeof I18N === 'object' ? I18N.dateSep() : '.') + (td.getMonth()+1).toString().padStart(2,'0') + ' ' +
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
          // a phone's narrow name column: cut to its width
          while (dn.length > 1 && ctx.measureText(dn).width > labelW - 26) dn = dn.slice(0, -2) + '\u2026';
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

              // Gradient bar (офлайн и обслуживание — штриховкой)
              if (segState === 'offline') {
                ctx.fillStyle = offlinePattern;
              } else if (segState === 'maintenance') {
                ctx.fillStyle = maintPattern;
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
          var labelW = tlLabelW();
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
          var baw2 = c2.offsetWidth - tlLabelW() - 10;
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
        // the words of the one dictionary (IA §10)
        var stateNames = { printing: 'Печатает', idle: 'Простой', paused: 'Пауза', finished: 'Завершено', error: 'Ошибка', cancelled: 'Отменено', offline: 'Офлайн', maintenance: 'Обслуживание', unknown: 'Нет данных' };
        canvas.addEventListener('mousemove', function (e) {
          if (dragActive) { tip.style.display = 'none'; return; }
          tlTipAt(e.clientX, e.clientY);
        });
        // The tooltip of the bar under (x, y): the printer, the time and the state, the job, the details.
        function tlTipAt(clientX, clientY) {
          var rc = canvas.getBoundingClientRect();
          var mx2 = clientX - rc.left;
          var my2 = clientY - rc.top;
          var lw3 = tlLabelW(); var mr3 = 10;
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
          tip.style.left = Math.max(8, Math.min(clientX + 15, window.innerWidth - 280)) + 'px';
          tip.style.top = (clientY - 10) + 'px';
          return true;
        }

        canvas.addEventListener('mouseleave', function () {
          if (tip) tip.style.display = 'none';
        });

        // A finger (plan step 15; touch-action: pan-y keeps the page's own scroll up and down):
        // sideways — the pan, two fingers — the pinch to their middle, a tap — the tooltip
        // (until the next tap or a scroll), a double tap — the reset. The mouse keeps its handlers.
        var touches = {}, pinch = null, tPan = null, lastTap = 0;
        function tFrac(x) {
          var r = canvas.getBoundingClientRect(), w = canvas.offsetWidth - tlLabelW() - 10;
          return w > 0 ? Math.max(0, Math.min(1, (x - r.left - tlLabelW()) / w)) : 0;
        }
        canvas.addEventListener('pointerdown', function (e) {
          if (e.pointerType !== 'touch') return;
          e.preventDefault();                                  // no mouse emulation: the mouse handlers stay the mouse's
          touches[e.pointerId] = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, t0: Date.now() };
          var ids = Object.keys(touches);
          if (ids.length === 2) {
            var a = touches[ids[0]], b = touches[ids[1]];
            pinch = { d: Math.abs(a.x - b.x) || 1, s: viewStart, e: viewEnd, f: tFrac((a.x + b.x) / 2) };
            tPan = null;
          } else if (ids.length === 1) {
            tPan = { x: e.clientX, s: viewStart };
          }
        });
        canvas.addEventListener('pointermove', function (e) {
          var t = touches[e.pointerId];
          if (!t || timelineData.length === 0) return;
          t.x = e.clientX;
          t.y = e.clientY;
          var span = viewEnd - viewStart, b = timelineBounds();
          if (pinch) {
            var ids = Object.keys(touches);
            if (ids.length < 2) return;
            var p = touches[ids[0]], q = touches[ids[1]], d = Math.abs(p.x - q.x) || 1;
            var span0 = pinch.e - pinch.s;
            var ns = Math.max(TIMELINE_MIN_SPAN, Math.min(span0 * pinch.d / d, b.hi - b.lo));
            var anchor = pinch.s + pinch.f * span0;
            viewStart = anchor - pinch.f * ns;
            viewEnd = viewStart + ns;
          } else if (tPan) {
            var w = canvas.offsetWidth - tlLabelW() - 10;
            if (w <= 0) return;
            viewStart = tPan.s + ((tPan.x - e.clientX) / w) * span;
            viewEnd = viewStart + span;
          } else {
            return;
          }
          tip.style.display = 'none';
          clampView();
          drawTimeline();
        });
        function tEnd(e) {
          var t = touches[e.pointerId];
          if (!t) return;
          delete touches[e.pointerId];
          var moved = Math.abs(t.x - t.x0) + Math.abs(t.y - t.y0);
          if (e.type === 'pointerup' && !pinch && moved < 10 && Date.now() - t.t0 < 400) {
            var now = Date.now();
            if (now - lastTap < 350 && timelineData.length) {   // a double tap: the whole period again
              var bb = timelineBounds();
              viewStart = bb.lo;
              viewEnd = bb.hi;
              tip.style.display = 'none';
              drawTimeline();
              lastTap = 0;
            } else {
              lastTap = now;
              if (!tlTipAt(t.x, t.y)) tip.style.display = 'none';
            }
          }
          if (!Object.keys(touches).length) {
            if (pinch || tPan) maybeLoadMoreAtLeftEdge();
            pinch = null;
            tPan = null;
          } else if (pinch) {
            pinch = null;                                      // one finger left: it pans from here
            var rest = touches[Object.keys(touches)[0]];
            tPan = { x: rest.x, s: viewStart };
          }
        }
        canvas.addEventListener('pointerup', tEnd);
        canvas.addEventListener('pointercancel', tEnd);
        // a tap elsewhere, a scroll: the tapped tooltip goes (a mouse's leaves with the pointer)
        document.addEventListener('pointerdown', function (e) {
          if (e.pointerType === 'touch' && e.target !== canvas && tip) tip.style.display = 'none';
        });
        window.addEventListener('scroll', function () {
          if (tip && lastTap && !Object.keys(touches).length) tip.style.display = 'none';
        }, { passive: true });
      })();

      /* ── Toast (уведомления настроек и пр.) ───── */
      const appToast = document.getElementById("appToast");

      function showToast(msg, ok) {
        appToast.textContent = msg;
        appToast.className = "app-toast show " + (ok ? "success" : "fail");
        setTimeout(function () { appToast.className = "app-toast"; }, 2800);
      }

      /* ── Timeline controls ───────────────────── */
      var HISTORY_NICE_TICKS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
      function autoTickStep(rangeSec) {
        var t = HISTORY_NICE_TICKS[HISTORY_NICE_TICKS.length - 1];
        for (var ni = 0; ni < HISTORY_NICE_TICKS.length; ni++) {
          if (rangeSec / HISTORY_NICE_TICKS[ni] <= 30) { t = HISTORY_NICE_TICKS[ni]; break; }
        }
        return t;
      }
      /* ── История: the one head (IA §7) ──────── */
      // The filter of every sub-tab (histFilterFrom's shape): the period, «Свой…», the
      // printer. It lives in the address; the session keeps it while other screens are
      // on. The sub-tab on screen loads its data for it (histLoadSub).
      var histFilter = histFilterFrom({});
      var historySub = HIST_SUBS[0];
      // forge-history-sub kept the old switch's sub-view: read once (timeline → «Хронология»,
      // jobs → «Печати», anything else: «Печати»), then removed — the address keeps the sub.
      var HIST_SUB_KEY = "forge-history-sub";
      try { var histSaved = localStorage.getItem(HIST_SUB_KEY); if (histOwn(HIST_SUB_FROM_SAVED, histSaved)) historySub = HIST_SUB_FROM_SAVED[histSaved]; localStorage.removeItem(HIST_SUB_KEY); } catch (e) {}
      var histEls = {
        head: document.getElementById("histHead"), sub: document.getElementById("historySub"),
        period: document.getElementById("histPeriod"), custom: document.getElementById("histCustom"),
        from: document.getElementById("hist-from"), to: document.getElementById("hist-to"),
        apply: document.getElementById("hist-apply"), printer: document.getElementById("histPrinter"),
        sum: document.getElementById("histSum"), sumText: document.getElementById("histSumText"),
        csv: document.getElementById("jobsCsvBtn")
      };
      var histPrinterSig = "";

      function histPrinterLabel(pid) {
        var p = cachedAllPrinters.filter(function (x) { return String(x.id) === pid; })[0];
        return p && p.label ? String(p.label) : pid;
      }
      // The printer choice: the farm by name (P2 before P10), plus the chosen one if it is
      // not in the farm (yet). Rebuilt only when the farm or the choice changed, never
      // under the user's hand (an open list would close).
      function histPrinterOptions() {
        if (!histEls || !histEls.printer) return;
        var sel = histEls.printer, cur = histFilter.printer || "";
        var list = cachedAllPrinters.map(function (p) { return { id: String(p.id), label: String(p.label || p.id) }; });
        if (cur && !list.some(function (p) { return p.id === cur; })) list.push({ id: cur, label: cur });
        list.sort(function (a, b) { return a.label.localeCompare(b.label, "ru", { numeric: true }); });
        var sig = JSON.stringify(list) + "|" + cur;
        if (sig === histPrinterSig || document.activeElement === sel) return;
        histPrinterSig = sig;
        sel.innerHTML = '<option value="">Все</option>' + list.map(function (o) {
          return '<option value="' + safeText(o.id, "") + '">' + safeText(o.label, "") + '</option>';
        }).join("");
        sel.value = cur;
      }
      // The head shows the filter: the period pressed, «Свой…»'s days, the printer, a phone's line.
      function histRenderHead() {
        if (!histEls.head) return;
        var f = histFilter;
        histEls.period.querySelectorAll("button[data-period]").forEach(function (b) {
          var on = b.dataset.period === f.period;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        var custom = histEls.period.querySelector('button[data-period="custom"]');
        if (custom) custom.setAttribute("aria-expanded", histEls.custom.hidden ? "false" : "true");
        if (f.period === "custom") { histEls.from.value = f.from; histEls.to.value = f.to; }
        histPrinterOptions();
        histEls.sumText.textContent = histSummaryText(f, f.printer ? histPrinterLabel(f.printer) : "");
      }
      // «Свой…» unfolds С / По with the days of the window on screen; «Применить» takes them.
      function histOpenCustom(open) {
        histEls.custom.hidden = !open;
        if (open && histFilter.period !== "custom") {
          var now = Date.now() / 1000, w = histWindow(histFilter, now);
          histEls.from.value = histDayOf(w.fr);
          histEls.to.value = histDayOf(Math.max(w.fr, w.to - 1));
        }
        var today = histDayOf(Date.now() / 1000);
        histEls.from.max = histEls.to.max = today;
        histRenderHead();
      }
      // The admins' only parts: «⇩ Выгрузить» (on «Печати» and «Действия», IA §7; the server
      // answers a viewer 403) and «Действия». Before the role is known: neither.
      function histRoleUI() {
        if (!histEls || !histEls.sub) return;
        var admin = !!isAdmin();
        if (histEls.csv) histEls.csv.hidden = !(admin && (historySub === "jobs" || historySub === "actions"));
        var act = histEls.sub.querySelector('button[data-sub="actions"]');
        if (act) act.hidden = !admin;
      }
      // The sub-tab on screen: its button, its section, the loaders of the others put away.
      function setHistorySub(sub) {
        historySub = HIST_SUBS.indexOf(sub) >= 0 ? sub : HIST_SUBS[0];
        document.querySelectorAll("#historySub [data-sub]").forEach(function (b) {
          var on = b.dataset.sub === historySub;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        document.querySelectorAll('.view-panel[data-panel="history"] [data-hsub]').forEach(function (el) {
          el.hidden = el.dataset.hsub !== historySub;
        });
        // another sub-view's pending load must not keep its loader over this one
        var tl = document.getElementById("timelineLoader"), jl = document.getElementById("jobsLoader");
        if (tl && historySub !== "chrono") tl.style.display = "none";
        if (jl && historySub !== "jobs") jl.style.display = "none";
        histRoleUI();
        renderScrName();
      }
      // The sub-tab on screen loads its data for the head's filter.
      function histLoadSub() {
        if (historySub === "jobs") loadJobsView();
        else if (historySub === "chrono") loadTimeline();
        else if (historySub === "stats") loadHistStats();
        else if (historySub === "actions") loadActs();
      }
      // A new filter from the head: the address follows in place (a click on a period is no
      // history entry, «Назад» leaves the screen), the sub-tab on screen reloads.
      function histSetFilter(f) {
        f = histFilterFrom(f);
        if (histSameFilter(f, histFilter)) return;
        histFilter = f;
        histRenderHead();
        routeSync(false);
        histLoadSub();
      }
      // The address' query (Back / Forward, a typed or followed address) -> true when it changed the filter
      function histApplyQuery(q) {
        var f = histFilterFrom(q);
        if (histSameFilter(f, histFilter)) return false;
        histFilter = f;
        histEls.custom.hidden = f.period !== "custom";
        histRenderHead();
        return true;
      }
      (function initHistHead() {
        if (!histEls.head) return;
        document.querySelectorAll("[data-hist-legend]").forEach(function (el) { el.innerHTML = histLegendHtml(); });
        setHistorySub(historySub);
        histRenderHead();
        histEls.sub.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-sub]");
          if (!b || b.dataset.sub === historySub) return;
          setHistorySub(b.dataset.sub);
          routeSync(true);
          histLoadSub();
        });
        histEls.period.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-period]");
          if (!b) return;
          if (b.dataset.period === "custom") { histOpenCustom(histEls.custom.hidden); return; }
          histEls.custom.hidden = true;
          if (b.dataset.period === histFilter.period) { histRenderHead(); return; }
          histSetFilter({ period: b.dataset.period, printer: histFilter.printer });
        });
        histEls.apply.addEventListener("click", function () {
          var a = histEls.from.value || histEls.to.value, z = histEls.to.value || histEls.from.value;
          if (histDayStart(a) == null || histDayStart(z) == null) { showToast("Укажите даты «С» и «по»", false); return; }
          if (histDayStart(a) > histDayStart(z)) { showToast("Начало периода позже конца", false); return; }
          histSetFilter({ period: "custom", from: a, to: z, printer: histFilter.printer });
          histRenderHead();
        });
        histEls.printer.addEventListener("change", function () {
          histSetFilter({ period: histFilter.period, from: histFilter.from, to: histFilter.to, printer: histEls.printer.value || null });
        });
        // a phone: the head folds into «24 ч · все принтеры ▾», a tap unfolds the filters
        histEls.sum.addEventListener("click", function () {
          var open = !histEls.head.classList.contains("hist-open");
          histEls.head.classList.toggle("hist-open", open);
          histEls.sum.setAttribute("aria-expanded", open ? "true" : "false");
        });
        var helpBtn = document.getElementById("histHelpBtn"), help = document.getElementById("histHelp");
        if (helpBtn && help) helpBtn.addEventListener("click", function () {
          help.hidden = !help.hidden;
          helpBtn.setAttribute("aria-expanded", help.hidden ? "false" : "true");
        });
      })();

      /* ── История → Статистика ──────────────── */
      // The journal's numbers (/jobs/stats, fast) land first, the time shares
      // (states-summary: seconds on the Pi, «Считаем за …») fill the bars and «Загрузка»
      // later. A re-open within HIST_STATS_FRESH_MS on the same filter asks nothing.
      var HIST_STATS_FRESH_MS = 120000;
      // ssKey / ssAt: the window (histSummaryWindow's key) the shares on hand are for and when they
      // landed; ssReq: the states-summary on its way (one per window — a printer's clicks share it).
      var histStats = { gen: 0, key: "", at: 0, ss: null, st: null, ssErr: false, stErr: false,
                        ssKey: "", ssAt: 0, ssReq: null, stBusy: false };
      var histStatsEls = {
        kpi: document.getElementById("histStatsKpi"), body: document.getElementById("histStatsBody"),
        empty: document.getElementById("histStatsEmpty"), loader: document.getElementById("statesSummaryLoader"),
        days: document.getElementById("jobsDays"), reasons: document.getElementById("jobsReasons"),
        seg: document.getElementById("histReasonsSeg")
      };
      function renderHistStats() {
        var s = histStats, pid = histFilter.printer, els = histStatsEls;
        if (!els.body) return;
        var ss = s.ss || (s.ssErr ? [] : null);    // a failed summary: the prints, without bars
        els.kpi.innerHTML = s.st ? histKpiHtml(s.st, ss ? histUtil(ss, pid) : undefined) : "";
        var rows = histStatsRows(ss, s.st, cachedAllPrinters, pid);
        els.body.innerHTML = rows.map(function (r) { return histStatsRowHtml(r, !!ss); }).join("");
        var msg = s.ssErr && s.stErr ? "Не удалось загрузить статистику — откройте вкладку ещё раз"
          : s.ssErr ? "Доли времени не загрузились — откройте вкладку ещё раз"
          : s.stErr ? "Печати не загрузились — откройте вкладку ещё раз"
          : !rows.length && ss && s.st ? "Нет данных за период" : "";
        els.empty.hidden = !msg;
        els.empty.textContent = msg;
        els.days.innerHTML = s.st ? jobsDayBarsHtml(s.st) : "";
        els.reasons.innerHTML = s.st ? jobsReasonsHtml(s.st) : "";
      }
      async function histFetchJson(url) {
        var r = await apiFetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }
      function loadHistStats(force) {
        var els = histStatsEls;
        if (!els.body) return;
        var key = JSON.stringify(histQueryOf(histFilter));
        if (!force && key === histStats.key && Date.now() - histStats.at < HIST_STATS_FRESH_MS && !histStats.ssReq &&
            !histStats.stBusy && (histStats.ss || histStats.st)) { renderHistStats(); return; }
        var gen = ++histStats.gen, now = Date.now() / 1000, w = histWindow(histFilter, now);
        // The time shares do not depend on the printer: a printer picked or dropped keeps them (or the
        // request on its way) and asks /jobs/stats only — the shares are the costliest call on the Pi.
        var sw = histSummaryWindow(histFilter, now);
        var keepSs = sw.key === histStats.ssKey &&
          (!!histStats.ssReq || (!force && !!histStats.ss && Date.now() - histStats.ssAt < HIST_STATS_FRESH_MS));
        // the same filter later: the numbers on screen stay until the new ones land
        if (key !== histStats.key) { histStats.st = null; if (!keepSs) histStats.ss = null; }
        histStats.key = key;
        histStats.at = Date.now();
        histStats.stBusy = true;
        histStats.stErr = false;
        els.loader.textContent = histLoadingText(histFilter);
        els.loader.style.display = "";
        function settled() {
          if (histStats.ssReq || histStats.stBusy) return;
          els.loader.style.display = "none";
        }
        var q = jobsFilterQuery({ fr: w.fr, to: w.to, printer: histFilter.printer }, now, false);
        histFetchJson("/api/history/jobs/stats?" + q).then(function (st) {
          if (gen !== histStats.gen) return;
          histStats.st = st;
          histStats.stBusy = false;
          settled();
          renderHistStats();
        }, function (e) {
          if (gen !== histStats.gen) return;
          console.error("loadHistStats: jobs stats failed", e);
          histStats.stErr = true;
          histStats.stBusy = false;
          settled();
          renderHistStats();
        });
        if (!keepSs) {
          // a new window: its own request; the one of another window still on its way goes to waste
          histStats.ssKey = sw.key;
          histStats.ssErr = false;
          var req = histStats.ssReq = histFetchJson("/api/history/states-summary?fr=" + sw.fr + "&to=" + sw.to);
          req.then(function (ss) {
            if (histStats.ssReq !== req) return;
            histStats.ss = Array.isArray(ss) ? ss : [];
            histStats.ssAt = Date.now();
          }, function (e) {
            if (histStats.ssReq !== req) return;
            console.error("loadHistStats: states-summary failed", e);
            histStats.ssErr = true;
            histStats.ssAt = 0;
            histStats.at = 0;                          // the next open asks again
          }).then(function () {
            if (histStats.ssReq !== req) return;
            histStats.ssReq = null;
            settled();
            renderHistStats();
          });
        }
        renderHistStats();
      }
      (function initHistStats() {
        var els = histStatsEls;
        if (!els.body) return;
        // a row picks its printer for the whole head (again: all printers)
        els.body.addEventListener("click", function (e) {
          var tr = e.target.closest("tr[data-pid]");
          if (!tr) return;
          histSetFilter({ period: histFilter.period, from: histFilter.from, to: histFilter.to,
                          printer: histFilter.printer === tr.dataset.pid ? null : tr.dataset.pid });
        });
        // «Печатей» / «Успешно» open «Печати» with that filter
        els.kpi.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-kpi]");
          if (!b) return;
          jobsSetResults(b.dataset.kpi ? [b.dataset.kpi] : []);
          setHistorySub("jobs");
          routeSync(true);
          histLoadSub();
          window.scrollTo(0, 0);
        });
        els.seg.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-reasons]");
          if (!b) return;
          els.reasons.dataset.show = b.dataset.reasons;
          els.seg.querySelectorAll("button[data-reasons]").forEach(function (x) {
            var on = x === b;
            x.classList.toggle("active", on);
            x.setAttribute("aria-pressed", on ? "true" : "false");
          });
        });
      })();

      /* ── История → Печати: the print journal ──── */
      // The markup comes from the pure jobs-ui helpers; this part fetches, holds
      // the result filter and wires the controls (the window and the printer are the
      // head's, histFilter). While the sub-view is visible the list (first page) and
      // the stats refresh every JOBS_REFRESH_MS; the running rows follow the 3 s
      // status poll (jobsTick from load()) without requests. A click on a row unfolds
      // its details; the admins' get the auto-ignore's presses from the audit too.
      var JOBS_REFRESH_MS = 60000;
      var JOBS_AUTO_MS = 60000;   // the auto-ignore's audit rows: asked once a minute at most
      var JOBS_AUTO_LIMIT = 500;  // the audit API's le=500
      var jobsFilter = { results: [] };
      var jobsLoadedKey = "";     // the head's filter the list on screen is for
      var jobsRows = [];
      var jobsStats = null;
      var jobsHasMore = false;
      var jobsOffset = 0;         // offset of the last list page loaded
      var jobsNow = 0;            // the loaded window's "now": further pages ask the same window
      var jobsLoadGen = 0;        // the filter changed: older responses go to waste
      var jobsLoadedAt = 0;       // Date.now() of the last full load; 0: nothing current on screen
      var jobsBusy = false;
      var jobsStatsStale = false; // a full load was started and has not landed: a result chip reloads everything
      var jobsOpenRows = {};      // job id → unfolded; kept over reloads while the row is on the list
      var jobsAuto = { rows: null, at: 0, busy: false };
      var jobsEls = {
        panel: document.getElementById("jobsPanel"), head: document.getElementById("jobsHead"),
        body: document.getElementById("jobsBody"), wrap: document.getElementById("jobsTableWrap"),
        empty: document.getElementById("jobsEmpty"), more: document.getElementById("jobsMore"),
        count: document.getElementById("jobsCount"), loader: document.getElementById("jobsLoader"),
        results: document.getElementById("jobsResultBtns")
      };
      // The requests' filter at `now`: the head's window and printer, the result chips
      function jobsQueryFilter(now) {
        var w = histWindow(histFilter, now);
        return { fr: w.fr, to: w.to, printer: histFilter.printer, results: jobsFilter.results };
      }
      function renderJobsChips() {
        if (jobsEls.results) jobsEls.results.innerHTML = jobsChipsHtml(jobsStats, jobsFilter.results);
      }
      // The result chips' choice (a chip, or «Печатей» / «Успешно» of «Статистика»)
      function jobsSetResults(results) {
        jobsFilter.results = (results || []).filter(function (x) { return x && JOB_RESULTS[x]; });
        renderJobsChips();
      }
      function jobsVisible() {
        var panel = document.querySelector('.view-panel[data-panel="history"]');
        return historySub === "jobs" && !!jobsEls.panel && !!panel && panel.classList.contains("active");
      }
      function jobsPrinterMap() {
        var m = {};
        cachedAllPrinters.forEach(function (p) { m[p.id] = p; });
        return m;
      }
      // The admins' «автопропуск нажал ✓» in the details: the auto-ignore's audit rows, once a
      // row is unfolded (a viewer has no audit: nothing is asked)
      async function jobsAutoNeed() {
        if (!isAdmin() || jobsAuto.busy || Date.now() - jobsAuto.at < JOBS_AUTO_MS) return;
        if (!Object.keys(jobsOpenRows).length) return;
        jobsAuto.busy = true;
        try {
          var r = await apiFetch("/api/admin/audit?user=auto-ignore&limit=" + JOBS_AUTO_LIMIT);
          if (!r.ok) throw new Error("HTTP " + r.status);
          var d = await r.json();
          jobsAuto.rows = Array.isArray(d && d.rows) ? d.rows : [];
          jobsAuto.at = Date.now();
          renderJobsTable();
        } catch (e) {
          console.error("jobsAutoNeed failed", e);
          jobsAuto.at = Date.now();              // no retry storm: again in a minute
        } finally {
          jobsAuto.busy = false;
        }
      }
      function jobsDetailRow(j, now) {
        var hits = isAdmin() && jobsAuto.rows ? jobAutoHits(j, jobsAuto.rows, now) : [];
        return '<tr class="job-xrow" data-xfor="' + safeText(j.id, "") + '"><td colspan="' + JOB_COLS.length + '">' +
          jobDetailHtml(j, { now: now, hits: hits }) + '</td></tr>';
      }
      function renderJobsTable() {
        var now = Date.now() / 1000, printers = jobsPrinterMap(), kept = {};
        // a re-render (the audit landed, a reload) keeps the focus on its row
        var focused = document.activeElement, fid = focused && jobsEls.body.contains(focused) && focused.dataset
          ? focused.dataset.jobId : null;
        jobsEls.head.innerHTML = jobsHeadHtml();
        jobsEls.body.innerHTML = jobsRows.map(function (j) {
          var open = !!jobsOpenRows[j.id];
          if (open) kept[j.id] = true;
          return jobRowHtml(j, j.result == null ? printers[j.printer_id] || null : null, { now: now, open: open }) +
            (open ? jobsDetailRow(j, now) : "");
        }).join("");
        jobsOpenRows = kept;
        var n = jobsRows.length;
        jobsEls.wrap.hidden = !n;
        jobsEls.empty.hidden = !!n;
        jobsEls.empty.textContent = histFilter.printer || jobsFilter.results.length
          ? "Печатей с такими фильтрами за период нет" : "Печатей за период нет";
        jobsEls.more.hidden = !jobsHasMore;
        jobsEls.more.textContent = "Показать ещё " + JOBS_PAGE;
        jobsEls.count.textContent = n ? jobsShownText(n, jobsStats, jobsFilter.results, jobsHasMore) : "";
        var back = fid ? jobsEls.body.querySelector('tr.job-row[data-job-id="' + fid + '"]') : null;
        if (back) back.focus({ preventScroll: true });
        jobsAutoNeed();
      }
      function renderJobsView() {
        renderJobsChips();
        renderJobsTable();
      }
      function renderJobsError() {
        ["head", "body"].forEach(function (k) { jobsEls[k].innerHTML = ""; });
        renderJobsChips();
        jobsEls.wrap.hidden = true;
        jobsEls.more.hidden = true;
        jobsEls.count.textContent = "";
        jobsEls.empty.hidden = false;
        jobsEls.empty.textContent = "Не удалось загрузить журнал — повторим через минуту";
      }
      // The list's first page and the stats of the current filter, in parallel.
      // listOnly (a result chip: the stats never carry that filter) keeps the stats.
      async function loadJobsView(opts) {
        if (!jobsEls.panel) return;
        var gen = ++jobsLoadGen;
        var now = Date.now() / 1000, f = jobsQueryFilter(now);
        // nothing current on screen, or it is another period / printer: the loader, not the old list
        var key = JSON.stringify(histQueryOf(histFilter));
        var first = !jobsLoadedAt || key !== jobsLoadedKey;
        jobsLoadedKey = key;
        // a full load still in flight (a new period): its stats are not on screen yet
        var listOnly = !!(opts && opts.listOnly) && !!jobsStats && !jobsStatsStale;
        if (!listOnly) jobsStatsStale = true;
        jobsLoadedAt = Date.now();
        jobsBusy = true;
        if (first) {
          jobsEls.loader.textContent = histLoadingText(histFilter);
          jobsEls.loader.style.display = "";
        }
        try {
          var res = await Promise.all([apiFetch("/api/history/jobs?" + jobsQuery(f, now, 0))].concat(
            listOnly ? [] : [apiFetch("/api/history/jobs/stats?" + jobsStatsQuery(f, now))]));
          if (!res.every(function (r) { return r.ok; })) {
            throw new Error("HTTP " + res.map(function (r) { return r.status; }).join(" / "));
          }
          var page = await res[0].json(), stats = listOnly ? jobsStats : await res[1].json();
          if (gen !== jobsLoadGen) return;
          jobsRows = page.rows || [];
          jobsHasMore = !!page.has_more;
          jobsOffset = 0;
          jobsNow = now;
          jobsStats = stats;
          jobsStatsStale = false;
          if (listOnly) renderJobsTable(); else renderJobsView();
        } catch (e) {
          if (gen !== jobsLoadGen) return;
          console.error("loadJobsView failed", e);
          if (first || !jobsStats) { jobsStats = null; renderJobsError(); }
        } finally {
          if (gen === jobsLoadGen) {
            jobsBusy = false;
            jobsEls.loader.style.display = "none";
          }
        }
      }
      function jobsReload(opts) {
        jobsLoadedAt = 0;
        loadJobsView(opts);
      }
      async function loadMoreJobs() {
        if (!jobsHasMore || jobsBusy) return;
        var gen = jobsLoadGen, offset = jobsOffset + JOBS_PAGE;
        jobsBusy = true;
        jobsEls.more.disabled = true;
        try {
          var res = await apiFetch("/api/history/jobs?" + jobsQuery(jobsQueryFilter(jobsNow), jobsNow, offset));
          if (!res.ok) throw new Error("HTTP " + res.status);
          var page = await res.json();
          if (gen !== jobsLoadGen) return;
          var seen = {};
          jobsRows.forEach(function (j) { seen[j.id] = true; });
          jobsRows = jobsRows.concat((page.rows || []).filter(function (j) { return !seen[j.id]; }));
          jobsHasMore = !!page.has_more;
          jobsOffset = offset;
          renderJobsTable();
        } catch (e) {
          if (gen !== jobsLoadGen) return;
          console.error("loadMoreJobs failed", e);
          showToast("Не удалось загрузить печати", false);
        } finally {
          if (gen === jobsLoadGen) jobsBusy = false;
          jobsEls.more.disabled = false;
        }
      }
      function jobsSetCell(tr, sel, html) {
        var td = tr.querySelector(sel);
        if (td && td._jobsHtml !== html) { td.innerHTML = html; td._jobsHtml = html; }
      }
      // Every status poll: the periodic reload, else the live cells of the running rows
      function jobsTick() {
        if (!jobsVisible()) return;
        if (!jobsBusy && jobsLoadedAt && jobsOffset === 0 && Date.now() - jobsLoadedAt >= JOBS_REFRESH_MS) {
          loadJobsView();
          return;
        }
        var open = jobsEls.body.querySelectorAll("tr.job-open");
        if (!open.length) return;
        var now = Date.now() / 1000, printers = jobsPrinterMap(), byId = {};
        jobsRows.forEach(function (j) { if (j.result == null) byId[j.id] = j; });
        open.forEach(function (tr) {
          var j = byId[tr.dataset.jobId];
          if (!j) return;
          jobsSetCell(tr, "td.job-state", jobStateHtml(j, printers[j.printer_id] || null, now));
          jobsSetCell(tr, "td.job-dur", jobDurCellHtml(j, now));
        });
      }
      // A row folds or unfolds its details; the focus stays on it (renderJobsTable)
      function jobsToggleRow(id) {
        if (jobsOpenRows[id]) delete jobsOpenRows[id]; else jobsOpenRows[id] = true;
        var tr = jobsEls.body.querySelector('tr.job-row[data-job-id="' + id + '"]');
        if (tr) tr.focus({ preventScroll: true });
        renderJobsTable();
      }

      (function initJobsView() {
        if (!jobsEls.panel) return;
        renderJobsChips();
        jobsEls.results.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-result]");
          if (!b) return;
          jobsSetResults(jobsResultsClick(jobsFilter.results, b.dataset.result, e.ctrlKey || e.metaKey));
          var again = jobsEls.results.querySelector('button[data-result="' + b.dataset.result + '"]');
          if (again) again.focus({ preventScroll: true });
          jobsReload({ listOnly: true });
        });
        document.getElementById("jobsCsvBtn").addEventListener("click", function () {
          if (!isAdmin()) return;
          var now = Date.now() / 1000;
          var url = historySub === "actions" ? actsCsvUrl(acts.filter, histWindow(histFilter, now), histFilter.printer)
            : jobsCsvUrl(jobsQueryFilter(now), now);
          // the column names in the panel's language (app/web/i18n.js); none in Russian
          var lang = typeof I18N === "object" ? I18N.lang() : "ru";
          if (lang !== "ru") url += "&lang=" + encodeURIComponent(lang);
          // a plain navigation cannot refresh an expired access cookie: refresh it first
          apiFetch("/api/auth/me").then(function () { window.location.href = url; }, function () {});
        });
        jobsEls.more.addEventListener("click", loadMoreJobs);
        jobsEls.body.addEventListener("click", function (e) {
          var tr = e.target.closest("tr.job-row");
          if (tr && !e.target.closest("a, button")) jobsToggleRow(Number(tr.dataset.jobId));
        });
        jobsEls.body.addEventListener("keydown", function (e) {
          if (e.key !== "Enter" && e.key !== " ") return;
          var tr = e.target.closest("tr.job-row");
          if (!tr || tr !== e.target) return;
          e.preventDefault();
          jobsToggleRow(Number(tr.dataset.jobId));
        });
      })();

      /* ── История → Действия: the audit journal (admins, IA §7) ──── */
      // The markup comes from the pure acts-ui helpers; this part fetches and wires. The head's
      // window and printer apply (histFilter); a chip, «Кто» and «Служебные» reload the list only
      // (the counts are per user and action, the chips read them for any of those). By AUDIT_LIMIT
      // with «Показать ещё» (IA §10), no endless loading. Loads come from histLoadSub — after the
      // IIFE (a microtask from setActiveView), so AUDIT_LIMIT is assigned by then.
      var acts = { filter: { group: "", who: "", svc: false }, rows: [], hasMore: false, offset: 0, w: null,
                   gen: 0, busy: false, counts: null, open: {} };
      var actsEls = {
        panel: document.getElementById("actsPanel"), chips: document.getElementById("actsChips"),
        who: document.getElementById("actsWho"), svc: document.getElementById("actsSvc"),
        body: document.getElementById("actsBody"), wrap: document.getElementById("actsTableWrap"),
        empty: document.getElementById("actsEmpty"), more: document.getElementById("actsMore"),
        count: document.getElementById("actsCount")
      };
      function actsLabels() {
        var m = {};
        cachedAllPrinters.forEach(function (p) { m[p.id] = p.label; });
        return m;
      }
      function renderActsHead() {
        var c = acts.counts ? actCounts(acts.counts, acts.filter.who) : null;
        actsEls.chips.innerHTML = actChipsHtml(c, acts.filter);
        var svc = actSvcHtml(c, acts.filter.svc);
        actsEls.svc.textContent = svc.text;
        actsEls.svc.setAttribute("data-tip", svc.tip);
        actsEls.svc.classList.toggle("on", acts.filter.svc);
        actsEls.svc.setAttribute("aria-pressed", acts.filter.svc ? "true" : "false");
        var opts = actWhoOptionsHtml(acts.counts, acts.filter.who);
        if (actsEls.who._opts !== opts) { actsEls.who.innerHTML = opts; actsEls.who._opts = opts; }
        actsEls.who.value = acts.filter.who;
        return c;
      }
      function renderActs() {
        var c = renderActsHead(), labels = actsLabels(), kept = {};
        var focused = document.activeElement, fid = focused && actsEls.body.contains(focused) && focused.dataset
          ? focused.dataset.actId : null;
        actsEls.body.innerHTML = acts.rows.map(function (e) {
          var open = !!acts.open[e.id];
          if (open) kept[e.id] = true;
          return actRowHtml(e, labels, open);
        }).join("");
        acts.open = kept;
        var n = acts.rows.length, total = actTotal(c, acts.filter);
        actsEls.wrap.hidden = !n;
        actsEls.empty.hidden = !!n;
        actsEls.empty.textContent = acts.filter.group || acts.filter.who || histFilter.printer
          ? "Действий с такими фильтрами за период нет" : "Действий за период нет";
        actsEls.more.hidden = !acts.hasMore;
        actsEls.more.textContent = "Показать ещё " + AUDIT_LIMIT;
        actsEls.count.textContent = n ? "показано " + n + (total != null && total >= n ? " из " + total : "") : "";
        var back = fid ? actsEls.body.querySelector('tr.act-row[data-act-id="' + fid + '"]') : null;
        if (back) back.focus({ preventScroll: true });
      }
      function renderActsError() {
        renderActsHead();
        actsEls.body.innerHTML = "";
        actsEls.wrap.hidden = true;
        actsEls.more.hidden = true;
        actsEls.count.textContent = "";
        actsEls.empty.hidden = false;
        actsEls.empty.textContent = "Не удалось загрузить действия — откройте вкладку ещё раз";
      }
      // The first page (and, unless listOnly, the window's counts) of the current filter.
      async function loadActs(listOnly) {
        if (!actsEls.panel || !isAdmin()) return;
        var gen = ++acts.gen, w = histWindow(histFilter, Date.now() / 1000), pid = histFilter.printer;
        acts.w = w;
        acts.busy = true;
        try {
          var res = await Promise.all([apiFetch("/api/admin/audit?" + actsQuery(acts.filter, w, pid, 0))].concat(
            listOnly && acts.counts ? [] : [apiFetch("/api/admin/audit/counts?" + actsCountsQuery(w, pid))]));
          if (!res.every(function (r) { return r.ok; })) {
            throw new Error("HTTP " + res.map(function (r) { return r.status; }).join(" / "));
          }
          var page = await res[0].json(), counts = res[1] ? await res[1].json() : null;
          if (gen !== acts.gen) return;
          acts.rows = page.rows || [];
          acts.hasMore = !!page.has_more;
          acts.offset = 0;
          if (counts) acts.counts = counts.rows || [];
          renderActs();
        } catch (e) {
          if (gen !== acts.gen) return;
          console.error("loadActs failed", e);
          renderActsError();
        } finally {
          if (gen === acts.gen) acts.busy = false;
        }
      }
      async function loadMoreActs() {
        if (!acts.hasMore || acts.busy || !acts.w) return;
        var gen = acts.gen, offset = acts.offset + AUDIT_LIMIT;
        acts.busy = true;
        actsEls.more.disabled = true;
        try {
          var r = await apiFetch("/api/admin/audit?" + actsQuery(acts.filter, acts.w, histFilter.printer, offset));
          if (!r.ok) throw new Error("HTTP " + r.status);
          var page = await r.json();
          if (gen !== acts.gen) return;
          var seen = {};
          acts.rows.forEach(function (e) { seen[e.id] = true; });
          acts.rows = acts.rows.concat((page.rows || []).filter(function (e) { return !seen[e.id]; }));
          acts.hasMore = !!page.has_more;
          acts.offset = offset;
          renderActs();
        } catch (e) {
          if (gen !== acts.gen) return;
          console.error("loadMoreActs failed", e);
          showToast("Не удалось загрузить действия", false);
        } finally {
          if (gen === acts.gen) acts.busy = false;
          actsEls.more.disabled = false;
        }
      }
      // «Срабатывания ›» of «Админ → Автоматика»: the automation's rows of «Действия».
      function actsOpenGroup(group) {
        if (!isAdmin()) return;
        acts.filter.group = actGroup(group) ? group : "";
        acts.filter.who = "";
        setHistorySub("actions");
        setActiveView("history");
        window.scrollTo(0, 0);
      }
      function actsToggleRow(id) {
        if (acts.open[id]) delete acts.open[id]; else acts.open[id] = true;
        var tr = actsEls.body.querySelector('tr.act-row[data-act-id="' + id + '"]');
        if (tr) tr.focus({ preventScroll: true });
        renderActs();
      }
      (function initActs() {
        if (!actsEls.panel) return;
        actsEls.chips.addEventListener("click", function (e) {
          var b = e.target.closest("button[data-act-group]");
          if (!b) return;
          acts.filter.group = actGroupClick(acts.filter.group, b.dataset.actGroup);
          renderActsHead();
          var again = actsEls.chips.querySelector('button[data-act-group="' + acts.filter.group + '"]');
          if (again) again.focus({ preventScroll: true });
          loadActs(true);
        });
        actsEls.who.addEventListener("change", function () {
          acts.filter.who = actsEls.who.value || "";
          renderActsHead();
          loadActs(true);
        });
        actsEls.svc.addEventListener("click", function () {
          acts.filter.svc = !acts.filter.svc;
          renderActsHead();
          loadActs(true);
        });
        actsEls.more.addEventListener("click", loadMoreActs);
        actsEls.body.addEventListener("click", function (e) {
          var tr = e.target.closest("tr.act-row");
          if (tr && !e.target.closest("a, button")) actsToggleRow(Number(tr.dataset.actId));
        });
        actsEls.body.addEventListener("keydown", function (e) {
          if (e.key !== "Enter" && e.key !== " ") return;
          var tr = e.target.closest("tr.act-row");
          if (!tr || tr !== e.target) return;
          e.preventDefault();
          actsToggleRow(Number(tr.dataset.actId));
        });
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
      var adJournalAll = false;
      var adDlgBusy = false;
      var adInited = false;
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
            rng.querySelectorAll('button').forEach(function (x) {
              x.classList.toggle('active', x === b);
              x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
            });
            loadAms();
          });
        });
        // Данные пишутся раз в минуту — чаще опрашивать смысла нет. «Админ → Автоматика» on
        // screen follows the engine the same way (its release cells, the plans of the blocks).
        setInterval(function () {
          if (amsPanelActive()) loadAms();
          else if (adAutoPaneActive()) loadAutodry();
        }, 60000);
        var grid = document.getElementById('amsGrid');
        if (grid) grid.addEventListener('click', function (e) {
          // a phone: the row unfolds its block (slots, charts, buttons) and folds it again
          var row = e.target.closest('[data-ams-row]');
          if (row && grid.contains(row)) { amsRowSet(row.dataset.amsRow, row.getAttribute('aria-expanded') !== 'true'); return; }
          var btn = e.target.closest('[data-ams-dry-open],[data-ams-dry-stop],[data-ams-dry-stop-go],[data-ams-dry-stop-no]');
          if (!btn || !grid.contains(btn) || btn.disabled) return;
          // The document click handler would close the popover this click opens.
          e.stopPropagation();
          var pid = btn.dataset.pid, idx = parseInt(btn.dataset.ams, 10), key = pid + '__' + idx;
          var p = findPrinter(pid);
          var unit = p ? amsUnits(p).find(function (x) { return x.index === idx; }) : null;
          if (!p || !unit || !unit.dry) return;
          if (btn.hasAttribute('data-ams-dry-open')) {
            closePop(true);
            openPop(btn, pid, dryPopHtml(p, unit), 'dry', 'amsTab');
            applyAndRender();
          } else if (btn.hasAttribute('data-ams-dry-stop-no')) {
            amsAskSet(null);
          } else if (amsReply[key] && amsReply[key].k === 'wait') {
            return;                                   // until the reply: no second stop
          } else if (btn.hasAttribute('data-ams-dry-stop')) {
            amsAskSet(key);                           // «Остановить сушку?» in the block's line
          } else {
            amsAskSet(null);
            amsReplySet(key, cmdWaitLine('dry_stop'));
            sendPrinterCmd(pid, 'dry_stop', { ams: idx }).then(function (r) { amsReplySet(key, r.line); });
          }
        });
      }
      // The AMS tab answers a block's command on the block (IA §10): key -> its last line, kept
      // through the minute's redraw (amsCardShell); a block not on screen — a toast instead.
      var amsReply = {};
      function amsReplySet(key, line) {
        var card = document.querySelector('#amsGrid .ams-card[data-key="' + key + '"]');
        amsReply[key] = line;
        if (!replyIn(card, line)) { delete amsReply[key]; return; }
        if (line.k !== 'bad' && line.k !== 'wait') {
          setTimeout(function () { if (amsReply[key] === line) delete amsReply[key]; }, CMD_RESULT_MS);
        }
      }
      // A block's «Остановить сушку?» (in place, IA §10): one block at a time, cancelled by itself
      // CONFIRM_MS after it was asked, as a card's; key null — no question.
      var amsAsk = null, amsAskToken = null, amsUnitsShown = [];
      function amsAskSet(key) {
        var was = amsAsk, token = {};
        amsAsk = key;
        amsAskToken = token;
        if (was && was !== key) amsDryRedraw(was);
        if (!key) return;
        amsDryRedraw(key);
        setTimeout(function () {
          if (amsAskToken !== token || amsAsk !== key) return;
          amsAsk = null;
          amsDryRedraw(key);
        }, CONFIRM_MS);
      }
      // Redraw one block's drying line in place (the charts stay): the block as amsCardShell draws it now.
      function amsDryRedraw(key) {
        var u = amsUnitsShown.filter(function (x) { return x.printer_id + '__' + x.unit_index === key; })[0];
        var card = document.querySelector('#amsGrid .ams-card[data-key="' + key + '"]');
        if (!u || !card) return;
        var tmp = document.createElement('div');
        tmp.innerHTML = amsCardShell(u, false);
        var fresh = tmp.querySelector('.ams-dry'), old = card.querySelector('.ams-dry');
        if (fresh && old) old.replaceWith(fresh);
      }
      // A phone's AMS row: unfold / fold its block in place (no re-render); key null — fold every block.
      function amsRowSet(key, open) {
        if (key == null) amsRowOpen = {};
        else if (open) amsRowOpen[key] = true;
        else delete amsRowOpen[key];
        document.querySelectorAll('#amsGrid [data-ams-row]').forEach(function (r) {
          var on = !!amsRowOpen[r.dataset.amsRow], card = r.nextElementSibling;
          r.setAttribute('aria-expanded', on ? 'true' : 'false');
          if (card && card.classList.contains('ams-card')) card.classList.toggle('ph-closed', !on);
        });
      }
      // The zones follow the auto-dry goal once its config is loaded (IA §6, amsZones).
      function amsZonesNow() { return amsZones(amsAutodry && amsAutodry.config); }
      function amsHumClass(pct, lvl) {
        if (pct != null) return amsHumZone(pct, amsZonesNow());
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
      // The temperature chart adds geo ({W,H,PL,PR,PT,PB}, default AMS_G*), span (the
      // left X label, default "-<amsHours>ч"), gid (the gradient id) and band / target
      // (amsChartOverlay). A point with v == null lifts the pen: line and area are drawn
      // per run (AMS points never have one, so their output stays byte-for-byte); with
      // opts.lone a run of one point is drawn as a dot (a zero-length segment, round caps).
      function amsChart(points, fr, to, ymin, ymax, color, opts) {
        opts = opts || {};
        var g = opts.geo || { W: AMS_GW, H: AMS_GH, PL: AMS_GPL, PR: AMS_GPR, PT: AMS_GPT, PB: AMS_GPB };
        var W = g.W, H = g.H, PL = g.PL, PR = g.PR, PT = g.PT, PB = g.PB;
        if (!points.some(function (p) { return p.v != null; })) return '<div class="ams-nodata">нет данных за период</div>';
        var iw = W - PL - PR, ih = H - PT - PB;
        function X(t) { return PL + iw * Math.max(0, Math.min(1, (t - fr) / (to - fr))); }
        function Y(v) { return PT + ih * (1 - (v - ymin) / (ymax - ymin)); }
        var line = '', area = '', run = [];
        function flush() {
          if (!run.length) return;
          var l = '';
          run.forEach(function (p, i) {
            l += (i ? 'L' : 'M') + X(p.t).toFixed(1) + ' ' + Y(p.v).toFixed(1) + ' ';
          });
          line += l + (opts.lone && run.length === 1 ? 'L' + X(run[0].t).toFixed(1) + ' ' + Y(run[0].v).toFixed(1) + ' ' : '');
          area += l + 'L' + X(run[run.length - 1].t).toFixed(1) + ' ' + (PT + ih) +
            ' L' + X(run[0].t).toFixed(1) + ' ' + (PT + ih) + ' Z';
          run = [];
        }
        points.forEach(function (p) { if (p.v == null) flush(); else run.push(p); });
        flush();
        var gl = '', yl = '';
        for (var k = 0; k <= 2; k++) {
          var val = ymin + (ymax - ymin) * k / 2, y = PT + ih * (1 - k / 2);
          gl += '<line class="gridline" x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1) + '"/>';
          yl += '<text class="axis" x="' + (PL - 4) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + Math.round(val) + '</text>';
        }
        var bandRects = '';
        if (opts.zones) {
          // true: the fixed 25 / 40 bands; an array: [[from, to, colour], ...] (the AMS tab: from the goal)
          var zones = Array.isArray(opts.zones) ? opts.zones : [[0, 25, 'var(--ok)'], [25, 40, 'var(--warn)'], [40, 50, 'var(--danger)']];
          zones.forEach(function (z) {
            if (!(z[1] > ymin && z[0] < ymax)) return;
            var yTop = Y(Math.min(z[1], ymax)), yBot = Y(Math.max(z[0], ymin));
            bandRects += '<rect x="' + PL + '" y="' + yTop.toFixed(1) + '" width="' + iw +
              '" height="' + (yBot - yTop).toFixed(1) + '" fill="' + z[2] + '" opacity="0.05"/>';
          });
        }
        var ov = amsChartOverlay({ W: W, H: H, PL: PL, PR: PR, PT: PT, PB: PB }, fr, to, ymin, ymax, opts);
        var gid = opts.gid || 'amsg' + Math.round(Math.abs(color.length * 13 + ymax * 7 + points.length));
        var span = opts.span != null ? opts.span : '-' + amsHours + 'ч';
        var xl = '<text class="axis" x="' + PL + '" y="' + (H - 4) + '" text-anchor="start">' + span + '</text>' +
          '<text class="axis" x="' + (W - PR) + '" y="' + (H - 4) + '" text-anchor="end">сейчас</text>';
        return '<svg class="ams-chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
          '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0" stop-color="' + color + '" stop-opacity="0.28"/>' +
          '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
          bandRects + gl + ov.under +
          '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
          '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round"' +
          (opts.lone ? ' stroke-linecap="round"' : '') + '/>' +
          ov.labels + yl + xl + '</svg>';
      }
      // hours: the window the time is for (default: the AMS tab's amsHours); > 24 h adds the date.
      function amsTipTime(t, hours) {
        var d = new Date(t * 1000);
        var s = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
        return (hours != null ? hours : amsHours) > 24 ? pad2(d.getDate()) + (typeof I18N === 'object' ? I18N.dateSep() : '.') + pad2(d.getMonth() + 1) + ' ' + s : s;
      }
      // Ховер по графику: вертикальная линия, маркер ближайшей точки и тултип «время · значение».
      // The temperature chart passes opts: geo (as amsChart), hours (amsTipTime), tip(best)
      // -> the tooltip text (best.i = the point's index) and clamp (the tooltip stays
      // inside the box: a popover would scroll sideways). Points with v == null are skipped.
      function amsAttachHover(box, points, fr, to, ymin, ymax, color, suf, opts) {
        opts = opts || {};
        var g = opts.geo || { W: AMS_GW, H: AMS_GH, PL: AMS_GPL, PR: AMS_GPR, PT: AMS_GPT, PB: AMS_GPB };
        var svg = box.querySelector('svg.ams-chart');
        if (!svg || !points.length) return;
        box.classList.add('ams-gbox');
        var iw = g.W - g.PL - g.PR, ih = g.H - g.PT - g.PB;
        var pts = [];
        points.forEach(function (p, i) {
          if (p.v == null) return;
          pts.push({
            x: g.PL + iw * Math.max(0, Math.min(1, (p.t - fr) / (to - fr))),
            y: g.PT + ih * (1 - (p.v - ymin) / (ymax - ymin)),
            t: p.t, v: p.v, i: i
          });
        });
        if (!pts.length) return;
        var line = document.createElement('div'); line.className = 'ams-hline';
        var dot = document.createElement('div'); dot.className = 'ams-hdot';
        var tip = document.createElement('div'); tip.className = 'ams-htip';
        line.style.top = (g.PT / g.H * 100) + '%';
        line.style.height = (ih / g.H * 100) + '%';
        dot.style.background = color;
        box.appendChild(line); box.appendChild(dot); box.appendChild(tip);
        function hide() {
          line.style.display = 'none'; dot.style.display = 'none'; tip.style.display = 'none';
        }
        hide();
        function move(e) {
          var rect = svg.getBoundingClientRect();
          if (!rect.width) return;
          var vx = (e.clientX - rect.left) / rect.width * g.W;
          var best = pts[0];
          for (var i = 1; i < pts.length; i++) {
            if (Math.abs(pts[i].x - vx) < Math.abs(best.x - vx)) best = pts[i];
          }
          var xp = best.x / g.W * 100;
          var yp = Math.max(0, Math.min(1, best.y / g.H)) * 100;
          line.style.left = xp + '%'; line.style.display = '';
          dot.style.left = xp + '%'; dot.style.top = yp + '%'; dot.style.display = '';
          tip.textContent = opts.tip ? opts.tip(best) : amsTipTime(best.t, opts.hours) + ' · ' + (Math.round(best.v * 10) / 10) + suf;
          tip.style.display = '';
          if (opts.clamp) {
            var bw = box.clientWidth, tw = tip.offsetWidth;
            tip.style.left = Math.max(0, Math.min(bw - tw, xp / 100 * bw - tw / 2)) + 'px';
            tip.style.transform = 'none';
            return;
          }
          tip.style.left = xp + '%';
          tip.style.transform = xp < 18 ? 'translateX(0)' : xp > 82 ? 'translateX(-100%)' : 'translateX(-50%)';
        }
        box.addEventListener('pointermove', move);
        box.addEventListener('pointerdown', move);
        // a touch pointer leaves right after pointerup: on the temperature chart a tap keeps
        // its tooltip until the next tap (the box is replaced on a redraw or the popover's close)
        box.addEventListener('pointerleave', function (e) { if (!opts.clamp || e.pointerType === 'mouse') hide(); });
      }
      // Card title only: unit_index is the physical unit id (history key) — 0-3 for
      // AMS / AMS 2 Pro / AMS Lite, 128-135 for AMS HT.
      function amsUnitName(index) {
        return index >= 128 ? 'AMS HT ' + (index - 127) : 'AMS ' + (index + 1);
      }
      // multi: the printer has more than one block (its row names the block). A phone shows a
      // row (amsRowText) over each card, the card folded unless amsRowOpen[key] (plan step 15).
      var amsRowOpen = {};
      function amsCardShell(u, multi) {
        var cls = 'ams-card' + (u.drying ? ' drying' : '') +
          (!u.drying && amsHumClass(u.humidity_pct, u.humidity) === 'bad' ? ' bad' : '');
        var hcls = amsHumClass(u.humidity_pct, u.humidity);
        var humTxt = u.humidity_pct != null ? '💧 ' + u.humidity_pct + '%'
          : (u.humidity != null ? '💧 ' + u.humidity + '/5' : '💧 н/д');
        var humBadge = '<span class="ams-badge ' + hcls + '">' + humTxt + '</span>', badges = humBadge;
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
        var key = u.printer_id + '__' + u.unit_index;
        // one drying line (IA §6): the printer's drying and the auto-dry's word for the unit together
        var adu = amsAutodry ? autodryUnit(key) : null, auto = null;
        if (adu) {
          var al = autodryLine(adu, amsAutodry.config, amsAutodry.limits, amsAutodry.now);
          auto = { card: al.card, cls: al.cls, status: adu.status };
        }
        var dry = amsTabDryHtml(u, p, can, auto, amsAsk === key);
        var amsModel = u.dry && u.dry.type === 'n3f' ? 'AMS 2 Pro' : u.dry && u.dry.type === 'n3s' ? 'AMS HT' : '';
        var z = amsZonesNow();
        var open = !!amsRowOpen[key];
        var row = '<button type="button" class="ams-row' + (dryActive(u) ? ' drying' : '') + '" data-ams-row="' + key +
          '" aria-expanded="' + (open ? 'true' : 'false') + '"><span class="ams-row-n">' + escHtml(u.label) +
          (multi ? ' · ' + amsUnitName(u.unit_index) : '') + '</span>' + humBadge +
          '<span class="ams-row-s">' + amsRowText(u, auto) + '</span><span class="ams-row-chev" aria-hidden="true">▾</span></button>';
        // «H1 · AMS 1 · AMS 2 Pro», the printer's model under it
        return row + '<div class="ams-card' + cls.slice(8) + (open ? '' : ' ph-closed') + '" data-key="' + key + '">' +
          '<div class="ams-chead"><div class="ams-spool">🧵</div>' +
          '<div class="ams-ctitle"><div class="ams-cname">' + escHtml(u.label) + ' · ' + amsUnitName(u.unit_index) +
          (amsModel ? ' · ' + amsModel : '') + ' <span class="ams-dot ' + (u.online ? 'on' : 'off') + '"></span></div>' +
          '<div class="ams-csub">' + (u.device_type ? escHtml(u.device_type) : 'AMS') + '</div></div>' +
          '<div class="ams-badges">' + badges + '</div></div>' +
          (u.maintenance ? '<div class="ams-maint"><span class="ams-badge warn">на обслуживании</span></div>' : '') +
          slots + dry +
          // the block's command answer (IA §10, amsReplySet): kept through the minute's redraw
          '<div class="reply-slot" data-ams-reply="' + key + '">' + cmdLineHtml(amsReply[key], 'data-reply-x') + '</div>' +
          '<div class="ams-gwrap"><div class="ams-gtitle"><span class="t">' +
          (u.humidity_pct != null ? 'Влажность, %' + (z.goal ? ' · зелёное — цель ≤ ' + z.ok + ' %' : '')
            : 'Уровень влажности (0–5)') + '</span>' +
          '<span class="mm" data-mm="hum-' + key + '"></span></div>' +
          '<div data-g="hum-' + key + '"><div class="ams-nodata">загрузка…</div></div></div>' +
          '<div class="ams-gwrap"><div class="ams-gtitle"><span class="t">Температура, °C · полосы — сушка</span>' +
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
        // IA §6: the zones from the goal, the chart from 0 (a 5 % goal sits under the old 10–50
        // range), one line — the goal (the start is one point above it, its own line only overlapped)
        var z = amsZonesNow(), top = amsHumTop(humPts.map(function (p) { return p.v; }), z);
        var adc = amsAutodry && amsAutodry.config;
        var thr = adc && adc.enabled ? [[adc.target_rh, 'цель', 'var(--ok)']] : [];
        humBox.innerHTML = usePct
          ? amsChart(humPts, fr, to, 0, top, amsHumColorVar(hcls), { zones: amsZoneBands(z, top), dry: bands, thr: thr })
          : amsChart(humPts, fr, to, 0, 5, 'var(--ok)', { dry: bands });
        tempBox.innerHTML = amsChart(tempPts, fr, to, 20, 60, 'var(--info)', { dry: bands });
        if (usePct) amsAttachHover(humBox, humPts, fr, to, 0, top, amsHumColorVar(hcls), '%');
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
          return;
        }
        if (empty) empty.style.display = 'none';
        // by printer name (IA §6): a block keeps its place whatever it does
        units.sort(amsUnitOrder);
        amsUnitsShown = units;
        var perPrinter = {};
        units.forEach(function (u) { perPrinter[u.printer_id] = (perPrinter[u.printer_id] || 0) + 1; });
        grid.innerHTML = units.map(function (u) { return amsCardShell(u, perPrinter[u.printer_id] > 1); }).join('');
        autodryRender();
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
      // A «Блоки» row's last cell: a pause / backoff / switched-off unit and its release (admins).
      function adRelCell(u) {
        return autodryRelCell(autodryLine(u, amsAutodry.config, amsAutodry.limits, amsAutodry.now), isAdmin(), u.key);
      }
      // «Админ → Автоматика → Блоки» (IA §8): a unit by printer name — auto on / off, its mode
      // (the plan it gives under the select) and, when held, «снять паузу». What the unit does
      // now is its drying line on the AMS tab (no status column).
      function adRenderRows() {
        var tb = adEl("adRows");
        if (!tb || !amsAutodry) return;
        var cfg = amsAutodry.config, units = (amsAutodry.units || []).slice().sort(function (a, b) {
          return amsUnitOrder({ label: a.label, unit_index: a.ams }, { label: b.label, unit_index: b.ams });
        });
        if (!units.length) { tb.innerHTML = '<tr><td colspan="4" class="muted">Нет блоков AMS 2 Pro / AMS HT.</td></tr>'; return; }
        tb.innerHTML = units.map(function (u) {
          var k = safeText(u.key, ""), lbl = safeText(u.label, "");
          var uc = (cfg.units && cfg.units[u.key]) || u.unit_cfg || { enabled: true, mode: "inherit", temp: 55, hours: 8 };
          var opts = AD_MODE_OPTS.map(function (o) {
            return '<option value="' + o[0] + '"' + (uc.mode === o[0] ? " selected" : "") + ">" + o[1] + "</option>";
          }).join("");
          return '<tr id="ad-row-' + k + '" data-u="' + k + '">' +
            '<td class="c-unit"><div class="ad-unit"><strong>' + lbl + '</strong> <span class="nm">' + safeText(u.name, "") +
            '</span><span class="sub">' + [safeText(u.device_type, ""), safeText(u.model, "")].filter(Boolean).join(" · ") + "</span></div></td>" +
            '<td class="c-auto"><label class="toggle-switch" data-tip="Автосушка для блока"><input type="checkbox" data-ad-on="' + k + '"' +
            (uc.enabled ? " checked" : "") + ' aria-label="Автосушка для ' + lbl + '" /><span class="toggle-slider"></span></label></td>' +
            '<td class="c-mode" data-l="Режим"><select class="sel sm" data-ad-mode="' + k + '" aria-label="Режим для ' + lbl + '">' + opts + "</select>" +
            '<span class="inl' + (uc.mode === "custom" ? " on" : "") + '"><input class="num sm" data-ad-ct="' + k + '" value="' + Number(uc.temp) +
            '" inputmode="numeric" aria-label="Температура, °C" />° · <input class="num sm" data-ad-ch="' + k + '" value="' + Number(uc.hours) +
            '" inputmode="numeric" aria-label="Часы" /> ч</span>' +
            '<div class="run" data-run="' + k + '">' + adRunCell(u) + "</div></td>" +
            '<td class="c-rel" data-rel="' + k + '">' + adRelCell(u) + "</td></tr>";
        }).join("");
      }
      // While the form has edits only the plan and release cells follow the server.
      function adRefreshCells() {
        document.querySelectorAll("#adRows tr[data-u]").forEach(function (tr) {
          var u = autodryUnit(tr.getAttribute("data-u"));
          if (!u) return;
          var run = tr.querySelector("[data-run]"), rel = tr.querySelector("[data-rel]");
          if (run) run.innerHTML = adRunCell(u);
          if (rel) rel.innerHTML = adRelCell(u);
        });
      }
      // The AMS tab's auto-drying line (both roles): its words, and the farm's drying commands
      // with «Правила ›» for admins only (a viewer has no grey controls, IA §13).
      function amsStripRender() {
        var st = adEl("amsStripState"), acts = adEl("amsStripActs");
        if (st) st.innerHTML = autodryStripHtml(amsAutodry);
        if (acts) acts.hidden = !(isAdmin() && amsAutodry);
      }
      function amsLegendRender() {
        var el = adEl("amsLegend");
        if (el) el.innerHTML = amsLegendHtml(amsZonesNow());
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
        // the trial mode has its own row here («Пробный режим»): the banner keeps the engine's troubles
        var lines = autodryBanner(amsAutodry, false).slice(amsAutodry.config && amsAutodry.config.dry_run ? 1 : 0);
        el.innerHTML = lines.join("");
        el.hidden = !lines.length;
      }
      // The drying journal at the bottom of the AMS tab: the last AD_JOURNAL_SHORT, «Все ›» unfolds
      // up to AD_JOURNAL_MAX in place (IA §6).
      var AD_JOURNAL_SHORT = 5, AD_JOURNAL_MAX = 50;
      function adRenderJournal() {
        var box = adEl("adJournal"), more = adEl("adJournalMore"), sec = adEl("amsJournal");
        if (sec) sec.hidden = !amsAutodry;
        if (!box || !amsAutodry) return;
        var rows = (amsAutodry.journal || []).slice(0, AD_JOURNAL_MAX);
        if (!rows.length) {
          box.innerHTML = '<div class="jr-empty">Событий пока нет</div>';
          if (more) more.hidden = true;
          return;
        }
        var shown = adJournalAll ? rows : rows.slice(0, AD_JOURNAL_SHORT);
        box.innerHTML = shown.map(function (e) { return autodryJournalRow(e, amsAutodry.now); }).join("");
        if (more) {
          more.hidden = rows.length <= AD_JOURNAL_SHORT;
          more.textContent = adJournalAll ? "Свернуть" : "Все " + rows.length + " ›";
        }
      }
      // «Пробный режим» of «Автоматика»: on — «Выключить…» (the dialog: real starts begin); off —
      // «Включить» (the safe way round: at once, no dialog). Saved apart from the form's edits.
      function adRenderTrial() {
        var sub = adEl("adTrialSub"), btn = adEl("adTrialBtn"), c = amsAutodry && amsAutodry.config;
        if (!c) return;
        if (sub) {
          sub.textContent = c.dry_run ? "Включён: автосушка только пишет в журнал, команды на принтеры не отправляет."
            : "Выключен: автосушка сама запускает сушку по правилам ниже.";
        }
        if (btn) {
          btn.hidden = !isAdmin();
          btn.textContent = c.dry_run ? "Выключить…" : "Включить";
          btn.classList.toggle("danger", !!c.dry_run);
        }
        var row = btn && btn.closest ? btn.closest(".ad-trial") : null;
        if (row) row.classList.toggle("on", !!c.dry_run);
      }
      // Unsaved edits: the save bar's mark, and a dot on «Автоматика» and on the AMS tab's
      // «Правила ›» — the warning stays in sight when the sub-tab is left (IA plan step 11).
      function adSetDirtyUI(on) {
        var d = adEl("adDirty"), s = adEl("adSave"), f = adEl("adSaved");
        if (d) d.classList.toggle("on", on);
        if (s) s.disabled = !on || !isAdmin();
        if (on && f) f.classList.remove("show");
        autoDirtyMarks();
      }
      // The dot on «Автоматика» (and «Правила ›»): the auto-dry rules or the auto-ignore codes unsaved.
      function autoDirtyMarks() {
        var ad = !!adForm, ai = aiIsDirty(), on = ad || ai;
        var marks = [document.querySelector('#adminSub button[data-asub="auto"]'), adEl("amsRulesBtn")];
        marks.forEach(function (b) {
          if (!b) return;
          b.classList.toggle("dirty", on);
          if (on) b.setAttribute("data-dirty", ad ? "есть несохранённые правки автосушки" : "есть несохранённые коды автопропуска");
          else b.removeAttribute("data-dirty");
        });
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
      // Every part of the auto-dry on the page: the AMS tab's line, zones and journal (both
      // roles), and the rules form of «Админ → Автоматика» (admins only see that sub-tab).
      function autodryRender() {
        var sec = adEl("autodry");
        amsStripRender();
        amsLegendRender();
        adRenderJournal();
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
        adRenderTrial();
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
          if (u.maintenance) {     // first, as run_now on the server
            return '<label class="ad-dlg-item dis"><input type="checkbox" disabled /><span>' + head + '<span class="sub">принтер на обслуживании</span></span></label>';
          }
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
        adOpenDlg('<h4>Сушить сейчас</h4><p class="sub0">Ручной запуск: порог, окно и лимит в сутки не проверяются. Параметры — по режиму блока (Админ → Автоматика).' +
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
        var go = list.filter(function (u) { return !u.maintenance; }).length;
        var items = list.map(function (u) {
          if (u.maintenance) {     // stop_all leaves it out: its card stops it (a single command)
            return '<label class="ad-dlg-item dis"><input type="checkbox" disabled /><span><b>' + safeText(u.label, "") + " · " +
              safeText(u.name, "") + '</b><span class="sub">принтер на обслуживании</span></span></label>';
          }
          var what = u.dry && u.dry.active ? "— осталось " + adLeft(u.dry.remaining_min) : "— запуск ещё не подтверждён";
          var who = u.phase === "running" && u.origin === "auto" ? "прогон " + Number(u.series) + " автосушки"
            : u.phase === "running" ? "запущена вручную" : "сушка запущена не автосушкой";
          return '<label class="ad-dlg-item"><input type="checkbox" checked data-pick="' + safeText(u.key, "") + '" /><span><b>' +
            safeText(u.label, "") + " · " + safeText(u.name, "") + "</b> " + what + '<span class="sub">' + who + "</span></span></label>";
        }).join("");
        adOpenDlg('<h4>Остановить сушку</h4><p class="sub0">После остановки автосушка не будет трогать эти блоки ' +
          Number(amsAutodry.config.manual_hold_h) + "\u00a0ч (правило «После ручной остановки»).</p>" +
          '<div class="ad-dlg-list">' + items + '</div><div class="ad-dlg-foot"><button type="button" class="ad-btn" data-x>Отмена</button>' +
          '<button type="button" class="ad-btn-danger" data-go>Остановить (' + go + ")</button></div>");
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
          autodryRender();
          replyIn(adTrialBox(), { k: ok ? "ok" : "bad", t: ok ? "Пробный режим выключен" : detail });   // the row it was asked from
          if (ok) { loadAms(); setTimeout(loadAms, 1500); }
        };
      }
      // «Автопропуск ошибок»: the saved codes (auto_ignore_print_errors), the list being edited, Bambu's texts.
      var ai = { saved: null, codes: [], texts: {}, busy: false };
      function aiIsDirty() { return !!(ai && ai.saved && !aiSame(ai.saved, ai.codes)); }
      function aiRender() {
        var box = adEl("aiCodes");
        if (!box) return;
        box.innerHTML = ai.saved === null ? '<div class="ai-empty">Загрузка…</div>' : aiCodesHtml(ai.codes, ai.texts);
        var dirty = aiIsDirty(), save = adEl("aiSaveBtn"), mark = adEl("aiDirty");
        if (save) save.disabled = !dirty || ai.busy;
        if (mark) mark.hidden = !dirty;
        autoDirtyMarks();
      }
      function aiShowError(text) {
        var el = adEl("aiError");
        if (el) { el.textContent = text || ""; el.hidden = !text; }
      }
      // Bambu's words for the codes not looked up yet (one request; the fleet's own models).
      async function aiLoadTexts() {
        var need = ai.codes.filter(function (c) { return !(c in ai.texts); });
        if (!need.length) return;
        try {
          var r = await apiFetch("/api/admin/print-error-texts?codes=" + encodeURIComponent(need.join(",")));
          var d = r.ok ? await r.json() : null;
          need.forEach(function (c) { ai.texts[c] = d && d.texts && d.texts[c] ? d.texts[c] : null; });
        } catch (e) { need.forEach(function (c) { ai.texts[c] = null; }); }
        aiRender();
      }
      async function loadAutoIgnore() {
        if (!isAdmin()) return;
        if (aiIsDirty()) { aiRender(); return; }               // unsaved edits stay as they are
        try {
          var r = await apiFetch("/api/admin/settings");
          var d = r.ok ? await r.json() : null;
          var codes = d && d.values && Array.isArray(d.values.auto_ignore_print_errors) ? d.values.auto_ignore_print_errors : [];
          if (aiIsDirty()) return;
          ai.saved = codes.slice();
          ai.codes = codes.slice();
        } catch (e) { console.error("loadAutoIgnore failed", e); }
        aiRender();
        aiLoadTexts();
      }
      function aiAddTyped() {
        var inp = adEl("aiCodeInput");
        if (!inp || ai.saved === null) return;
        var res = aiAdd(ai.codes, inp.value);
        aiShowError(res.error);
        if (res.error) return;
        ai.codes = res.codes;
        inp.value = "";
        aiRender();
        aiLoadTexts();
      }
      // «Сохранить»: the server checks the codes (PUT /settings, auto_ignore_print_errors); its word is shown as is.
      async function aiSave() {
        if (!isAdmin() || !aiIsDirty() || ai.busy) return;
        ai.busy = true;
        aiRender();
        try {
          var r = await apiFetch("/api/admin/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                          body: JSON.stringify({ auto_ignore_print_errors: ai.codes }) });
          var d = null;
          try { d = await r.json(); } catch (x) { d = null; }
          if (!r.ok) aiShowError(d && typeof d.detail === "string" ? d.detail : "Не сохранилось (" + r.status + ")");
          else {
            var codes = d && d.values && Array.isArray(d.values.auto_ignore_print_errors) ? d.values.auto_ignore_print_errors : ai.codes;
            ai.saved = codes.slice();
            ai.codes = codes.slice();
            aiShowError(null);
            var f = adEl("aiSaved");
            if (f) { f.classList.add("show"); setTimeout(function () { f.classList.remove("show"); }, 2200); }
          }
        } catch (x) { aiShowError("Ошибка сети — не сохранилось"); }
        ai.busy = false;
        aiRender();
      }
      (function initAutoIgnore() {
        var box = adEl("aiCodes");
        if (!box) return;
        box.addEventListener("click", function (e) {
          var b = e.target.closest("[data-ai-del]");
          if (!b) return;
          var c = b.getAttribute("data-ai-del");
          ai.codes = ai.codes.filter(function (x) { return x !== c; });
          aiShowError(null);
          aiRender();
        });
        var inp = adEl("aiCodeInput");
        if (inp) inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); aiAddTyped(); } });
        var add = adEl("aiAddBtn"), save = adEl("aiSaveBtn"), trips = adEl("aiTripsBtn");
        if (add) add.addEventListener("click", aiAddTyped);
        if (save) save.addEventListener("click", aiSave);
        // «Срабатывания ›»: «История → Действия» on the automation's rows (the auto-ignore's among them)
        if (trips) trips.addEventListener("click", function () { actsOpenGroup("auto"); });
      })();
      // «к карточке ›» of the park table: «Принтеры», the card scrolled into view and flashed.
      function admGotoCard(pid) {
        setActiveView("fleet");
        finGoto(pid);
      }
      // «Пробный режим»'s row: its answers are said there (IA §10), under its text.
      function adTrialBox() {
        var sub = adEl("adTrialSub");
        return sub ? sub.parentNode : null;
      }
      async function adTrialOn() {
        if (!amsAutodry || !isAdmin()) return;
        var btn = adEl("adTrialBtn");
        if (btn) btn.disabled = true;
        var line;
        try {
          var cfg = JSON.parse(JSON.stringify(amsAutodry.config));
          cfg.dry_run = true;
          var r = await apiFetch("/api/admin/ams/autodry", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                             body: JSON.stringify(cfg) });
          var d = null;
          try { d = await r.json(); } catch (x) { d = null; }
          if (r.ok && d) { amsAutodry = d; autodryRender(); line = { k: "ok", t: "Пробный режим включён" }; }
          else line = { k: "bad", t: d && typeof d.detail === "string" ? d.detail : "Не получилось" };
        } catch (x) { line = { k: "bad", t: "Ошибка сети" }; }
        if (btn) btn.disabled = false;
        replyIn(adTrialBox(), line);
      }
      function adAutoPaneActive() {
        var p = document.querySelector('.view-panel[data-panel="admin"]');
        return !!(p && p.classList.contains("active") && adminSub === "auto");
      }
      // «Админ → Автоматика» opened: the auto-dry snapshot alone (the AMS tab loads it with its blocks).
      async function loadAutodry() {
        amsInit();
        try {
          var r = await apiFetch("/api/ams/autodry");
          amsAutodry = r.ok ? await r.json() : null;
        } catch (e) {
          console.error("loadAutodry failed", e);
          amsAutodry = null;
        }
        autodryRender();
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
          replyIn(b.closest("td"), { k: "bad", t: d && typeof d.detail === "string" ? d.detail : "Не получилось" });   // in its row
        } catch (x) { replyIn(b.closest("td"), { k: "bad", t: "Ошибка сети" }); }
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
            if (e.target.closest("#adDryRunOff")) adDryRunOffDlg();
          });
        }
        function on(id, fn) { var el = adEl(id); if (el) el.addEventListener("click", fn); }
        on("adSave", adSave);
        on("adReset", adReset);
        on("adDryAll", adRunNowDlg);
        on("adStopAll", adStopDlg);
        on("adJournalMore", function () { adJournalAll = !adJournalAll; adRenderJournal(); });
        on("adTrialBtn", function () {
          if (amsAutodry && amsAutodry.config && amsAutodry.config.dry_run) adDryRunOffDlg(); else adTrialOn();
        });
        // «Правила ›» → «Админ → Автоматика» (a viewer never has the button)
        on("amsRulesBtn", function () {
          if (!isAdmin()) return;
          setAdminSub("auto");
          setActiveView("admin");
          window.scrollTo(0, 0);
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

      function pad2(n) { return (n < 10 ? '0' : '') + n; }

      function fmtAuditTime(ts) {
        var d = new Date(ts * 1000);
        return pad2(d.getDate()) + (typeof I18N === 'object' ? I18N.dateSep() : '.') + pad2(d.getMonth() + 1) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
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

      // «Доступ» (IA §8, plan step 13): one row per user — the role chosen in the row (asked
      // there: the user's sessions end), «Сменить пароль» and «Удалить» as in-row forms; no prompt,
      // confirm or alert. The rows come from the pure userRowHtml (adm-ui block).
      var admUsers = [];
      var admUsersUi = { pw: null, del: null, role: null, err: {} };
      function renderAdminUsers() {
        var tbody = document.querySelector('#adminUsersTable tbody');
        if (!tbody) return;
        var me = currentUser ? currentUser.username : null;
        tbody.innerHTML = admUsers.slice().sort(function (a, b) {
          return String(a.username).localeCompare(String(b.username), 'ru', { numeric: true });
        }).map(function (u) { return userRowHtml(u, me, admUsersUi); }).join('');
        if (admUsersUi.pw != null) {
          var inp = tbody.querySelector('[data-pw-for="' + admUsersUi.pw + '"]');
          if (inp) inp.focus();
        }
      }
      async function loadAdminUsers() {
        try {
          var res = await apiFetch('/api/admin/users');
          if (!res.ok) throw new Error('HTTP ' + res.status);
          admUsers = await res.json();
          renderAdminUsers();
        } catch (e) { console.error('loadAdminUsers', e); }
      }
      // A PUT / DELETE of one user: done -> the list again; refused -> the server's words in the row.
      async function admUserSend(id, method, body, okText) {
        admUsersUi.err = {};
        try {
          var res = await apiFetch('/api/admin/users/' + encodeURIComponent(id), body
            ? { method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : { method: method });
          var d = null;
          try { d = await res.json(); } catch (x) { d = null; }
          if (!res.ok) { admUsersUi.err[id] = (d && typeof d.detail === 'string' ? d.detail : 'Ошибка ' + res.status); renderAdminUsers(); return; }
          admUsersUi = { pw: null, del: null, role: null, err: {} };
          await loadAdminUsers();
          // in the row it was done in (IA §10); a deleted user's row is gone — then a toast
          var row = document.querySelector('#adminUsersTable tbody tr[data-uid="' + id + '"]');
          replyIn(row ? row.querySelector('td.adm-actions') : null, { k: 'ok', t: okText });
        } catch (e) { admUsersUi.err[id] = 'Ошибка сети'; renderAdminUsers(); }
      }
      (function initAdminUsers() {
        var tbody = document.querySelector('#adminUsersTable tbody');
        if (!tbody) return;
        tbody.addEventListener('change', function (e) {
          var sel = e.target.closest('select.adm-role');
          if (!sel || sel.disabled) return;
          var u = admUsers.filter(function (x) { return String(x.id) === sel.dataset.uid; })[0];
          admUsersUi = { pw: null, del: null, err: {}, role: u && sel.value !== u.role ? { id: u.id, role: sel.value } : null };
          renderAdminUsers();
        });
        tbody.addEventListener('click', function (e) {
          var b = e.target.closest('button[data-uact]');
          if (!b) return;
          var act = b.dataset.uact, id = b.dataset.uid;
          if (act === 'cancel') { admUsersUi = { pw: null, del: null, role: null, err: {} }; renderAdminUsers(); return; }
          if (act === 'pw') { admUsersUi = { pw: id, del: null, role: null, err: {} }; renderAdminUsers(); return; }
          if (act === 'del') { admUsersUi = { pw: null, del: id, role: null, err: {} }; renderAdminUsers(); return; }
          if (act === 'pw-save') {
            var inp = tbody.querySelector('[data-pw-for="' + id + '"]'), pass = inp ? inp.value : '';
            if (pass.length < 8) { admUsersUi.err = {}; admUsersUi.err[id] = 'Пароль — не меньше 8 символов'; renderAdminUsers(); return; }
            admUserSend(id, 'PUT', { password: pass }, 'Пароль обновлён');
          } else if (act === 'role-yes' && admUsersUi.role) {
            admUserSend(id, 'PUT', { role: admUsersUi.role.role }, 'Роль обновлена');
          } else if (act === 'del-yes') {
            admUserSend(id, 'DELETE', null, 'Пользователь удалён');
          }
        });
        tbody.addEventListener('keydown', function (e) {
          var inp = e.target.closest('[data-pw-for]');
          if (!inp) return;
          if (e.key === 'Enter') { var b = tbody.querySelector('button[data-uact="pw-save"]'); if (b) b.click(); }
          if (e.key === 'Escape') { admUsersUi = { pw: null, del: null, role: null, err: {} }; renderAdminUsers(); }
        });
        var open = document.getElementById('addUserOpen'), form = document.getElementById('addUserForm');
        if (open && form) open.addEventListener('click', function () {
          form.hidden = !form.hidden;
          open.setAttribute('aria-expanded', form.hidden ? 'false' : 'true');
          if (!form.hidden) { var n = document.getElementById('newUsername'); if (n) n.focus(); }
        });
      })();

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
                  var form = document.getElementById('addUserForm'), open = document.getElementById('addUserOpen');
                  if (form) form.hidden = true;
                  if (open) open.setAttribute('aria-expanded', 'false');
                  showToast('Пользователь добавлен', true);
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
      var prnLightOff = null;  // printer_light_disabled (GET /api/admin/settings), null until it answers
      var prnNeedLive = false; // the table was drawn before /api/printers answered: once more when it does
      var prnEditingId = null; // null = форма закрыта, '' = создание, иначе id
      var prnOpenAfterLoad = null; // a printer id: its form opens after the next load (the card's ⋯)
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
          // the settings for the Creality light switch of the form (printer_light_disabled), alongside
          apiFetch('/api/admin/settings').then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
            if (d && d.values) prnLightOff = Array.isArray(d.values.printer_light_disabled) ? d.values.printer_light_disabled : [];
          }).catch(function () {});
          var res = await apiFetch('/api/admin/printers');
          // a refusal (a role taken away, a server error) keeps the last table: its body is no list
          if (!res.ok) throw new Error('HTTP ' + res.status);
          prnRows = await res.json();
          prnNeedLive = !cachedAllPrinters.length;
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
          // the card's ⋯ «Параметры принтера ›» asked for this printer's form
          if (prnOpenAfterLoad != null) {
            var want = prnOpenAfterLoad;
            prnOpenAfterLoad = null;
            var row = prnRows.filter(function (r) { return String(r.id) === want; })[0];
            if (row) prnOpenForm(row.id);
          }
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
        var pending = 0, nowMs = Date.now();
        // IA §8: the serial number and the access code live in the form (the table fits without a sideways
        // scroll); maintenance is a status with «к карточке ›» — put on and taken off through the card's ⋯
        tbody.innerHTML = prnRows.slice().sort(prnCompare).map(function (p) {
          if (p.pending) pending++;
          var live = (cachedAllPrinters || []).filter(function (s) { return String(s.id) === String(p.id); })[0] || null;
          var name = '<b>' + escHtml(p.label) + '</b>' + (p.pending ? ' ' + PENDING_BADGES[p.pending] : '');
          var ip = '<span class="mono dim">' + escHtml(p.host) +
            (p.port ? '<span class="muted">:' + p.port + '</span>' : '') + '</span>';
          var actions = parkActsHtml(p, prnAsk);
          // data-l: a phone's card says what a cell is (plan step 15: no table there)
          return '<tr' + (p.pending === 'deleted' ? ' class="row-del"' : '') + '>' +
            '<td class="prk-name">' + name + '</td>' +
            '<td class="dim prk-model" data-l="модель">' + escHtml(p.model || '—') + '</td>' +
            '<td class="prk-kind">' + (KIND_BADGES[p.kind] || escHtml(p.kind)) + '</td>' +
            '<td class="prk-ip">' + ip + '</td>' +
            '<td class="prk-fw" data-l="прошивка">' + fwCell(p) + '</td>' +
            '<td class="prk-maint" data-l="обслуживание">' + (p.pending === 'deleted' ? '<span class="muted">—</span>' : parkMaintHtml(p, nowMs)) + '</td>' +
            '<td class="prk-ctl" data-l="управление">' + parkCtlHtml(live) + '</td>' +
            '<td class="adm-actions">' + actions + '</td></tr>';
        }).join('');
        var bar = document.getElementById('printersApplyBar');
        if (bar) bar.style.display = pending ? '' : 'none';
        var cnt = document.getElementById('printersPendingCount');
        if (cnt) cnt.textContent = pending;
        prnPending = pending;
        if (!pending) prnBarAsk(null);
      }
      // The apply bar asks in itself (IA §10, no native dialog): what = 'apply' | 'discard' | null.
      var prnAsk = null, prnPending = 0, prnBarWhat = null, prnRestarting = false;
      function prnBarAsk(what) {
        prnBarWhat = what;
        var ask = document.getElementById('printersAsk');
        if (!ask) return;
        var n = prnPending;
        document.getElementById('printersAskText').textContent = what === 'apply'
          ? 'Применить правки (' + n + ') и перезапустить сервис? Панель будет недоступна ~10 секунд.'
          : what === 'discard' ? 'Отменить все непримененные правки принтеров (' + n + ')?' : '';
        var yes = document.getElementById('printersAskYes');
        yes.textContent = what === 'apply' ? 'Применить' : 'Отменить правки';
        yes.className = what === 'apply' ? 'apply-btn' : 'discard-btn danger';
        ask.hidden = !what;
        var apply = document.getElementById('printersApplyBtn'), discard = document.getElementById('printersDiscardBtn');
        apply.hidden = discard.hidden = !!what;
        [yes, apply, discard].forEach(function (b) { b.disabled = prnRestarting; });
        if (what) yes.focus({ preventScroll: true });
      }
      // A park row's answer under its actions (a refused delete / restore).
      function prnRowReply(id, text) {
        var btn = document.querySelector('#adminPrintersTable tbody [data-id="' + id + '"]');
        replyIn(btn ? btn.closest('td') : null, { k: 'bad', t: text });
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
        // a Creality's light switch: the setting printer_light_disabled (every Creality has it by default)
        document.getElementById('prnLight').checked = parkLightOn(prnLightOff, p ? p.id : null);
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
            var saved = null;
            try { saved = await res.json(); } catch (x) { saved = null; }
            var lightErr = kind === 'creality' && saved && saved.id ? await prnSaveLight(saved.id) : null;
            prnCloseForm();
            showToast(lightErr || (creating ? 'Принтер добавлен' : 'Принтер обновлён'), !lightErr);
            loadAdminPrinters();
          } else {
            var d = await res.json();
            err.textContent = d.detail || 'Ошибка';
          }
        } catch (e) { err.textContent = 'Ошибка сети'; }
      }

      // The form's «Разрешить управление светом» of a Creality: printer_light_disabled through
      // PUT /api/admin/settings (applies at once, no restart) when it changed -> an error text or null.
      async function prnSaveLight(id) {
        var on = document.getElementById('prnLight').checked;
        if (prnLightOff === null || parkLightOn(prnLightOff, id) === on) return null;
        try {
          var r = await apiFetch('/api/admin/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                                          body: JSON.stringify({ printer_light_disabled: parkLightList(prnLightOff, id, on) }) });
          var d = null;
          try { d = await r.json(); } catch (x) { d = null; }
          if (!r.ok) return 'Свет не сохранился: ' + (d && typeof d.detail === 'string' ? d.detail : r.status);
          if (d && d.values && Array.isArray(d.values.printer_light_disabled)) prnLightOff = d.values.printer_light_disabled;
          return null;
        } catch (x) { return 'Свет не сохранился: ошибка сети'; }
      }

      // The restart's progress is said in the apply bar it was asked from; the bar goes once
      // nothing is pending, and then a toast says it (replyIn, IA §10).
      function prnWaitForRestart() {
        var txt = document.getElementById('printersApplyTxt');
        prnRestarting = true;
        prnBarAsk(null);
        replyIn(txt, { k: 'wait', t: 'Сервис перезапускается…' });
        var start = Date.now();
        setTimeout(function poll() {
          fetch('/api/health').then(function (r) {
            if (r.ok) {
              prnRestarting = false;
              loadAdminPrinters().then(function () {
                replyIn(txt, { k: 'ok', t: 'Сервис перезапущен, конфигурация применена' });
              });
              return;
            }
            throw new Error('not up');
          }).catch(function () {
            if (Date.now() - start > 90000) {
              prnRestarting = false;
              prnBarAsk(null);
              replyIn(txt, { k: 'bad', t: 'Сервис не поднялся за 90 сек — проверь вручную' });
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
          if (btn.dataset.act === 'card') {
            admGotoCard(id);
          } else if (btn.dataset.act === 'edit') {
            prnOpenForm(id);
          } else if (btn.dataset.act === 'delete') {
            if (!p) return;
            prnAsk = id;                            // «Удалить «P3»?» in the row (parkActsHtml)
            renderAdminPrinters();
            var yes = table.querySelector('tbody [data-act="del-yes"]');
            if (yes) yes.focus({ preventScroll: true });
          } else if (btn.dataset.act === 'del-no') {
            prnAsk = null;
            renderAdminPrinters();
          } else if (btn.dataset.act === 'del-yes') {
            prnAsk = null;
            apiFetch('/api/admin/printers/' + id, { method: 'DELETE' }).then(function (res) {
              if (res.ok) { if (prnEditingId === id) prnCloseForm(); loadAdminPrinters(); return; }
              renderAdminPrinters();
              return res.json().catch(function () { return null; }).then(function (d) {
                prnRowReply(id, d && typeof d.detail === 'string' ? d.detail : 'Ошибка ' + res.status);
              });
            }).catch(function () { renderAdminPrinters(); prnRowReply(id, 'Ошибка сети'); });
          } else if (btn.dataset.act === 'restore') {
            apiFetch('/api/admin/printers/' + id + '/restore', { method: 'POST' }).then(function (res) {
              if (res.ok) { loadAdminPrinters(); return; }
              return res.json().catch(function () { return null; }).then(function (d) {
                prnRowReply(id, d && typeof d.detail === 'string' ? d.detail : 'Ошибка ' + res.status);
              });
            }).catch(function () { prnRowReply(id, 'Ошибка сети'); });
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
        // «Отменить правки» / «Применить и перезапустить» ask in the bar itself; «Отмена» puts it back
        document.getElementById('printersDiscardBtn').addEventListener('click', function () { prnBarAsk('discard'); });
        document.getElementById('printersApplyBtn').addEventListener('click', function () { prnBarAsk('apply'); });
        document.getElementById('printersAskNo').addEventListener('click', function () { prnBarAsk(null); });
        document.getElementById('printersAskYes').addEventListener('click', function () {
          var what = prnBarWhat, txt = document.getElementById('printersApplyTxt');
          if (!what || prnRestarting) return;
          prnBarAsk(null);
          if (what === 'apply') {
            apiFetch('/api/admin/printers/apply', { method: 'POST' }).then(function () {
              prnWaitForRestart();
            }).catch(function () { prnWaitForRestart(); });
            return;
          }
          apiFetch('/api/admin/printers/discard', { method: 'POST' }).then(function (res) {
            if (!res.ok) { replyIn(txt, { k: 'bad', t: 'Не отменилось (' + res.status + ')' }); return; }
            prnCloseForm();
            return loadAdminPrinters().then(function () { replyIn(txt, { k: 'ok', t: 'Правки отменены' }); });
          }).catch(function () { replyIn(txt, { k: 'bad', t: 'Ошибка сети' }); });
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

      var proxyAsk = null, proxyTg = null;     // the proxy whose «Удалить» asks; the last status drawn
      function renderProxies(tg) {
        var tbody = document.querySelector('#proxyTable tbody');
        if (!tbody || !tg) return;
        proxyTg = tg;
        var rows = tg.proxies.map(function (p) {
          var st = p.online
            ? '<span class="proxy-st ok"><span class="d"></span>онлайн</span>'
            : '<span class="proxy-st err"><span class="d"></span>офлайн</span>';
          var lat = p.latency_ms !== null ? '<span class="mono dim">' + p.latency_ms + ' мс</span>' : '<span class="muted">—</span>';
          var best = p.is_best ? '<span class="badge badge-ok">используется</span>' : '';
          // «Удалить» asks in the row (IA §10, no native dialog): «Удалить прокси? Удалить / Отмена»
          var acts = proxyAsk === p.url
            ? '<div class="adm-inrow"><span class="adm-ask">Удалить прокси?</span>' +
              '<button type="button" class="ghost-btn danger" data-act="del-yes">Удалить</button>' +
              '<button type="button" class="adm-link" data-act="del-no">Отмена</button></div>'
            : '<button type="button" class="ghost-btn danger" data-act="del">Удалить</button>';
          return '<tr data-url="' + encodeURIComponent(p.url) + '">' +
            '<td class="mono dim proxy-url"><span class="proxy-url-text">' + escHtml(maskProxyUrl(p.url)) + '</span> ' +
            '<button type="button" class="eye" data-act="eye" aria-label="Показать адрес целиком">👁</button></td>' +
            '<td>' + st + '</td><td>' + lat + '</td><td>' + best + '</td>' +
            '<td class="adm-actions">' + acts + '</td></tr>';
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
        if (srvEl('tgLang')) srvEl('tgLang').value = values.telegram_lang || 'ru';
        ['Finish', 'Error', 'Paused'].forEach(function (k) {
          var tpl = srvEl('tgTpl' + k);
          if (tpl) tpl.classList.toggle('tpl-dim', !srvEl('tgNotify' + k).checked);
        });
        renderTgStatus(tg);
        renderProxies(tg);
        // The proxies only carry the bot (IA §8): shown while it is switched on.
        var px = srvEl('proxySection'), pxOff = srvEl('proxyOffHint');
        if (px) px.hidden = !values.telegram_enabled;
        if (pxOff) pxOff.hidden = !!values.telegram_enabled;
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
        if (srvEl('tgLang')) srvEl('tgLang').addEventListener('change', function () {
          saveServerSettings({ telegram_lang: this.value }, 'tgSavedFlash', 'tgError').catch(function () {});
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
          } else if (btn.dataset.act === 'del' || btn.dataset.act === 'del-no') {
            proxyAsk = btn.dataset.act === 'del' ? url : null;
            renderProxies(proxyTg);
            var yes = proxyAsk && document.querySelector('#proxyTable tbody [data-act="del-yes"]');
            if (yes) yes.focus({ preventScroll: true });
          } else if (btn.dataset.act === 'del-yes') {
            proxyAsk = null;
            renderProxies(proxyTg);
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

      // --- PWA: the service worker (app/web/pwa/sw.js) caches /static only ---
      // Registered from the app only: the login page has no assets to cache.
      // On a self-signed address (LAN / mesh) register() fails with a
      // SecurityError — expected, logged at debug level. ?nosw in the URL
      // removes the worker and its caches instead (the page's kill switch).
      function forgeClearCaches() {
        if (!window.caches) return Promise.resolve();
        return caches.keys().then(function (keys) {
          return Promise.all(keys.filter(function (k) { return k.indexOf("forge-") === 0; })
            .map(function (k) { return caches.delete(k); }));
        }).catch(function () {});
      }
      (function initServiceWorker() {
        if (!("serviceWorker" in navigator)) return;
        var noSw = false;
        try { noSw = new URLSearchParams(location.search).has("nosw"); } catch (e) {}
        if (noSw) {
          var controlled = !!navigator.serviceWorker.controller;
          navigator.serviceWorker.getRegistrations().then(function (regs) {
            return Promise.all(regs.map(function (r) { return r.unregister(); }));
          }).catch(function () {}).then(forgeClearCaches).then(function () {
            // The old worker still controls this page and would store its
            // assets again: one reload runs without it (that load is not
            // controlled, so it does not reload again).
            if (controlled) location.reload();
          });
          return;
        }
        function register() {
          navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" }).catch(function (e) {
            console.debug("Service worker not registered:", e);
          });
        }
        if (document.readyState === "complete") register();
        else window.addEventListener("load", register);
      })();
      // The cached assets go with the session: the worker drops its cache
      // (forge-clear) and the page clears too, best-effort, at most 300 ms.
      function forgeLogoutClear() {
        try {
          var sw = navigator.serviceWorker && navigator.serviceWorker.controller;
          if (sw) sw.postMessage({ type: "forge-clear" });
        } catch (e) {}
        return Promise.race([forgeClearCaches(), new Promise(function (r) { setTimeout(r, 300); })]);
      }

      // Logout button (👤 «Этот браузер», at the top)
      var logoutBtn = document.getElementById('logoutBtn');
      if (logoutBtn) {
        logoutBtn.addEventListener('click', function() {
          var cleared = forgeLogoutClear();
          function leave() {
            cleared.then(function () { window.location.replace('/'); });
          }
          fetch('/api/auth/logout', { method: 'POST' }).then(leave, leave);
        });
      }

      // --- 👤 «Этот браузер» (IA §9): the account and «Выйти», the looks, the
      // browser's notifications, the install, the version. A panel on the right,
      // a bottom sheet on a phone; the same for both roles, nothing server-side.
      (function initMePanel() {
        var btn = document.getElementById("meBtn"), panel = document.getElementById("mePanel");
        var back = document.getElementById("meBack"), close = document.getElementById("meClose");
        if (!btn || !panel || !back) return;
        function setOpen(open) {
          var inside = panel.contains(document.activeElement);   // before hiding moves the focus away
          panel.hidden = !open;
          back.hidden = !open;
          btn.setAttribute("aria-expanded", open ? "true" : "false");
          if (open) sheetOpened(meSheetClose); else sheetClosed(meSheetClose);   // a phone's «Назад» closes the sheet
          if (open) panel.focus({ preventScroll: true });
          else if (inside) btn.focus({ preventScroll: true });
        }
        function meSheetClose() { setOpen(false); }
        btn.addEventListener("click", function () { setOpen(panel.hidden); });
        back.addEventListener("click", function () { setOpen(false); });
        if (close) close.addEventListener("click", function () { setOpen(false); });
        document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) setOpen(false); });
      })();
      // The role in words on the button (a viewer sees why there are no buttons
      // for them, IA §4) and «<логин> · <роль>» at the top of the panel.
      function renderMeWho() {
        var known = !!(currentUser && currentUser.role);
        var role = !known ? "—" : isAdmin() ? "Админ" : "Зритель";
        var label = document.getElementById("meRole"), btn = document.getElementById("meBtn");
        if (label) label.textContent = role;
        if (btn) btn.classList.toggle("viewer", known && !isAdmin());
        var who = document.getElementById("meWho");
        if (!who) return;
        who.textContent = "";
        if (currentUser && currentUser.username) {
          var name = document.createElement("span");
          name.className = "me-login";
          name.textContent = currentUser.username;
          who.appendChild(name);
          who.appendChild(document.createTextNode(" · "));
        }
        var b = document.createElement("b");
        b.textContent = role;
        who.appendChild(b);
      }
      // «Приложение · Установить»: only while the browser offers to install the
      // panel (beforeinstallprompt; never fires in the installed app itself).
      (function initInstall() {
        var sec = document.getElementById("meInstallSec"), b = document.getElementById("meInstallBtn"), offer = null;
        if (!sec || !b) return;
        window.addEventListener("beforeinstallprompt", function (e) {
          e.preventDefault();
          offer = e;
          sec.hidden = false;
        });
        window.addEventListener("appinstalled", function () { offer = null; sec.hidden = true; });
        b.addEventListener("click", function () {
          var e = offer;
          offer = null;
          sec.hidden = true;
          if (e) { try { e.prompt(); } catch (x) {} }
        });
      })();

      // A long-lived window (an installed app may sleep in the background for
      // days) reloads after a deploy: back in view, at most once per 5 min, it
      // asks /api/version again; another commit reloads the page unless the
      // user is in the middle of something — then the next return does. The
      // first commit comes from loadAppVersion's request below.
      var appVer = { commit: null, checkedAt: Date.now(), newer: false };
      function appUserBusy() {
        var a = document.activeElement, tag = a ? a.tagName : "";
        if (tag === "TEXTAREA" || tag === "SELECT" || (a && a.isContentEditable)) return true;
        // a focused checkbox / button left after a click is not typing
        if (tag === "INPUT" && !/^(checkbox|radio|range|button|submit|reset|color|file)$/i.test(a.type)) return true;
        var dlg = document.getElementById("adDlgBack");
        if (dlg && dlg.classList.contains("on")) return true;
        if (adForm || aiIsDirty()) return true;  // unsaved auto-dry rules / auto-ignore codes (Админ → Автоматика)
        return pcPopPid != null || !!nfPop || !!bulkDlg;
      }
      function appReloadIfNewer() {
        if (appVer.newer && !appUserBusy()) location.reload();
      }
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState !== "visible") return;
        if (appVer.newer) return appReloadIfNewer();
        if (Date.now() - appVer.checkedAt < 5 * 60 * 1000) return;
        appVer.checkedAt = Date.now();
        apiFetch("/api/version").then(function (res) { return res.json(); }).then(function (v) {
          if (!v || !v.commit) return;
          if (appVer.commit === null) appVer.commit = v.commit;
          else if (v.commit !== appVer.commit) {
            appVer.newer = true;
            appReloadIfNewer();
          }
        }).catch(function () {});
      });

      // Версия деплоя из git на сервере — внизу 👤 «Этот браузер»
      (function loadAppVersion() {
        var el = document.getElementById('appVersion');
        apiFetch('/api/version').then(function (res) { return res.json(); }).then(function (v) {
          if (v && v.commit && appVer.commit === null) appVer.commit = v.commit;
          if (!el) return;
          var date = v.date ? v.date.split('-').reverse().join('.') : '';
          el.textContent = (v.commit || 'dev') + (date ? ' · ' + date : '');
        }).catch(function () {});
      })();

      // Загрузка админ-данных живёт в setActiveView — и клик по табу, и
      // восстановление вкладки после перезагрузки страницы проходят через неё.
    })();