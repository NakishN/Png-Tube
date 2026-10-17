import argparse
import base64
import json
import os
import sys
import zipfile
from pathlib import Path

import webview

APP_NAME = 'PNGTuber'
SLOTS = ('idle', 'speaking', 'idle_blink', 'speaking_blink')
SCENE_FILTER = ('Сцена PNGTuber (*.pngtuber)', 'Все файлы (*.*)')


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


def encode_data_url(data):
    return 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')


def first_path(result):
    """create_file_dialog возвращает кортеж/список путей или None."""
    if not result:
        return None
    return result if isinstance(result, str) else result[0]


class Api:
    """Методы, доступные из JavaScript как window.pywebview.api.*"""

    def __init__(self, options):
        self.window = None
        self.options = options
        self.config_dir = get_config_dir()
        self.settings_file = self.config_dir / 'settings.json'
        self.images_dir = self.config_dir / 'images'

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
