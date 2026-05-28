import re
import json
from typing import TypedDict, Optional

class ParsedReport(TypedDict):
    id: Optional[str]
    location: Optional[str]
    pension: float
    trade: float
    subscription: float
    raw_text: str

# === ТВОЇ ДОВІДНИКИ (ЖОРСТКО ЗАДАНІ ДЛЯ ІДЕАЛЬНОЇ ТОЧНОСТІ) ===
VALID_INDICES = [
    "77202", "77510", "77552", "77502", "77503", "77504", "77540", "77542", 
    "77551", "77210", "77554", "77523", "77531", "77543", "77220", "77512", 
    "77516", "77624", "77623", "77653", "77621", "77622", "77650", "77601", 
    "77605", "77620", "77631", "77635", "77640", "77662", "77670", "77673", 
    "76545", "76546", "76547", "76548", "76594"
]

VALID_LOCATIONS = [
    "Болехів", "Велика Тур'я", "Вигода", "Долина-2", "Долина-3", "Долина-4", 
    "Княжолука", "Кропивник", "Мала Тур'я", "Міжріччя", "Новошин", "Солуків", 
    "Станківці", "Старий Мізунь", "Тисів", "Тростянець", "Раків", "Спас", 
    "Грабів", "Липовиця", "Верхній Струтин", "Лоп'янка", "Ілемня", "Рожнятів", 
    "Сваричів", "Нижній Струтин", "Рівня", "Петранка", "Цінева", "Перегінське", 
    "Небилів", "Ясень", "ПВПЗ 15", "ПВПЗ 16", "ПВПЗ 17", "ПВПЗ 18", "ПВПЗ 64",
    # Додаткові варіації написання ПВПЗ для надійності:
    "ПВПЗ-15", "ПВПЗ-16", "ПВПЗ-17", "ПВПЗ-18", "ПВПЗ-64",
    "ПВПЗ15", "ПВПЗ16", "ПВПЗ17", "ПВПЗ18", "ПВПЗ64"
]

# Ключові слова (корені слів) для пошуку показників
KW_PENSION = [r'пенсі', r'пен']
KW_TRADE = [r'торгівл', r'товар', r'продаж', r'друковані медіа', r'роздріб']
KW_SUB = [r'передплат', r'переплат', r'підписк', r'передпл']

def extract_amount(keywords, text):
    """
    Універсальна функція для витягування суми поруч із ключовими словами.
    Повертає float або None.
    """
    # Варіант 1: Ключове слово -> цифра (наприклад: "Товар 150.50", "Торгівля- 1465грн")
    pattern_forward = r'(?i)(?:' + '|'.join(keywords) + r')[^\d]*(\d+(?:[\.,]\d+)?)'
    match = re.search(pattern_forward, text)
    
    if match:
        num_str = match.group(1).replace(',', '.')
        return float(num_str)
        
    # Варіант 2: Цифра -> Ключове слово (наприклад: "1515-товар", "0 підписка")
    pattern_backward = r'(?i)(\d+(?:[\.,]\d+)?)\s*(?:-|грн|шт)?\s*(?:' + '|'.join(keywords) + r')'
    match_b = re.search(pattern_backward, text)
    
    if match_b:
        num_str = match_b.group(1).replace(',', '.')
        return float(num_str)

    return None

def parse_report(report_string):
    """
    Аналізує текст звіту та витягує ID, локацію і фінансові показники.
    """
    # Базова структура (замість None ставимо 0.0 для зручності таблиць)
    data = {
        "id": None,
        "location": None,
        "pension": 0.0,
        "trade": 0.0,
        "subscription": 0.0,
        "raw_text": report_string.replace('\n', ' ')
    }
    
    try:
        # Очищуємо текст для зручного пошуку
        clean_text = report_string.lower().replace('\n', ' ')
        
        # --- 1. Витягування ID (Індексу) ---
        # Шукаємо чітко 5 цифр
        five_digits = re.findall(r'\b\d{5}\b', clean_text)
        for num in five_digits:
            if num in VALID_INDICES:
                data["id"] = num
                break
                
        # Якщо 5 цифр немає, але є згадка ПВПЗ з цифрою (наприклад, пвпз17)
        if not data["id"]:
            pvpz_match = re.search(r'(?i)пвпз[\s\-]*(\d{2,3})', clean_text)
            if pvpz_match:
                # Це не повноцінний 5-значний індекс, але ми фіксуємо його як ID
                data["id"] = "ПВПЗ " + pvpz_match.group(1)

        # --- 2. Витягування Локації (Назви) ---
        for loc in VALID_LOCATIONS:
            # Шукаємо назву відділення незалежно від регістру
            if loc.lower() in clean_text:
                data["location"] = loc
                break # Беремо перший знайдений збіг

        # --- 3. Витягування фінансових показників ---
        pension_val = extract_amount(KW_PENSION, clean_text)
        if pension_val is not None:
            data["pension"] = pension_val
            
        trade_val = extract_amount(KW_TRADE, clean_text)
        if trade_val is not None:
            data["trade"] = trade_val
            
        sub_val = extract_amount(KW_SUB, clean_text)
        if sub_val is not None:
            data["subscription"] = sub_val

    except Exception as e:
        # Якщо станеться будь-яка аномалія, парсер не впаде
        print(f"Помилка парсингу: {e} для тексту: {report_string}")
        
    return data

# === БЛОК ТЕСТУВАННЯ ===
if __name__ == "__main__":
    # Взято кілька найважчих і нестандартних прикладів з твого списку
    test_reports = [
        "77202, Болехів 2,пенсія---11шт,торгівля--1417грн,передплата-0. (Продаж газет---1170грн.)",
        "77551 Мала Тур'я\nПенсія - 3\nТоргівля - 112,50 (12 газети)\nПередплата - 0",
        "77510 1515-товар",
        "Пвпз17 _пенсія 7чол ,передплата 0,товар 695 грн",
        "77635\nТовар 1252.50 грн+(газета 12грн)\n Передплата 57 шт\nПенсія 0",
        "77504 Пенсія -.13; Торгівля -500; Передплата-0;",
        "Міжріччя 77210 пенсія-4 торгівля - 900\nпередплата -2"
    ]

    parsed_data = []
    for report in test_reports:
        parsed_data.append(parse_report(report))

    # Виводимо красиво у форматі JSON
    print(json.dumps(parsed_data, indent=2, ensure_ascii=False))