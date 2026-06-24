"""Конфигурация shekel-bot.

ПЕРЕНОСИМОСТЬ: все настройки и секреты бот берёт из локального файла `.env`
РЯДОМ с папкой бота (BASE_DIR/.env) — он едет вместе с папкой, поэтому проект
самодостаточен: скопировал папку на другой ПК → работает. Системное хранилище
(creds.txt) — лишь fallback на «родном» ПК, если .env чего-то не содержит.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_dotenv(path):
    """Простой загрузчик .env (без сторонней зависимости). Значения из реального
    окружения имеют приоритет (setdefault не перезатирает)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)
    except FileNotFoundError:
        pass


_load_dotenv(BASE_DIR / ".env")

DB_PATH = os.environ.get("SHEKEL_DB", str(DATA_DIR / "shekel.db"))

# --- секреты: сперва .env/окружение, потом vault родного ПК (fallback) ---
VAULT_PATH = os.environ.get("VAULT_PATH", "")  # set per-machine; no default path published


def vault_secret(name):
    # 1) окружение/.env по тому же имени ключа (переносимо, едет с папкой)
    env = os.environ.get(name)
    if env:
        return env
    # 2) vault родного ПК (fallback; на чужом ПК файла нет — вернём "")
    try:
        with open(VAULT_PATH, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.strip().startswith(name + "="):
                    return ln.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


# --- Telegram ---
TG_TOKEN = os.environ.get("TG_TOKEN") or vault_secret("shekel_bot_tg_token")
_ids = os.environ.get("TG_ALLOWED_IDS", "")
TG_ALLOWED_IDS = [int(x) for x in _ids.split(",") if x.strip()]  # set TG_ALLOWED_IDS in .env

# Прокси для api.telegram.org.
# TG_PROXY  — адрес прокси (пусто = всегда напрямую).
# TG_PROXY_AUTO=1 — авто-режим: если Telegram доступен НАПРЯМУЮ —
#   идём напрямую, прокси игнорируем; если прямой доступ заблокирован — через
#   TG_PROXY. Так одна и та же папка работает и с прокси, и без него
#   (напрямую) без правок. При необходимости впиши в .env свой прокси
#   для сети, где Telegram блокируется.
TG_PROXY = os.environ.get("TG_PROXY", "")
TG_PROXY_AUTO = os.environ.get("TG_PROXY_AUTO", "1").strip().lower() not in ("0", "false", "no", "off", "")

# --- OpenAI ---
OPENAI_KEY = os.environ.get("OPENAI_API_KEY") or vault_secret("shekel_bot_openai_key")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")            # рутина (парсинг/новости)
OPENAI_MODEL_SMART = os.environ.get("OPENAI_MODEL_SMART", "gpt-4o-mini")  # важный анализ (можно усилить)

# --- Supabase Postgres (session pooler; порты 5432/6543 доступны с РФ; прямой хост IPv6-only) ---
SUPABASE_REF = os.environ.get("SUPABASE_REF", "your-project-ref")
SUPABASE_REGION = os.environ.get("SUPABASE_REGION", "eu-north-1")
SUPABASE_DB_HOST = os.environ.get("SUPABASE_DB_HOST", "aws-1-" + SUPABASE_REGION + ".pooler.supabase.com")
SUPABASE_DB_PORT = int(os.environ.get("SUPABASE_DB_PORT", "6543"))


def supabase_dsn():
    pw = os.environ.get("SUPABASE_DB_PASS") or vault_secret("shekel_bot_supabase_db_pass")
    return ("host={h} port={p} dbname=postgres user=postgres.{ref} password={pw} sslmode=require"
            .format(h=SUPABASE_DB_HOST, p=SUPABASE_DB_PORT, ref=SUPABASE_REF, pw=pw))

# --- Google Sheet ---
GSHEET_ID = os.environ.get("GSHEET_ID", "YOUR_GOOGLE_SHEET_ID")
GSHEET_TAB = os.environ.get("GSHEET_TAB", "Лист1")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", str(BASE_DIR / "secrets" / "google.json"))

# --- Горизонты прогноза (в торговых днях) ---
HORIZONS = {"1 неделя": 5, "2 недели": 10, "1 месяц": 21}

# --- ИИ-прогноз: новости (RSS, бесплатно, без ключа) + модель ---
# Лента: макроэкономика, нефть, экономика РФ/ЦБ. Переопределяется NEWS_FEEDS в .env
# (через запятую). Битые/недоступные ленты бот молча пропускает.
NEWS_FEEDS = [s.strip() for s in os.environ.get("NEWS_FEEDS", "").split(",") if s.strip()] or [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",        # мировые рынки (англ)
    "https://oilprice.com/rss/main",              # нефть/энергетика
    "https://rssexport.rbc.ru/rbcnews/economics/index.rss",  # экономика РФ
    "https://www.cbr.ru/rss/eventrss",            # события ЦБ РФ (ставка и т.п.)
]
NEWS_MAX_AGE_DAYS = int(os.environ.get("NEWS_MAX_AGE_DAYS", "7"))
NEWS_MAX_ITEMS = int(os.environ.get("NEWS_MAX_ITEMS", "35"))
# Модель для ИИ-прогноза (рассуждение по новостям). Можно усилить через .env,
# напр. AI_FORECAST_MODEL=gpt-4o (дороже, но сильнее в анализе).
AI_FORECAST_MODEL = os.environ.get("AI_FORECAST_MODEL", OPENAI_MODEL_SMART)

# --- Целевая доля шекелей в портфеле (для инвентарной рекомендации) ---
TARGET_SHEKEL_SHARE = 0.50

# --- стартовые балансы леджера (задать под свою таблицу) ---
LEDGER_START_ILS = 0.0
LEDGER_START_RUB = 0.0

# --- Рыночные допущения для bias (обновлять; дата фиксируется) ---
ASSUMPTIONS = {
    "as_of": "2026-05-30",
    "cbr_key_rate": 14.5,                # ключевая ставка ЦБ РФ, %
    "ils_cb_rate": 3.75,                    # ставка ЦБ шекелевой стороны, %
    "usdrub_yearend_consensus": 90.0,    # консенсус аналитиков на конец 2026
    "brent_usd": 64.0,                   # консенсус Brent 2026, $/барр
}

# --- Веса сигналов bias (тюнятся; сумма модулей = 1) ---
BIAS_WEIGHTS = {
    "rub_reversion": 0.30,   # рубль крепче среднего -> склонен слабеть -> ILS/RUB вверх
    "ils_reversion": 0.25,   # шекель крепче среднего -> склонен слабеть -> ILS/RUB вниз
    "momentum": 0.20,        # недавний тренд кросса
    "carry": 0.15,           # дифференциал ставок (высокая ставка ЦБ держит рубль)
    "seasonality": 0.10,     # сезонность рубля
}

# Сезонные тилты по месяцам для ILS/RUB (>0 = склонность к росту кросса = рубль слабее).
# H2 и декабрь — традиционно слабее рубль; весна (налоговый период) — крепче.
SEASONALITY = {1: 0.1, 2: 0.0, 3: -0.1, 4: -0.2, 5: -0.1, 6: 0.1,
               7: 0.1, 8: 0.2, 9: 0.2, 10: 0.1, 11: 0.2, 12: 0.3}
