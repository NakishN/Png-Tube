import base64
import io
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


def png_data_url(width=300, height=200, color=(255, 0, 0, 255)):
    pytest.importorskip('PIL')
    from PIL import Image

    image = Image.new('RGBA', (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


class FakeWindow:
    def __init__(self, path):
        self.path = path
        self.x = 100
        self.y = 100

    def create_file_dialog(self, *args, **kwargs):
        return (str(self.path),)


def test_sanitize_settings_handles_garbage():
    settings = main.sanitize_settings({
        'threshold': '40',
        'gain': 5,
        'scale': '999',
        'bgColor': 'not-a-color',
        'enableAnim': 'yes',
        'deviceId': 'x' * 300,
    })
    assert settings['thresholdOpen'] == 40
    assert settings['thresholdClose'] == 34
    assert settings['gain'] == 10
    assert settings['scale'] == 100
    assert settings['bgColor'] == '#00ff00'
    assert settings['enableAnim'] is True
    assert len(settings['deviceId']) == 200


def test_sanitize_settings_close_never_above_open():
    settings = main.sanitize_settings({'thresholdOpen': 20, 'thresholdClose': 90})
    assert settings['thresholdClose'] <= settings['thresholdOpen']


def test_atomic_write_concurrent_unique_files(tmp_path):
    target = tmp_path / 'image.png'
    payloads = [bytes([i]) * 4096 for i in range(8)]
    errors = []

    def writer(payload):
        try:
            main.atomic_write_bytes(target, payload)
        except Exception as exc:  # pragma: no cover - диагностика при падении
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert target.read_bytes() in payloads
    assert not list(tmp_path.glob('*.tmp'))
    assert not list(tmp_path.glob('.*.tmp'))


def test_normalize_png_uses_pillow():
    pytest.importorskip('PIL')
    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (2400, 1200), (0, 0, 255)).save(buffer, format='JPEG')
    normalized = main.normalize_png(buffer.getvalue())
    assert normalized
    result = Image.open(io.BytesIO(normalized))
    assert result.format == 'PNG'
    assert max(result.size) <= main.MAX_IMAGE_SIDE


def test_scene_roundtrip(tmp_path):
    api = main.Api({'transparent': False}, config_dir=tmp_path)
    api.window = FakeWindow(tmp_path / 'scene.pngtuber')
    try:
        result = api.set_image('idle', png_data_url())
        assert result['ok'] is True, result

        saved = api.save_scene(json.dumps({
            'thresholdOpen': 22,
            'thresholdClose': 11,
            'hold': 120,
            'attack': 0,
            'gain': 130,
            'bgColor': '#00ff00',
            'scale': 90,
            'enableAnim': True,
            'enableBlink': False,
            'flip': True,
            'onTop': True,
            'obsBackground': False,
        }))
        assert saved['ok'] is True, saved
        assert (tmp_path / 'scene.pngtuber').exists()
        (tmp_path / 'images' / 'idle.png').unlink()

        loaded = api.load_scene()
        assert loaded['ok'] is True, loaded
        assert loaded['config']['gain'] == 130
        assert loaded['config']['onTop'] is True
        assert 'idle' in loaded['images']
        assert (tmp_path / 'images' / 'idle.png').read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'
    finally:
        api.shutdown()


def test_concurrent_set_image_same_slot_is_not_corrupted(tmp_path):
    api = main.Api({'transparent': False}, config_dir=tmp_path)
    api.window = FakeWindow(tmp_path / 'scene.pngtuber')
    try:
        urls = [png_data_url(600, 400, color=(255, 0, 0, 255)),
                png_data_url(600, 400, color=(0, 255, 0, 255))]
        results = []
        threads = [
            threading.Thread(target=lambda url=url: results.append(api.set_image('idle', url)))
            for url in urls
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 2
        assert all(result['ok'] for result in results), results
        data = (tmp_path / 'images' / 'idle.png').read_bytes()
        assert main.normalize_png(data) is not None
        assert data[:8] == b'\x89PNG\r\n\x1a\n'
        assert not list((tmp_path / 'images').glob('*.tmp'))
    finally:
        api.shutdown()
