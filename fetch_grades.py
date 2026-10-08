import os
import json
import sys
import base64
from datetime import datetime
import requests

# Читаем данные из переменных окружения (безопасный вариант)
ESCHOOLS_LOGIN = os.environ.get("ESCHOOLS_LOGIN")
ESCHOOLS_PASSWORD = os.environ.get("ESCHOOLS_PASSWORD")
GH_TOKEN = os.environ.get("GH_TOKEN")
GH_REPO = os.environ.get("GH_REPO")

if not all([ESCHOOLS_LOGIN, ESCHOOLS_PASSWORD, GH_TOKEN, GH_REPO]):
    print(" Ошибка: Не все переменные окружения заданы в update.bat!")
    sys.exit(1)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://diary.e-schools.by",
    "Referer": "https://diary.e-schools.by/"
})

def get_week_name(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day, month = dt.day, dt.month
        if month == 9:
            if 1 <= day <= 4: return "1–4 сен (1-я нед.)"
            elif 7 <= day <= 11: return "7–11 сен (2-я нед.)"
            elif 14 <= day <= 18: return "14–18 сен (3-я нед.)"
            elif 21 <= day <= 25: return "21–25 сен (4-я нед.)"
            elif 28 <= day <= 30: return "28 сен – 2 окт (5-я нед.)"
        elif month == 10:
            if 1 <= day <= 2: return "28 сен – 2 окт (5-я нед.)"
            elif 5 <= day <= 9: return "5–9 окт (6-я нед.)"
            elif 12 <= day <= 16: return "12–16 окт (7-я нед.)"
            elif 19 <= day <= 23: return "19–23 окт (8-я нед.)"
    except Exception:
        pass
    return "Прочие даты"

def upload_to_github(result_data):
    print("Отправка data.json на GitHub через API...")
    gh_url = f"https://api.github.com/repos/{GH_REPO}/contents/data.json"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    sha = None
    get_res = requests.get(gh_url, headers=headers)
    if get_res.status_code == 200:
        sha = get_res.json().get("sha")

    json_bytes = json.dumps(result_data, ensure_ascii=False, indent=2).encode("utf-8")
    content_b64 = base64.b64encode(json_bytes).decode("utf-8")

    payload = {
        "message": "Автообновление оценок с ПК",
        "content": content_b64
    }
    if sha:
        payload["sha"] = sha

    put_res = requests.put(gh_url, headers=headers, json=payload)
    if put_res.status_code in (200, 201):
        print(" Успешно! Файл data.json обновлен на GitHub Pages!")
    else:
        print(f" Ошибка отправки на GitHub ({put_res.status_code}): {put_res.text}")

def main():
    print("Подключение к e-schools.by...")
    
    login_url = "https://diary.e-schools.by/api/v1/auth/login/parent"
    resp = session.post(login_url, json={"login": ESCHOOLS_LOGIN, "password": ESCHOOLS_PASSWORD}, timeout=20)
    if resp.status_code not in (200, 201):
        print(f" Ошибка входа ({resp.status_code}): {resp.text}")
        sys.exit(1)

    res = session.get("https://diary.e-schools.by/api/v1/pupil/grades", timeout=20)
    if res.status_code != 200:
        print(f" Ошибка получения оценок ({res.status_code})")
        sys.exit(1)

    raw_data = res.json()

    subjects_list = []
    weekly_grades = {}
    all_flat_grades = []

    if "subjects" in raw_data:
        for item in raw_data["subjects"]:
            name = item.get("name", "Предмет")
            raw_grades = item.get("grades", [])

            num_grades = []
            for g in raw_grades:
                val = g.get("grade") if isinstance(g, dict) else g
                date_val = g.get("date") if isinstance(g, dict) else None

                if isinstance(val, (int, float)):
                    num_grades.append(val)
                    all_flat_grades.append(val)
                    if date_val:
                        w_name = get_week_name(date_val)
                        weekly_grades.setdefault(w_name, []).append(val)

            count = len(num_grades)
            avg = round(sum(num_grades) / count, 2) if count > 0 else 0.0

            subjects_list.append({
                "name": name,
                "average": avg,
                "grades": num_grades if count > 0 else "Нет оценок",
                "count": count,
                "status": "Достаточно" if count >= 3 else "Не хватает",
                "color": "#00875A" if count >= 3 else "#DE350B"
            })

    overall_avg = round(sum(all_flat_grades) / len(all_flat_grades), 2) if all_flat_grades else 0.0

    weeks_list = []
    for week_title, grades_arr in weekly_grades.items():
        if week_title != "Прочие даты" and grades_arr:
            w_avg = round(sum(grades_arr) / len(grades_arr), 2)
            weeks_list.append({
                "week": week_title,
                "average": w_avg,
                "overallAverage": overall_avg
            })

    result_json = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overallAverage": overall_avg,
        "subjects": subjects_list,
        "weeks": weeks_list
    }

    upload_to_github(result_json)

if __name__ == "__main__":
    main()
