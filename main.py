import argparse
import base64
import json
import mimetypes
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

# Предотвращаем усыпление Chromium в фоне и отключаем лишние подсистемы для экономии памяти
os.environ.setdefault(
    'QTWEBENGINE_CHROMIUM_FLAGS',
    '--disable-background-timer-throttling '
    '--disable-renderer-backgrounding '
    '--disable-backgrounding-occluded-windows '
    '--disable-features=CalculateNativeWinOcclusion,Translate,OptimizationHints,MediaRouter '
    '--renderer-process-limit=1 '
    '--js-flags=--max-old-space-size=64 '
    '--disable-gpu-memory-buffer-video-frames '
    '--in-process-gpu'
)

import webview

try:
    import numpy as np
    import sounddevice as sd
except (ImportError, OSError):   # OSError: в системе нет PortAudio
    np = sd = None

APP_NAME = 'PNGTuber'
SLOTS = ('idle', 'speaking', 'idle_blink', 'speaking_blink')
SCENE_FILTER = ('Сцена PNGTuber (*.pngtuber)', 'Все файлы (*.*)')
IMAGE_FILTER = ('Изображения (*.png;*.jpg;*.jpeg;*.gif;*.webp)', 'Все файлы (*.*)')


def get_web_dir():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / 'web'


def get_config_dir():
    if sys.platform == 'win32':
        root = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support'
    else:
        root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return root / APP_NAME


def decode_data_url(data_url):
    """Байты из data:...;base64,... либо None, если строка пустая или битая."""
    if not data_url or ',' not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(',', 1)[1])
    except ValueError:
        return None


def encode_data_url(data, mime='image/png'):
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


def first_path(result):
    """create_file_dialog возвращает кортеж/список путей или None."""
    if not result:
        return None
    return result if isinstance(result, str) else result[0]


class Microphone:
    """Захват звука через PortAudio с двухпороговым регистром и квантованием 10 мс."""

    TICK = 0.01             # шаг расчёта уровня 10 мс (100 Гц — плавно и экономно для CPU)
    METER_EVERY = 3         # шкалу уровня шлём на страницу раз в N шагов (~30 мс) и только пока открыта панель

    def __init__(self, window_getter):
        self.window_getter = window_getter
        self.stream = None
        self.raw = 0.0
        self.threshold_open = 18.0    # порог открытия рта (Open Threshold)
        self.threshold_close = 10.0   # порог закрытия рта (Close Threshold)
        self.hold = 0.15              # задержка закрытия рта, с (Hold delay)
        self.attack = 0.0             # задержка открытия рта, с (Attack delay)
        self.gain = 1.0               # коэффициент усиления микрофона (1.0 = 100%)
        self.meter = False
        self.lock = threading.RLock()

    @staticmethod
    def device_names():
        if sd is None:
            return []
        return [d['name'] for d in sd.query_devices()
                if d['max_input_channels'] > 0 and d['hostapi'] == sd.default.hostapi]

    @staticmethod
    def device_index(name):
        if not name:
            return None
        for i, d in enumerate(sd.query_devices()):
            if d['name'] == name and d['max_input_channels'] > 0 and d['hostapi'] == sd.default.hostapi:
                return i
        return None

    def _callback(self, indata, frames, time_info, status):
        # Внутри аудио-коллбэка вычисляем только амплитуду, не нагружая поток
        samples = (indata[:, 0] - np.mean(indata[:, 0])) * self.gain
        rms = float(np.sqrt(np.mean(np.square(samples))))
        self.raw = min(100.0, rms * 400.0)

    def _js(self, code):
        try:
            win = self.window_getter()
            if win:
                win.evaluate_js(code)
        except Exception:
            pass

    def _loop(self, stream):
        level = 0.0
        last_loud = 0.0
        first_loud = 0.0
        speaking = False
        shown = -1
        tick = 0
        while self.stream is stream:
            raw = self.raw
            level += (raw - level) * (0.6 if raw > level else 0.2)   # быстрая атака, плавный спад
            now = time.monotonic()

            t_open = self.threshold_open
            t_close = min(self.threshold_close, t_open)

            # Двухпороговый триггер Шмитта:
            if speaking:
                if level >= t_close:
                    last_loud = now
                # При hold == 0 мс рот мгновенно закрывается, как только level < t_close
                now_speaking = (level >= t_close) or (now - last_loud < self.hold)
                first_loud = 0.0
            else:
                if level >= t_open:
                    if first_loud == 0.0:
                        first_loud = now
                    # Проверка задержки атаки (Attack delay)
                    if now - first_loud >= self.attack:
                        now_speaking = True
                        last_loud = now
                    else:
                        now_speaking = False
                else:
                    first_loud = 0.0
                    now_speaking = False

            if now_speaking != speaking:
                speaking = now_speaking
                self._js(f'window.__micState&&window.__micState({"true" if speaking else "false"})')

            tick += 1
            if self.meter and tick % self.METER_EVERY == 0:
                width = round(min(100, level * 1.5))
                if width != shown:
                    shown = width
                    self._js(f'window.__micMeter&&window.__micMeter({width})')

            time.sleep(self.TICK)

        if speaking:
            self._js('window.__micState&&window.__micState(false)')

    def set_audio_params(self, threshold_open=18, threshold_close=10, hold_ms=150, attack_ms=0, gain_pct=100):
        self.threshold_open = float(threshold_open)
        self.threshold_close = float(threshold_close)
        self.hold = float(hold_ms) / 1000.0
        self.attack = float(attack_ms) / 1000.0
        self.gain = max(0.1, float(gain_pct) / 100.0)

    def start(self, name):
        if sd is None:
            raise RuntimeError('не найдена библиотека PortAudio (sounddevice)')
        with self.lock:
            self.stop()
            stream = sd.InputStream(device=self.device_index(name), channels=1, dtype='float32',
                                    blocksize=256, callback=self._callback)
            stream.start()
            self.stream = stream
            self.raw = 0.0
            threading.Thread(target=self._loop, args=(stream,), daemon=True).start()

    def stop(self):
        with self.lock:
            stream, self.stream = self.stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._js('window.__micState&&window.__micState(false)')


