"""Замер утечек памяти PNGTuber.

Два независимых профиля:

  core — Python: Api.set_image/set_image(''), settings, scene save/load,
         get_state. Показывает, растёт ли RSS/PSS процесса при повторных
         операциях и не остаются ли временные файлы/потоки.

  web  — Chromium (Playwright): повторная загрузка/очистка картинки,
         переключение вкладок/панели, __micTick. Меряет JS heap после
         принудительного GC и RSS всего дерева процессов браузера.

Запуск:
    python tests/memory_probe.py --mode all --cycles 200 --json report.json

На Windows/macOS PSS недоступен, используется RSS. Скрипт не открывает
реальное окно pywebview и не трогает пользовательский конфиг — только temp.
"""
from __future__ import annotations

import argparse
import base64
import gc
import io
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _rss_bytes():
    if psutil is None:
        return None
    return psutil.Process().memory_info().rss


def _pss_bytes():
    """PSS только Linux; на остальных None."""
    path = Path('/proc/self/smaps_rollup')
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            if line.startswith('Pss:'):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _proc_stats():
    return {
        'rss': _rss_bytes(),
        'pss': _pss_bytes(),
        'threads': __import__('threading').active_count(),
        'fds': len(os.listdir('/proc/self/fd')) if Path('/proc/self/fd').exists() else None,
    }


def _slope_per_cycle(samples):
    if len(samples) < 2:
        return 0.0
    xs = list(range(len(samples)))
    ys = [float(v) for v in samples]
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom


def _summary(samples, label):
    if not samples:
        return {}
    first, last = samples[0], samples[-1]
    return {
        'label': label,
        'samples': len(samples),
        'first_kb': round(first / 1024, 1),
        'last_kb': round(last / 1024, 1),
        'peak_kb': round(max(samples) / 1024, 1),
        'growth_kb': round((last - first) / 1024, 1),
        'slope_kb_per_sample': round(_slope_per_cycle(samples) / 1024, 3),
    }


class FakeWindow:
    def __init__(self, path):
        self.path = path
        self.x = 100
        self.y = 100

    def create_file_dialog(self, *args, **kwargs):
        return (str(self.path),)


