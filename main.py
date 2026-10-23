import argparse
import base64
import faulthandler
import importlib
import io
import json
import logging
import logging.handlers
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
import zipfile
from array import array
from functools import partial
from importlib import metadata
from pathlib import Path

# ---------------------------------------------------------------------------
# Флаги Chromium до импорта webview.
#
# ВАЖНО: на Windows pywebview использует WebView2 (Edge), а не Qt, поэтому
# QTWEBENGINE_CHROMIUM_FLAGS там не работал вообще. Для WebView2 нужен
# WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS.
#
# Убраны вредные флаги:
#   --in-process-gpu            — падение/зависание GPU вешало весь процесс;
#   --max-old-space-size=64     — при картинках в base64 куча упиралась в 64 МБ,
#                                 начинался бесконечный GC и «мёртвое» окно.
# ---------------------------------------------------------------------------
CHROMIUM_FLAGS = (
    '--disable-background-timer-throttling '
    '--disable-renderer-backgrounding '
    '--disable-backgrounding-occluded-windows '
    '--disable-features=CalculateNativeWinOcclusion,Translate,OptimizationHints,MediaRouter '
    '--renderer-process-limit=1 '
    '--js-flags=--max-old-space-size=128'
)
# На Linux QtWebEngine тянет GPU-процесс и видеопамять, хотя аватару нужны
# только CSS/PNG. Software-рендеринг стабильнее в VM/VMware/старых драйверах
# и экономит ~40 МБ PSS. При желании можно переопределить env-переменной.
QT_FLAGS = CHROMIUM_FLAGS + ' --disable-gpu --disable-gpu-compositing'
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', QT_FLAGS)
if sys.platform == 'win32':
    os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', CHROMIUM_FLAGS)

import webview  # noqa: E402

try:
    import sounddevice as sd
except Exception:
    # ImportError — нет sounddevice; OSError — нет PortAudio; PortAudioError —
    # на Linux нет доступного PulseAudio/ALSA-сервера (часто в CI/контейнере).
    # Приложение должно запускаться и без микрофона.
    sd = None

# Pillow используется как основной обработчик картинок. Это позволяет не
# тащить PyQt6/QtWebEngine на Windows и macOS, где pywebview работает через
# родной WebView2/WKWebView. Qt остаётся запасным вариантом.
try:
    from PIL import Image, ImageOps
    from PIL import UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = 40_000_000
    PIL_ERRORS = (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError)
except ImportError:
    Image = ImageOps = None
    PIL_ERRORS = (OSError, ValueError)

APP_NAME = 'PNGTuber'
APP_VERSION = '1.2.0'
SLOTS = ('idle', 'speaking', 'idle_blink', 'speaking_blink')
SCENE_FILTER = ('Сцена PNGTuber (*.pngtuber)', 'Все файлы (*.*)')
IMAGE_FILTER = ('Изображения (*.png;*.jpg;*.jpeg;*.gif;*.webp)', 'Все файлы (*.*)')

# Аватар живёт в окне 560x720 (в OBS — тоже размер окна), поэтому хранить
# исходные 3000x4000 px бессмысленно: это гигабайты декодированных битмапов.
# 1000 px достаточно для окна 560x720 и scale<=100%, но заметно меньше
# памяти, чем 1200: декодированный RGBA-битмап 1000x1000 ~4 МБ на состояние.
MAX_IMAGE_SIDE = 1000
THUMB_SIDE = 160                           # превью в панели, а не полный PNG
MAX_IMAGE_INPUT_BYTES = 100 * 1024 * 1024   # защита от «случайно выбрал 2 ГБ»
MAX_SCENE_BYTES = 150 * 1024 * 1024         # защита от zip-бомб и «сцен» на гигабайт
MAX_SCENE_CONFIG_BYTES = 1024 * 1024        # config.json не должен быть гигантским
SINGLE_INSTANCE_PORT = 47831
WEBVIEW2_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'

log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------------------
# Пути, логирование, мелкие утилиты
# ---------------------------------------------------------------------------

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


def ensure_config_dir():
    """Возвращает каталог, в который реально можно писать.

    Если APPDATA/XDG_CONFIG_HOME недоступен (чужой пользователь, read-only
    профиль, странная песочница), приложение не должно падать до создания
    окна. В этом случае уходим во временный каталог.
    """
    primary = get_config_dir()
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    except OSError:
        fallback = Path(tempfile.gettempdir()) / APP_NAME
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


_fault_file = None


