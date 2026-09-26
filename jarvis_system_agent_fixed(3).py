# JARVIS SYSTEM AGENT (fixed/merged) — локальный AI-помощник Windows
# Python 3.10+ / Windows 10-11
#
# Установка зависимостей:
#   pip install requests psutil pyautogui pycaw comtypes
#
# Запуск Ollama (в отдельном терминале, если ещё не запущен):
#   ollama pull llama3.2:3b
#   ollama serve
#
# Что исправлено по сравнению с твоими v2/v3:
# 1. Модель почти всегда не знала ТЕКУЩУЮ громкость -> просьбы вроде
#    "сделай погромче" ломались или ставили случайный %. Теперь текущая
#    громкость передаётся модели в каждом снимке системы, и добавлен
#    инструмент adjust_volume(delta) для ОТНОСИТЕЛЬНЫХ изменений.
# 2. Список инструментов (tool) теперь жёстко ограничен через enum в
#    JSON-schema — маленькая модель (3B) больше не может придумать
#    несуществующий инструмент.
# 3. Добавлен цикл из нескольких шагов на один запрос пользователя:
#    сначала read-only инструменты (inspect_system/discover_apps), затем
#    одно "изменяющее" действие с подтверждением — как в v2, но с надёжным
#    структурированным JSON из v3.
# 4. Если pycaw не установлен, set_volume/adjust_volume не падают "тихо",
#    а либо честно просят поставить pycaw, либо (для adjust_volume)
#    используют запасной вариант через медиаклавиши.
# 5. discover_apps теперь ищет и в Program Files/AppData, и в PATH, и в
#    ярлыках меню "Пуск" — объединение подходов v2 и v3.

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Печатать сразу, без буферизации — иначе в некоторых консолях/IDE кажется,
# что программа "молчит", хотя она просто ждёт вывода.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

def log(*a, **kw):
    kw.setdefault("flush", True)
    print(*a, **kw)

try:
    import requests
    import psutil
except ImportError as e:
    print("Не хватает библиотеки:", e)
    print("Установи: pip install requests psutil pyautogui pycaw comtypes")
    raise SystemExit

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = os.environ.get("JARVIS_MODEL", "llama3.2:3b")

# ---------------- READ-ONLY: СИСТЕМА ----------------

def get_volume():
    """Текущая громкость и mute-статус. Требует pycaw."""
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        volume = device.EndpointVolume
        return {
            "ok": True,
            "percent": round(volume.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(volume.GetMute()),
        }
    except ImportError:
        return {"ok": False, "error": "pycaw не установлен (pip install pycaw comtypes)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def system_snapshot_light():
    """Маленький и быстрый снимок — это то, что уходит в модель на КАЖДЫЙ запрос.
    Без списка процессов: он тяжёлый и почти всегда не нужен для команд типа
    громкости/запуска приложений."""
    vm = psutil.virtual_memory()
    return {
        "os": platform.platform(),
        "computer": platform.node(),
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "ram_available_gb": round(vm.available / 1024**3, 1),
        "volume": get_volume(),
    }


def inspect_system():
    """Полный снимок (с процессами) — доступен модели ТОЛЬКО по явному вызову
    инструмента inspect_system, не подмешивается автоматически в контекст."""
    vm = psutil.virtual_memory()
    return {
        "os": platform.platform(),
        "computer": platform.node(),
        "cpu": platform.processor(),
        "cpu_count": os.cpu_count(),
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "ram_available_gb": round(vm.available / 1024**3, 1),
        "volume": get_volume(),
        "running_processes": sorted(
            {p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]}
        )[:120],
    }


def _start_menu_dirs():
    dirs = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    return [p for p in dirs if p.exists()]


# Папки, которые никогда не стоит обходить: они огромные, системные, часто
# содержат junction/reparse-точки, из-за которых рекурсивный обход мог
# зацикливаться и идти "бесконечно".
_SKIP_DIR_NAMES = {
    "windowsapps", "temp", "tmp", "cache", "caches", "node_modules",
    "$recycle.bin", "system volume information", "packages",
    ".git", "appdata", "onedrive",
}


def _walk_for_exe(root, name_filter, time_budget=3.0, max_results=40):
    """Ограниченный по времени обход БЕЗ следования по симлинкам/junction-ам
    (os.walk по умолчанию их не разворачивает — в отличие от Path.rglob,
    который может зациклиться на Windows-junction-ах и висеть бесконечно)."""
    results = []
    if not root or not root.exists():
        return results
    start = time.time()
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if time.time() - start > time_budget or len(results) >= max_results:
                break
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIR_NAMES]
            for fn in filenames:
                fnl = fn.lower()
                if fnl.endswith(".exe") and (not name_filter or name_filter in fnl):
                    results.append(str(Path(dirpath) / fn))
                    if len(results) >= max_results:
                        break
    except (OSError, PermissionError):
        pass
    return results


