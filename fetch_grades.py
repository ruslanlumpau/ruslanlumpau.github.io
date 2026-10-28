import os
import re
import json
import sys
import time
import base64
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
import requests

# ============================================================
# CONFIG
# ============================================================

ESCHOOLS_LOGIN = os.environ.get("ESCHOOLS_LOGIN")
ESCHOOLS_PASSWORD = os.environ.get("ESCHOOLS_PASSWORD")
GH_TOKEN = os.environ.get("GH_TOKEN")
GH_REPO = os.environ.get("GH_REPO")

if not all([ESCHOOLS_LOGIN, ESCHOOLS_PASSWORD, GH_TOKEN, GH_REPO]):
    print("Ошибка: Не все переменные окружения заданы (ESCHOOLS_LOGIN, ESCHOOLS_PASSWORD, GH_TOKEN, GH_REPO)!")
    sys.exit(1)

SCHOOL_ID = "a82a9a7f-65a5-4978-0208-000190769772"
CLASS_ID = "43cb2dce-bcf6-45b2-bc5d-c8a38fe13781"
STUDENT_ID = "916215f6-76c2-4116-998e-3fa0e3ebf6cd"

BASE_URL = "https://diary.e-schools.by"
LOGIN_URL = f"{BASE_URL}/api/v1/auth/login/parent"
REQUEST_TIMEOUT = 20
MAX_PAGES = 20
PAUSE_BETWEEN_WEEKS = 0.3

STUDENT_BASE_URL = f"{BASE_URL}/api/v1/education/diary/schools/{SCHOOL_ID}/classes/{CLASS_ID}/students/{STUDENT_ID}"

# Вкладка «Итоговые отметки» дневника (четвертные, экзамен, годовая) и список предметов ученика.
FINAL_URL = f"{STUDENT_BASE_URL}/final/whole"
SUBJECTS_URL = f"{STUDENT_BASE_URL}/educational_subjects"

# Реальный адрес списка недель найден в DevTools: .../diary/time_activities/week_activities
# (в ответе у недели есть uuid, summary вида "28 сентября - 04 октября", время в миллисекундах
# и time_activity_id - он одинаков у всех недель одной четверти).
TIME_ACTIVITIES_URL = f"{BASE_URL}/api/v1/education/diary/time_activities/week_activities"
_IDS = {"school": SCHOOL_ID, "class": CLASS_ID, "student": STUDENT_ID}

# (url, params): сначала настоящий адрес без параметров и с ними, остальное - запасные варианты
WEEK_ATTEMPTS = [
    (TIME_ACTIVITIES_URL, {}),
    (TIME_ACTIVITIES_URL, _IDS),
    (f"{STUDENT_BASE_URL}/week_activities", {}),
    (f"{BASE_URL}/api/v1/education/diary/week_activities", _IDS),
]

# Для экспериментов без правки кода: например WEEKS_EXTRA_PARAMS='{"year": 2026}'
WEEKS_EXTRA_PARAMS = json.loads(os.environ.get("WEEKS_EXTRA_PARAMS") or "{}")

# FULL_REFRESH=1 - заново скачать все недели учебного года. По умолчанию скачивается текущая четверть
# плюс недели прошлых четвертей, которых ещё нет в истории.
FULL_REFRESH = os.environ.get("FULL_REFRESH") == "1"

STORE_PATH = "grades_store.json"   # накопленная история оценок за весь учебный год (в репозитории)
DATA_PATH = "data.json"            # данные для дашборда
DEBUG_DIR = "debug"
MINSK = timezone(timedelta(hours=3))

# Предметы, по которым отметок не бывает: в дашборд и в историю не попадают
EXCLUDED_SUBJECT_RE = re.compile(
    r"(классный|информационный)\s+час|(класная|інфармацыйная)\s+гадзіна",
    re.I,
)


def is_excluded_subject(name):
    return bool(name) and bool(EXCLUDED_SUBJECT_RE.search(str(name)))


# Четверти определяются по данным API (time_activity_id недель). Эта таблица - запасной вариант на случай,
# если в ответе API такого поля не окажется. Источник: график Минобразования, опубликованный на neg.by.
QUARTERS = [
    ("I четверть",   date(2026, 9, 1),   date(2026, 10, 31)),
    ("II четверть",  date(2026, 11, 9),  date(2026, 12, 24)),
    ("III четверть", date(2027, 1, 11),  date(2027, 3, 20)),
    ("IV четверть",  date(2027, 3, 29),  date(2027, 5, 31)),
]
QUARTER_NAMES = ["I четверть", "II четверть", "III четверть", "IV четверть"]

# У 9-11 классов последняя четверть заканчивается 25 мая (API отдаёт недели до 31 мая): только для шапки.
LAST_QUARTER_END = (5, 25)

# ============================================================
# SESSION / AUTH
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
})