def setup_logging(config_dir):
    """Лог в файл: без него «зависло намертво» невозможно разобрать."""
    global _fault_file
    config_dir.mkdir(parents=True, exist_ok=True)
    log_path = config_dir / 'pngtuber.log'

    fmt = logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s')
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
        file_handler.setFormatter(fmt)
        handlers = [file_handler]
    except OSError:
        handlers = []

    # В windowed-сборке PyInstaller sys.stderr может быть None. Тогда
    # StreamHandler бесполезен и только создаёт ошибки внутри logging.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        handlers.append(stream_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger('pywebview').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)

    # Падения в нативном коде (Qt/WebView2/PortAudio) тоже попадут в файл.
    try:
        _fault_file = open(config_dir / 'crash.log', 'a', encoding='utf-8')
        faulthandler.enable(file=_fault_file)
    except OSError:
        pass

    def excepthook(exc_type, exc, tb):
        log.critical('Необработанное исключение', exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        log.critical('Исключение в потоке %s', getattr(args.thread, 'name', '?'),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return log_path


def decode_data_url(data_url):
    """Байты из data:...;base64,... либо None, если строка пустая или битая."""
    if not data_url or ',' not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(',', 1)[1])
    except (ValueError, TypeError):
        return None


def encode_data_url(data, mime='image/png'):
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


def first_path(result):
    """create_file_dialog возвращает кортеж/список путей или None."""
    if not result:
        return None
    return result if isinstance(result, str) else result[0]


_write_locks = {}
_write_locks_guard = threading.Lock()


def _atomic_path_lock(path):
    """Один лок на путь: на Windows два os.replace одного файла дают WinError 5."""
    key = str(path.absolute())
    with _write_locks_guard:
        return _write_locks.setdefault(key, threading.Lock())


def atomic_write_bytes(path, data):
    """Пишем через уникальный временный файл.

    Раньше имя было фиксированным (`file.tmp`). Два потока, пишущих один и
    тот же слот одновременно, открывали один и тот же файл, один успевал
    сделать os.replace, второй получал ENOENT, а итоговая картинка могла
    оказаться не той, которую выбрал пользователь. Уникальный tmp + лок
    на слот (см. Api._store_image) решают и порядок, и целостность.

    На Windows os.replace иногда получает PermissionError от antivirus/
    индексатора, который ещё держит файл. Делаем несколько коротких попыток
    и сериализуем замены одного и того же пути.
    """
    with _atomic_path_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())

            last_error = None
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError as e:
                    # Windows + антивирус/индексатор: подождать и повторить.
                    last_error = e
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            if last_error is not None:
                raise last_error
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode('utf-8'))


# ---------------------------------------------------------------------------
# Картинки: нормализация, уменьшение, проверка
# ---------------------------------------------------------------------------

_qt_api = None
_QT_BINDINGS = ('qtpy', 'PyQt6', 'PySide6')


def qt_image_api():
    """Ленивый импорт Qt: нужен только для картинок, не для логики.

    Идём по списку биндингов, потому что в собранном PyInstaller-ом приложении
    qtpy может не оказаться (импорт внутри функции не виден анализатору),
    а PyQt6 там есть всегда.
    """
    global _qt_api
    if _qt_api is None:
        for binding in _QT_BINDINGS:
            try:
                core = importlib.import_module(f'{binding}.QtCore')
                gui = importlib.import_module(f'{binding}.QtGui')
                _qt_api = (core.QBuffer, core.QByteArray, core.QIODevice,
                           core.Qt, gui.QImage, gui.QImageReader)
                log.info('Обработка картинок через %s', binding)
                break
            except Exception as e:
                log.debug('%s недоступен: %s', binding, e)
        else:
            log.warning('PyQt/PySide не найден — картинки не будут уменьшаться')
            _qt_api = False
    return _qt_api or None


def _pil_normalize(data, max_side):
    """Pillow-путь: первый кадр, EXIF, уменьшение, RGBA PNG."""
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if im.mode != 'RGBA':
                im = im.convert('RGBA')
            out = io.BytesIO()
            im.save(out, format='PNG')
            return out.getvalue()
    except PIL_ERRORS as e:
        log.debug('Pillow не смог обработать картинку: %s', e)
        return None


def _qt_normalize(data, max_side):
    """Запасной путь через QImageReader (нужен только если нет Pillow)."""
    api = qt_image_api()
    if api is None:
        return None

    QBuffer, QByteArray, QIODevice, Qt, QImage, QImageReader = api

    buf = QBuffer()
    buf.setData(QByteArray(data))
    if not buf.open(QIODevice.OpenModeFlag.ReadOnly):
        return None

    reader = QImageReader(buf)
    reader.setAutoTransform(True)            # EXIF-поворот у jpeg
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        buf.close()
        return None

    if max(size.width(), size.height()) > max_side:
        scaled = size.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(scaled)

    image = reader.read()
    buf.close()
    if image.isNull():
        return None

    if image.format() != QImage.Format.Format_ARGB32:
        image = image.convertToFormat(QImage.Format.Format_ARGB32)

    out = QBuffer()
    out.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = image.save(out, 'PNG')
    out.close()
    return bytes(out.data()) if ok else None


def normalize_png(data, max_side=MAX_IMAGE_SIDE):
    """Декодирует картинку, ужимает до max_side и перекодирует в PNG.

    Основной путь — Pillow (Windows/macOS не тащат Qt), запасной — Qt.
    Возвращает bytes или None, если это не картинка / формат не поддержан.
    """
    if not data:
        return None

    if Image is not None:
        png = _pil_normalize(data, max_side)
        if png is not None:
            return png

    if qt_image_api() is not None:
        return _qt_normalize(data, max_side)

    # Без Pillow и Qt — хотя бы проверяем подпись PNG, чтобы не записать мусор.
    if Image is None:
        return data if data[:8] == b'\x89PNG\r\n\x1a\n' else None
    return None


def thumbnail_png(data, size=THUMB_SIDE):
    """Маленькое превью для панели.

    Без него браузер декодировал полный PNG дважды: для аватара и для
    миниатюры 76 px. На четырёх состояниях это лишние десятки мегабайт.
    """
    if Image is None or not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            if im.mode != 'RGBA':
                im = im.convert('RGBA')
            out = io.BytesIO()
            im.save(out, format='PNG')
            return out.getvalue()
    except PIL_ERRORS as e:
        log.debug('Не удалось сделать превью: %s', e)
        return None