class Api:
    """Методы, доступные из JavaScript как window.pywebview.api.*"""

    def __init__(self, options):
        self.window = None
        self.options = options
        self.obs_background = True
        self.saved_geometry = None
        self.is_parked = False
        self.config_dir = get_config_dir()
        self.settings_file = self.config_dir / 'settings.json'
        self.images_dir = self.config_dir / 'images'
        self.mic = Microphone(lambda: self.window)

    # ---- Состояние между запусками ----

    def get_state(self):
        try:
            settings = json.loads(self.settings_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}

        # Обратная совместимость с единым threshold
        if 'threshold' in settings and 'thresholdOpen' not in settings:
            settings['thresholdOpen'] = settings['threshold']
            settings['thresholdClose'] = max(1, settings['threshold'] - 6)

        images = {}
        for slot in SLOTS:
            try:
                images[slot] = encode_data_url((self.images_dir / f'{slot}.png').read_bytes())
            except OSError:
                pass
        return {'settings': settings, 'images': images, 'options': self.options}

    def save_settings(self, settings_json):
        try:
            json.loads(settings_json)   # не пишем на диск мусор
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(settings_json, encoding='utf-8')
        except (OSError, ValueError) as e:
            print('Не удалось сохранить настройки:', e)

    def set_image(self, slot, data_url):
        """Запоминает картинку слота; пустая строка возвращает слот к значению по умолчанию."""
        if slot not in SLOTS:
            return
        path = self.images_dir / f'{slot}.png'
        try:
            data = decode_data_url(data_url)
            if data:
                self.images_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            print('Не удалось сохранить изображение:', e)

    def pick_image(self):
        """Нативный диалог выбора картинки; <input type=file> в Qt WebEngine на Windows не открывается."""
        dialog_type = getattr(webview, 'FileDialog', None)
        open_dlg = dialog_type.OPEN if dialog_type else webview.OPEN_DIALOG
        path = first_path(self.window.create_file_dialog(
            open_dlg, file_types=IMAGE_FILTER))
        if not path:
            return None
        try:
            mime = mimetypes.guess_type(path)[0] or 'image/png'
            if not mime.startswith('image/'):
                return None
            return encode_data_url(Path(path).read_bytes(), mime)
        except OSError as e:
            print('Не удалось прочитать изображение:', e)
            return None

    # ---- Управление окном (OBS & Always On Top) ----

    def set_always_on_top(self, on_top):
        if self.window:
            try:
                self.window.on_top = bool(on_top)
                return True
            except Exception as e:
                print('Не удалось переключить on_top:', e)
        return False

    def set_obs_background(self, enabled):
        self.obs_background = bool(enabled)
        return True

    def park_window(self):
        """Паркует окно за пределы экрана без минимизации, сохраняя активный рендеринг для OBS."""
        if not self.window:
            return False
        try:
            from webview.platforms import qt
            view = qt.BrowserView.instances.get(self.window.uid)
            if view and not self.is_parked:
                self.saved_geometry = (view.x(), view.y())
                view.move(-15000, -15000)
                self.is_parked = True
                return True
        except Exception as e:
            print('Не удалось припарковать окно:', e)
        return False

    def restore_window(self):
        """Возвращает припаркованное окно обратно на экран."""
        if not self.window:
            return False
        try:
            from webview.platforms import qt
            view = qt.BrowserView.instances.get(self.window.uid)
            if view and self.is_parked:
                self.is_parked = False
                if self.saved_geometry:
                    view.move(self.saved_geometry[0], self.saved_geometry[1])
                else:
                    view.move(100, 100)
                view.raise_()
                view.activateWindow()
                return True
        except Exception as e:
            print('Не удалось восстановить окно:', e)
        return False

    # ---- Микрофон ----

    def get_audio_devices(self):
        try:
            return {'available': sd is not None, 'devices': Microphone.device_names()}
        except Exception as e:
            print('Не удалось получить список микрофонов:', e)
            return {'available': False, 'devices': []}

    def start_audio(self, device_name):
        try:
            self.mic.start(device_name)
            return {'ok': True}
        except Exception as e:
            print('Не удалось открыть микрофон:', e)
            return {'ok': False, 'error': str(e)}

    def stop_audio(self):
        self.mic.stop()

    def set_audio_params(self, threshold_open=18, threshold_close=10, hold_ms=150, attack_ms=0, gain_pct=100):
        self.mic.set_audio_params(threshold_open, threshold_close, hold_ms, attack_ms, gain_pct)

    def set_meter(self, on):
        self.mic.meter = bool(on)

    # ---- Сцены (.pngtuber) ----

    def save_scene(self, config_json, idle, speaking, idle_blink, speaking_blink):
        dialog_type = getattr(webview, 'FileDialog', None)
        save_dlg = dialog_type.SAVE if dialog_type else webview.SAVE_DIALOG
        path = first_path(self.window.create_file_dialog(
            save_dlg, save_filename='model.pngtuber', file_types=SCENE_FILTER))
        if not path:
            return False
        if not path.endswith('.pngtuber'):
            path += '.pngtuber'

        images = dict(zip(SLOTS, (idle, speaking, idle_blink, speaking_blink)))
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('config.json', config_json)
                for name, data_url in images.items():
                    data = decode_data_url(data_url)
                    if data:
                        zf.writestr(f'{name}.png', data)
            return True
        except OSError as e:
            print('Не удалось сохранить сцену:', e)
            return False

    def load_scene(self):
        dialog_type = getattr(webview, 'FileDialog', None)
        open_dlg = dialog_type.OPEN if dialog_type else webview.OPEN_DIALOG
        path = first_path(self.window.create_file_dialog(
            open_dlg, file_types=SCENE_FILTER))
        if not path:
            return None

        scene = {}
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if 'config.json' in names:
                    scene['config'] = json.loads(zf.read('config.json').decode('utf-8'))
                for name in SLOTS:
                    if f'{name}.png' in names:
                        scene[name] = encode_data_url(zf.read(f'{name}.png'))
            return scene
        except (OSError, zipfile.BadZipFile, ValueError) as e:
            print('Не удалось загрузить сцену:', e)
            return None