def login():
    try:
        resp = session.post(LOGIN_URL, json={"login": ESCHOOLS_LOGIN, "password": ESCHOOLS_PASSWORD},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code not in (200, 201):
            print(f" Ошибка авторизации ({resp.status_code}): {resp.text[:500]}")
            sys.exit(1)
        data = resp.json()
        token = data.get("auth_token") or data.get("token")
        if not token:
            print(" Ошибка: токен авторизации не найден.")
            sys.exit(1)
    except Exception as exc:
        print(f" Ошибка авторизации: {exc}")
        sys.exit(1)
    session.headers["authorization"] = str(token)


def api_get(url, params=None):
    """GET с одной повторной авторизацией при 401 (токен мог протухнуть за время долгого цикла)."""
    res = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if res.status_code == 401:
        print("      401 -> повторная авторизация")
        login()
        res = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    return res

# ============================================================
# DATE HELPERS
# ============================================================

DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y")


def normalize_date(value):
    """Возвращает 'YYYY-MM-DD' или None. Понимает ISO, dd.mm.yyyy, yyyy/mm/dd и unix-время (сек/мс)."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit() and len(value.strip()) >= 10):
        try:
            ts = float(value)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=MINSK).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return None

    text = str(value).strip()
    if len(text) < 8:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def get_school_year_start(today):
    start_year = today.year if today.month >= 9 else today.year - 1
    return date(start_year, 9, 1)


def fmt_ru(iso):
    return date.fromisoformat(iso).strftime("%d.%m.%Y")

# ============================================================
# RECURSIVE JSON WALK
# ============================================================

def recursive_walk(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from recursive_walk(value)
    elif isinstance(data, list):
        for item in data:
            yield from recursive_walk(item)


def describe_structure(data, depth=0, max_depth=3, indent="    "):
    """Короткий вывод структуры ответа в лог - чтобы видеть, что реально вернул API."""
    pad = indent + "  " * depth
    if isinstance(data, dict):
        print(f"{pad}dict, ключи: {list(data.keys())[:15]}")
        if depth < max_depth:
            for k, v in list(data.items())[:5]:
                if isinstance(v, (dict, list)):
                    print(f"{pad}- {k}:")
                    describe_structure(v, depth + 1, max_depth, indent)
    elif isinstance(data, list):
        print(f"{pad}list, элементов: {len(data)}")
        if data and depth < max_depth:
            describe_structure(data[0], depth + 1, max_depth, indent)
    else:
        print(f"{pad}{type(data).__name__}: {str(data)[:60]}")

# ============================================================
# WEEK DISCOVERY
# ============================================================

UUID_KEYS = ("week_activity_uuid", "week_uuid", "uuid", "id")
START_KEYS = ("start_ts", "start_date", "start", "begin_date", "date_start", "week_start_date",
              "week_start", "date_from", "from", "begin", "startDate")
END_KEYS = ("end_ts", "end_date", "end", "finish_date", "date_end", "week_end_date",
            "week_end", "date_to", "to", "endDate")


def _first_date(obj, keys):
    for key in keys:
        if key in obj:
            d = normalize_date(obj[key])
            if d:
                return d
    return None


def extract_week_candidate(obj):
    if not isinstance(obj, dict):
        return None

    uuid = None
    for key in UUID_KEYS:
        if obj.get(key):
            uuid = obj[key]
            break

    start = _first_date(obj, START_KEYS)
    if not start:
        # запасной вариант: любое поле, в названии которого есть start/begin/from
        for key, value in obj.items():
            if re.search(r"(start|begin|from)", str(key), re.I):
                start = normalize_date(value)
                if start:
                    break

    if not uuid or not start:
        return None

    # реальные недели дневника: {"uuid", "summary", "time_activity_id", "start_ts", "end_ts"}
    strong = "time_activity_id" in obj or "summary" in obj
    return {"uuid": str(uuid), "start": start, "end": _first_date(obj, END_KEYS), "strong": strong,
            "quarter_key": str(obj["time_activity_id"]) if obj.get("time_activity_id") else None}


def find_week_objects(data):
    unique = {}
    for obj in recursive_walk(data):
        cand = extract_week_candidate(obj)
        if cand and cand["uuid"] not in unique:
            unique[cand["uuid"]] = cand

    candidates = list(unique.values())

    # 1) Приоритет: объекты в формате недели дневника (summary / time_activity_id). Короткие недели
    #    ("21 декабря - 24 декабря", "31 мая - 31 мая") тоже настоящие, поэтому длину не проверяем.
    strong = [w for w in candidates if w.get("strong")]

    # 2) Запасной вариант для другого формата ответа: диапазон 4-7 дней отсекает уроки/события.
    def span(w):
        if not w["end"]:
            return None
        return (date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days

    weeklike = [w for w in candidates if span(w) is not None and 4 <= span(w) <= 7]
    result = strong or weeklike or candidates

    for w in result:
        if not w["end"]:
            w["end"] = (date.fromisoformat(w["start"]) + timedelta(days=6)).isoformat()
    return result


def week_overlaps(week, period_start, period_end):
    ws = date.fromisoformat(week["start"])
    we = date.fromisoformat(week["end"])
    return ws <= period_end and we >= period_start


def fetch_all_pages(url, params):
    """Забирает список недель с одного URL, пробуя листать страницы (page=2,3...), пока появляются новые недели."""
    weeks = {}
    raw_first = None

    for page in range(1, MAX_PAGES + 1):
        p = dict(params)
        if page > 1:
            p["page"] = page
        try:
            res = api_get(url, p)
        except Exception as exc:
            print(f"      Ошибка запроса: {exc}")
            break

        if page == 1:
            print(f"      HTTP {res.status_code}")
        if res.status_code != 200:
            if page == 1:
                keep = ("server", "content-type", "www-authenticate", "retry-after", "x-request-id", "cf-ray", "via")
                hdrs = {k: v for k, v in res.headers.items() if k.lower() in keep}
                print(f"      заголовки: {hdrs or '-'}; тело ({len(res.text)} байт): {res.text[:200]!r}")
                return None, None
            break

        try:
            data = res.json()
        except ValueError:
            break
        if page == 1:
            raw_first = data

        new = [w for w in find_week_objects(data) if w["uuid"] not in weeks]
        if not new:
            break
        for w in new:
            weeks[w["uuid"]] = w

    return list(weeks.values()), raw_first


def discover_weeks(period_start, period_end):
    print("\nЗапрос списка недель (перебираю все варианты URL и выбираю самый полный)...")
    best = None

    for idx, (url, base_params) in enumerate(WEEK_ATTEMPTS, 1):
        params = {**base_params, **WEEKS_EXTRA_PARAMS}
        print(f"  [{idx}] {url}  params={list(params) or '-'}")
        weeks, raw = fetch_all_pages(url, params)
        if weeks is None:
            continue
        if not weeks:
            print("      200, но недель не распознано. Примеры объектов с uuid (смотрю, как называются поля дат):")
            shown = 0
            for obj in recursive_walk(raw):
                if isinstance(obj, dict) and any(k in obj for k in UUID_KEYS):
                    print("        ", {k: (str(v)[:40]) for k, v in list(obj.items())[:10]})
                    shown += 1
                    if shown >= 3:
                        break

        in_range = [w for w in weeks if week_overlaps(w, period_start, period_end)]
        print(f"      недель в ответе: {len(weeks)}, из них в нужном периоде: {len(in_range)}")
        if weeks:
            starts = sorted(w["start"] for w in weeks)
            print(f"      диапазон дат в ответе: {starts[0]} .. {starts[-1]}")

        score = (len(in_range), len(weeks))
        if best is None or score > best["score"]:
            best = {"score": score, "url": url, "weeks": weeks, "raw": raw}

    if best is None:
        return None

    print(f"\nВыбран источник: {best['url']}")
    print("  Структура ответа:")
    describe_structure(best["raw"])

    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, "weeks_raw.json"), "w", encoding="utf-8") as f:
        json.dump(best["raw"], f, ensure_ascii=False, indent=2)
    print(f"  Сырой ответ сохранён в {DEBUG_DIR}/weeks_raw.json (только локально, на GitHub не уходит)")
    return best["weeks"]


MARK_HINT = re.compile(r"(mark|grade|score|assess|point|value|ball|rating|estimate)", re.I)


def scan_keys(data, limit=25):
    """Названия полей, похожих на оценки, по всему JSON: [(имя, (сколько раз, тип значения)), ...]."""
    stats = {}
    for obj in recursive_walk(data):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if MARK_HINT.search(str(key)):
                    cnt, _ = stats.get(key, (0, None))
                    stats[key] = (cnt + 1, type(value).__name__)
    return sorted(stats.items(), key=lambda kv: -kv[1][0])[:limit]


def save_debug(name, data):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"      (не удалось сохранить {name}: {exc})")


def coverage_report(weeks, period_start, period_end):
    """Список понедельников, для которых в ответе API нет ни одной недели."""
    covered = set()
    for w in weeks:
        d = date.fromisoformat(w["start"])
        e = date.fromisoformat(w["end"])
        while d <= e:
            covered.add(d)
            d += timedelta(days=1)

    missing = []
    monday = period_start - timedelta(days=period_start.weekday())
    while monday <= period_end:
        if not any((monday + timedelta(days=i)) in covered for i in range(7)):
            missing.append(monday.isoformat())
        monday += timedelta(days=7)
    return missing

# ============================================================
# QUARTERS
# ============================================================

def build_quarters(year_weeks, year_start):
    """Четверти по time_activity_id недель: у всех недель одной четверти он одинаковый.
    Если у недель его нет - берём таблицу QUARTERS. Возвращает список по возрастанию даты:
    [{number, name, start, end, display_end}]."""
    groups = defaultdict(list)
    for w in year_weeks:
        if w.get("quarter_key"):
            groups[w["quarter_key"]].append(w)

    quarters = [{"start": min(w["start"] for w in ws), "end": max(w["end"] for w in ws)}
                for ws in groups.values()]

    if not quarters:
        print("  ВНИМАНИЕ: у недель нет time_activity_id - использую таблицу QUARTERS из скрипта.")
        year_end = date(year_start.year + 1, 8, 31)
        quarters = [{"start": s.isoformat(), "end": e.isoformat()}
                    for _, s, e in QUARTERS if year_start <= s <= year_end]

    quarters.sort(key=lambda q: q["start"])
    for i, q in enumerate(quarters, 1):
        q["number"] = i
        q["name"] = QUARTER_NAMES[i - 1] if i <= len(QUARTER_NAMES) else f"{i} четверть"
        q["display_end"] = q["end"]
        if i == 4:
            cap = date(date.fromisoformat(q["end"]).year, *LAST_QUARTER_END).isoformat()
            q["display_end"] = min(q["end"], cap)
    return quarters


def quarter_of(week_start, quarters):
    """Номер четверти, в которую попадает неделя (по дате её начала), или None (каникулы)."""
    for q in quarters:
        if q["start"] <= week_start <= q["end"]:
            return q["number"]
    return None


def current_quarter(quarters, today):
    """Последняя начавшаяся четверть. На каникулах остаётся предыдущая - до первого дня следующей,
    поэтому дашборд обнуляется ровно с начала новой четверти."""
    started = [q for q in quarters if q["start"] <= today.isoformat()]
    if started:
        return started[-1]
    return quarters[0] if quarters else None

# ============================================================
# GRADE HELPERS
# ============================================================

def get_subject_name(obj):
    if not isinstance(obj, dict):
        return None

    subject_name = (
        obj.get("subject_name")
        or obj.get("subject_title")
        or obj.get("discipline_name")
        or obj.get("subjectName")
    )

    if isinstance(subject_name, dict):
        subject_name = subject_name.get("name") or subject_name.get("title")

    if not subject_name and isinstance(obj.get("subject"), dict):
        subject_name = obj["subject"].get("name") or obj["subject"].get("title")

    if subject_name:
        return str(subject_name).strip()
    return None


def get_grade_value(mark):
    if isinstance(mark, dict):
        value = mark.get("value") if mark.get("value") is not None else mark.get("mark")
        if value is None:
            value = mark.get("grade") if mark.get("grade") is not None else mark.get("point")
    else:
        value = mark

    if value is None:
        return None

    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None

    if not number.is_integer():
        return None

    number = int(number)
    if 1 <= number <= 10:
        return number
    return None


# Поля, в которых урок/оценка может хранить дату (точные названия в API неизвестны, поэтому перебираем)
LESSON_DATE_KEYS = ("date", "lesson_date", "day", "lesson_day", "date_ts")
MARK_DATE_KEYS = ("date", "mark_date", "created_at", "created", "updated_at", "set_at")


def date_from(obj, keys):
    """Первая правдоподобная дата из перечисленных полей ('YYYY-MM-DD') или None.
    Отсекает мусор вроде day=3 (номер дня недели), который иначе превратился бы в 1970 год."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj:
            d = normalize_date(obj[key])
            if d and d >= "2020-01-01":
                return d
    return None


def walk_with_context(data, subject=None, day=None):
    """Обход JSON, при котором предмет и дата урока, найденные у родителя, наследуются вложенными
    объектами (оценка может лежать глубже, чем объект с названием предмета и датой)."""
    if isinstance(data, dict):
        subject = get_subject_name(data) or subject
        day = date_from(data, LESSON_DATE_KEYS) or day
        yield data, subject, day
        for value in data.values():
            yield from walk_with_context(value, subject, day)
    elif isinstance(data, list):
        for item in data:
            yield from walk_with_context(item, subject, day)


def extract_grade_pairs(obj, inherited_subject=None):
    subject_name = get_subject_name(obj) or inherited_subject
    if not subject_name:
        return []

    result = []
    for field in ("marks", "grades", "assessments", "marks_list"):
        value = obj.get(field)
        if isinstance(value, list):
            for mark in value:
                result.append((subject_name, mark))

    for field in ("mark", "grade"):
        if field in obj and obj.get(field) not in (None, ""):
            result.append((subject_name, obj.get(field)))

    return result


def make_grade_key(obj, mark, subject, week_start):
    """Возвращает (ключ, уникален_ли_сам_по_себе). Ключи без uuid получают счётчик повторов (см. extract_grades)."""
    if isinstance(mark, dict):
        for field in ("uuid", "id", "mark_uuid", "grade_uuid", "assessment_uuid"):
            val = mark.get(field)
            if val:
                return f"mark:{val}", True

    if isinstance(obj, dict):
        for field in ("mark_uuid", "grade_uuid", "assessment_uuid"):
            val = obj.get(field)
            if val:
                return f"mark:{val}", True

    grade_val = get_grade_value(mark)
    lesson_id = (obj.get("lesson_uuid") or obj.get("lesson_id")) if isinstance(obj, dict) else None
    if lesson_id:
        return f"lesson:{lesson_id}|subject:{subject}|grade:{grade_val}", False

    return f"fallback|week:{week_start}|subject:{subject}|grade:{grade_val}", False


def extract_grades(data, week_uuid, week_start):
    result = []
    seen = set()
    repeats = defaultdict(int)

    for obj, inherited, day in walk_with_context(data):
        for subject, mark in extract_grade_pairs(obj, inherited):
            if is_excluded_subject(subject):
                continue

            value = get_grade_value(mark)
            if value is None:
                continue

            key, is_unique = make_grade_key(obj, mark, subject, week_start)
            if is_unique:
                if key in seen:
                    continue
                seen.add(key)
            else:
                repeats[key] += 1
                key = f"{key}#{repeats[key]}"   # стабильный ключ между запусками

            result.append({
                "key": key,
                "subject": subject,
                "grade": value,
                # дата получения: дата урока, а если её нет - собственная дата оценки
                "date": day or date_from(mark, MARK_DATE_KEYS),
                "week_uuid": week_uuid,
                "week_start": week_start,
            })
    return result


def extract_subjects(data):
    """Все предметы, встретившиеся в ответе (в том числе те, по которым оценок нет)."""
    names = set()
    for obj, _, _ in walk_with_context(data):
        name = get_subject_name(obj)
        if name and not is_excluded_subject(name):
            names.add(name)
    return names


DATE_HINT = re.compile(r"(date|day|time|_ts$|^ts|start|created|updated)", re.I)


def scan_date_keys(data, limit=20):
    """Поля, похожие на дату: {имя: (сколько раз, пример значения)} - для диагностики, если даты оценок не нашлись."""
    stats = {}
    for obj in recursive_walk(data):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if DATE_HINT.search(str(key)) and not isinstance(value, (dict, list)):
                    cnt, sample = stats.get(key, (0, value))
                    stats[key] = (cnt + 1, sample)
    return sorted(stats.items(), key=lambda kv: -kv[1][0])[:limit]


def apply_first_seen(fetched, stored, now_iso, baseline):
    """Проставляет каждой свежей оценке first_seen - время, когда скрипт впервые её увидел.
    Оценка, которая уже есть в истории, сохраняет прежнее значение (у старых записей оно пустое - они
    никогда не считаются новыми). Новая оценка получает now_iso. baseline=True - истории ещё нет
    (первый запуск или сбой чтения): тогда ничего не помечаем, чтобы не выдать все оценки за новые."""
    known = {g["key"]: g.get("first_seen") for g in stored if g.get("key")}
    for g in fetched:
        if g["key"] in known:
            g["first_seen"] = known[g["key"]]
        else:
            g["first_seen"] = None if baseline else now_iso
    return fetched

# ============================================================
# SUBJECTS AND FINAL (QUARTER / EXAM / YEAR) MARKS
# ============================================================

def fetch_subject_names():
    """{uuid предмета: название} из educational_subjects. При ошибке - пустой словарь."""
    try:
        res = api_get(SUBJECTS_URL)
        if res.status_code != 200:
            print(f"  educational_subjects: HTTP {res.status_code}")
            return {}
        names = {}
        for obj in recursive_walk(res.json()):
            if obj.get("uuid") and obj.get("name"):
                names[str(obj["uuid"])] = str(obj["name"]).strip()
        print(f"  Предметов в списке ученика: {len(names)}")
        return names
    except Exception as exc:
        print(f"  Не удалось получить список предметов: {exc}")
        return {}


QUARTER_TYPES = {"FIRST_QUARTER": "1", "SECOND_QUARTER": "2", "THIRD_QUARTER": "3", "FOURTH_QUARTER": "4"}


def classify_final_type(mark_type):
    """Тип итоговой отметки -> столбец: '1'..'4', 'exam', 'year' или None, если тип не распознан.
    Типы экзамена и годовой пока угаданы по названию (EXAM / YEAR / ANNUAL): уточним, когда появятся."""
    t = str(mark_type).upper()
    if t in QUARTER_TYPES:
        return QUARTER_TYPES[t]
    if "EXAM" in t:
        return "exam"
    if "YEAR" in t or "ANNUAL" in t:
        return "year"
    return None


def _final_mark_text(item):
    """Отметка как строка (может быть не только число: 'зачтено' и т.п.). Пусто -> None."""
    if isinstance(item, list):
        parts = [p for p in (_final_mark_text(x) for x in item) if p]
        return "/".join(parts) if parts else None
    if isinstance(item, dict):
        item = item.get("mark")
    if item is None:
        return None
    text = str(item).strip()
    return text or None


def parse_final_marks(data, names_by_uuid):
    """Разбор ответа final/whole. Возвращает ({название предмета: {'1': '9', 'year': '8', ...}},
    {нераспознанный тип: значение}, [uuid предметов без названия])."""
    rows, unknown, unnamed = {}, {}, []
    marks = data.get("marks") if isinstance(data, dict) else None
    if not isinstance(marks, dict):
        return rows, unknown, unnamed

    for subject_uuid, by_type in marks.items():
        if not isinstance(by_type, dict):
            continue
        name = names_by_uuid.get(str(subject_uuid))
        if not name:
            unnamed.append(str(subject_uuid))
            continue
        row = rows.setdefault(name, {})
        for mark_type, item in by_type.items():
            value = _final_mark_text(item)
            if value is None:
                continue
            column = classify_final_type(mark_type)
            if column is None:
                unknown[str(mark_type)] = value
                continue
            row[column] = value
    return rows, unknown, unnamed


def fetch_final_marks(names_by_uuid):
    """Возвращает (rows, unknown) или None, если запрос не удался."""
    try:
        res = api_get(FINAL_URL)
    except Exception as exc:
        print(f"  Ошибка запроса итоговых отметок: {exc}")
        return None
    print(f"  final/whole: HTTP {res.status_code}")
    if res.status_code != 200:
        print(f"      тело ({len(res.text)} байт): {res.text[:200]!r}")
        return None
    try:
        data = res.json()
    except ValueError:
        print("      ответ не JSON")
        return None

    save_debug("final_whole.json", data)
    if not isinstance(data, dict) or "marks" not in data:
        print("      В ответе нет поля marks - структура изменилась:")
        describe_structure(data, indent="        ")
        return None

    rows, unknown, unnamed = parse_final_marks(data, names_by_uuid)
    if unnamed:
        print(f"      Предметов без названия в списке (пропущены): {len(unnamed)}")
    if unknown:
        print(f"      Нераспознанные типы отметок: {unknown}  <- пришлите эту строку, добавлю в classify_final_type")
    print(f"  Итоговых отметок: {sum(len(r) for r in rows.values())}")
    return rows, unknown

# ============================================================
# GITHUB
# ============================================================

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def github_get(path):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    res = requests.get(url, headers=GH_HEADERS, timeout=REQUEST_TIMEOUT)
    if res.status_code == 200:
        payload = res.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        return content, payload.get("sha")
    if res.status_code == 404:
        return None, None
    raise RuntimeError(f"GitHub GET {path}: HTTP {res.status_code} {res.text[:300]}")


def github_put(path, text, sha=None):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    payload = {
        "message": f"Auto-update {path} {datetime.now(MINSK).strftime('%Y-%m-%d %H:%M')}",
        "content": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    res = requests.put(url, headers=GH_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    if res.status_code in (200, 201):
        print(f"  {path} обновлён на GitHub.")
        return True
    print(f"  Ошибка отправки {path} ({res.status_code}): {res.text[:300]}")
    return False


def load_store():
    """Возвращает (оценки, sha файла, известные предметы)."""
    try:
        content, sha = github_get(STORE_PATH)
    except Exception as exc:
        print(f"  Не удалось прочитать {STORE_PATH}: {exc}")
        return [], None, []
    if not content:
        return [], sha, []
    try:
        stored = json.loads(content)
        return stored.get("grades", []), sha, stored.get("subjects", [])
    except ValueError:
        return [], sha, []

# ============================================================
# BUILD data.json
# ============================================================

def build_subjects_block(cur_grades, known_subjects):
    """Предметы текущей четверти. Предмет без оценок остаётся в списке (с пустым списком оценок)."""
    subject_items = defaultdict(list)
    for item in cur_grades:
        subject_items[item["subject"]].append(item)

    subjects_list = []
    for subject in sorted(set(known_subjects) | set(subject_items)):
        items = subject_items.get(subject, [])
        grades = [i["grade"] for i in items]
        count = len(grades)
        avg = round(sum(grades) / count, 2) if count else 0.0
        subjects_list.append({
            "name": subject,
            "average": avg,
            "grades": grades,
            # то же, что grades, но с датой и отметкой первого появления (по ней дашборд подсвечивает новые оценки)
            "grades_detail": [{"value": i["grade"], "date": i.get("date"), "first_seen": i.get("first_seen")}
                              for i in items],
            "count": count,
            "status": "Достаточно" if count >= 3 else "Не хватает",
            "color": "#00875A" if count >= 3 else "#DE350B",
        })
    return subjects_list


def build_weekly_block(quarter_weeks, cur_grades):
    grades_by_week = defaultdict(list)
    for item in cur_grades:
        grades_by_week[item["week_start"]].append(item["grade"])

    week_starts = sorted({w["start"] for w in quarter_weeks} | set(grades_by_week))
    weekly = []
    for ws in week_starts:
        grades = grades_by_week.get(ws, [])
        weekly.append({
            "label": date.fromisoformat(ws).strftime("%d.%m"),
            "date": ws,
            "avg": round(sum(grades) / len(grades), 2) if grades else None,  # None -> разрыв на графике, а не падение в 0
            "count": len(grades),
        })
    return weekly


def build_final_block(rows, unknown, known_subjects, stale=False):
    names = {n for n in set(known_subjects) | set(rows) if not is_excluded_subject(n)}
    subjects = []
    for name in sorted(names):
        row = rows.get(name, {})
        subjects.append({
            "name": name,
            "q": {k: row[k] for k in ("1", "2", "3", "4") if k in row},
            "exam": row.get("exam"),
            "year": row.get("year"),
        })
    return {
        "subjects": subjects,
        "hasExam": any(s["exam"] for s in subjects),
        "unknownTypes": unknown,
        "stale": stale,
    }

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70)
    print("СБОР ОЦЕНОК ИЗ E-SCHOOLS")
    print("=" * 70)

    today = datetime.now(MINSK).date()
    year_start = get_school_year_start(today)
    year_end = date(year_start.year + 1, 8, 31)
    print(f"Сегодня:              {today}")
    print(f"Начало учебного года: {year_start}")

    print("\n[1/6] Подключение и авторизация...")
    try:
        session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        print(f" Ошибка подключения: {exc}")
        sys.exit(1)
    login()
    print("  Авторизация успешна.")

    print("\n[2/6] Получение списка недель и определение четверти...")
    all_weeks = discover_weeks(year_start, today)
    if not all_weeks:
        print("\n ОШИБКА: API не вернул ни одной недели. Смотрите debug/weeks_raw.json и структуру выше.")
        sys.exit(1)

    all_weeks.sort(key=lambda w: (w["start"], w["uuid"]))
    year_weeks = [w for w in all_weeks if year_start.isoformat() <= w["start"] <= year_end.isoformat()]
    quarters = build_quarters(year_weeks, year_start)
    cur = current_quarter(quarters, today)
    if not cur:
        print("\n ОШИБКА: не удалось определить четверти - прерываюсь, чтобы не затереть данные.")
        sys.exit(1)

    for q in quarters:
        mark = "  <- текущая" if q["number"] == cur["number"] else ""
        print(f"  {q['name']}: {q['start']} .. {q['display_end']}{mark}")

    weeks_in_year = [w for w in year_weeks if week_overlaps(w, year_start, today)]
    quarter_weeks = [w for w in weeks_in_year if quarter_of(w["start"], quarters) == cur["number"]]
    missing = coverage_report(quarter_weeks, date.fromisoformat(cur["start"]),
                              min(today, date.fromisoformat(cur["end"])))

    print(f"\nНедель в API всего: {len(all_weeks)}; в учебном году: {len(year_weeks)}; "
          f"в текущей четверти (до сегодня): {len(quarter_weeks)}")
    if missing:
        print(f"  ВНИМАНИЕ: для {len(missing)} недель текущей четверти API не вернул данных: {', '.join(missing)}")
        print("  Это ограничение самого API (или парсинга дат), а не цикла. Отсутствующие недели возьмутся из истории,")
        print("  если они уже собирались ранее. Иначе нужно найти запрос, который диарий шлёт при листании назад")
        print("  (DevTools -> Network) и передать его параметры через WEEKS_EXTRA_PARAMS.")

    if not quarter_weeks:
        print("\n Ни одна неделя текущей четверти не найдена - прерываюсь, чтобы не затереть данные.")
        sys.exit(1)

    print("\n[3/6] Чтение накопленной истории...")
    stored_grades, store_sha, stored_subjects = load_store()
    # Истории нет (первый запуск или не удалось прочитать) - новыми ничего не помечаем
    baseline = not stored_grades
    # Классный и информационный час вычищаем и из старой истории
    stored_grades = [g for g in stored_grades if not is_excluded_subject(g.get("subject"))]
    stored_weeks = {g.get("week_start") for g in stored_grades}
    print(f"  оценок в истории: {len(stored_grades)}")

    # Текущая четверть скачивается заново; прошлые - только недели, которых ещё нет в истории.
    to_fetch = [w for w in weeks_in_year
                if FULL_REFRESH
                or quarter_of(w["start"], quarters) == cur["number"]
                or w["start"] not in stored_weeks]
    print(f"  к загрузке недель: {len(to_fetch)} (текущая четверть: {len(quarter_weeks)}, "
          f"из прошлых четвертей: {len(to_fetch) - len([w for w in to_fetch if quarter_of(w['start'], quarters) == cur['number']])})")

    print("\n[4/6] Получение уроков и оценок по неделям...")
    fetched_grades = []
    fresh_subjects = set()
    sample_lessons = None   # ответ одной недели с оценками - для диагностики дат
    week_results = []
    diag_printed = False

    for index, week in enumerate(to_fetch, start=1):
        week_uuid, week_start = week["uuid"], week["start"]
        print(f"\n  [{index}/{len(to_fetch)}] {week_start} (UUID: {week_uuid})")

        try:
            res = api_get(f"{STUDENT_BASE_URL}/lessons", {"week_activity_uuid": week_uuid})
            if res.status_code != 200:
                print(f"      HTTP {res.status_code}")
                week_results.append({"date": week_start, "status": f"http_{res.status_code}", "grades": 0})
                continue

            lessons_data = res.json()
            save_debug(f"lessons_{week_start}.json", lessons_data)
            grades = extract_grades(lessons_data, week_uuid, week_start)
            fresh_subjects |= extract_subjects(lessons_data)
            if grades and sample_lessons is None:
                sample_lessons = lessons_data

            # Диагностика структуры - только для давно прошедшей недели текущей четверти: в самом начале
            # четверти оценок законно нет, и подробный вывод был бы лишним шумом.
            if (not grades and not diag_printed and quarter_of(week_start, quarters) == cur["number"]
                    and (today - date.fromisoformat(week_start)).days > 7):
                diag_printed = True
                print("      Оценок 0 - показываю структуру ответа lessons (без значений, только названия полей):")
                describe_structure(lessons_data, max_depth=4, indent="        ")
                found = scan_keys(lessons_data)
                if found:
                    print("      Поля, похожие на оценки (имя: сколько раз, тип значения):")
                    for key, (cnt, typ) in found:
                        print(f"        {key}: {cnt}, {typ}")
                else:
                    print("      Полей, похожих на оценки, не найдено.")
                print(f"      Сырой ответ: {DEBUG_DIR}/lessons_{week_start}.json (только локально)")

            fetched_grades.extend(grades)
            print(f"      Оценок найдено: {len(grades)}")
            week_results.append({"date": week_start, "status": "ok" if grades else "empty", "grades": len(grades)})
        except Exception as exc:
            print(f"      ОШИБКА запроса: {exc}")
            week_results.append({"date": week_start, "status": "request_error", "grades": 0})

        time.sleep(PAUSE_BETWEEN_WEEKS)

    print("\n[5/6] Слияние с историей, предметы и итоговые отметки...")
    in_year = [g for g in stored_grades
               if (year_start - timedelta(days=6)).isoformat() <= g.get("week_start", "") <= today.isoformat()]
    # Неделя заменяется свежими данными, только если в них что-то нашлось. Пустой ответ не стирает историю
    # (иначе при смене формата API или сбое накопленные оценки пропали бы).
    weeks_with_fresh = {g["week_start"] for g in fetched_grades}
    kept = [g for g in in_year if g.get("week_start") not in weeks_with_fresh]

    # Когда оценка появилась впервые (для подсветки новых на дашборде)
    now_iso = datetime.now(MINSK).strftime("%Y-%m-%dT%H:%M")
    apply_first_seen(fetched_grades, stored_grades, now_iso, baseline)

    all_grades = kept + fetched_grades
    print(f"  из истории сохранено: {len(kept)}, свежих: {len(fetched_grades)}, всего за год: {len(all_grades)}")

    cur_grades = [g for g in all_grades if quarter_of(g["week_start"], quarters) == cur["number"]]
    new_grades = [g for g in cur_grades if g.get("first_seen") == now_iso]
    print(f"  оценок текущей четверти ({cur['name']}): {len(cur_grades)}")

    names_by_uuid = fetch_subject_names()

    # Все предметы: из истории, из уроков, из списка ученика и из самих оценок (классный/информационный час исключены)
    known_subjects = {s for s in (set(stored_subjects) | fresh_subjects | set(names_by_uuid.values())
                                  | {g["subject"] for g in all_grades})
                      if s and not is_excluded_subject(s)}
    print(f"  предметов всего: {len(known_subjects)}, из них без оценок в текущей четверти: "
          f"{len(known_subjects - {g['subject'] for g in cur_grades})}")

    # Даты получения оценок (для всплывающей подсказки на дашборде)
    undated = [g for g in fetched_grades if not g.get("date")]
    print(f"  оценок с датой: {len(fetched_grades) - len(undated)} из {len(fetched_grades)}")
    if undated and sample_lessons is not None:
        print("  ВНИМАНИЕ: у части оценок дата не найдена. Поля ответа, похожие на дату (имя: сколько раз, пример):")
        for key, (cnt, sample) in scan_date_keys(sample_lessons):
            print(f"    {key}: {cnt}, например {str(sample)[:40]!r}")
        print("  Пришлите этот список - я подставлю нужное поле в LESSON_DATE_KEYS.")
    if baseline:
        print("  Истории не было - оценки не помечаю как новые (подсветка начнётся со следующих оценок).")
    else:
        print(f"  Новых оценок с прошлого запуска: {len(new_grades)}")
        for g in new_grades:
            print(f"    + {g['subject']}: {g['grade']}")

    # Прежний data.json: нужен для sha и как запасной вариант блока "final", если сайт не ответил
    prev_data, data_sha = None, None
    try:
        prev_text, data_sha = github_get(DATA_PATH)
        if prev_text:
            prev_data = json.loads(prev_text)
    except Exception as exc:
        print(f"  Не удалось прочитать прежний {DATA_PATH}: {exc}")

    final_block = None
    if names_by_uuid:
        fetched_final = fetch_final_marks(names_by_uuid)
        if fetched_final:
            rows, unknown = fetched_final
            final_block = build_final_block(rows, unknown, known_subjects)
    else:
        print("  Список предметов недоступен - итоговые отметки пропущены.")
    if final_block is None and prev_data and prev_data.get("final"):
        final_block = dict(prev_data["final"])
        final_block["stale"] = True
        print("  Итоговые отметки не обновились - оставляю прежние (stale).")

    if not all_grades and not final_block:
        print("\n ВНИМАНИЕ: оценок нет ни в свежих данных, ни в истории.")
        print(" data.json на GitHub НЕ перезаписываю, чтобы не затереть дашборд пустыми данными.")
        print(" Отправьте вывод выше и файл debug/lessons_<дата>.json (проверьте его на личные данные).")
        sys.exit(2)

    print("\n[6/6] Формирование data.json...")
    subjects_list = build_subjects_block(cur_grades, known_subjects)
    weekly = build_weekly_block(quarter_weeks, cur_grades)

    period_end = min(today.isoformat(), cur["display_end"])
    flat = [item["grade"] for item in cur_grades]
    result_data = {
        "last_updated": datetime.now(MINSK).strftime("%Y-%m-%d %H:%M:%S"),
        "quarter": cur["name"],
        "quarterNumber": cur["number"],
        "quarterStart": cur["start"],
        "quarterEnd": cur["display_end"],
        "quarterFinished": today.isoformat() > cur["display_end"],
        "period": f"{fmt_ru(cur['start'])} — {fmt_ru(period_end)}",
        "startDate": cur["start"],
        "endDate": period_end,
        "overallAverage": round(sum(flat) / len(flat), 2) if flat else 0.0,
        "subjects": subjects_list,
        "weekly": weekly,
        "weeks": weekly,
        "final": final_block,
        "diagnostics": {
            "apiWeeksFound": len(all_weeks),
            "weeksInQuarter": len(quarter_weeks),
            "weeksFetched": len(to_fetch),
            "missingWeeksInApi": missing,
            "freshGrades": len(fetched_grades),
            "grades_from_history": len(kept),
            "quarterGrades": len(cur_grades),
            "newGradesThisRun": len(new_grades),
            "weekResults": week_results,
        },
    }

    json_str = json.dumps(result_data, ensure_ascii=False, indent=2)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        f.write(json_str)
    print("Локальный data.json сохранён.")

    store_str = json.dumps({"updated": result_data["last_updated"], "subjects": sorted(known_subjects),
                            "grades": all_grades},
                           ensure_ascii=False, indent=1)
    try:
        github_put(DATA_PATH, json_str, data_sha)
        github_put(STORE_PATH, store_str, store_sha)
    except Exception as exc:
        print(f" Ошибка отправки на GitHub: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
