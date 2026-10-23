"""
Русские названия классов COCO (80 классов, которые отдаёт YOLOv8).

Ключ — английское имя класса от ultralytics (person, dog, bear, ...).
Значение — русское название для UI, логов и уведомлений.

Использование:
    from .labels_ru import label_ru
    label_ru("dog")   # → "собака"
    label_ru("bear")  # → "медведь"
    label_ru("???")   # → "???"  (неизвестный возвращается как есть)

ВАЖНО: label (английский) остаётся в JSONL как внутренний ключ
(cooldown, фильтры, whitelist). label_ru добавляется рядом — для отображения.
"""

LABEL_RU = {
    # --- Люди ---
    "person": "человек",
    # --- Транспорт ---
    "bicycle": "велосипед",
    "car": "автомобиль",
    "motorcycle": "мотоцикл",
    "airplane": "самолёт",
    "bus": "автобус",
    "train": "поезд",
    "truck": "грузовик",
    "boat": "лодка",
    # --- Дорожная инфраструктура ---
    "traffic light": "светофор",
    "fire hydrant": "гидрант",
    "stop sign": "знак «стоп»",
    "parking meter": "паркомат",
    "bench": "скамейка",
    # --- Животные ---
    "bird": "птица",
    "cat": "кошка",
    "dog": "собака",
    "horse": "лошадь",
    "sheep": "овца",
    "cow": "корова",
    "elephant": "слон",
    "bear": "медведь",
    "zebra": "зебра",
    "giraffe": "жираф",
    # --- Аксессуары ---
    "backpack": "рюкзак",
    "umbrella": "зонт",
    "handbag": "сумка",
    "tie": "галстук",
    "suitcase": "чемодан",
    # --- Спорт ---
    "frisbee": "фрисби",
    "skis": "лыжи",
    "snowboard": "сноуборд",
    "sports ball": "мяч",
    "kite": "воздушный змей",
    "baseball bat": "бейсбольная бита",
    "baseball glove": "бейсбольная перчатка",
    "skateboard": "скейтборд",
    "surfboard": "сёрфборд",
    "tennis racket": "теннисная ракетка",
    # --- Посуда ---
    "bottle": "бутылка",
    "wine glass": "бокал",
    "cup": "чашка",
    "fork": "вилка",
    "knife": "нож",
    "spoon": "ложка",
    "bowl": "миска",
    # --- Еда ---
    "banana": "банан",
    "apple": "яблоко",
    "sandwich": "бутерброд",
    "orange": "апельсин",
    "broccoli": "брокколи",
    "carrot": "морковь",
    "hot dog": "хот-дог",
    "pizza": "пицца",
    "donut": "пончик",
    "cake": "торт",
    # --- Мебель и интерьер ---
    "chair": "стул",
    "couch": "диван",
    "potted plant": "растение",
    "bed": "кровать",
    "dining table": "стол",
    "toilet": "унитаз",
    # --- Техника ---
    "tv": "телевизор",
    "laptop": "ноутбук",
    "mouse": "мышь",
    "remote": "пульт",
    "keyboard": "клавиатура",
    "cell phone": "телефон",
    "microwave": "микроволновка",
    "oven": "печь",
    "toaster": "тостер",
    "sink": "раковина",
    "refrigerator": "холодильник",
    # --- Прочее ---
    "book": "книга",
    "clock": "часы",
    "vase": "ваза",
    "scissors": "ножницы",
    "teddy bear": "плюшевый мишка",
    "hair drier": "фен",
    "toothbrush": "зубная щётка",
}


def label_ru(name: str) -> str:
    """Возвращает русское название класса или исходное имя, если перевода нет."""
    if not name:
        return name or ""
    return LABEL_RU.get(str(name).strip().lower(), name)


def label_bilingual(name: str) -> str:
    """Формат 'русское (english)' — для логов и отладки."""
    ru = label_ru(name)
    if ru and ru != name:
        return f"{ru} ({name})"
    return ru or name or ""
