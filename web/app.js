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
        for (const img of Object.values(imgs)) img.classList.toggle('active', img === target);
        bounce.classList.toggle('bouncing', settings.enableAnim && isSpeaking);
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
            // сброс: возвращаемся к встроенным idle.png / speaking.png, у остальных — пусто
            const builtin = slot === 'idle' || slot === 'speaking';
            if (builtin) imgs[slot].src = thumb.src = slot + '.png';
            else { imgs[slot].removeAttribute('src'); thumb.removeAttribute('src'); }
            thumb.hidden = !builtin;
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

    for (const slot of SLOTS) {
        const el = slotEls[slot];
        const input = el.querySelector('input');
        input.addEventListener('change', () => { readFile(slot, input.files[0]); input.value = ''; });
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

    const togglePanel = (hide) => panel.classList.toggle('hidden', hide);
    $('hide-settings-btn').addEventListener('click', () => togglePanel(true));
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.target.matches && e.target.matches('select, input[type="text"]')) return;
        if (e.key.toLowerCase() === 's' || e.key.toLowerCase() === 'ы') togglePanel();
    });

    // ---------- Звук ----------
    let audioCtx = null;
    let analyser = null;
    let stream = null;
    let buffer = null;
    let rafId = 0;
    let level = 0;          // сглаженная громкость 0..100
    let lastLoud = 0;       // момент, когда громкость последний раз превышала порог

    function setStatus(state, text) {
        ui.status.className = 'status' + (state ? ' ' + state : '');
        ui.statusText.textContent = text;
    }

    async function listDevices() {
        try {
            const devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
            ui.device.innerHTML = '<option value="">По умолчанию</option>';
            devices.forEach((d, i) => {
                const opt = document.createElement('option');
                opt.value = d.deviceId;
                opt.textContent = d.label || ('Микрофон ' + (i + 1));
                ui.device.appendChild(opt);
            });
            ui.device.value = devices.some(d => d.deviceId === settings.deviceId) ? settings.deviceId : '';
        } catch (_) {}
    }

    function stopAudio() {
        cancelAnimationFrame(rafId);
        if (stream) stream.getTracks().forEach(t => t.stop());
        if (audioCtx) audioCtx.close();
        audioCtx = analyser = stream = null;
    }

    async function startAudio() {
        stopAudio();
        ui.start.disabled = true;
        ui.start.textContent = 'Подключение…';
        try {
            // Обработка голоса (шумо-/эхоподавление, автоусиление) не нужна и заметно грузит CPU;
            // автоусиление к тому же выравнивает громкость и ломает порог срабатывания
            const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
            if (settings.deviceId) audio.deviceId = { exact: settings.deviceId };
            stream = await navigator.mediaDevices.getUserMedia({ audio });
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            analyser = audioCtx.createAnalyser();
            analyser.fftSize = 1024;
            audioCtx.createMediaStreamSource(stream).connect(analyser);
            buffer = new Uint8Array(analyser.fftSize);

            ui.start.textContent = 'Перезапустить микрофон';
            ui.start.disabled = false;
            setStatus('live', 'микрофон активен');
            listDevices();   // после разрешения доступны названия устройств
            loop();
        } catch (err) {
            console.error('Ошибка доступа к микрофону:', err);
            ui.start.disabled = false;
            ui.start.textContent = 'Включить микрофон';
            setStatus('', err && err.name === 'NotAllowedError' ? 'нет доступа' : 'ошибка микрофона');
            toast(err && err.name === 'NotAllowedError' ? 'Разрешите доступ к микрофону' : 'Не удалось открыть микрофон');
        }
    }

    function loop() {
        rafId = requestAnimationFrame(loop);
        analyser.getByteTimeDomainData(buffer);

        // RMS по временной области — ровнее, чем среднее по спектру
        let sum = 0;
        for (let i = 0; i < buffer.length; i++) {
            const v = (buffer[i] - 128) / 128;
            sum += v * v;
        }
        const raw = Math.min(100, Math.sqrt(sum / buffer.length) * 400);
        level += (raw - level) * (raw > level ? 0.6 : 0.2);   // быстрая атака, плавный спад

        const now = performance.now();
        if (level > settings.threshold) lastLoud = now;
        const speaking = now - lastLoud < settings.hold;

        if (speaking !== isSpeaking) {
            isSpeaking = speaking;
            updateAvatar();
            if (!panel.classList.contains('hidden')) setStatus(speaking ? 'talking' : 'live', speaking ? 'говорит' : 'микрофон активен');
        }
        if (!panel.classList.contains('hidden')) ui.fill.style.width = Math.min(100, level * 1.5) + '%';
    }

    ui.start.addEventListener('click', startAudio);
    ui.device.addEventListener('change', () => {
        settings.deviceId = ui.device.value;
        saveSettings();
        if (stream) startAudio();
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
