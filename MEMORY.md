# Замеры памяти PNGTuber

Дата: 23.10.2026. Замеры выполнены на трёх ОС через GitHub Actions
(`.github/workflows/memory.yml`), по 1000 циклов на профиль.

## Что именно меряется

`tests/memory_probe.py` запускает два независимых профиля:

- **core** — Python: `Api.set_image()` с картинками 256/512/768/1000 px,
  очистка слота, `save_settings()`, `get_state()`, `save_scene()`/`load_scene()`
  каждые 25 циклов. Меряются RSS, PSS (Linux), потоки, открытые fd и
  оставшиеся `.tmp`-файлы.
- **web** — Chromium через Playwright: 1000 раз загружает/очищает картинку
  в слот, переключает вкладки/панель, вызывает `__micTick()`. После
  принудительного `HeapProfiler.collectGarbage` меряются JS heap, DOM и
  суммарный RSS дерева процессов Chromium.

Это не «полное приложение с QtWebEngine», а две его самые важные части:
Python-бэкенд и JS-фронтенд. Полный Linux-процесс с QtWebEngine измерен
отдельно ниже.

## Результаты: 1000 циклов

| ОС | core RSS, начало → конец | core PSS | JS heap, начало → конец | DOM | Временные файлы |
|---|---:|---:|---:|---:|---:|
| Linux (ubuntu-latest) | 36,2 → 35,7 МБ | 29,8 → 29,3 МБ | 1 286 → 1 346 КБ | 125 → 125 | 0 |
| Windows (windows-latest) | 39,3 → 40,1 МБ | — | 1 287 → 1 347 КБ | 125 → 125 | 0 |
| macOS (macos-latest) | 107,6 → 135,3 МБ | — | 1 290 → 1 349 КБ | 125 → 125 | 0 |

### Интерпретация

- **Python-core: утечки нет.** Linux и Windows держатся в пределах шума
  (±1 МБ на 1000 циклов). macOS показывает +28 МБ, но это ступенчатый
  рост аллокатора с выходом на плато: последние 60 замеров RSS стоят на
  135,3 МБ и не растут. Это high-water mark malloc/Pillow, а не линейная
  утечка.
- **JS heap: утечки нет.** Прирост ~60 КБ на 1000 циклов (0,06 КБ/цикл) —
  это ленивые кэши V8/строк, а не удержание DOM. Число DOM-узлов
  неизменно 125, `img[src]` после очистки — 0.
- **RSS Chromium** вырос на 7,7 МБ (Windows), 32 МБ (Linux) и 44 МБ
  (macOS) за 1000 быстрых перерисовок. JS heap при этом стабилен, а хвост
  замеров выходит на плато, поэтому это кэши/арены самого Chromium, а не
  утечка приложения. В реальном PNGTuber такие циклы происходят в сотни
  раз реже; при 200 циклах рост был 6–11 МБ.
- **Временные файлы не остаются**, потоки core не растут (2), страничных
  ошибок JS нет.

## Полное Linux-приложение

С QtWebEngine и `--disable-gpu` (флаг добавлен для Linux):

| Процесс | PSS |
|---|---:|
| Python + QtWebEngine browser | ~172 МБ |
| QtWebEngine zygote ×2 | ~22 + 18 МБ |
| QtWebEngine renderer | ~76 МБ |
| **Итого** | **~281 МБ** |

Без `--disable-gpu` было ~324 МБ. Это цена Qt WebEngine на Linux;
на Windows/macOS используется родной WebView2/WKWebView, там полный
GUI-процесс заметно легче (core-процесс в CI — 39/115 МБ, но это без
запуска окна).

## Размер сборок после отказа от Qt на Windows/macOS

По артефактам `Build check` (23.10.2026):

| ОС | zip |
|---|---:|
| Windows | 21,5 МБ |
| macOS | 15,7 МБ |
| Linux | 386 МБ |

Windows/macOS больше не содержат PyQt6/QtWebEngine — только родной webview,
Pillow и PortAudio. Linux по-прежнему тащит Qt WebEngine (это его самый
большой компонент).

## Выводы и лимиты

- Утечек в Python-логике и JS-состоянии не обнаружено.
- macOS core RSS требует поправки на аллокатор: смотреть на плато, а не
  на первый скачок.
- Для дальнейшего снижения памяти:
  - картинки 1000 px и отдельные превью 160 px (уже сделано);
  - отказ от NumPy (уже сделано; core PSS на Linux упал ~51 → 30 МБ);
  - `--disable-gpu` на Linux (уже сделано; ~324 → 281 МБ);
  - на Windows/macOS не устанавливаются PyQt6/QtWebEngine (уже сделано).
- Главный остаточный потребитель на Linux — Qt WebEngine. Полностью убрать
  его можно только сменой Linux-бэкенда на WebKitGTK/CEF, что увеличит
  системные зависимости и риск нестабильности.

## Как повторить

```bash
pip install -r requirements-memory.txt
playwright install chromium
python tests/memory_probe.py --mode all --cycles 1000 --json memory.json
```

Или вручную запустить workflow `Memory leak probes` и скачать артефакты
`memory-ubuntu-latest`, `memory-windows-latest`, `memory-macos-latest`.
