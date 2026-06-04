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
    "ПВПЗ-15", "ПВПЗ-16", "ПВПЗ-17", "ПВПЗ-18", "ПВПЗ-64",
    "ПВПЗ15", "ПВПЗ16", "ПВПЗ17", "ПВПЗ18", "ПВПЗ64"
]

# Додав "переплат" як найчастішу людську помилку
KW_PENSION = [r'пенсі', r'пен']
KW_TRADE = [r'торгівл', r'торг', r'товар', r'продаж', r'друковані медіа', r'роздріб']
KW_SUB = [r'передплат', r'переплат', r'підписк', r'передпл']

def extract_amount(keywords, text):
    """
    Бронебійна функція для витягування суми поруч із ключовими словами.
    Ігнорує будь-яку пунктуацію та злиплі символи.
    """
    kw_pattern = '|'.join(keywords)
    
    # Варіант 1: Слово -> Число (наприклад "Пенсія-12;", "Торгівля- 567.00;.")
    # [\s\-:=.,;]* — ігнорує будь-яке сміття (пробіли, тире, крапки з комами) між словом і цифрою
    # (\d+(?:[.,]\d+)?) — захоплює ціле число або дріб (але ігнорує крапку в кінці речення)
    pattern_forward = rf'(?i)(?:{kw_pattern})[\s\-:=.,;]*(\d+(?:[.,]\d+)?)'
    match_f = re.search(pattern_forward, text)
    
    if match_f:
        return float(match_f.group(1).replace(',', '.'))
        
    # Варіант 2: Число -> Слово (наприклад "1515-товар", "0 підписка")
    pattern_backward = rf'(?i)(\d+(?:[.,]\d+)?)[\s\-:=.,;]*(?:грн|шт|кг)?[\s\-:=.,;]*(?:{kw_pattern})'
    match_b = re.search(pattern_backward, text)
    
    if match_b:
        return float(match_b.group(1).replace(',', '.'))

    return None

def parse_report(report_string):
    """
    Аналізує текст звіту та витягує ID, локацію і фінансові показники.
    """
    data = {
        "id": None,
        "location": None,
        "pension": 0.0,
        "trade": 0.0,
        "subscription": 0.0,
        "raw_text": report_string.replace('\n', ' ')
    }
    
    try:
        clean_text = report_string.lower().replace('\n', ' ')
        
        # --- 1. Витягування ID (Індексу) ---
        five_digits = re.findall(r'\b\d{5}\b', clean_text)
        for num in five_digits:
            if num in VALID_INDICES:
                data["id"] = num
                break
                
        if not data["id"]:
            pvpz_match = re.search(r'(?i)пвпз[\s\-]*(\d{2,3})', clean_text)
            if pvpz_match:
                data["id"] = "ПВПЗ " + pvpz_match.group(1)

        # 🔥 КРИТИЧНИЙ ФІКС: Видаляємо знайдений індекс із тексту!
        # Це гарантує, що парсер ніколи не прийме індекс "77551" за суму пенсії
        if data["id"]:
            clean_text = clean_text.replace(data["id"].lower(), ' ')
            # На всяк випадок підчистимо будь-які 5-значні поштові індекси з тексту
            clean_text = re.sub(r'\b7[67]\d{3}\b', ' ', clean_text)

        # --- 2. Витягування Локації (Назви) ---
        for loc in VALID_LOCATIONS:
            if loc.lower() in clean_text:
                data["location"] = loc
                break

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

        # Якщо повідомлення не схоже на звіт, повертаємо None.
        if pension_val is None and trade_val is None and sub_val is None:
            return None

    except Exception as e:
        print(f"Помилка парсингу: {e} для тексту: {report_string}")
        
    return data

# === БЛОК ТЕСТУВАННЯ ===
if __name__ == "__main__":
    test_reports = [
        "77662. Товар-226. Пенсія-2.Передплата-4.",
        "77673 ВПЗ Ясень Пенсії - 49 Торгівля- 346грн (+106грн газети) Передплата -2.",
        "77551 пенсія - 4, торгівля - 632.30, передплата - 0.",
        "77512 Товар - 552. (в.т.числі газети. , Передплати - 7., пенсій - 9.",
        "77552-передплата-0 Товар-528",
        "77624 Пенсія 17 Передплата 15 Товар 1023",
        "77510 пенсій-5,товар-405,передплати-10.",
        "77631 Пенсія-61 Товар-1200 Переплата-18"
    ]

    parsed_data = []
    for report in test_reports:
        parsed_data.append(parse_report(report))

    print(json.dumps(parsed_data, indent=2, ensure_ascii=False))