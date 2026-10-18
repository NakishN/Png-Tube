(() => {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const SLOTS = ['idle', 'speaking', 'idle_blink', 'speaking_blink'];
    const STORAGE_KEY = 'pngtuber_settings_v2';   // запасной вариант, если страница открыта без десктопной оболочки
    const DEFAULTS = {
        threshold: 15,
        hold: 150,
        bgColor: '#00ff00',
        scale: 100,
        enableAnim: true,
        enableBlink: true,
        flip: false,
        deviceId: ''
    };

    // ---------- Связь с десктопной оболочкой ----------
    // Настройки и картинки хранит Python (конфиг-каталог пользователя): у встроенного
    // HTTP-сервера порт каждый раз новый, так что localStorage между запусками не живёт.
    const backend = () => (window.pywebview && window.pywebview.api) || null;

    async function whenBackendReady() {
        if (backend()) return backend();
        await new Promise(resolve => {
            window.addEventListener('pywebviewready', resolve, { once: true });
            setTimeout(resolve, 800);   // страница открыта в обычном браузере
        });
        return backend();
    }

    // ---------- Настройки ----------
    let settings = { ...DEFAULTS };
    let options = { transparent: false };

    let saveTimer;
    function saveSettings() {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            const api = backend();
            if (api) api.save_settings(JSON.stringify(settings));
            else try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (_) {}
        }, 300);
    }

    // ---------- Элементы ----------
    const panel = $('settings-panel');
    const bounce = $('avatar-bounce');
    const imgs = Object.fromEntries(SLOTS.map(s => [s, $(s + '-img')]));
    const slotEls = Object.fromEntries(SLOTS.map(s => [s, document.querySelector(`.slot[data-slot="${s}"]`)]));
    const dataURLs = Object.fromEntries(SLOTS.map(s => [s, '']));  // пользовательские изображения (для сохранения сцены)

    const ui = {
        threshold: $('threshold-slider'), thresholdValue: $('threshold-value'),
        hold: $('hold-slider'), holdValue: $('hold-value'),
        scale: $('scale-slider'), scaleValue: $('scale-value'),
        bg: $('bg-color-picker'),
        anim: $('animation-toggle'), blink: $('blink-toggle'), flip: $('flip-toggle'),
        device: $('device-select'), start: $('start-btn'),
        fill: $('volume-fill'), mark: $('threshold-mark'),
        status: $('status'), statusText: $('status-text')
    };

    let toastTimer;
    function toast(msg) {
        const el = $('toast');
        el.textContent = msg;
        el.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
    }

    // ---------- Состояние аватара ----------
    let isSpeaking = false;
    let isBlinking = false;

    const loaded = (img) => img.complete && img.naturalWidth > 0;

    let shown = imgs.idle;  // что сейчас показано: лишние перекраски копят память в Chromium
    let bouncing = false;
    // Одна анимация на всё время работы: пересоздание CSS-анимации при каждой фразе копит память
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

    for (const img of Object.values(imgs)) img.addEventListener('load', updateAvatar);

    // ---------- Моргание ----------
    function scheduleBlink() {
        setTimeout(() => {
            if (settings.enableBlink) {
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
    function setSlotImage(slot, src, persist = true) {
        dataURLs[slot] = src || '';
        const thumb = slotEls[slot].querySelector('img');
        if (src) {
            imgs[slot].src = src;
            thumb.src = src;
            thumb.hidden = false;
            slotEls[slot].classList.add('filled');
        } else {
            imgs[slot].removeAttribute('src');
            thumb.removeAttribute('src');
            thumb.hidden = true;
            slotEls[slot].classList.remove('filled');
        }
        if (persist && backend()) backend().set_image(slot, dataURLs[slot]);
        updateAvatar();
    }

    function readFile(slot, file) {
        if (!file || !file.type.startsWith('image/')) return toast('Нужен файл изображения');
        const reader = new FileReader();
        reader.onload = (e) => setSlotImage(slot, e.target.result);
        reader.readAsDataURL(file);
    }

    async function pickImage(slot) {
        if (!backend()) return toast('Выбор файла доступен только в десктопной версии');
        const src = await backend().pick_image();
        if (src) setSlotImage(slot, src);
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
        ui.threshold.value = settings.threshold;
        ui.thresholdValue.textContent = settings.threshold;
        ui.mark.style.left = Math.min(100, settings.threshold * 1.5) + '%';
        ui.hold.value = settings.hold;
        ui.holdValue.textContent = settings.hold + ' мс';
        ui.scale.value = settings.scale;
        ui.scaleValue.textContent = settings.scale + '%';
        ui.bg.value = settings.bgColor;
        ui.anim.checked = settings.enableAnim;
        ui.blink.checked = settings.enableBlink;
        ui.flip.checked = settings.flip;

        document.documentElement.style.setProperty('--bg-color', settings.bgColor);
        document.documentElement.style.setProperty('--avatar-scale', settings.scale / 100);
        document.body.classList.toggle('transparent', options.transparent);
        // transform на контейнере конфликтует с анимацией прыжка — отражаем сами изображения
        for (const img of Object.values(imgs)) img.style.transform = settings.flip ? 'scaleX(-1)' : '';
        updateAvatar();
        syncAudioParams();
    }

    const bind = (el, event, key, parse) => el.addEventListener(event, () => {
        settings[key] = parse(el);
        saveSettings();
        applySettings();
    });
    bind(ui.threshold, 'input', 'threshold', e => Number(e.value));
    bind(ui.hold, 'input', 'hold', e => Number(e.value));
    bind(ui.scale, 'input', 'scale', e => Number(e.value));
    bind(ui.bg, 'input', 'bgColor', e => e.value);
    bind(ui.anim, 'change', 'enableAnim', e => e.checked);
    bind(ui.blink, 'change', 'enableBlink', e => e.checked);
    bind(ui.flip, 'change', 'flip', e => e.checked);

    // ---------- Вкладки и панель ----------
    document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + btn.dataset.tab));
    }));

    const togglePanel = (hide) => { panel.classList.toggle('hidden', hide); syncAudioParams(); };
    $('hide-settings-btn').addEventListener('click', () => togglePanel(true));
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.target.matches && e.target.matches('select, input[type="text"]')) return;
        if (e.key.toLowerCase() === 's' || e.key.toLowerCase() === 'ы') togglePanel();
    });

    // ---------- Звук ----------
    // Микрофон читает Python (PortAudio) и присылает уровень сюда: браузерный getUserMedia
    // потребовал бы разрешения у системы при каждом запуске
    let micOn = false;

    function setStatus(state, text) {
        ui.status.className = 'status' + (state ? ' ' + state : '');
        ui.statusText.textContent = text;
    }

    async function listDevices() {
        const api = backend();
        if (!api) return;
        try {
            const { devices } = await api.get_audio_devices();
            ui.device.innerHTML = '<option value="">По умолчанию</option>';
            devices.forEach(name => {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                ui.device.appendChild(opt);
            });
            ui.device.value = devices.includes(settings.deviceId) ? settings.deviceId : '';
        } catch (_) {}
    }

    async function startAudio() {
        const api = backend();
        if (!api) {
            setStatus('', 'только в приложении');
            return toast('Микрофон доступен только в десктопной версии');
        }
        ui.start.disabled = true;
        ui.start.textContent = 'Подключение…';
        const res = await api.start_audio(settings.deviceId || '');
        ui.start.disabled = false;
        if (!res.ok) {
            micOn = false;
            ui.start.textContent = 'Включить микрофон';
            setStatus('', 'ошибка микрофона');
            return toast('Не удалось открыть микрофон');
        }
        micOn = true;
        syncAudioParams();
        ui.start.textContent = 'Перезапустить микрофон';
        setStatus('live', 'микрофон активен');
    }

    // Порог, сглаживание и задержка считаются в Python; сюда приходят только смены состояния
    window.__micState = (speaking) => {
        if (speaking === isSpeaking) return;
        isSpeaking = speaking;
        updateAvatar();
    };
    window.__micMeter = (width) => { ui.fill.style.width = width + '%'; };

    function syncAudioParams() {
        if (!backend()) return;
        backend().set_audio_params(settings.threshold, settings.hold);
        backend().set_meter(!panel.classList.contains('hidden'));
    }

    ui.start.addEventListener('click', startAudio);
    ui.device.addEventListener('change', () => {
        settings.deviceId = ui.device.value;
        saveSettings();
        if (micOn) startAudio();
    });

    // ---------- Сцены (десктопная версия) ----------
    $('save-scene-btn').addEventListener('click', async () => {
        if (!backend()) return toast('Сохранение доступно только в десктопной версии');
        const { threshold, hold, bgColor, scale, enableAnim, enableBlink, flip } = settings;
        const ok = await backend().save_scene(
            JSON.stringify({ threshold, hold, bgColor, scale, enableAnim, enableBlink, flip }),
            dataURLs.idle, dataURLs.speaking, dataURLs.idle_blink, dataURLs.speaking_blink
        );
        toast(ok ? 'Сцена сохранена' : 'Сохранение отменено');
    });

    $('load-scene-btn').addEventListener('click', async () => {
        if (!backend()) return toast('Загрузка доступна только в десктопной версии');
        const scene = await backend().load_scene();
        if (!scene) return toast('Загрузка отменена');
        if (scene.config) {
            for (const key of Object.keys(DEFAULTS)) {
                if (key !== 'deviceId' && key in scene.config) settings[key] = scene.config[key];
            }
            saveSettings();
            applySettings();
        }
        SLOTS.forEach(slot => setSlotImage(slot, scene[slot] || ''));
        toast('Сцена загружена');
    });

    // ---------- Запуск ----------
    async function init() {
        const api = await whenBackendReady();
        if (api) {
            const state = await api.get_state();
            Object.assign(settings, state.settings);
            Object.assign(options, state.options);
            SLOTS.forEach(slot => setSlotImage(slot, state.images[slot] || '', false));
        } else {
            try { Object.assign(settings, JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')); } catch (_) {}
            SLOTS.forEach(slot => setSlotImage(slot, '', false));
        }
        applySettings();
        listDevices();
        // Автозапуск (в OBS и при уже выданном разрешении интерфейс не требуется)
        startAudio();
    }
    init();
})();
