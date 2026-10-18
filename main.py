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
    """Захват звука через PortAudio. Движок браузера микрофон не открывает, поэтому системного запроса разрешения нет."""

    TICK = 0.02             # шаг расчёта уровня, с
    METER_EVERY = 3         # шкалу уровня шлём на страницу раз в N шагов, и только пока открыта панель

    def __init__(self, window_getter):
        self.window_getter = window_getter
        self.stream = None
        self.raw = 0.0
        self.threshold = 15
        self.hold = 0.15
        self.meter = False
        self.lock = threading.Lock()

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
        self.raw = min(100.0, float(np.sqrt(np.mean(np.square(indata)))) * 400)

    def _js(self, code):
        # каждый вызов evaluate_js оставляет мусор в движке страницы, поэтому шлём только изменения
        try:
            self.window_getter().evaluate_js(code)
        except Exception:
            pass

    def _loop(self, stream):
        level = 0.0
        last_loud = 0.0
        speaking = False
        shown = -1
        tick = 0
        while self.stream is stream:
            raw = self.raw
            level += (raw - level) * (0.6 if raw > level else 0.2)   # быстрая атака, плавный спад
            now = time.monotonic()
            if level > self.threshold:
                last_loud = now
            now_speaking = now - last_loud < self.hold
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

    def start(self, name):
        if sd is None:
            raise RuntimeError('не найдена библиотека PortAudio (sounddevice)')
        with self.lock:
            self.stop()
            stream = sd.InputStream(device=self.device_index(name), channels=1, dtype='float32',
                                    blocksize=1024, callback=self._callback)
            stream.start()
            self.stream = stream
            self.raw = 0.0
            threading.Thread(target=self._loop, args=(stream,), daemon=True).start()

    def stop(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.stop()
            stream.close()


class Api:
    """Методы, доступные из JavaScript как window.pywebview.api.*"""

    def __init__(self, options):
        self.window = None
        self.options = options
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
        path = first_path(self.window.create_file_dialog(
            webview.OPEN_DIALOG, file_types=IMAGE_FILTER))
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

    def set_audio_params(self, threshold, hold_ms):
        self.mic.threshold = float(threshold)
        self.mic.hold = float(hold_ms) / 1000

    def set_meter(self, on):
        self.mic.meter = bool(on)

    # ---- Сцены (.pngtuber) ----

    def save_scene(self, config_json, idle, speaking, idle_blink, speaking_blink):
        path = first_path(self.window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename='model.pngtuber', file_types=SCENE_FILTER))
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
        path = first_path(self.window.create_file_dialog(
            webview.OPEN_DIALOG, file_types=SCENE_FILTER))
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


if __name__ == '__main__':
    args = parse_args()
    api = Api({'transparent': args.transparent})
    api.window = webview.create_window(
        APP_NAME,
        url=str(get_web_dir() / 'index.html'),
        js_api=api,
        width=560,
        height=680,
        transparent=args.transparent,
    )
    webview.start(http_server=True, debug=False)