def parse_args():
    parser = argparse.ArgumentParser(description='Лёгкий PNGTuber')
    parser.add_argument('--transparent', action='store_true',
                        help='прозрачное окно вместо хромакей-фона (на части систем Linux даёт артефакты)')
    return parser.parse_args()


def on_window_minimized(api):
    """Когда пользователь нажал 'свернуть' в заголовке окна:

    при включенном obs_background окно паркуется за экран, чтобы буфер рендеринга
    не уничтожался ОС и окно продолжало транслироваться в OBS.
    """
    if not api or not api.obs_background:
        return
    try:
        from webview.platforms import qt
        view = qt.BrowserView.instances.get(api.window.uid)
        if view:
            # Восстанавливаем состояние окна из Minimized обратно в Normal
            view.showNormal()
            api.park_window()
    except Exception as e:
        print('Обработка минимизации для OBS:', e)


if __name__ == '__main__':
    args = parse_args()
    api = Api({'transparent': args.transparent})
    window = webview.create_window(
        APP_NAME,
        url=str(get_web_dir() / 'index.html'),
        js_api=api,
        width=560,
        height=720,
        transparent=args.transparent,
    )
    api.window = window
    window.events.minimized += lambda: on_window_minimized(api)
    window.events.restored += lambda: api.restore_window()
    gui = 'qt' if sys.platform.startswith('linux') else None
    webview.start(gui=gui, http_server=True, debug=False)
