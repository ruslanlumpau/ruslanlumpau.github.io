import os
import json
import sys
from datetime import datetime
import requests

LOGIN = os.environ.get("ESCHOOLS_LOGIN")
PASSWORD = os.environ.get("ESCHOOLS_PASSWORD")

if not LOGIN or not PASSWORD:
    print("Ошибка: Переменные окружения ESCHOOLS_LOGIN или ESCHOOLS_PASSWORD не найдены.")
    sys.exit(1)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://diary.e-schools.by",
    "Referer": "https://diary.e-schools.by/"
})

def get_week_name(date_str):
    """Определяет учебную неделю I четверти по дате оценки."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        # Номера недель I четверти (сентябрь - октябрь)
        day = dt.day
        month = dt.month
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

def main():
    print("Начало процесса получения оценок...")
    
    # 1. Вход в систему
    login_url = "https://diary.e-schools.by/api/v1/auth/login/parent"
    login_payload = {"login": LOGIN, "password": PASSWORD}
    
    try:
        response = session.post(login_url, json=login_payload, timeout=15)
        if response.status_code not in (200, 201):
            print(f"Ошибка входа ({response.status_code}): {response.text}")
            sys.exit(1)
        print("Авторизация успешна!")
    except Exception as e:
        print(f"Ошибка подключения: {e}")
        sys.exit(1)

    # 2. Запрос всех оценок за I четверть
    grades_url = "https://diary.e-schools.by/api/v1/pupil/grades"
    try:
        res = session.get(grades_url, timeout=15)
        if res.status_code == 200:
            raw_data = res.json()
        else:
            print(f"Не удалось получить список оценок. Код: {res.status_code}")
            sys.exit(1)
    except Exception as e:
        print(f"Ошибка запроса оценок: {e}")
        sys.exit(1)

    subjects_list = []
    weekly_grades = {}  # { "1–4 сен (1-я нед.)": [9, 10, 10] }
    all_flat_grades = []

    if "subjects" in raw_data:
        for item in raw_data["subjects"]:
            name = item.get("name", "Предмет")
            raw_grades = item.get("grades", [])
            
            num_grades = []
            for g in raw_grades:
                # Извлекаем числовое значение оценки и её дату
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

    # Общий средний балл за четверть с 1 сентября
    overall_avg = round(sum(all_flat_grades) / len(all_flat_grades), 2) if all_flat_grades else 0.0

    # Расчет динамики по неделям
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

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)

    print("Файл data.json успешно обновлен со всеми свежими данными!")

if __name__ == "__main__":
    main()
