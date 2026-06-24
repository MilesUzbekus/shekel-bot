"""Локальный SQLite: курсы (для прогноза), прогнозы, доверенные пользователи с ролями.

Леджер сделок живёт в Google-таблице (src/sheets.py). Здесь — только то, что
не должно зависеть от сети: история курсов и список доступа. Надёжно, без пулера.
"""
import sqlite3
import datetime
from contextlib import contextmanager

from src import config


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS rates (
    date TEXT PRIMARY KEY,
    usd_rub REAL, usd_ils REAL, ils_rub REAL,
    source TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, horizon TEXT, spot REAL,
    low68 REAL, high68 REAL, low95 REAL, high95 REAL,
    p_up REAL, bias REAL, recommendation TEXT, rationale TEXT
);
CREATE TABLE IF NOT EXISTS trusted_users (
    chat_id INTEGER PRIMARY KEY,
    name TEXT, role TEXT, added_at TEXT
);
CREATE TABLE IF NOT EXISTS deleted_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT, qty REAL, my_rate REAL, google_rate REAL,
    date TEXT, client TEXT, deleted_at TEXT, restored INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, made_date TEXT, spot REAL,
    dir_1w TEXT, dir_1m TEXT, confidence TEXT,
    drivers TEXT, scenarios TEXT, rationale TEXT, news_digest TEXT,
    spot_1w REAL, hit_1w INTEGER, spot_1m REAL, hit_1m INTEGER, evaluated_at TEXT
);
"""


def init():
    with db() as c:
        c.executescript(SCHEMA)
        try:
            c.execute("ALTER TABLE trusted_users ADD COLUMN role TEXT")
        except sqlite3.OperationalError:
            pass


# ---------- курсы ----------

def upsert_rate(date, usd_rub, usd_ils, ils_rub, source, fetched_at=None):
    fetched_at = fetched_at or datetime.datetime.now().isoformat(timespec="seconds")
    with db() as c:
        c.execute(
            "INSERT INTO rates(date,usd_rub,usd_ils,ils_rub,source,fetched_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
            "usd_rub=excluded.usd_rub, usd_ils=excluded.usd_ils, ils_rub=excluded.ils_rub, "
            "source=excluded.source, fetched_at=excluded.fetched_at",
            (date, usd_rub, usd_ils, ils_rub, source, fetched_at))


def get_series(limit=540):
    with db() as c:
        rows = c.execute(
            "SELECT date,usd_rub,usd_ils,ils_rub FROM rates WHERE ils_rub IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def insert_forecast(ts, horizon, spot, cc, bias, recommendation, rationale):
    with db() as c:
        c.execute(
            "INSERT INTO forecasts(ts,horizon,spot,low68,high68,low95,high95,p_up,bias,"
            "recommendation,rationale) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, horizon, spot, cc["low68"], cc["high68"], cc["low95"], cc["high95"],
             cc["p_up"], bias, recommendation, rationale))


# ---------- корзина удалённых строк (для восстановления) ----------

def push_deleted(direction, qty, my_rate, google_rate, date, client):
    """Сохранить удалённую строку, чтобы её можно было восстановить. Возвращает id."""
    with db() as c:
        cur = c.execute(
            "INSERT INTO deleted_ops(direction,qty,my_rate,google_rate,date,client,deleted_at,restored) "
            "VALUES(?,?,?,?,?,?,?,0)",
            (direction, qty, my_rate, google_rate, date, client,
             datetime.datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def peek_deleted():
    """Самая свежая ещё не восстановленная удалённая строка (или None)."""
    with db() as c:
        r = c.execute(
            "SELECT id,direction,qty,my_rate,google_rate,date,client,deleted_at "
            "FROM deleted_ops WHERE restored=0 ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def mark_restored(del_id):
    with db() as c:
        c.execute("UPDATE deleted_ops SET restored=1 WHERE id=?", (del_id,))


# ---------- ИИ-прогнозы + их сверка с фактом (обучение на результатах) ----------

def save_ai_forecast(ts, made_date, spot, dir_1w, dir_1m, confidence,
                     drivers, scenarios, rationale, news_digest):
    with db() as c:
        cur = c.execute(
            "INSERT INTO ai_forecasts(ts,made_date,spot,dir_1w,dir_1m,confidence,"
            "drivers,scenarios,rationale,news_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, made_date, spot, dir_1w, dir_1m, confidence,
             drivers, scenarios, rationale, news_digest))
        return cur.lastrowid


def recent_ai_forecasts(limit=20):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM ai_forecasts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def ai_forecasts_unevaluated():
    """Прогнозы, у которых ещё не проставлен факт хотя бы по одному горизонту."""
    with db() as c:
        rows = c.execute(
            "SELECT * FROM ai_forecasts WHERE hit_1w IS NULL OR hit_1m IS NULL "
            "ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def set_ai_outcome(fid, spot_1w=None, hit_1w=None, spot_1m=None, hit_1m=None, evaluated_at=None):
    sets, args = [], []
    if spot_1w is not None:
        sets += ["spot_1w=?", "hit_1w=?"]; args += [spot_1w, hit_1w]
    if spot_1m is not None:
        sets += ["spot_1m=?", "hit_1m=?"]; args += [spot_1m, hit_1m]
    if not sets:
        return
    sets.append("evaluated_at=?")
    args.append(evaluated_at or datetime.datetime.now().isoformat(timespec="seconds"))
    args.append(fid)
    with db() as c:
        c.execute(f"UPDATE ai_forecasts SET {', '.join(sets)} WHERE id=?", args)


def ai_track_record():
    """Итоговая статистика попаданий прогноза (для подачи ИИ обратно)."""
    with db() as c:
        r = c.execute(
            "SELECT "
            "SUM(hit_1w=1) h1w, SUM(hit_1w=0) m1w, "
            "SUM(hit_1m=1) h1m, SUM(hit_1m=0) m1m, COUNT(*) n "
            "FROM ai_forecasts").fetchone()
    return {"h1w": r["h1w"] or 0, "m1w": r["m1w"] or 0,
            "h1m": r["h1m"] or 0, "m1m": r["m1m"] or 0, "n": r["n"] or 0}


# ---------- доступ (роли: owner вне БД через ALLOWED; здесь editor/viewer) ----------

def get_role(chat_id):
    with db() as c:
        r = c.execute("SELECT role FROM trusted_users WHERE chat_id=?", (chat_id,)).fetchone()
    return (r["role"] or "editor") if r else None


def is_trusted(chat_id):
    return get_role(chat_id) is not None


def add_trusted(chat_id, name=None, role="editor"):
    with db() as c:
        c.execute(
            "INSERT INTO trusted_users(chat_id,name,role,added_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET role=excluded.role, name=excluded.name",
            (chat_id, name, role, datetime.datetime.now().isoformat(timespec="seconds")))


def list_trusted():
    with db() as c:
        rows = c.execute("SELECT chat_id,name,role,added_at FROM trusted_users ORDER BY added_at").fetchall()
    return [dict(r) for r in rows]


def remove_trusted(chat_id):
    with db() as c:
        cur = c.execute("DELETE FROM trusted_users WHERE chat_id=?", (chat_id,))
        return cur.rowcount