def _make_png_data_url(size, color):
    from PIL import Image

    image = Image.new('RGBA', (size, size), color)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def probe_core(cycles: int):
    import main

    config_dir = Path(tempfile.mkdtemp(prefix='pngtuber-mem-core-'))
    scene_path = config_dir / 'probe.pngtuber'
    api = main.Api({'transparent': False}, config_dir=config_dir)
    api.window = FakeWindow(scene_path)

    urls = [
        _make_png_data_url(256, (255, 0, 0, 255)),
        _make_png_data_url(512, (0, 255, 0, 255)),
        _make_png_data_url(768, (0, 0, 255, 255)),
        _make_png_data_url(1200, (255, 255, 0, 255)),
    ]
    settings = json.dumps({
        'thresholdOpen': 20, 'thresholdClose': 12, 'hold': 150,
        'attack': 0, 'gain': 100, 'scale': 100,
        'bgColor': '#00ff00', 'enableAnim': True, 'enableBlink': True,
        'flip': False, 'onTop': False, 'obsBackground': True, 'deviceId': '',
    })

    def work(i):
        url = urls[i % len(urls)]
        api.set_image('idle', url)
        if i % 3 == 0:
            api.set_image('idle', '')
        if i % 10 == 0:
            api.get_state()
            api.save_settings(settings)
        if i % 25 == 0:
            api.save_scene(settings)
            api.load_scene()

    # Прогрев: импорты Qt/Pillow, первичные аллокации, дисковый кэш.
    for i in range(min(20, max(4, cycles // 5))):
        work(i)
    gc.collect()

    base = _proc_stats()
    rss_samples = []
    pss_samples = []
    started = time.perf_counter()
    for i in range(cycles):
        work(i)
        if i % 5 == 0:
            gc.collect()
            stats = _proc_stats()
            if stats['rss'] is not None:
                rss_samples.append(stats['rss'])
            if stats['pss'] is not None:
                pss_samples.append(stats['pss'])

    gc.collect()
    final = _proc_stats()
    elapsed = time.perf_counter() - started
    temp_files = [str(p.name) for p in config_dir.rglob('*.tmp')]
    api.shutdown()

    return {
        'mode': 'core',
        'platform': platform.platform(),
        'python': sys.version,
        'cycles': cycles,
        'seconds': round(elapsed, 2),
        'base': base,
        'final': final,
        'rss': _summary(rss_samples, 'process RSS'),
        'pss': _summary(pss_samples, 'process PSS'),
        'rss_samples_kb': [round(v / 1024, 1) for v in rss_samples],
        'pss_samples_kb': [round(v / 1024, 1) for v in pss_samples],
        'leftover_tmp_files': temp_files,
        'config_dir': str(config_dir),
    }


def _browser_rss():
    """RSS всех Chromium-процессов, запущенных Playwright из этого теста."""
    if psutil is None:
        return None
    try:
        total = 0
        current = psutil.Process()
        for child in current.children(recursive=True):
            try:
                name = child.name().lower()
                if 'chrom' in name or 'headless' in name:
                    total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total or None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def probe_web(cycles: int, headless: bool = True):
    from playwright.sync_api import sync_playwright

    index = (ROOT / 'web' / 'index.html').as_uri()
    report = {
        'mode': 'web',
        'platform': platform.platform(),
        'python': sys.version,
        'cycles': cycles,
        'headless': headless,
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(index)
        # init() ждёт pywebviewready 3 секунды, затем работаем без backend.
        page.wait_for_timeout(4000)
        page.evaluate("""() => {
            const el = document.getElementById('blink-toggle');
            el.checked = false;
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""")

        cdp = context.new_cdp_session(page)
        cdp.send('HeapProfiler.enable')

        def drop_image(seed):
            page.evaluate("""async (seed) => {
                const canvas = document.createElement('canvas');
                canvas.width = 256; canvas.height = 256;
                const ctx = canvas.getContext('2d');
                ctx.fillStyle = `hsl(${seed % 360}, 80%, 50%)`;
                ctx.fillRect(0, 0, 256, 256);
                const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
                const file = new File([blob], 'probe.png', {type: 'image/png'});
                const dt = new DataTransfer();
                dt.items.add(file);
                const slot = document.querySelector('.slot[data-slot="idle"]');
                slot.dispatchEvent(new DragEvent('drop', {
                    dataTransfer: dt, bubbles: true, cancelable: true,
                }));
            }""", seed)
            page.wait_for_function(
                "() => { const i = document.getElementById('idle-img'); return i.complete && i.naturalWidth > 0; }",
                timeout=10000,
            )

        def clear_image():
            page.evaluate("""() => {
                const btn = document.querySelector('.slot[data-slot="idle"] .clear');
                btn.click();
            }""")
            page.wait_for_function(
                "() => !document.getElementById('idle-img').getAttribute('src')",
                timeout=10000,
            )

        def churn(seed):
            if seed % 2 == 0:
                drop_image(seed)
            else:
                clear_image()
            page.evaluate("""(seed) => {
                window.__micTick(seed % 3 === 0, seed % 100);
                window.__micTick(false, 0);
                document.querySelector('.tab-btn[data-tab="' + (seed % 2 ? 'audio' : 'display') + '"]').click();
                if (seed % 7 === 0) {
                    document.getElementById('hide-settings-btn').click();
                    document.querySelector('.tab-btn[data-tab="images"]').click();
                    document.getElementById('settings-panel').classList.remove('hidden');
                    document.getElementById('settings-panel').inert = false;
                }
            }""", seed)

        def sample():
            cdp.send('HeapProfiler.collectGarbage')
            heap = cdp.send('Runtime.getHeapUsage')
            dom = page.evaluate("""() => ({
                nodes: document.getElementsByTagName('*').length,
                imgs: document.querySelectorAll('img[src]').length,
                srcChars: document.getElementById('idle-img').getAttribute('src')?.length || 0,
            })""")
            return {
                'js_used': heap['usedSize'],
                'js_total': heap['totalSize'],
                'rss': _browser_rss(),
                **dom,
            }

        for i in range(max(10, cycles // 5)):
            churn(i)
        sample()
        js_samples = []
        rss_samples = []
        dom_samples = []
        per_cycle = []
        started = time.perf_counter()
        for i in range(cycles):
            t0 = time.perf_counter()
            churn(i)
            per_cycle.append(time.perf_counter() - t0)
            if i % 5 == 0:
                s = sample()
                js_samples.append(s['js_used'])
                if s['rss'] is not None:
                    rss_samples.append(s['rss'])
                dom_samples.append(s['nodes'])
        final = sample()
        elapsed = time.perf_counter() - started
        report.update({
            'seconds': round(elapsed, 2),
            'ms_per_cycle': round(statistics.fmean(per_cycle) * 1000, 2),
            'js_heap': _summary(js_samples, 'JS heap used'),
            'browser_rss': _summary(rss_samples, 'browser tree RSS'),
            'js_samples_kb': [round(v / 1024, 1) for v in js_samples],
            'rss_samples_kb': [round(v / 1024, 1) for v in rss_samples],
            'dom_nodes': {
                'first': dom_samples[0] if dom_samples else None,
                'last': dom_samples[-1] if dom_samples else None,
                'max': max(dom_samples) if dom_samples else None,
            },
            'final': final,
            'page_errors': errors[:10],
        })
        context.close()
        browser.close()
    return report


def print_report(report):
    print(f"\n=== {report['mode']} | {report['platform']} ===")
    print(f"cycles={report.get('cycles')} time={report.get('seconds')}s")
    for key in ('rss', 'pss', 'js_heap', 'browser_rss'):
        summary = report.get(key)
        if summary:
            print(f"{summary['label']}: first={summary['first_kb']} KB, "
                  f"last={summary['last_kb']} KB, peak={summary['peak_kb']} KB, "
                  f"growth={summary['growth_kb']} KB, "
                  f"slope={summary['slope_kb_per_sample']} KB/sample")
    if 'dom_nodes' in report:
        print(f"DOM nodes: {report['dom_nodes']}")
    if report.get('leftover_tmp_files'):
        print(f"WARNING leftover tmp: {report['leftover_tmp_files']}")
    if report.get('page_errors'):
        print(f"PAGE ERRORS: {report['page_errors']}")
    if report.get('rss_samples_kb'):
        print(f"RSS samples KB: {report['rss_samples_kb']}")
    if report.get('js_samples_kb'):
        print(f"JS heap samples KB: {report['js_samples_kb']}")


def main():
    parser = argparse.ArgumentParser(description='PNGTuber memory leak probe')
    parser.add_argument('--mode', choices=['core', 'web', 'all'], default='all')
    parser.add_argument('--cycles', type=int, default=200)
    parser.add_argument('--json', type=Path, default=None)
    parser.add_argument('--headed', action='store_true', help='показать Chromium (для web)')
    args = parser.parse_args()

    reports = []
    if args.mode in ('core', 'all'):
        reports.append(probe_core(args.cycles))
    if args.mode in ('web', 'all'):
        reports.append(probe_web(args.cycles, headless=not args.headed))

    for report in reports:
        print_report(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\nJSON: {args.json}")


if __name__ == '__main__':
    main()