def prepare_image_file(path):
    """Читает картинку слота; если она больше лимита — уменьшает и перезаписывает.

    Нужно и для миграции: у старых версий в %APPDATA% могли лежать PNG на
    3000–4000 px, которые при старте отдавались в JS как есть.
    """
    try:
        stat = path.stat()
        if stat.st_size <= 0:
            return None
        if stat.st_size > MAX_IMAGE_INPUT_BYTES:
            log.warning('Файл %s слишком большой (%d байт) — пропускаю', path, stat.st_size)
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None

    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
        except PIL_ERRORS:
            return None
    else:
        api = qt_image_api()
        if api is None:
            return data if data[:8] == b'\x89PNG\r\n\x1a\n' else None
        _, _, _, _, _, QImageReader = api
        reader = QImageReader(str(path))
        size = reader.size()
        if not reader.canRead() or not size.isValid():
            return None
        width, height = size.width(), size.height()

    if max(width, height) <= MAX_IMAGE_SIDE and data[:8] == b'\x89PNG\r\n\x1a\n':
        return data

    png = normalize_png(data)
    if png is None:
        return None
    try:
        atomic_write_bytes(path, png)
    except OSError as e:
        log.warning('Не удалось уменьшить %s: %s', path, e)
    return png


# ---------------------------------------------------------------------------
# Настройки: всегда полный и валидный словарь
# ---------------------------------------------------------------------------

_BOOL_DEFAULTS = {
    'enableAnim': True,
    'enableBlink': True,
    'flip': False,
    'onTop': False,
    'obsBackground': True,
}


def _number(raw, key, default, lo, hi, integer=False):
    value = raw.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    if value != value:                        # NaN
        value = default
    value = max(lo, min(hi, value))
    return int(round(value)) if integer else value


def sanitize_settings(raw):
    """Приводит настройки к известной схеме и безопасным диапазонам.

    Раньше битый/устаревший settings.json (или сцена) мог принести NaN,
    строку вместо числа или порог вне шкалы — и приложение вело себя странно.
    """
    if not isinstance(raw, dict):
        raw = {}

    # Обратная совместимость с единым threshold
    if 'threshold' in raw and 'thresholdOpen' not in raw:
        raw = dict(raw)
        raw['thresholdOpen'] = raw['threshold']
        try:
            raw['thresholdClose'] = max(1, float(raw['threshold']) - 6)
        except (TypeError, ValueError):
            pass

    settings = {
        'thresholdOpen': _number(raw, 'thresholdOpen', 18, 1, 90, integer=True),
        'thresholdClose': _number(raw, 'thresholdClose', 10, 1, 90, integer=True),
        'hold': _number(raw, 'hold', 150, 0, 600, integer=True),
        'attack': _number(raw, 'attack', 0, 0, 100, integer=True),
        'gain': _number(raw, 'gain', 100, 10, 500, integer=True),
        'scale': _number(raw, 'scale', 100, 30, 100, integer=True),
    }
    # Закрытие не может быть выше открытия — иначе шмитт-триггер ломается.
    settings['thresholdClose'] = min(settings['thresholdClose'], settings['thresholdOpen'])

    color = raw.get('bgColor')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        color = '#00ff00'
    settings['bgColor'] = color.lower()

    for key, default in _BOOL_DEFAULTS.items():
        value = raw.get(key, default)
        settings[key] = value if isinstance(value, bool) else default

    device = raw.get('deviceId')
    settings['deviceId'] = device[:200] if isinstance(device, str) else ''

    return settings


# ---------------------------------------------------------------------------
# Один экземпляр: второй запуск будит первый, а не плодит зомби-процессы
# ---------------------------------------------------------------------------

