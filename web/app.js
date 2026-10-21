(() => {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const SLOTS = ['idle', 'speaking', 'idle_blink', 'speaking_blink'];
    const STORAGE_KEY = 'pngtuber_settings_v3';   // запасной вариант, если страница открыта без десктопной оболочки
    const MAX_SIDE = 1400;                       // предварительное уменьшение при drag&drop
    const DEFAULTS = {
        thresholdOpen: 18,
        thresholdClose: 10,
        hold: 150,
        attack: 0,
        gain: 100,
        bgColor: '#00ff00',
        scale: 100,
        enableAnim: true,
        enableBlink: true,
        flip: false,
        onTop: false,
        obsBackground: true,
        deviceId: ''
    };

    // ---------- Связь с десктопной оболочкой ----------

    const backend = () => (window.pywebview && window.pywebview.api) || null;

    async function whenBackendReady() {
        if (backend()) return backend();
        await new Promise(resolve => {
            let done = false;
            const finish = () => { if (!done) { done = true; resolve(); } };
            window.addEventListener('pywebviewready', finish, { once: true });
            setTimeout(finish, 3000);   // страница открыта в обычном браузере
        });
        return backend();
    }

    // Любой вызов через мост может «залипнуть» (WebView2/Qt), если окно занято.
    // Таймаут не даёт интерфейсу навсегда остаться в состоянии «Подключение…».
    function withTimeout(promise, ms, label) {
        return new Promise((resolve, reject) => {
            const timer = setTimeout(
                () => reject(new Error((label || 'операция') + ': приложение не отвечает')),
                ms
            );
            Promise.resolve(promise).then(
                value => { clearTimeout(timer); resolve(value); },
                error => { clearTimeout(timer); reject(error); }
            );
        });
    }

    function apiCall(name, ms, ...args) {
        const api = backend();
        if (!api || typeof api[name] !== 'function') {
            return Promise.reject(new Error('нет связи с приложением'));
        }
        return withTimeout(api[name](...args), ms, name);
    }

    function safe(promise) {
        if (promise && typeof promise.catch === 'function') promise.catch(() => {});
    }

    // ---------- Настройки ----------

    const clampInt = (value, lo, hi, fallback) => {
        const num = Math.round(Number(value));
        if (!Number.isFinite(num)) return fallback;
        return Math.max(lo, Math.min(hi, num));
    };

    function sanitizeSettings(raw) {
        const src = (raw && typeof raw === 'object') ? raw : {};
        const out = { ...DEFAULTS, ...src };
        out.thresholdOpen = clampInt(out.thresholdOpen, 1, 90, DEFAULTS.thresholdOpen);
        out.thresholdClose = clampInt(out.thresholdClose, 1, 90, DEFAULTS.thresholdClose);
        out.thresholdClose = Math.min(out.thresholdClose, out.thresholdOpen);
        out.hold = clampInt(out.hold, 0, 600, DEFAULTS.hold);
        out.attack = clampInt(out.attack, 0, 100, DEFAULTS.attack);
        out.gain = clampInt(out.gain, 100, 500, DEFAULTS.gain);
        out.scale = clampInt(out.scale, 30, 100, DEFAULTS.scale);
        if (typeof out.bgColor !== 'string' || !/^#[0-9a-f]{6}$/i.test(out.bgColor)) {
            out.bgColor = DEFAULTS.bgColor;
        }
        for (const key of ['enableAnim', 'enableBlink', 'flip', 'onTop', 'obsBackground']) {
            if (typeof out[key] !== 'boolean') out[key] = DEFAULTS[key];
        }
        if (typeof out.deviceId !== 'string') out.deviceId = '';
        return out;
    }

    let settings = { ...DEFAULTS };
    let options = { transparent: false };

    let saveTimer;
    function saveSettings() {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            const api = backend();
            if (api) safe(api.save_settings(JSON.stringify(settings)));
            else try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (_) {}
        }, 300);
    }

    // ---------- Элементы ----------

    const panel = $('settings-panel');
    const bounce = $('avatar-bounce');
    const imgs = Object.fromEntries(SLOTS.map(s => [s, $(s + '-img')]));
    const slotEls = Object.fromEntries(SLOTS.map(s => [s, document.querySelector(`.slot[data-slot="${s}"]`)]));
    const imageTokens = Object.fromEntries(SLOTS.map(s => [s, 0]));
    const pendingImageSaves = new Set();

    const ui = {
        gain: $('gain-slider'), gainValue: $('gain-value'),
        thresholdOpen: $('threshold-open-slider'), thresholdOpenValue: $('threshold-open-value'),
        thresholdClose: $('threshold-close-slider'), thresholdCloseValue: $('threshold-close-value'),
        hold: $('hold-slider'), holdValue: $('hold-value'),
        attack: $('attack-slider'), attackValue: $('attack-value'),
        scale: $('scale-slider'), scaleValue: $('scale-value'),
        bg: $('bg-color-picker'),
        anim: $('animation-toggle'), blink: $('blink-toggle'), flip: $('flip-toggle'),
        onTop: $('on-top-toggle'), obsBg: $('obs-bg-toggle'), parkBtn: $('park-btn'),
        device: $('device-select'), start: $('start-btn'),
        fill: $('volume-fill'), markOpen: $('threshold-open-mark'), markClose: $('threshold-close-mark'),
        status: $('status'), statusText: $('status-text')
    };

    // ---------- Загрузочный экран и ошибки ----------

    const splash = $('splash');
    let splashHidden = false;

    function hideSplash() {
        if (splashHidden || !splash) return;
        splashHidden = true;
        splash.classList.add('gone');
        setTimeout(() => splash.remove(), 400);
    }

    function showFatal(message, hint) {
        hideSplash();
        const box = $('fatal');
        if (!box) return;
        $('fatal-text').textContent = message;
        $('fatal-hint').textContent = hint || '';
        box.hidden = false;
    }

    let toastTimer;
    function toast(msg) {
        const el = $('toast');
        el.textContent = msg;
        el.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
    }

    // ---------- Состояние аватара ----------
    let isSpeaking = false;
    let isBlinking = false;

    const loaded = (img) => Boolean(img.getAttribute('src')) && img.complete && img.naturalWidth > 0;

    let shown = imgs.idle;
    let bouncing = false;
    // Одна анимация на всё время работы: пересоздание CSS-анимации при каждой фразе копит память в Chromium
    const bounceAnim = bounce.animate(
        [{ transform: 'translateY(0)' }, { transform: 'translateY(-2.2vh)' }],
        { duration: 220, iterations: Infinity, direction: 'alternate', easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
    );
    bounceAnim.pause();

    function updateAvatar() {
        let target;
        if (isSpeaking) {
            if (isBlinking && loaded(imgs.speaking_blink)) target = imgs.speaking_blink;
            else if (loaded(imgs.speaking)) target = imgs.speaking;
            else if (isBlinking && loaded(imgs.idle_blink)) target = imgs.idle_blink;
            else target = imgs.idle;
        } else {
            target = (isBlinking && loaded(imgs.idle_blink)) ? imgs.idle_blink : imgs.idle;
        }
        if (target !== shown) {
            if (shown) shown.classList.remove('active');
            target.classList.add('active');
            shown = target;
        }
        const wantBounce = settings.enableAnim && isSpeaking;
        if (wantBounce !== bouncing) {
            bouncing = wantBounce;
            if (bouncing) bounceAnim.play();
            else { bounceAnim.pause(); bounceAnim.currentTime = 0; }
        }
    }

    for (const img of Object.values(imgs)) {
        img.decoding = 'async';
        img.addEventListener('load', updateAvatar);
    }

    // ---------- Моргание ----------
    function scheduleBlink() {
        setTimeout(() => {
            if (settings.enableBlink) {
                if (document.hidden) { scheduleBlink(); return; }
                isBlinking = true;
                updateAvatar();
                setTimeout(() => { isBlinking = false; updateAvatar(); scheduleBlink(); }, 140);
            } else {
                scheduleBlink();
            }
        }, 2500 + Math.random() * 3500);
    }
    scheduleBlink();

    // ---------- Изображения ----------

    function applySlot(slot, src) {
        const img = imgs[slot];
        const thumb = slotEls[slot].querySelector('img');
        if (src) {
            if (img.getAttribute('src') !== src) img.src = src;
            if (thumb.getAttribute('src') !== src) thumb.src = src;
            thumb.hidden = false;
            slotEls[slot].classList.add('filled');
        } else {
            img.classList.remove('active');
            img.removeAttribute('src');      // сначала освобождаем декодированный битмап
            thumb.removeAttribute('src');
            thumb.hidden = true;
            slotEls[slot].classList.remove('filled');
        }
        updateAvatar();
    }

    /**
     * @param {string} src  data URL картинки ('' — очистить слот)
     * @param {{persist?: boolean}} opts  persist=false, если Python уже сохранил файл
     */
    function setSlotImage(slot, src, { persist = true } = {}) {
        const token = ++imageTokens[slot];
        applySlot(slot, src || '');
        if (!persist) return Promise.resolve();

        const api = backend();
        if (!api) return Promise.resolve();

        const task = (async () => {
            try {
                // Python возвращает уже уменьшенный PNG — показываем именно его,
                // иначе в памяти остаётся исходная картинка на 30 МБ.
                const res = await withTimeout(api.set_image(slot, src || ''), 20000, 'картинка');
                if (token !== imageTokens[slot]) return;     // пользователь уже выбрал другую
                if (res && res.ok === false) { toast(res.error || 'Не удалось сохранить картинку'); return; }
                if (res && typeof res.url === 'string' && res.url !== src) applySlot(slot, res.url);
            } catch (e) {
                if (token === imageTokens[slot]) toast('Картинка: ' + e.message);
            }
        })();
        pendingImageSaves.add(task);
        task.finally(() => pendingImageSaves.delete(task));
        return task;
    }

    function readAsDataURL(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = (e) => resolve(e.target.result);
            reader.onerror = () => reject(reader.error || new Error('не удалось прочитать файл'));
            reader.readAsDataURL(file);
        });
    }

    async function fileToPngDataUrl(file) {
        if (typeof createImageBitmap !== 'function') return readAsDataURL(file);
        const bitmap = await createImageBitmap(file);
        try {
            const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(bitmap.width * scale));
            canvas.height = Math.max(1, Math.round(bitmap.height * scale));
            const ctx = canvas.getContext('2d');
            ctx.imageSmoothingQuality = 'high';
            ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            return canvas.toDataURL('image/png');
        } finally {
            if (bitmap.close) bitmap.close();
        }
    }

    async function readFile(slot, file) {
        if (!file) return;
        if (!file.type.startsWith('image/')) return toast('Нужен файл изображения');
        try {
            // Уменьшаем ДО отправки в Python: огромный PNG не должен идти через мост.
            const src = await fileToPngDataUrl(file);
            setSlotImage(slot, src);
        } catch (e) {
            toast('Не удалось прочитать изображение');
            console.warn(e);
        }
    }

    async function pickImage(slot) {
        if (!backend()) return toast('Выбор файла доступен только в десктопной версии');
        try {
            const res = await apiCall('pick_image', 120000, slot);
            if (!res) return;                                   // отменили
            if (res.ok === false) return toast(res.error || 'Не удалось открыть картинку');
            setSlotImage(slot, res.url, { persist: false });     // Python уже сохранил
        } catch (e) {
            toast(e.message);
        }
    }

    for (const slot of SLOTS) {
        const el = slotEls[slot];
        el.addEventListener('click', () => pickImage(slot));
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickImage(slot); }
        });
        el.querySelector('.clear').addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            setSlotImage(slot, '');
        });
        el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drag'); });
        el.addEventListener('dragleave', () => el.classList.remove('drag'));
        el.addEventListener('drop', (e) => {
            e.preventDefault();
            el.classList.remove('drag');
            readFile(slot, e.dataTransfer.files[0]);
        });
    }

    // ---------- Применение настроек к интерфейсу ----------
    function applySettings() {
        ui.gain.value = settings.gain;
        ui.gainValue.textContent = settings.gain === 100 ? '100% (норма)' : settings.gain + '% (x' + (settings.gain / 100).toFixed(1) + ')';
        ui.thresholdOpen.value = settings.thresholdOpen;
        ui.thresholdOpenValue.textContent = settings.thresholdOpen;
        ui.thresholdClose.value = settings.thresholdClose;
        ui.thresholdCloseValue.textContent = settings.thresholdClose;
        ui.hold.value = settings.hold;
        ui.holdValue.textContent = settings.hold === 0 ? '0 мс (без задержки)' : settings.hold + ' мс';
        ui.attack.value = settings.attack;
        ui.attackValue.textContent = settings.attack === 0 ? '0 мс (выкл)' : settings.attack + ' мс';

        ui.markOpen.style.left = Math.min(100, settings.thresholdOpen * 1.5) + '%';
        ui.markClose.style.left = Math.min(100, settings.thresholdClose * 1.5) + '%';

        ui.scale.value = settings.scale;
        ui.scaleValue.textContent = settings.scale + '%';
        ui.bg.value = settings.bgColor;
        ui.anim.checked = settings.enableAnim;
        ui.blink.checked = settings.enableBlink;
        ui.flip.checked = settings.flip;
        ui.onTop.checked = Boolean(settings.onTop);
        ui.obsBg.checked = Boolean(settings.obsBackground);

        document.documentElement.style.setProperty('--bg-color', settings.bgColor);
        document.documentElement.style.setProperty('--avatar-scale', settings.scale / 100);
        document.body.classList.toggle('transparent', Boolean(options.transparent));

        for (const img of Object.values(imgs)) img.style.transform = settings.flip ? 'scaleX(-1)' : '';
        updateAvatar();
        scheduleAudioSync();
        scheduleWindowSync();
        setMeter(!panel.classList.contains('hidden'));
    }

    const bind = (el, event, key, parse) => el.addEventListener(event, () => {
        settings[key] = parse(el);
        saveSettings();
        applySettings();
    });

    let manualClose = false;
    bind(ui.gain, 'input', 'gain', e => Number(e.value));
    ui.thresholdOpen.addEventListener('input', () => {
        settings.thresholdOpen = Number(ui.thresholdOpen.value);
        if (!manualClose) {
            settings.thresholdClose = Math.max(1, Math.round(settings.thresholdOpen * 0.6));
        } else if (settings.thresholdClose > settings.thresholdOpen) {
            settings.thresholdClose = settings.thresholdOpen;
        }
        saveSettings();
        applySettings();
    });
    ui.thresholdClose.addEventListener('input', () => {
        manualClose = true;
        settings.thresholdClose = Number(ui.thresholdClose.value);
        if (settings.thresholdClose > settings.thresholdOpen) {
            settings.thresholdOpen = settings.thresholdClose;
        }
        saveSettings();
        applySettings();
    });
    bind(ui.hold, 'input', 'hold', e => Number(e.value));
    bind(ui.attack, 'input', 'attack', e => Number(e.value));
    bind(ui.scale, 'input', 'scale', e => Number(e.value));
    bind(ui.bg, 'input', 'bgColor', e => e.value);
    bind(ui.anim, 'change', 'enableAnim', e => e.checked);
    bind(ui.blink, 'change', 'enableBlink', e => e.checked);
    bind(ui.flip, 'change', 'flip', e => e.checked);
    bind(ui.onTop, 'change', 'onTop', e => e.checked);
    bind(ui.obsBg, 'change', 'obsBackground', e => e.checked);

    // Пресеты хромакея
    document.querySelectorAll('.preset-dot').forEach(btn => {
        btn.addEventListener('click', () => {
            const col = btn.dataset.color;
            if (col) {
                settings.bgColor = col;
                saveSettings();
                applySettings();
            }
        });
    });

    // ---------- Вкладки и панель ----------
    document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + btn.dataset.tab));
        if (btn.dataset.tab === 'audio') ensureDevices();
    }));

    const togglePanel = (hide) => {
        panel.classList.toggle('hidden', hide);
        setMeter(!hide);
    };
    $('hide-settings-btn').addEventListener('click', () => togglePanel(true));
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.target.matches && e.target.matches('select, input[type="text"]')) return;
        if (e.key.toLowerCase() === 's' || e.key.toLowerCase() === 'ы') togglePanel();
    });

    // Кнопка парковки в фон для OBS
    ui.parkBtn.addEventListener('click', async () => {
        if (!backend()) return toast('Парковка доступна только в приложении');
        try {
            await apiCall('park_window', 5000);
            toast('Окно в фоне. Нажмите S или запустите PNGTuber ещё раз, чтобы вернуть');
        } catch (e) {
            toast(e.message);
        }
    });

    // ---------- Звук ----------
    let micOn = false;
    let micStarting = false;
    let devicesLoaded = false;
    let lastAudioKey = '';
    let lastOnTop = null;
    let lastObsBg = null;
    let audioSyncTimer;
    let windowSyncTimer;

    function setStatus(state, text) {
        ui.status.className = 'status' + (state ? ' ' + state : '');
        ui.statusText.textContent = text;
    }

    let lastMeter = null;
    function setMeter(on) {
        if (lastMeter === on) return;
        lastMeter = on;
        const api = backend();
        if (api) safe(api.set_meter(on));
    }

    async function ensureDevices(force = false) {
        if (devicesLoaded && !force) return;
        const api = backend();
        if (!api) return;
        try {
            const res = await apiCall('get_audio_devices', 10000);
            const devices = (res && res.devices) || [];
            ui.device.innerHTML = '<option value="">По умолчанию</option>';
            devices.forEach(name => {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                ui.device.appendChild(opt);
            });
            ui.device.value = devices.includes(settings.deviceId) ? settings.deviceId : '';
            if (settings.deviceId && !devices.includes(settings.deviceId)) {
                // Сохранённый микрофон пропал — переключаемся на «по умолчанию»,
                // иначе каждый старт заканчивался бы ошибкой.
                settings.deviceId = '';
                saveSettings();
            }
            devicesLoaded = true;
        } catch (e) {
            console.warn('devices', e);
        }
    }

    async function startAudio({ quiet = false } = {}) {
        const api = backend();
        if (!api) {
            setStatus('', 'только в приложении');
            if (!quiet) toast('Микрофон доступен только в десктопной версии');
            return;
        }
        if (micStarting) return;
        micStarting = true;
        ui.start.disabled = true;
        ui.start.textContent = 'Подключение…';
        setStatus('', 'ищу микрофон…');

        try {
            const res = await withTimeout(api.start_audio(settings.deviceId || ''), 10000, 'микрофон');
            if (!res || !res.ok) throw new Error((res && res.error) || 'не удалось открыть микрофон');
            micOn = true;
            lastAudioKey = '';                 // заставим отправить параметры заново
            scheduleAudioSync(0);
            ui.start.textContent = 'Перезапустить микрофон';
            setStatus('live', 'микрофон активен');
        } catch (e) {
            micOn = false;
            ui.start.textContent = 'Включить микрофон';
            setStatus('', 'ошибка микрофона');
            if (!quiet) toast('Микрофон: ' + e.message);
            console.warn('mic', e);
        } finally {
            micStarting = false;
            ui.start.disabled = false;
        }
    }

    window.__micTick = (speaking, width) => {
        if (typeof width === 'number' && width >= 0) ui.fill.style.width = width + '%';
        if (speaking === isSpeaking) return;
        isSpeaking = speaking;
        updateAvatar();
    };
    // Совместимость со старым вызовом (на случай обновления только бэкенда)
    window.__micState = (speaking) => window.__micTick(speaking);
    window.__micMeter = (width) => { ui.fill.style.width = width + '%'; };

    function scheduleAudioSync(delay = 120) {
        clearTimeout(audioSyncTimer);
        audioSyncTimer = setTimeout(() => {
            const api = backend();
            if (!api) return;
            const key = [settings.thresholdOpen, settings.thresholdClose, settings.hold,
                settings.attack, settings.gain].join('|');
            if (key === lastAudioKey) return;
            lastAudioKey = key;
            // Раньше это вызывалось на КАЖДОЕ движение слайдера: десятки
            // синхронных вызовов через мост подряд — окно переставало отвечать.
            safe(api.set_audio_params(
                settings.thresholdOpen,
                settings.thresholdClose,
                settings.hold,
                settings.attack,
                settings.gain
            ));
        }, delay);
    }

    function scheduleWindowSync(delay = 150) {
        clearTimeout(windowSyncTimer);
        windowSyncTimer = setTimeout(() => {
            const api = backend();
            if (!api) return;
            if (settings.onTop !== lastOnTop) {
                lastOnTop = settings.onTop;
                safe(api.set_always_on_top(settings.onTop));
            }
            if (settings.obsBackground !== lastObsBg) {
                lastObsBg = settings.obsBackground;
                safe(api.set_obs_background(settings.obsBackground));
            }
        }, delay);
    }

    ui.start.addEventListener('click', () => startAudio());
    ui.device.addEventListener('change', () => {
        settings.deviceId = ui.device.value;
        saveSettings();
        if (micOn) startAudio({ quiet: true });
    });

    // ---------- Сцены (.pngtuber) ----------
    $('save-scene-btn').addEventListener('click', async () => {
        if (!backend()) return toast('Сохранение доступно только в десктопной версии');
        const { thresholdOpen, thresholdClose, hold, attack, gain, bgColor, scale,
            enableAnim, enableBlink, flip } = settings;
        try {
            // Картинки Python берёт с диска — сначала дождёмся незавершённых записей.
            await Promise.allSettled([...pendingImageSaves]);
            // Через мост идут только настройки.
            const res = await apiCall('save_scene', 30000, JSON.stringify({
                thresholdOpen, thresholdClose, hold, attack, gain, bgColor, scale,
                enableAnim, enableBlink, flip
            }));
            if (!res) return toast('Сохранение отменено');
            toast(res.ok ? 'Сцена сохранена' : 'Не удалось: ' + (res.error || 'ошибка'));
        } catch (e) {
            toast(e.message);
        }
    });

    $('load-scene-btn').addEventListener('click', async () => {
        if (!backend()) return toast('Загрузка доступна только в десктопной версии');
        try {
            await Promise.allSettled([...pendingImageSaves]);
            const res = await apiCall('load_scene', 60000);
            if (!res) return toast('Загрузка отменена');
            if (!res.ok) return toast('Не удалось: ' + (res.error || 'ошибка'));

            if (res.config) {
                const merged = { ...settings, ...res.config };
                if ('threshold' in res.config && !('thresholdOpen' in res.config)) {
                    merged.thresholdOpen = res.config.threshold;
                    merged.thresholdClose = Math.max(1, Number(res.config.threshold) - 6);
                }
                settings = sanitizeSettings(merged);
                saveSettings();
            }
            applySettings();

            const images = res.images || {};
            for (const slot of SLOTS) {
                if (images[slot]) setSlotImage(slot, images[slot], { persist: false });  // уже на диске
                else setSlotImage(slot, '');                                            // чистим слот
            }
            toast('Сцена загружена');
        } catch (e) {
            toast(e.message);
        }
    });

    // ---------- Запуск ----------
    let initRunning = false;
    let backendStateLoaded = false;

    async function init() {
        if (initRunning) return;
        initRunning = true;

        try {
            const api = await whenBackendReady();
            if (api) {
                const state = await withTimeout(api.get_state(), 20000, 'загрузка настроек');
                settings = sanitizeSettings(state.settings);
                options = { ...options, ...(state.options || {}) };
                await Promise.all(SLOTS.map(slot =>
                    setSlotImage(slot, (state.images && state.images[slot]) || '', { persist: false })));
                backendStateLoaded = true;
            } else {
                try { settings = sanitizeSettings(JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')); } catch (_) {}
                SLOTS.forEach(slot => setSlotImage(slot, '', { persist: false }));
            }
        } catch (e) {
            console.error(e);
            toast('Настройки не загрузились — работаю со значениями по умолчанию');
            SLOTS.forEach(slot => setSlotImage(slot, '', { persist: false }));
        } finally {
            initRunning = false;
            try { applySettings(); } catch (e) { console.error(e); }
            hideSplash();
        }

        // Всё, что может занять секунды (перебор устройств, старт PortAudio),
        // запускаем ПОСЛЕ первого кадра, чтобы окно уже было отзывчивым.
        requestAnimationFrame(() => requestAnimationFrame(async () => {
            await ensureDevices();
            startAudio({ quiet: true });
        }));
    }

    init();

    // Если мост появился позже таймаута — догоняем инициализацию.
    window.addEventListener('pywebviewready', () => {
        if (!backend()) return;
        if (!backendStateLoaded) { init(); return; }
        if (!devicesLoaded) ensureDevices(true);
    });

    window.addEventListener('error', (e) => console.error('JS error:', e.message));
    window.addEventListener('unhandledrejection', (e) => console.error('JS rejection:', e.reason));
})();
