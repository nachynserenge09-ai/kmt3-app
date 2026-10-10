import asyncio, hashlib, hmac, json, os, sqlite3, time, urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "kmt.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")        # токен от BotFather
GAS_URL = os.getenv("GAS_URL", "")            # адрес расписания (Google Apps Script)
DEMO = os.getenv("DEMO", "0") == "1"          # 1 = выдавать демо-оценки
REFRESH_SECONDS = 30 * 60                     # как часто обновлять расписание

# Расписание звонков. ЗАМЕНИ на реальное время вашего колледжа.
BELLS = [
    {"n": 1, "start": "08:30", "end": "10:00"},
    {"n": 2, "start": "10:10", "end": "11:40"},
    {"n": 3, "start": "12:10", "end": "13:40"},
    {"n": 4, "start": "13:50", "end": "15:20"},
    {"n": 5, "start": "15:30", "end": "17:00"},
]

# ---------- расписание с сайта колледжа ----------
SCHEDULE = {"updated": None, "dates": [], "groups": [], "data": {}}

def parse_schedule(raw):
    """JSON сайта -> {dates, groups, data[дата][группа] = [пары]}.
    Берём только листы с заполненной датой, как и сам сайт."""
    dates, data, groups = [], {}, set()
    for sheet in raw:
        date = str(sheet.get("дата") or "").strip()
        if not date:
            continue
        try:
            iso = datetime.strptime(date, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        by_group = {}
        for row in sheet.get("данные", []):
            group = str(row.get("Группа") or "").strip()
            if not group:
                continue
            lessons = []
            for i in range(1, 6):
                title = str(row.get(f"{i} пара") or "").strip()
                room = str(row.get(f"ауд{i}") or "").strip()
                if not title or title.lower().startswith("нет занят"):
                    continue
                if room.lower() == "нет":
                    room = ""
                lessons.append({"num": i, "title": title, "room": room})
            by_group[group] = lessons
            groups.add(group)
        data[date] = by_group
        dates.append({"date": date, "iso": iso, "weekday": str(sheet.get("лист") or "")})
    dates.sort(key=lambda d: d["iso"])
    return {"dates": dates, "data": data, "groups": sorted(groups)}

def fetch_schedule():
    req = urllib.request.Request(GAS_URL, headers={"User-Agent": "kmt-miniapp"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = json.loads(r.read().decode("utf-8"))
    SCHEDULE.update(parse_schedule(raw))
    SCHEDULE["updated"] = int(time.time())

async def refresher():
    while True:
        try:
            if GAS_URL:
                await asyncio.to_thread(fetch_schedule)
            else:
                print("GAS_URL не задан: расписание не загружается")
        except Exception as e:
            print("Не удалось обновить расписание:", e)
        await asyncio.sleep(REFRESH_SECONDS if SCHEDULE["dates"] else 60)

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(refresher())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# ---------- база данных (студенты и оценки) ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS students(
        telegram_id INTEGER PRIMARY KEY, name TEXT, group_name TEXT);
    CREATE TABLE IF NOT EXISTS grades(
        id INTEGER PRIMARY KEY, telegram_id INTEGER, subject TEXT,
        grade INTEGER, date TEXT);
    """)
    con.commit(); con.close()

init_db()

# ---------- проверка подписи Telegram ----------
def get_user(init_data: str) -> dict:
    """Возвращает данные пользователя из initData или кидает 401."""
    if not BOT_TOKEN:  # режим разработки: без токена проверки нет
        return {"id": 1, "first_name": "Тест"}
    try:
        data = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            raise ValueError("bad hash")
        if time.time() - int(data["auth_date"]) > 86400:
            raise ValueError("expired")
        return json.loads(data["user"])
    except Exception:
        raise HTTPException(401, "Неверные данные Telegram")

# ---------- API ----------
@app.get("/api/me")
def me(x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    con = db()
    s = con.execute("SELECT * FROM students WHERE telegram_id=?", (user["id"],)).fetchone()
    con.close()
    return {"registered": bool(s), "name": s["name"] if s else user.get("first_name", ""),
            "group": s["group_name"] if s else None, "groups": SCHEDULE["groups"]}

class Reg(BaseModel):
    name: str
    group: str

DEMO_SUBJECTS = ["Математика", "Информатика", "Русский язык", "История"]

@app.post("/api/register")
def register(r: Reg, x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    if r.group not in SCHEDULE["groups"]:
        raise HTTPException(400, "Такой группы нет в расписании")
    con = db()
    con.execute("INSERT OR REPLACE INTO students VALUES(?,?,?)",
                (user["id"], r.name.strip()[:60], r.group))
    if DEMO and not con.execute("SELECT 1 FROM grades WHERE telegram_id=?", (user["id"],)).fetchone():
        vals = [(user["id"], sub, g, f"2026-10-0{i+1}")
                for sub in DEMO_SUBJECTS for i, g in enumerate([5, 4, 4])]
        con.executemany("INSERT INTO grades(telegram_id,subject,grade,date) VALUES(?,?,?,?)", vals)
    con.commit(); con.close()
    return {"ok": True}

@app.get("/api/schedule")
def schedule(x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    con = db()
    s = con.execute("SELECT group_name FROM students WHERE telegram_id=?", (user["id"],)).fetchone()
    con.close()
    if not s:
        raise HTTPException(404, "Сначала зарегистрируйтесь")
    g = s["group_name"]
    dates = [{**d, "lessons": SCHEDULE["data"].get(d["date"], {}).get(g, [])} for d in SCHEDULE["dates"]]
    return {"dates": dates, "bells": BELLS, "updated": SCHEDULE["updated"]}

@app.get("/api/grades")
def grades(x_init_data: str = Header(default="")):
    user = get_user(x_init_data)
    con = db()
    rows = con.execute("SELECT subject,grade,date FROM grades WHERE telegram_id=? ORDER BY date",
                       (user["id"],)).fetchall()
    con.close()
    by = {}
    for r in rows:
        by.setdefault(r["subject"], []).append(r["grade"])
    items = [{"subject": k, "grades": v, "avg": round(sum(v) / len(v), 2)} for k, v in by.items()]
    allg = [g for v in by.values() for g in v]
    return {"items": items, "gpa": round(sum(allg) / len(allg), 2) if allg else None}

@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")