class SingleInstance:
    MAGIC = b'PNGTuber-wake'
    ACK = b'ok'

    def __init__(self):
        self.sock = None
        self.on_wake = None

    def request_exit(self):
        """True — приложение уже запущено (его попросили показаться), этот процесс выходит."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # SO_REUSEADDR намеренно не ставим: на Windows он разрешает двойной bind.
            sock.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
            sock.listen(4)
        except OSError:
            sock.close()
            return self._wake_existing()

        self.sock = sock
        threading.Thread(target=self._serve, name='instance-listener', daemon=True).start()
        return False

    @staticmethod
    def _recv_at_least(conn, size):
        data = b''
        while len(data) < size:
            chunk = conn.recv(max(32, size - len(data)))
            if not chunk:
                break
            data += chunk
        return data

    def _wake_existing(self):
        try:
            with socket.create_connection(('127.0.0.1', SINGLE_INSTANCE_PORT), timeout=1.5) as conn:
                conn.settimeout(1.5)
                conn.sendall(self.MAGIC)
                return self._recv_at_least(conn, len(self.ACK)) == self.ACK
        except OSError as e:
            log.warning('Порт %s занят, но это не наш экземпляр: %s', SINGLE_INSTANCE_PORT, e)
            return False

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    # TCP может разбить 13 байт на несколько сегментов; без
                    # таймаута один «зависший» клиент блокировал весь listener.
                    conn.settimeout(2.0)
                    if self._recv_at_least(conn, len(self.MAGIC)).startswith(self.MAGIC):
                        conn.sendall(self.ACK)     # сначала отвечаем, потом трогаем окно
                        if self.on_wake:
                            threading.Thread(target=self.on_wake, name='wake', daemon=True).start()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Микрофон
# ---------------------------------------------------------------------------

_audio_devices = {'time': 0.0, 'items': []}
_audio_devices_lock = threading.Lock()
AUDIO_DEVICES_TTL = 3.0


def input_devices(force=False):
    """Список (index, label) для устройств ввода.

    query_devices() на Windows через WASAPI может думать секунду-две, поэтому
    результат кэшируем. Показываем устройства ВСЕХ host API, а не только
    default: иначе на Windows не видно WASAPI/ASIO-микрофонов, а на Linux —
    части PipeWire/PulseAudio-устройств. Одинаковые имена различаем именем
    host API в квадратных скобках.
    """
    if sd is None:
        return []
    with _audio_devices_lock:
        now = time.monotonic()
        if not force and now - _audio_devices['time'] < AUDIO_DEVICES_TTL:
            return _audio_devices['items']
        try:
            hostapis = sd.query_hostapis()
            default_hostapi = sd.default.hostapi
            raw = []
            for index, device in enumerate(sd.query_devices()):
                if device['max_input_channels'] <= 0:
                    continue
                host_index = device['hostapi']
                host_name = hostapis[host_index]['name'] if host_index < len(hostapis) else '?'
                raw.append((index, device['name'], host_name, host_index))

            name_counts = {}
            for _, name, _, _ in raw:
                name_counts[name] = name_counts.get(name, 0) + 1

            entries = []
            for index, name, host_name, host_index in raw:
                label = name if name_counts[name] == 1 else f'{name} [{host_name}]'
                entries.append((index, label, host_index))

            entries.sort(key=lambda item: (item[2] != default_hostapi, item[1].lower()))
            items = [(index, label) for index, label, _ in entries]
        except Exception as e:
            log.warning('Не удалось перечислить микрофоны: %s', e)
            items = []
        _audio_devices.update(time=now, items=items)
        return items


class Microphone:
    """Захват звука через PortAudio с двухпороговым регистром и квантованием 10 мс.

    Обновления в JS идут через отдельный поток-отправитель: если WebView2
    подвиснет на evaluate_js, аудиологика продолжит считать состояние и
    отправит только самое свежее значение, а не встанет вместе с мостом.
    """

    TICK = 0.01              # шаг расчёта уровня 10 мс (100 Гц)
    METER_EVERY = 6          # шкалу шлём ~16 раз в секунду (было 33 — много для моста)
    LEVEL_STEP = 2           # изменения меньше 2% не отправляем

    def __init__(self, window_getter):
        self.window_getter = window_getter
        self.stream = None
        self.raw = 0.0
        self.threshold_open = 18.0
        self.threshold_close = 10.0
        self.hold = 0.15
        self.attack = 0.0
        self.gain = 1.0
        self.meter = False
        self.lock = threading.RLock()

        self._pending = None
        self._pending_lock = threading.Lock()
        self._wake = threading.Event()
        threading.Thread(target=self._sender_loop, name='mic-js', daemon=True).start()

    @staticmethod
    def device_names(force=False):
        return [name for _, name in input_devices(force=force)]

    @staticmethod
    def device_index(name):
        if not name:
            return None
        devices = input_devices()
        for index, device_name in devices:
            if device_name == name:
                return index
        # Совместимость со старыми настройками, где хранилось только имя без
        # уточнения host API: "Микрофон" -> "Микрофон [WASAPI]".
        base = name.split(' [', 1)[0]
        for index, device_name in devices:
            if device_name.split(' [', 1)[0] == base:
                return index
        return None

    # ---- мост в JS ----

    def _emit(self, code):
        with self._pending_lock:
            self._pending = code
        self._wake.set()

    def _sender_loop(self):
        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            with self._pending_lock:
                code, self._pending = self._pending, None
            if not code:
                continue
            win = self.window_getter()
            if not win:
                continue
            try:
                win.evaluate_js(code)
            except Exception:
                # окно закрывается/перезагружается — не повод падать
                pass

    def _callback(self, indata, frames, time_info, status, channels=1):
        # Внутри аудио-коллбэка вычисляем только амплитуду. RawInputStream
        # отдаёт bytes, поэтому NumPy не нужен: меньше памяти и размер сборки.
        try:
            values = array('f')
            values.frombytes(bytes(indata))
            if channels > 1:
                values = values[0::channels]
            count = len(values)
            if not count:
                return
            mean = sum(values) / count
            variance = sum((v - mean) * (v - mean) for v in values) / count
            rms = math.sqrt(max(0.0, variance))
            self.raw = min(100.0, rms * self.gain * 400.0)
        except Exception:
            self.raw = 0.0

    def _loop(self, stream):
        level = 0.0
        last_loud = 0.0
        first_loud = 0.0
        speaking = False
        sent_width = -1
        tick = 0
        while self.stream is stream:
            raw = self.raw
            level += (raw - level) * (0.6 if raw > level else 0.2)   # быстрая атака, плавный спад
            now = time.monotonic()

            t_open = self.threshold_open
            t_close = min(self.threshold_close, t_open)

            if speaking:
                if level >= t_close:
                    last_loud = now
                now_speaking = (level >= t_close) or (now - last_loud < self.hold)
                first_loud = 0.0
            else:
                if level >= t_open:
                    if first_loud == 0.0:
                        first_loud = now
                    if now - first_loud >= self.attack:
                        now_speaking = True
                        last_loud = now
                    else:
                        now_speaking = False
                else:
                    first_loud = 0.0
                    now_speaking = False

            tick += 1
            # Шкала 1:1 с ползунком: threshold=40 действительно показывает 40%.
            # Раньше множитель 1.5 упирал маркеры >66% в правый край.
            width = round(min(100, level))

            state_changed = now_speaking != speaking
            meter_due = self.meter and tick % self.METER_EVERY == 0
            width_changed = abs(width - sent_width) >= self.LEVEL_STEP

            if state_changed:
                speaking = now_speaking
                # новое состояние шлём сразу, вместе со свежей шкалой
                self._emit(f'window.__micTick&&window.__micTick({"true" if speaking else "false"},{width})')
                sent_width = width
            elif meter_due and width_changed:
                self._emit(f'window.__micTick&&window.__micTick({"true" if speaking else "false"},{width})')
                sent_width = width

            time.sleep(self.TICK)

        self._emit('window.__micTick&&window.__micTick(false,0)')

    def set_audio_params(self, threshold_open=18, threshold_close=10, hold_ms=150, attack_ms=0, gain_pct=100):
        t_open = max(1.0, min(100.0, float(threshold_open)))
        self.threshold_open = t_open
        self.threshold_close = max(1.0, min(t_open, float(threshold_close)))
        self.hold = max(0.0, min(2.0, float(hold_ms) / 1000.0))
        self.attack = max(0.0, min(1.0, float(attack_ms) / 1000.0))
        self.gain = max(0.1, min(5.0, float(gain_pct) / 100.0))

    def start(self, name):
        if sd is None:
            raise RuntimeError('не найдена библиотека PortAudio (sounddevice)')
        with self.lock:
            self.stop()
            index = self.device_index(name) if name else None
            if name and index is None:
                # Микрофон могли отключить/переименовать: лучше стартовать с
                # устройством по умолчанию, чем остаться совсем без звука.
                log.warning('Микрофон «%s» не найден — беру устройство по умолчанию', name)
            # Некоторые устройства (особенно виртуальные ASIO/PipeWire)
            # отказываются открываться в mono. Пробуем 1 канал, затем 2.
            last_error = None
            stream = None
            for channels in (1, 2):
                try:
                    stream = sd.RawInputStream(
                        device=index, channels=channels, dtype='float32',
                        blocksize=256, callback=partial(self._callback, channels=channels))
                    stream.start()
                    break
                except Exception as e:
                    last_error = e
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    stream = None
            if stream is None:
                raise last_error or RuntimeError('не удалось открыть микрофон')
            self.stream = stream
            self.raw = 0.0
            threading.Thread(target=self._loop, args=(stream,), name='mic-loop', daemon=True).start()

    def stop(self):
        with self.lock:
            stream, self.stream = self.stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._emit('window.__micTick&&window.__micTick(false,0)')


# ---------------------------------------------------------------------------
# JS API
# ---------------------------------------------------------------------------

class Api:
    """Методы, доступные из JavaScript как window.pywebview.api.*"""

    def __init__(self, options, log_file=None, config_dir=None):
        self.window = None
        self.options = options
        self.obs_background = True
        self.is_parked = False
        self.last_geometry = None
        self._parking = False
        self._closed = False

        self.config_dir = Path(config_dir) if config_dir else ensure_config_dir()
        self.settings_file = self.config_dir / 'settings.json'
        self.images_dir = self.config_dir / 'images'
        self.log_file = Path(log_file) if log_file else self.config_dir / 'pngtuber.log'
        self.mic = Microphone(lambda: self.window)

        # Один слот — один писатель. RLock, потому что set_image() очищает слот
        # и вызывает _store_image() под тем же локом.
        self._image_locks = {slot: threading.RLock() for slot in SLOTS}
        self._settings_lock = threading.Lock()
        self._scene_lock = threading.Lock()
        self._audio_lock = threading.Lock()
        self._audio_generation = 0

    # ---- Состояние между запусками ----

    def load_settings(self):
        try:
            raw = json.loads(self.settings_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as e:
            log.warning('settings.json повреждён (%s) — беру значения по умолчанию', e)
            try:
                self.settings_file.replace(self.settings_file.with_name('settings.json.bad'))
            except OSError:
                pass
            raw = {}
        return sanitize_settings(raw)

    def get_state(self):
        images = {}
        thumbs = {}
        for slot in SLOTS:
            path = self.images_dir / f'{slot}.png'
            if not path.exists():
                continue
            data = prepare_image_file(path)
            if not data:
                # Не удаляем файл: он может быть валидным, но слишком большим
                # или просто нечитаемым из-за временной ошибки ФС. Пользователь
                # сможет заменить его через панель.
                log.warning('Файл слота %s не прочитан — пропускаю', slot)
                continue
            images[slot] = encode_data_url(data)
            thumb = thumbnail_png(data)
            if thumb:
                thumbs[slot] = encode_data_url(thumb)

        return {'settings': self.load_settings(), 'images': images, 'thumbs': thumbs,
                'options': self.options, 'logFile': str(self.log_file)}

    def save_settings(self, settings_json):
        try:
            raw = json.loads(settings_json)
        except (ValueError, TypeError):
            log.warning('Отказался сохранять некорректные настройки')
            return False
        if not isinstance(raw, dict):
            return False
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            with self._settings_lock:
                atomic_write_text(self.settings_file,
                                  json.dumps(sanitize_settings(raw), ensure_ascii=False))
            return True
        except OSError as e:
            log.warning('Не удалось сохранить настройки: %s', e)
            return False

    # ---- Картинки ----

    def _store_image(self, slot, data):
        """Общая запись: нормализует картинку и атомарно кладёт в конфиг.

        Лок на слот сериализует быстрые перетаскивания одной и той же картинки.
        Без него два потока pywebview могли писать один слот одновременно, и
        на диске оказывалась не последняя выбранная картинка.
        """
        with self._image_locks[slot]:
            png = normalize_png(data)
            if png is None:
                return {'ok': False, 'error': 'это не картинка или формат не поддерживается'}
            try:
                self.images_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(self.images_dir / f'{slot}.png', png)
            except OSError as e:
                log.warning('Не удалось сохранить изображение %s: %s', slot, e)
                return {'ok': False, 'error': 'не удалось записать файл'}
            thumb = thumbnail_png(png)
            return {
                'ok': True,
                'url': encode_data_url(png),
                'thumb': encode_data_url(thumb) if thumb else '',
                'bytes': len(png),
            }

    def set_image(self, slot, data_url):
        """Сохраняет картинку слота; пустая строка возвращает слот к значению по умолчанию."""
        if slot not in SLOTS:
            return {'ok': False, 'error': 'неизвестный слот'}
        path = self.images_dir / f'{slot}.png'
        if isinstance(data_url, str) and len(data_url) > MAX_IMAGE_INPUT_BYTES * 4 // 3 + 1024:
            return {'ok': False, 'error': 'слишком большая картинка'}
        data = decode_data_url(data_url) if data_url else None
        if data and len(data) > MAX_IMAGE_INPUT_BYTES:
            return {'ok': False, 'error': 'слишком большая картинка'}
        with self._image_locks[slot]:
            if not data:
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    log.warning('Не удалось удалить изображение %s: %s', slot, e)
                return {'ok': True, 'url': '', 'thumb': ''}
            return self._store_image(slot, data)

    def pick_image(self, slot=None):
        """Нативный диалог выбора картинки; сразу сохраняет и возвращает готовый data URL."""
        if slot is not None and slot not in SLOTS:
            return {'ok': False, 'error': 'неизвестный слот'}
        dialog_type = getattr(webview, 'FileDialog', None)
        open_dlg = dialog_type.OPEN if dialog_type else webview.OPEN_DIALOG
        path = first_path(self.window.create_file_dialog(open_dlg, file_types=IMAGE_FILTER))
        if not path:
            return None
        try:
            size = os.path.getsize(path)
            if size > MAX_IMAGE_INPUT_BYTES:
                return {'ok': False, 'error': 'файл слишком большой (больше 100 МБ)'}
            data = Path(path).read_bytes()
        except OSError as e:
            log.warning('Не удалось прочитать изображение: %s', e)
            return {'ok': False, 'error': 'не удалось прочитать файл'}
        if slot is None:
            png = normalize_png(data)
            thumb = thumbnail_png(png) if png else None
            return {'ok': bool(png), 'url': encode_data_url(png) if png else '',
                    'thumb': encode_data_url(thumb) if thumb else ''}
        return self._store_image(slot, data)

    # ---- Управление окном (OBS & Always On Top) ----

    def set_always_on_top(self, on_top):
        if self.window:
            try:
                self.window.on_top = bool(on_top)
                return True
            except Exception as e:
                log.warning('Не удалось переключить on_top: %s', e)
        return False

    def set_obs_background(self, enabled):
        self.obs_background = bool(enabled)
        return True

    @staticmethod
    def _valid_geometry(x, y):
        # -20000 — служебная координата парковки, её нельзя запоминать.
        return x is not None and y is not None and x > -10000 and y > -10000

    def _window_geometry(self):
        if not self.window:
            return None
        try:
            x, y = int(self.window.x), int(self.window.y)
            return (x, y) if self._valid_geometry(x, y) else None
        except Exception:
            return None

    def remember_geometry(self, x=None, y=None):
        """Запоминает позицию окна.

        Событие moved в pywebview передаёт x/y аргументами. Читать window.x/y
        из фонового потока обработчика нельзя: Qt-виджет опрашивается не из
        GUI-потока. Если аргументов нет — используем безопасный fallback.
        """
        if self._parking or self.is_parked or not self.window:
            return
        if x is None or y is None:
            geometry = self._window_geometry()
            if geometry is None:
                return
            x, y = geometry
        if self._valid_geometry(x, y):
            self.last_geometry = (int(x), int(y))

    def park_window(self):
        """Уводит окно за пределы экрана, не минимизируя (иначе OBS теряет картинку)."""
        if not self.window:
            return False
        if self.is_parked:
            return True
        if sys.platform.startswith('linux') and \
                os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland':
            log.warning('Wayland не разрешает клиенту двигать окно: парковка может не сработать')
        try:
            if self.last_geometry is None:
                geometry = self._window_geometry()
                if geometry:
                    self.last_geometry = geometry
            self._parking = True
            self.is_parked = True
            self.window.move(-20000, -20000)
            return True
        except Exception as e:
            self.is_parked = False
            log.warning('Не удалось припарковать окно: %s', e)
            return False
        finally:
            self._parking = False

    def park_window_after_minimize(self):
        """Вызывается по событию minimized: возвращаем окно в Normal и уводим за экран.

        Важно: здесь нельзя вызывать restore_window() из события restored.
        park_window_after_minimize сам вызывает window.restore(), и обработчик
        restored сразу возвращал окно обратно на экран. Поэтому подписка на
        restored в main() удалена, а состояние парковки живёт до wake/restore.
        """
        if not self.window or self.is_parked:
            return
        if sys.platform.startswith('linux') and \
                os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland':
            log.warning('Wayland не разрешает клиенту двигать окно: парковка может не сработать')
        try:
            if self.last_geometry is None:
                geometry = self._window_geometry()
                if geometry:
                    self.last_geometry = geometry
            self._parking = True
            self.is_parked = True
            self.window.restore()          # из Minimized, иначе move не сработает
            self.window.move(-20000, -20000)
        except Exception as e:
            self.is_parked = False
            log.warning('Не удалось обработать сворачивание: %s', e)
        finally:
            self._parking = False

    def restore_window(self):
        """Возвращает припаркованное окно обратно на экран."""
        if not self.window or not self.is_parked or self._parking:
            return False
        try:
            self._parking = True
            x, y = self.last_geometry or (100, 100)
            self.window.move(x, y)
            self.window.restore()
            self.window.show()
            self.is_parked = False
            return True
        except Exception as e:
            # Окно могло остаться за экраном: обязательно сохраняем состояние
            # «припарковано», иначе пользователь потеряет его до перезапуска.
            self.is_parked = True
            log.warning('Не удалось восстановить окно: %s', e)
            return False
        finally:
            self._parking = False

    def wake(self):
        """Второй запуск приложения: показываем и поднимаем уже работающее окно."""
        try:
            self.restore_window()
            self.window.show()
            self.window.restore()
        except Exception as e:
            log.warning('Не удалось разбудить окно: %s', e)

    # ---- Микрофон ----

    def get_audio_devices(self, force=False):
        try:
            return {'available': sd is not None,
                    'devices': Microphone.device_names(force=bool(force))}
        except Exception as e:
            log.warning('Не удалось получить список микрофонов: %s', e)
            return {'available': False, 'devices': []}

    def start_audio(self, device_name):
        with self._audio_lock:
            self._audio_generation += 1
            generation = self._audio_generation
        try:
            self.mic.start(device_name)
            with self._audio_lock:
                stale = generation != self._audio_generation
            if stale:
                # Пока PortAudio открывался, пользователь нажал «Выключить»
                # или сработал таймаут в JS. Не оставляем микрофон включённым.
                self.mic.stop()
                return {'ok': False, 'error': 'запуск отменён'}
            log.info('Микрофон запущен: %s', device_name or 'по умолчанию')
            return {'ok': True}
        except Exception as e:
            log.warning('Не удалось открыть микрофон: %s', e)
            return {'ok': False, 'error': str(e)}

    def stop_audio(self):
        with self._audio_lock:
            self._audio_generation += 1
        self.mic.stop()
        return {'ok': True}

    def log_client_error(self, message, stack=''):
        """JS-ошибки в тот же лог: в упакованном приложении консоли нет."""
        try:
            text = str(message)[:4000]
            trace = str(stack or '')[:8000]
            log.error('JS: %s%s', text, ('\n' + trace) if trace else '')
        except Exception:
            pass
        return True

    def set_audio_params(self, threshold_open=18, threshold_close=10, hold_ms=150, attack_ms=0, gain_pct=100):
        self.mic.set_audio_params(threshold_open, threshold_close, hold_ms, attack_ms, gain_pct)

    def set_meter(self, on):
        self.mic.meter = bool(on)

    # ---- Сцены (.pngtuber) ----

    def save_scene(self, config_json, idle=None, speaking=None, idle_blink=None, speaking_blink=None):
        if isinstance(config_json, str) and len(config_json) > MAX_SCENE_CONFIG_BYTES:
            return {'ok': False, 'error': 'настройки сцены слишком большие'}
        try:
            config = json.loads(config_json)
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'настройки сцены повреждены'}
        if not isinstance(config, dict):
            return {'ok': False, 'error': 'настройки сцены повреждены'}

        dialog_type = getattr(webview, 'FileDialog', None)
        save_dlg = dialog_type.SAVE if dialog_type else webview.SAVE_DIALOG
        path = first_path(self.window.create_file_dialog(
            save_dlg, save_filename='model.pngtuber', file_types=SCENE_FILTER))
        if not path:
            return None
        if not path.endswith('.pngtuber'):
            path += '.pngtuber'

        # Картинки берём с диска: не гоняем мегабайты base64 через мост.
        images = {}
        for slot in SLOTS:
            try:
                images[slot] = (self.images_dir / f'{slot}.png').read_bytes()
            except OSError:
                pass

        # Страховка для старых сценариев: если на диске пусто — берём data URL от JS.
        if not images:
            for slot, data_url in zip(SLOTS, (idle, speaking, idle_blink, speaking_blink)):
                data = decode_data_url(data_url)
                png = normalize_png(data) if data else None
                if png:
                    images[slot] = png

        directory = Path(path)
        tmp = None
        try:
            with self._scene_lock:
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f'.{directory.name}.', suffix='.part', dir=str(directory.parent))
                os.close(fd)
                tmp = Path(tmp_name)
                with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                    zf.writestr('config.json', json.dumps(config, ensure_ascii=False, indent=2),
                                compress_type=zipfile.ZIP_DEFLATED)
                    for name, data in images.items():
                        # PNG уже сжат: deflate только тратит CPU.
                        zf.writestr(f'{name}.png', data,
                                    compress_type=zipfile.ZIP_STORED)
                os.replace(tmp, directory)
                try:
                    size_kb = directory.stat().st_size // 1024
                except OSError:
                    size_kb = 0
            log.info('Сцена сохранена: %s (%d слота, %d КБ)',
                     directory, len(images), size_kb)
            return {'ok': True, 'path': str(directory)}
        except (OSError, RuntimeError) as e:
            log.warning('Не удалось сохранить сцену: %s', e)
            return {'ok': False, 'error': 'не удалось записать файл'}
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def load_scene(self):
        dialog_type = getattr(webview, 'FileDialog', None)
        open_dlg = dialog_type.OPEN if dialog_type else webview.OPEN_DIALOG
        path = first_path(self.window.create_file_dialog(open_dlg, file_types=SCENE_FILTER))
        if not path:
            return None

        try:
            with self._scene_lock:
                if os.path.getsize(path) > MAX_SCENE_BYTES:
                    return {'ok': False, 'error': 'файл сцены слишком большой'}
                with zipfile.ZipFile(path) as zf:
                    if sum(info.file_size for info in zf.infolist()) > MAX_SCENE_BYTES:
                        return {'ok': False, 'error': 'сцена слишком большая'}

                    config = {}
                    try:
                        info = zf.getinfo('config.json')
                        if info.file_size > MAX_SCENE_CONFIG_BYTES:
                            return {'ok': False, 'error': 'config.json слишком большой'}
                        config = json.loads(zf.read('config.json').decode('utf-8'))
                    except KeyError:
                        log.warning('В сцене нет config.json')
                    except (ValueError, UnicodeDecodeError):
                        log.warning('config.json в сцене повреждён')

                    images = {}
                    thumbs = {}
                    for slot in SLOTS:
                        try:
                            info = zf.getinfo(f'{slot}.png')
                        except KeyError:
                            continue
                        if info.file_size > MAX_IMAGE_INPUT_BYTES:
                            log.warning('Картинка слота %s в сцене слишком большая', slot)
                            continue
                        try:
                            raw = zf.read(f'{slot}.png')
                        except KeyError:
                            continue
                        png = normalize_png(raw)
                        if png is None:
                            log.warning('В сцене битая картинка слота %s', slot)
                            continue
                        # Сразу кладём на диск: JS не придётся гонять это обратно.
                        try:
                            with self._image_locks[slot]:
                                self.images_dir.mkdir(parents=True, exist_ok=True)
                                atomic_write_bytes(self.images_dir / f'{slot}.png', png)
                        except OSError as e:
                            log.warning('Не удалось сохранить картинку сцены %s: %s', slot, e)
                        images[slot] = encode_data_url(png)
                        thumb = thumbnail_png(png)
                        if thumb:
                            thumbs[slot] = encode_data_url(thumb)

                log.info('Сцена загружена: %s (%d слота)', path, len(images))
                return {'ok': True, 'config': config if isinstance(config, dict) else {},
                        'images': images, 'thumbs': thumbs}
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as e:
            log.warning('Не удалось загрузить сцену: %s', e)
            return {'ok': False, 'error': 'файл сцены повреждён'}

    # ---- Завершение ----

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.mic.stop()
        except Exception:
            log.exception('Ошибка при остановке микрофона')
        log.info('PNGTuber завершён')


def parse_args():
    parser = argparse.ArgumentParser(description='Лёгкий PNGTuber')
    parser.add_argument('--transparent', action='store_true',
                        help='прозрачное окно вместо хромакей-фона (на части систем Linux даёт артефакты)')
    return parser.parse_args()


def on_window_minimized(api):
    """Пользователь нажал «свернуть»: при включённом obs_background паркуем окно
    за экран, чтобы OBS продолжал получать кадры, а ОС не уничтожила буфер."""
    if not api or not api.obs_background:
        return
    api.park_window_after_minimize()


def on_window_closing(api):
    # Watchdog запускаем ДО остановки PortAudio: если драйвер зависнет в
    # stream.stop()/close(), таймер всё равно завершит процесс.
    timer = threading.Timer(5.0, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    api.shutdown()


def window_error_hint(error):
    """Понятная подсказка вместо сырого исключения pywebview."""
    text = str(error)
    lowered = text.lower()
    if sys.platform == 'win32' and 'webview2' in lowered:
        return (f'Не найден Microsoft Edge WebView2 Runtime.\n'
                f'Установите его: {WEBVIEW2_URL}\n\n{text}')
    if sys.platform == 'darwin' and 'pyobjc' in lowered:
        return ('Не удалось загрузить Cocoa-бэкенд pywebview (PyObjC).\n'
                f'Проверьте установку зависимостей.\n\n{text}')
    return text


def fatal_message(text):
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
        else:
            print(text, file=sys.stderr)
    except Exception:
        pass


def main():
    args = parse_args()
    config_dir = ensure_config_dir()
    log_path = setup_logging(config_dir)
    try:
        webview_version = metadata.version('pywebview')
    except metadata.PackageNotFoundError:
        webview_version = '?'
    log.info('Старт %s %s, Python %s, pywebview %s, platform %s',
             APP_NAME, APP_VERSION, sys.version.split()[0], webview_version, sys.platform)
    log.info('Каталог настроек: %s, лог: %s', config_dir, log_path)
    if config_dir != get_config_dir():
        log.warning('Основной каталог настроек недоступен — использую %s', config_dir)

    instance = SingleInstance()
    if instance.request_exit():
        log.info('PNGTuber уже запущен — разбудили существующее окно')
        return 0

    api = Api({'transparent': args.transparent}, log_path, config_dir=config_dir)
    instance.on_wake = api.wake

    try:
        window = webview.create_window(
            APP_NAME,
            url=str(get_web_dir() / 'index.html'),
            js_api=api,
            width=560,
            height=720,
            min_size=(360, 480),
            transparent=args.transparent,
        )
    except Exception as e:
        log.exception('Не удалось создать окно')
        fatal_message(f'Не удалось создать окно: {window_error_hint(e)}')
        return 1

    api.window = window
    # events.moved передаёт x/y аргументами — не читаем QWidget из чужого потока.
    window.events.moved += lambda x, y: api.remember_geometry(x, y)
    window.events.minimized += lambda: on_window_minimized(api)
    # ВАЖНО: restored здесь намеренно не подписан. park_window_after_minimize
    # сам вызывает restore(), и обработчик restored сразу возвращал окно на
    # экран, полностью ломая фоновый режим OBS.
    window.events.closing += lambda: on_window_closing(api)

    gui = 'qt' if sys.platform.startswith('linux') else None
    try:
        webview.start(gui=gui, debug=False)
    except Exception as e:
        log.exception('Не удалось запустить окно')
        fatal_message(f'Не удалось запустить окно: {window_error_hint(e)}')
        return 1
    finally:
        api.shutdown()
    return 0


if __name__ == '__main__':
    code = main()
    logging.shutdown()
    # Не даём фоновым потокам PortAudio/WebView2 удержать процесс-зомби.
    os._exit(code)