def discover_apps(query=""):
    """Read-only: реальные .exe/ярлыки на этом ПК. query фильтрует по имени.
    Специально ограничено по времени, чтобы никогда не "зависать"."""
    q = (query or "").strip().lower()
    found = []

    # 1) Ярлыки в меню "Пуск" — обычно небольшая папка, но всё равно с лимитом времени
    for base in _start_menu_dirs():
        start = time.time()
        try:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                if time.time() - start > 2.0:
                    break
                for fn in filenames:
                    if fn.lower().endswith(".lnk"):
                        name = Path(fn).stem
                        if not q or q in name.lower():
                            found.append({"name": name, "path": str(Path(dirpath) / fn), "type": "shortcut"})
        except (OSError, PermissionError):
            pass

    # 2) PATH — мгновенно, диск не сканирует
    common_names = ["chrome", "firefox", "msedge", "opera", "steam",
                     "discord", "code", "notepad", "explorer", "vlc", "spotify"]
    names_to_check = [q] if q else common_names
    for name in names_to_check:
        for candidate in (name, name if name.endswith(".exe") else name + ".exe"):
            hit = shutil.which(candidate)
            if hit:
                found.append({"name": Path(hit).stem, "path": hit, "type": "path"})

    # 3) Ограниченный обход Program Files — только если явно задан query,
    #    и только с жёстким бюджетом времени на каждый корень.
    if q:
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        ]
        for root in roots:
            for p in _walk_for_exe(root, q, time_budget=3.0, max_results=10):
                found.append({"name": Path(p).stem, "path": p, "type": "exe"})

    seen, unique = set(), []
    for x in found:
        key = x["path"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(x)
    return unique[:60]


def inspect_path(path):
    p = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not p.exists():
        return {"exists": False, "path": str(p)}
    st = p.stat()
    return {
        "exists": True, "path": str(p.resolve()), "is_file": p.is_file(),
        "is_dir": p.is_dir(), "size": st.st_size if p.is_file() else None,
    }

# ---------------- ИЗМЕНЯЮЩИЕ ДЕЙСТВИЯ ----------------

def set_volume(percent):
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        volume = device.EndpointVolume
        pct = max(0, min(100, int(percent)))
        volume.SetMasterVolumeLevelScalar(pct / 100.0, None)
        return {"ok": True, "percent": pct}
    except ImportError:
        return {"ok": False, "error": "Нужен pycaw. Выполни: pip install pycaw comtypes"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def adjust_volume(delta):
    """Относительное изменение громкости, например +10 или -15."""
    info = get_volume()
    if info.get("ok"):
        new_pct = max(0, min(100, info["percent"] + int(delta)))
        return set_volume(new_pct)
    # Запасной вариант без pycaw: медиаклавиши (приблизительно)
    try:
        import pyautogui
        key = "volumeup" if delta > 0 else "volumedown"
        presses = max(1, round(abs(int(delta)) / 2))
        pyautogui.press(key, presses=presses, interval=0.02)
        return {"ok": True, "approx": True, "note": f"Нажал {key} {presses} раз(а); точный % недоступен без pycaw"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_app(path):
    if not path or not Path(path).exists():
        return {"ok": False, "error": "Файл приложения не найден"}
    try:
        if str(path).lower().endswith(".lnk"):
            os.startfile(path)
        else:
            subprocess.Popen([path], shell=False)
        return {"ok": True, "launched": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_url(url):
    import webbrowser
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    webbrowser.open(url)
    return {"ok": True, "url": url}


def press_key(key):
    import pyautogui
    pyautogui.press(key)
    return {"ok": True}


def hotkey(keys):
    import pyautogui
    pyautogui.hotkey(*keys)
    return {"ok": True}


def type_text(text):
    import pyautogui
    pyautogui.write(text, interval=0.01)
    return {"ok": True}


_KNOWN_APPS_CACHE = None

def get_known_apps(force_refresh=False):
    """Базовый список приложений (ярлыки меню "Пуск" + PATH) — без глубокого
    обхода Program Files, поэтому быстро (пара секунд максимум) и без риска
    зависнуть. Кэшируем, чтобы не пересканировать на каждый запрос."""
    global _KNOWN_APPS_CACHE
    if force_refresh or _KNOWN_APPS_CACHE is None:
        log("🔍 Сканирую установленные приложения...")
        _KNOWN_APPS_CACHE = discover_apps()
        log(f"   Готово, найдено {len(_KNOWN_APPS_CACHE)} приложений.")
    return _KNOWN_APPS_CACHE


READ_ONLY = {"inspect_system", "discover_apps", "inspect_path"}
CHANGE = {"set_volume", "adjust_volume", "open_app", "open_url", "key", "hotkey", "type_text"}
ALL_TOOLS = sorted(READ_ONLY | CHANGE)


def execute(tool, args):
    if tool == "inspect_system": return inspect_system()
    if tool == "discover_apps": return discover_apps(args.get("query", ""))
    if tool == "inspect_path": return inspect_path(args.get("path", ""))
    if tool == "set_volume": return set_volume(args.get("percent", 50))
    if tool == "adjust_volume": return adjust_volume(args.get("delta", 0))
    if tool == "open_app": return open_app(args.get("path", ""))
    if tool == "open_url": return open_url(args.get("url", ""))
    if tool == "key": return press_key(args.get("key", ""))
    if tool == "hotkey": return hotkey(args.get("keys", []))
    if tool == "type_text": return type_text(args.get("text", ""))
    return {"ok": False, "error": "Неизвестный инструмент"}

# ---------------- МОДЕЛЬ ----------------

SYSTEM_PROMPT = """Ты JARVIS, локальный помощник Windows.
Ты обязан вернуть РОВНО один JSON-объект и ничего кроме него.

Формат:
{"message":"короткий ответ пользователю","action":null}
или
{"message":"что собираешься сделать","action":{"tool":"название","args":{...}}}

Инструменты (используй ТОЛЬКО эти имена в поле tool):
- inspect_system: read-only, args {} — ОС/CPU/RAM/процессы/ТЕКУЩАЯ громкость и mute-статус (поле volume).
- discover_apps: read-only, args {"query": "часть имени приложения"} — найти реальные .exe/ярлыки.
  ВСЕГДА указывай непустой query (например "steam", "chrome") — без него глубокий поиск в
  Program Files не выполняется, чтобы не тормозить систему.
- inspect_path: read-only, args {"path": "..."}.
- set_volume: изменить громкость на АБСОЛЮТНОЕ значение, args {"percent": 0..100}.
- adjust_volume: изменить громкость ОТНОСИТЕЛЬНО текущей, args {"delta": -100..100}. Используй это для просьб
  вроде "сделай погромче/потише", "прибавь/убавь громкость" — НЕ пытайся угадать абсолютное число сам.
- open_app: запустить найденный (через discover_apps) exe/ярлык, args {"path":"C:\\..."}.
- open_url: открыть URL, args {"url":"https://..."}.
- key: нажать клавишу, args {"key":"..."}.
- hotkey: сочетание клавиш, args {"keys":["ctrl","c"]}.
- type_text: ввести текст, args {"text":"..."}.

Правила:
1. Текущая громкость и mute-статус уже есть в снимке системы (system_snapshot.volume) — не спрашивай их
   отдельным инструментом и не выдумывай значение.
2. Для запуска приложения сначала посмотри known_apps в снимке или вызови discover_apps, если нужного там нет.
   Никогда не выдумывай путь к .exe.
3. Любое изменяющее действие: верни action, программа сама запросит разрешение пользователя — не спрашивай
   разрешение в тексте message.
4. Если запрос нельзя выполнить доступными инструментами — action должен быть null, объясни почему в message.
5. За один ответ — не больше одного действия в поле action.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "action": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": ALL_TOOLS},
                        "args": {"type": "object"},
                    },
                    "required": ["tool", "args"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["message", "action"],
    "additionalProperties": False,
}


def ollama(messages):
    log("🧠 Думаю (жду ответ от Ollama)...")
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": MODEL,
            "messages": messages,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0.1},
        }, timeout=180)
    except requests.exceptions.Timeout:
        log("JARVIS: Ollama слишком долго не отвечает (таймаут 180с). "
            "Возможно, модель перегружена или контекст слишком большой.")
        return ""
    except requests.exceptions.ConnectionError:
        log("JARVIS: не могу подключиться к Ollama на http://127.0.0.1:11434. "
            "Убедись, что выполнено 'ollama serve'.")
        return ""
    if not r.ok:
        log(f"JARVIS: Ollama вернул ошибку {r.status_code}: {r.text[:300]}")
        return ""
    return r.json()["message"]["content"]


def parse_json(raw):
    if not raw:
        return None
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception:
        m = re.search(r"\{.*\}", clean, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def confirm(tool, args):
    log(f"\n⚠️  JARVIS хочет выполнить: {tool} {json.dumps(args, ensure_ascii=False)}")
    return input("Разрешить? [д/н]: ").strip().lower() in ("д", "да", "y", "yes")


def check_ollama():
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        if not r.ok:
            return False
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(MODEL in m for m in models):
            print(f"Модель {MODEL} не найдена. Выполни: ollama pull {MODEL}")
            return False
        return True
    except Exception:
        print("Ollama не отвечает на http://127.0.0.1:11434 — убедись, что 'ollama serve' запущен.")
        return False


def process_user_turn(user_text):
    # Каждый запрос строится с нуля — НЕ накапливаем историю разговора.
    # Это системный агент, а не чат: старая история только раздувала запрос
    # к модели с каждым разом и делала ответ всё медленнее.
    context = {"system_snapshot": system_snapshot_light(), "known_apps": get_known_apps()}
    conv = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Актуальные read-only данные ПК:\n" + json.dumps(context, ensure_ascii=False)},
        {"role": "user", "content": user_text},
    ]

    for step in range(4):  # до 4 шагов рассуждения на один запрос
        raw = ollama(conv)
        if not raw:
            # ollama() уже вывел причину (таймаут / нет соединения / ошибка HTTP)
            return
        conv.append({"role": "assistant", "content": raw})
        obj = parse_json(raw)
        if obj is None:
            log("JARVIS: не удалось разобрать ответ модели.")
            log("RAW:", raw)
            return
        if obj.get("message"):
            log("JARVIS:", obj["message"])
        action = obj.get("action")
        if not action:
            return
        tool, args = action.get("tool"), action.get("args") or {}
        if tool not in ALL_TOOLS:
            log(f"JARVIS: модель выбрала неизвестный инструмент «{tool}».")
            return
        if tool in CHANGE and not confirm(tool, args):
            log("JARVIS: действие отменено.")
            return
        log(f"⚙️  Выполняю: {tool}...")
        result = execute(tool, args)
        log("[РЕЗУЛЬТАТ]", json.dumps(result, ensure_ascii=False))
        if tool in READ_ONLY:
            conv.append({
                "role": "user",
                "content": f"Результат инструмента {tool}:\n{json.dumps(result, ensure_ascii=False)}\n"
                           "Продолжай: вызови следующий нужный инструмент или дай финальный ответ (action:null).",
            })
            continue
        return  # одно изменяющее действие за запрос — и стоп
    log("JARVIS: превышен лимит шагов на этот запрос.")


def main():
    print("=" * 64)
    print("JARVIS SYSTEM AGENT (fixed) — локальный AI-помощник Windows")
    print("/system — снимок системы, /volume — текущая громкость,")
    print("/apps — пересканировать приложения, /quit — выход")
    print("=" * 64)

    if not check_ollama():
        return

    get_known_apps()  # первое (медленное) сканирование — сразу при старте, не во время запроса
    print("\nJARVIS: Система запущена. Я готов.")
    while True:
        try:
            user = input("\nТы: ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not user:
            continue
        low = user.lower()
        if low == "/quit":
            break
        if low == "/system":
            print(json.dumps(inspect_system(), ensure_ascii=False, indent=2))
            continue
        if low == "/volume":
            print(json.dumps(get_volume(), ensure_ascii=False, indent=2))
            continue
        if low == "/apps":
            get_known_apps(force_refresh=True)
            continue
        try:
            process_user_turn(user)
        except Exception as e:
            print("JARVIS: ошибка:", e)


if __name__ == "__main__":
    main()
