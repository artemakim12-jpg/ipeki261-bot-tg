import asyncio
import json
import logging
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import quote
from calendar import monthrange

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

TG_TOKEN = os.getenv("TG_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
TARGET_GROUP = "И-26-1"
TARGET_COURSE = "1 курс"
CLOUD_URL = "https://cloud.pilot-ipek.ru/s/7HDT8FZGBd7J5cs"
BASE_URL = "https://www.pilot-ipek.ru"
API_URL = f"{BASE_URL}/api/get_list"
CHECK_INTERVAL = 10800
DAYS_AHEAD = 7
CACHE_FILE = "/app/data/schedule_cache_i261.json"

MONTHS_RU = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня", 7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
MONTHS_NOM_RU = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь", 7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"}
MONTH_NUM_BY_NAME = {name: num for num, name in MONTHS_NOM_RU.items()}
DATES_PER_PAGE = 3
MONTHS_PER_PAGE = 6

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
last_cloud_signature = None
loop = None
schedule_snapshot = {}


def load_cache():
    global schedule_snapshot, last_cloud_signature
    
    if not os.path.exists(CACHE_FILE):
        logger.info("Файл кэша не найден — начинаем с нуля.")
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded = data.get("schedule", {})
        
        is_new_format = True
        for day_key, lessons in loaded.items():
            if lessons and isinstance(lessons[0], str):
                is_new_format = False
                break
        if not is_new_format:
            logger.info("Старый формат кэша — игнорируем, начинаем с нуля.")
            schedule_snapshot = {}
            last_cloud_signature = data.get("cloud_signature")
            return
        schedule_snapshot = loaded
        last_cloud_signature = data.get("cloud_signature")
        logger.info(f"Кэш загружен: {len(schedule_snapshot)} дней.")
    except Exception as e:
        logger.error(f"Ошибка загрузки кэша: {e}")


def save_cache():
    try:
        data = {"schedule": schedule_snapshot, "cloud_signature": last_cloud_signature}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша: {e}")


def get_last_day_of_month(month_name, year=None):
    if year is None:
        year = datetime.now().year
    m = MONTH_NUM_BY_NAME.get(month_name)
    return monthrange(year, m)[1] if m else 31


def extract_days_from_text(t):
    return [int(x) for x in re.findall(r"\b(\d{1,2})\b", t)]


def extract_month_from_text(t):
    tl = t.lower()
    for n, name in MONTHS_RU.items():
        if name in tl:
            return n
    return None


