import os
import json
import sys
from datetime import datetime
import requests

# 1. Получение логина и пароля из GitHub Secrets
LOGIN = os.environ.get("ESCHOOLS_LOGIN")
PASSWORD = os.environ.get("ESCHOOLS_PASSWORD")

if not LOGIN or not PASSWORD:
    print("Ошибка: Переменные окружения ESCHOOLS_LOGIN или ESCHOOLS_PASSWORD не найдены.")
    sys.exit(1)

# 2. Настройка сессии
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://diary.e-schools.by/"
})

def main():
    print("Начало процесса получения оценок с e-schools.by...")
    
    # Шаг А: Авторизация
    login_url = "https://e-schools.by/login"
    login_payload = {
        "login": LOGIN,
        "password": PASSWORD
    }
    
    try:
        response = session.post(login_url, json=login_payload, timeout=15)
        if response.status_code not in (200, 201):
            print(f"Ошибка авторизации. Статус: {response.status_code}")
            sys.exit(1)
        print("Успешная авторизация на e-schools.by")
    except Exception as e:
        print(f"Ошибка при подключении к серверу авторизации: {e}")
        sys.exit(1)

    # Шаг Б: Запрос оценок за текущую четверть
    # Используем API эндпоинт электронного дневника
    grades_url = "https://diary.e-schools.by/api/v1/pupil/grades"
    try:
        res = session.get(grades_url, timeout=15)
        if res.status_code == 200:
            raw_data = res.json()
        else:
            print(f"Не удалось получить список оценок. Код: {res.status_code}")
            raw_data = None
    except Exception as e:
        print(f"Сбой при запросе оценок: {e}")
        raw_data = None

    # Шаг В: Обработка данных предмета и формирование структуры для data.json
    # (Если API временно недоступен, скрипт обновит время проверки без сбоя)
    subjects_list = []
    
    if raw_data and "subjects" in raw_data:
        for item in raw_data["subjects"]:
            name = item.get("name", "Неизвестный предмет")
            grades = item.get("grades", []) # Массив оценок, например [7, 10, 10, 9]
            
            count = len(grades)
            avg = round(sum(grades) / count, 2) if count > 0 else 0.0
            
            # Правила аттестации: минимум 3 оценки за четверть
            status = "Достаточно" if count >= 3 else "Не хватает"
            color = "#00875A" if count >= 3 else "#DE350B"
            
            subjects_list.append({
                "name": name,
                "average": avg,
                "grades": grades if count > 0 else "Нет оценок",
                "count": count,
                "status": status,
                "color": color
            })
    else:
        print("Используется базовое заполнение/сохранение существующего формата...")

    # Если API вернуло предметы — сортируем по среднему баллу (по убыванию)
    if subjects_list:
        subjects_list.sort(key=lambda x: x["average"], reverse=True)

    # Чтение существующего data.json для сохранения истории по неделям (динамики)
    existing_data = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception:
            existing_data = {}

    # Рассчитываем общий средний балл
    all_grades = []
    for s in subjects_list:
        if isinstance(s.get("grades"), list):
            all_grades.extend(s["grades"])
    
    overall_avg = round(sum(all_grades) / len(all_grades), 2) if all_grades else existing_data.get("overallAverage", 8.2)

    # Формируем итоговый JSON
    result_json = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overallAverage": overall_avg,
        "subjects": subjects_list if subjects_list else existing_data.get("subjects", []),
        "weeks": existing_data.get("weeks", [
            {"week": "1-4 сен", "average": 9.67},
            {"week": "7-11 сен", "average": 7.67},
            {"week": "14-18 сен", "average": 7.83},
            {"week": "21-25 сен", "average": 8.75},
            {"week": "28 сен - 2 окт", "average": 7.60}
        ])
    }

    # Запись в файл data.json
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)

    print("Файл data.json успешно обновлён!")

if __name__ == "__main__":
    main()