def get_cloud_signature():
    h = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(CLOUD_URL, headers=h, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Облако недоступно: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    parts = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        n = cells[0].get_text(" ", strip=True)
        d = cells[-1].get_text(" ", strip=True)
        if n and d:
            parts.append(f"{n}|{d}")
    if not parts:
        parts.append(soup.get_text(" ", strip=True)[:2000])
    return "\n".join(parts)


def check_cloud_and_notify():
    global last_cloud_signature
    sig = get_cloud_signature()
    if not sig:
        return
    if last_cloud_signature is None:
        last_cloud_signature = sig
        save_cache()
        return
    if sig != last_cloud_signature:
        last_cloud_signature = sig
        save_cache()
        msg = "🔔 Новые материалы в дистанционном обучении!\n\n📚 Группа " + TARGET_GROUP + " · " + TARGET_COURSE + "\n🔗 " + CLOUD_URL + "\n\nОткройте: " + TARGET_COURSE + " → " + TARGET_GROUP
        try:
            asyncio.run_coroutine_threadsafe(bot.send_message(ADMIN_CHAT_ID, msg, disable_web_page_preview=True), loop)
            logger.info("Уведомление об облаке отправлено.")
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
    else:
        logger.info("Облако без изменений.")


def get_api_data():
    try:
        r = requests.get(API_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API недоступно: {e}")
        return {}


def get_all_date_links():
    data = get_api_data()
    out = []
    for mk, dates in data.items():
        if not isinstance(dates, list):
            continue
        if mk in ("Текущий месяц", "Расписание звонков"):
            continue
        for dt in dates:
            out.append((dt, BASE_URL + "/raspo/" + quote(dt)))
    return out


def get_months_with_dates():
    data = get_api_data()
    out = []
    for n in range(1, 13):
        name = MONTHS_NOM_RU[n]
        ds = data.get(name, [])
        if isinstance(ds, list) and ds:
            out.append((name, ds))
    return out


def find_link_for_date(target):
    day = target.day
    mn = MONTHS_RU[target.month]
    for text, url in get_all_date_links():
        tl = text.lower()
        if mn not in tl:
            continue
        if re.search(r"(?<!\d)0?" + str(day) + r"(?!\d)", tl):
            return text, url
    return None, None


def get_latest_available_date():
    latest = None
    for dt, _ in get_all_date_links():
        days = extract_days_from_text(dt)
        mn = extract_month_from_text(dt)
        if not days or not mn:
            continue
        for d in days:
            try:
                dt2 = datetime(datetime.now().year, mn, d)
            except ValueError:
                continue
            if latest is None or dt2 > latest:
                latest = dt2
    return latest


def get_day_from_text(t):
    m = re.match(r"^(\d{1,2})", t.strip())
    return int(m.group(1)) if m else None


def filter_dates_by_range(dates, s, e):
    out = []
    for d in dates:
        day = get_day_from_text(d)
        if day is None:
            continue
        if e == 0:
            if s <= day <= 31:
                out.append(d)
        else:
            if s <= day <= e:
                out.append(d)
    out.sort(key=lambda x: get_day_from_text(x) or 0)
    return out


def get_available_ranges(month_name, dates):
    last = get_last_day_of_month(month_name)
    ranges = [("1–10", filter_dates_by_range(dates, 1, 10)), ("11–20", filter_dates_by_range(dates, 11, 20)), ("21–" + str(last), filter_dates_by_range(dates, 21, 0))]
    return [(l, i) for l, i in ranges if i]


def filter_by_range_label(dates, label):
    if label == "1–10":
        return filter_dates_by_range(dates, 1, 10)
    if label == "11–20":
        return filter_dates_by_range(dates, 11, 20)
    if label.startswith("21–"):
        return filter_dates_by_range(dates, 21, 0)
    return []


def clean_cell_text(cell):
    ps = cell.find_all("p")
    if not ps:
        t = cell.get_text(" ", strip=True)
        return t if t else "—"
    parts = [p.get_text(" ", strip=True) for p in ps]
    parts = [p for p in parts if p and p.strip() != "–"]
    if not parts:
        return "—"
    return " / ".join(parts)


def parse_all_tables_from_url(url):
    h = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=h, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Не открыть {url}: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for w in soup.find_all("div", class_="table-wrapper"):
        table = w.find("table")
        if not table:
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        hc = rows[0].find_all(["td", "th"])
        col = None
        for i, c in enumerate(hc):
            if TARGET_GROUP in c.get_text(strip=True):
                col = i
                break
        if col is None:
            continue
        tt = w.find_previous(["p", "h1", "h2", "h3"])
        title = tt.get_text(" ", strip=True) if tt else ""
        lessons = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= col:
                continue
            tc = cells[0].get_text(" ", strip=True)
            if "пара" not in tc.lower():
                continue
            lessons.append((tc, clean_cell_text(cells[col])))
        if lessons:
            out.append({"title": title, "lessons": lessons})
    return out


def parse_schedule_for_date(target):
    _, url = find_link_for_date(target)
    if not url:
        return []
    tables = parse_all_tables_from_url(url)
    day = target.day
    mn = MONTHS_RU[target.month]
    pat = re.compile(r"\b0?" + str(day) + r"\s+" + mn + r"\s+\d{4}\b", re.IGNORECASE)
    return [t for t in tables if pat.search(t["title"])]


def parse_schedule_for_date_text(dt_text):
    days = extract_days_from_text(dt_text)
    mn = extract_month_from_text(dt_text)
    if not days or not mn:
        return []
    url = None
    for text, u in get_all_date_links():
        if text.lower() == dt_text.lower():
            url = u
            break
    if not url:
        return []
    tables = parse_all_tables_from_url(url)
    if not tables:
        return []
    mn_name = MONTHS_RU[mn]
    pats = [re.compile(r"\b0?" + str(d) + r"\s+" + mn_name + r"\s+\d{4}\b", re.IGNORECASE) for d in days]
    matched = []
    for t in tables:
        for p in pats:
            if p.search(t["title"]):
                matched.append(t)
                break
    return matched if matched else tables


def make_snapshot_for_day(target):
    import time
    for attempt in range(3):
        try:
            tables = parse_schedule_for_date(target)
        except Exception as e:
            logger.error("Ошибка парсинга: " + str(e))
            tables = []
        if tables:
            lessons = tables[0]["lessons"]
            result = []
            for ti, content in lessons:
                t = re.sub(r"\s+", " ", ti).strip()
                c = re.sub(r"\s+", " ", content).strip()
                result.append({"time": t, "content": c})
            return result
        time.sleep(5)
    return None

def parse_date_key(key):
    try:
        return datetime.strptime(key, "%d.%m.%Y")
    except ValueError:
        return None


def build_new_snapshot():
    snapshot = {}
    today = datetime.now()
    for i in range(DAYS_AHEAD):
        target = today + timedelta(days=i)
        key = target.strftime("%d.%m.%Y")
        lessons = make_snapshot_for_day(target)
        if lessons is None:
            logger.info("День " + key + " не удалось загрузить — пропускаем.")
            continue
        if lessons:
            snapshot[key] = lessons
    return snapshot


def format_change_message(changes):
    lines = ["⚠️ Изменения в расписании!", ""]
    for change in changes:
        lines.append(change)
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    return msg


def lesson_to_str(lesson):
    """Превращает словарь пары в строку для сравнения."""
    return lesson["time"] + "|" + lesson["content"]


def format_table_from_lessons(title_key, lessons):
    """Красивое оформление дня — как в обычном расписании."""
    lines = ["━━━━━━━━━━━━━━━━━", "📅 Расписание · " + TARGET_GROUP]
    lines.append("📆 " + title_key)
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("")
    for les in lessons:
        t = re.sub(r"\s+", " ", les["time"]).strip().replace(".", ":")
        t = re.sub(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", r"\1 – \2", t)
        lines.append("🔵 " + t)
        parts = [p.strip() for p in les["content"].split(" / ") if p.strip()]
        for i, part in enumerate(parts):
            if i == 0:
                lines.append("   📚 " + part)
            elif i == len(parts) - 1 and len(parts) > 1:
                lines.append("   🚪 " + part)
            else:
                lines.append("   👤 " + part)
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def diff_snapshots(old, new):
    changes = []
    only_new = True
    for key in sorted(new.keys(), key=lambda k: parse_date_key(k) or datetime.min):
        old_l = old.get(key, [])
        new_l = new.get(key, [])

        old_s = set(lesson_to_str(x) for x in old_l)
        new_s = set(lesson_to_str(x) for x in new_l)

        if old_s == new_s:
            continue

        if not old_l and new_l:
            if only_new and len(changes) == 0:
                changes.append("📢 Появилось новое расписание!")
                changes.append("")
            else:
                changes.append("📢 " + key + " — новое расписание")
                changes.append("")
            changes.append(format_table_from_lessons(key, new_l))
            changes.append("")
            continue

        only_new = False
        changes.append("📢 Изменения на " + key + ":")
        changes.append("")
        changes.append(format_table_from_lessons(key, new_l))
        changes.append("")
    return changes, only_new


def check_schedule_changes():
    global schedule_snapshot
    logger.info("Проверка расписания на изменения...")
    new_snap = build_new_snapshot()
    if not schedule_snapshot:
        schedule_snapshot = new_snap
        save_cache()
        logger.info("Первый слепок сохранён: " + str(len(new_snap)) + " дней.")
        return
    changes, only_new = diff_snapshots(schedule_snapshot, new_snap)
    schedule_snapshot = new_snap
    save_cache()
    if not changes:
        logger.info("Изменений нет.")
        return
    logger.info("Найдено изменений: " + str(len(changes)) + " строк. Отправляем...")
    if only_new:
        msg = "\n".join(changes)
    else:
        msg = format_change_message(changes)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    try:
        asyncio.run_coroutine_threadsafe(bot.send_message(ADMIN_CHAT_ID, msg, disable_web_page_preview=True), loop)
        logger.info("Уведомление отправлено.")
    except Exception as e:
        logger.error("Ошибка отправки: " + str(e))


def format_single_table(title, lessons):
    lines = ["━━━━━━━━━━━━━━━━━", "📅 Расписание · " + TARGET_GROUP]
    ds = ""
    if title:
        m = re.search(r"НА\s+(.+?года\s*\([^)]+\))", title, re.IGNORECASE)
        ds = m.group(1).strip() if m else title.strip()
    if ds:
        lines.append("📆 " + ds)
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("")
    for ti, content in lessons:
        t = re.sub(r"\s+", " ", ti).strip().replace(".", ":")
        t = re.sub(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", r"\1 – \2", t)
        lines.append("🔵 " + t)
        parts = [p.strip() for p in content.split(" / ") if p.strip()]
        for i, part in enumerate(parts):
            if i == 0:
                lines.append("   📚 " + part)
            elif i == len(parts) - 1 and len(parts) > 1:
                lines.append("   🚪 " + part)
            else:
                lines.append("   👤 " + part)
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_multiple_tables(tables):
    msg = "\n\n".join(format_single_table(t["title"], t["lessons"]) for t in tables)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    return msg


def format_distance_message():
    return "🎓 Дистанционное обучение\n\n📚 Материалы для группы " + TARGET_GROUP + " (" + TARGET_COURSE + ")\n\n🔗 " + CLOUD_URL + "\n\nЧто делать:\n1. Нажмите «" + TARGET_COURSE + "»\n2. Затем «" + TARGET_GROUP + "»\n3. Внутри — файлы и задания"


def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Календарь", callback_data="open_calendar")],
        [InlineKeyboardButton(text="📆 Сегодня", callback_data="quick:today"), InlineKeyboardButton(text="📆 Завтра", callback_data="quick:tomorrow")],
        [InlineKeyboardButton(text="🎓 Дистанционное обучение", callback_data="distance")],
    ])


def get_distance_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")]])


def get_months_keyboard(months, page=1):
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    start = (page - 1) * MONTHS_PER_PAGE
    pm = months[start:start + MONTHS_PER_PAGE]
    rows = []
    cur = MONTHS_NOM_RU[datetime.now().month]
    row = []
    for name, _ in pm:
        prefix = "🔵 " if name == cur else ""
        row.append(InlineKeyboardButton(text=prefix + name, callback_data="month:" + name))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅ Месяцы", callback_data="months_page:" + str(page - 1)))
    if page < total:
        nav.append(InlineKeyboardButton(text="➡ Месяцы", callback_data="months_page:" + str(page + 1)))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_ranges_keyboard(month_name, ranges):
    rows = []
    for label, _ in ranges:
        rows.append([InlineKeyboardButton(text="📅 " + label + " " + month_name, callback_data="range:" + month_name + "|" + label)])
    rows.append([InlineKeyboardButton(text="⬅ К месяцам", callback_data="open_calendar")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_days_keyboard(month_name, range_label, dates, page=1):
    total = (len(dates) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    start = (page - 1) * DATES_PER_PAGE
    pd = dates[start:start + DATES_PER_PAGE]
    rows = []
    for d in pd:
        rows.append([InlineKeyboardButton(text=d, callback_data="date:" + d)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅ Назад", callback_data="page:" + month_name + "|" + range_label + "|" + str(page - 1)))
    if page < total:
        nav.append(InlineKeyboardButton(text="➡ Дальше", callback_data="page:" + month_name + "|" + range_label + "|" + str(page + 1)))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📅 Диапазоны " + month_name, callback_data="back_ranges:" + month_name)])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer("👋 Привет! Я слежу за расписанием группы " + TARGET_GROUP + ".", reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery):
    await callback.message.edit_text("👋 Главное меню. Группа " + TARGET_GROUP + ".", reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "open_calendar")
async def open_calendar_cb(callback: CallbackQuery):
    months = await asyncio.to_thread(get_months_with_dates)
    if not months:
        await callback.message.edit_text("📭 Нет данных с сайта.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    await callback.message.edit_text("📅 Выберите месяц (страница 1 из " + str(total) + "):", reply_markup=get_months_keyboard(months, page=1))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("months_page:"))
async def months_page_cb(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    months = await asyncio.to_thread(get_months_with_dates)
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    await callback.message.edit_text("📅 Выберите месяц (страница " + str(page) + " из " + str(total) + "):", reply_markup=get_months_keyboard(months, page=page))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("month:"))
async def month_cb(callback: CallbackQuery):
    mn = callback.data.split(":", 1)[1]
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    if not isinstance(dates, list) or not dates:
        await callback.message.edit_text("📭 В " + mn + " нет расписания.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    ranges = get_available_ranges(mn, dates)
    await callback.message.edit_text("📅 " + mn + ". Выберите диапазон:", reply_markup=get_ranges_keyboard(mn, ranges))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("range:"))
async def range_cb(callback: CallbackQuery):
    payload = callback.data.split(":", 1)[1]
    mn, rl = payload.split("|", 1)
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    filtered = filter_by_range_label(dates, rl)
    if not filtered:
        await callback.message.edit_text("📭 В этом диапазоне нет дат.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    total = (len(filtered) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    await callback.message.edit_text("📅 " + mn + ", " + rl + " — страница 1 из " + str(total) + ".\nВыберите день:", reply_markup=get_days_keyboard(mn, rl, filtered, page=1))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("page:"))
async def page_cb(callback: CallbackQuery):
    payload = callback.data.split(":", 1)[1]
    mn, rl, page = payload.split("|")
    page = int(page)
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    filtered = filter_by_range_label(dates, rl)
    total = (len(filtered) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    await callback.message.edit_text("📅 " + mn + ", " + rl + " — страница " + str(page) + " из " + str(total) + ".\nВыберите день:", reply_markup=get_days_keyboard(mn, rl, filtered, page=page))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("back_ranges:"))
async def back_ranges_cb(callback: CallbackQuery):
    mn = callback.data.split(":", 1)[1]
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    ranges = get_available_ranges(mn, dates)
    await callback.message.edit_text("📅 " + mn + ". Выберите диапазон:", reply_markup=get_ranges_keyboard(mn, ranges))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("date:"))
async def date_cb(callback: CallbackQuery):
    dt = callback.data.split(":", 1)[1]
    await callback.answer("Загружаю…")
    tables = await asyncio.to_thread(parse_schedule_for_date_text, dt)
    if not tables:
        await callback.message.answer("📭 На «" + dt + "» нет расписания.", reply_markup=get_main_keyboard())
        return
    await callback.message.answer(format_multiple_tables(tables), reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == "quick:today")
async def today_cb(callback: CallbackQuery):
    target = datetime.now()
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await callback.message.answer("📭 На сегодня нет расписания.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    await callback.message.answer(format_multiple_tables(tables), reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "quick:tomorrow")
async def tomorrow_cb(callback: CallbackQuery):
    target = datetime.now() + timedelta(days=1)
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await callback.message.answer("📭 На завтра нет расписания.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    await callback.message.answer(format_multiple_tables(tables), reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "distance")
async def distance_cb(callback: CallbackQuery):
    await callback.message.edit_text(format_distance_message(), reply_markup=get_distance_keyboard())
    await callback.answer()


async def scheduler_loop():
    while True:
        try:
            await asyncio.to_thread(check_schedule_changes)
        except Exception as e:
            logger.error("Ошибка проверки расписания: " + str(e))
        try:
            await asyncio.to_thread(check_cloud_and_notify)
        except Exception as e:
            logger.error("Ошибка проверки облака: " + str(e))
        await asyncio.sleep(CHECK_INTERVAL)


async def main():
    global loop
    loop = asyncio.get_running_loop()
    load_cache()
    asyncio.create_task(scheduler_loop())
    logger.info("TG-бот запущен. Группа " + TARGET_GROUP + ".")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
