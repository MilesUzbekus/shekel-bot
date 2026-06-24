"""Источники курса.

Текущий курс ILS/RUB:
  - ПЕРВЫЙ ИСТОЧНИК — Google Finance (прямой ILS→RUB). Это ровно тот курс,
    который бизнес записывает как «гугл» в сделках; на него и надо равняться.
  - FALLBACK — рыночный кросс из ОДНОГО источника open.er-api: RUB/ILS
    (обе ноги из одного места, без рассинхрона). Если и он недоступен —
    USD/RUB ЦБ РФ ÷ USD/ILS Frankfurter.
  ВАЖНО: раньше кросс считался как ЦБ-USD/RUB ÷ open.er-api-USD/ILS — это
  смешивало ОФИЦИАЛЬНЫЙ (запаздывающий на 1–3 дня) рублёвый курс с РЫНОЧНЫМ
  шекелевым и давало заниженный гибрид (~−1% к Google). Больше так не делаем.
Историю (для волатильности и трендов):
  - USD/RUB  — ЦБ РФ XML_dynamic (единственный бесплатный источник с глубокой
    историей рубля; ECB рубль не публикует с 2022).
  - USD/ILS  — Frankfurter (ECB reference rates, без ключа)
"""
import datetime as dt
import re
import xml.etree.ElementTree as ET

import requests

from src import storage

CBR_DAILY = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_DYNAMIC = "https://www.cbr.ru/scripts/XML_dynamic.asp"
CBR_USD_CODE = "R01235"
OPEN_ER = "https://open.er-api.com/v6/latest/USD"
FRANKFURTER = "https://api.frankfurter.app"
GOOGLE_FIN = "https://www.google.com/finance/quote/{}-{}"

UA = {"User-Agent": "shekel-bot/0.1 (personal fx tracker)"}
# Google Finance отдаёт цену только «браузерному» UA; форсим en-US, чтобы
# десятичный разделитель был точкой, а не запятой.
BROWSER_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _num(s):
    """'71,0224' -> 71.0224 (ЦБ использует запятую и неразрывные пробелы)."""
    return float(s.replace("\xa0", "").replace(" ", "").replace(",", ".").strip())


def fetch_fx_google(base, quote):
    """Прямой курс base→quote как показывает Google (Google Finance).
    Возвращает float. Бросает исключение, если цену не удалось извлечь.

    На странице много span'ов jsname="Pdsbrc" (таблица «похожих» пар, ленты
    валют). ГЛАВНАЯ котировка — единственная, у которой сразу за ценой идёт
    блок изменения <div class="DAicsd"> (класс пустой, без SpkPOc). На него и
    якоримся, иначе хватается чужая пара (1124.78 и т.п.)."""
    url = GOOGLE_FIN.format(base.upper(), quote.upper())
    r = requests.get(url, headers=BROWSER_UA, timeout=20)
    r.raise_for_status()
    m = re.search(
        r'jsname="Pdsbrc" class=""><span>([0-9][0-9,]*\.?[0-9]*)</span></span></div><div class="DAicsd"',
        r.text)
    if not m:
        raise RuntimeError("Google Finance: главная котировка не найдена в разметке")
    return float(m.group(1).replace(",", ""))


def fetch_usd_ils_frankfurter():
    """Последний USD/ILS из Frankfurter (ECB) — fallback к open.er-api."""
    r = requests.get(f"{FRANKFURTER}/latest", params={"from": "USD", "to": "ILS"},
                     headers=UA, timeout=20).json()
    return float(r["rates"]["ILS"])


# ---------- текущий курс ----------

def fetch_usd_rub_cbr():
    r = requests.get(CBR_DAILY, headers=UA, timeout=20)
    r.encoding = "windows-1251"
    root = ET.fromstring(r.text)
    for v in root.findall("Valute"):
        if v.findtext("CharCode") == "USD":
            return _num(v.findtext("Value")) / int(v.findtext("Nominal"))
    raise RuntimeError("USD не найден в дневном XML ЦБ")


def fetch_open_er():
    """Возвращает (usd_rub, usd_ils) из бесплатного open.er-api."""
    r = requests.get(OPEN_ER, headers=UA, timeout=20).json()
    rates = r["rates"]
    return float(rates["RUB"]), float(rates["ILS"])


def current_snapshot():
    """Снимок курса на сейчас.

    ils_rub — главное число, показываемое пользователю; берём ПРЯМОЙ курс
    Google (как «гугл» в сделках). Если Google недоступен — рыночный кросс
    из одного источника (open.er-api RUB/ILS), затем ЦБ÷Frankfurter.
    usd_rub/usd_ils — рыночные ноги для разбивки и для fallback-кросса.
    """
    # --- рыночные USD-ноги (один источник, без рассинхрона) ---
    usd_rub = usd_ils = None
    leg_src = None
    try:
        usd_rub, usd_ils = fetch_open_er()      # RUB и ILS из одного места
        leg_src = "open.er-api"
    except Exception:
        pass
    if usd_ils is None:
        try:
            usd_ils = fetch_usd_ils_frankfurter()
        except Exception:
            pass
    if usd_rub is None:
        try:
            usd_rub = fetch_usd_rub_cbr()       # официальный, запаздывает — только как крайний fallback
        except Exception:
            pass

    market_cross = (usd_rub / usd_ils) if (usd_rub and usd_ils) else None

    # --- главный курс: прямой Google, иначе рыночный кросс ---
    src = []
    ils_rub = None
    try:
        g = fetch_fx_google("ILS", "RUB")
        # защита от «битого» парсинга: если Google резко расходится с рыночным
        # кроссом (>8%), значит схватили не ту пару — не доверяем, идём в кросс.
        if market_cross and abs(g / market_cross - 1) > 0.08:
            raise RuntimeError(f"Google {g:.3f} расходится с рынком {market_cross:.3f}")
        ils_rub = g
        src.append("Google")
    except Exception:
        pass
    if ils_rub is None:
        ils_rub = market_cross
        if ils_rub is not None and leg_src:
            src.append(leg_src + " (кросс)")
    if ils_rub is None:
        raise RuntimeError("ни один источник курса недоступен")

    # Разбивку USD держим внутренне согласованной с показанным кроссом:
    # при наличии рыночного USD/ILS подгоняем USD/RUB под фактический ils_rub.
    if usd_ils:
        usd_rub = ils_rub * usd_ils
        if leg_src and leg_src not in " ".join(src):
            src.append(leg_src)
    return {
        "date": dt.date.today().isoformat(),
        "usd_rub": usd_rub,
        "usd_ils": usd_ils,
        "ils_rub": ils_rub,
        "source": " + ".join(src) if src else "n/a",
    }


# ---------- историческая подгрузка ----------

def fetch_usd_rub_history_cbr(date_from, date_to):
    p = {
        "date_req1": date_from.strftime("%d/%m/%Y"),
        "date_req2": date_to.strftime("%d/%m/%Y"),
        "VAL_NM_RQ": CBR_USD_CODE,
    }
    r = requests.get(CBR_DYNAMIC, params=p, headers=UA, timeout=60)
    r.encoding = "windows-1251"
    root = ET.fromstring(r.text)
    out = {}
    for rec in root.findall("Record"):
        d = dt.datetime.strptime(rec.get("Date"), "%d.%m.%Y").date()
        out[d.isoformat()] = _num(rec.findtext("Value")) / int(rec.findtext("Nominal"))
    return out


def fetch_usd_ils_history_frankfurter(date_from, date_to):
    url = f"{FRANKFURTER}/{date_from.isoformat()}..{date_to.isoformat()}"
    r = requests.get(url, params={"from": "USD", "to": "ILS"}, headers=UA, timeout=60).json()
    return {d: float(v["ILS"]) for d, v in r.get("rates", {}).items()}


def backfill(days=540):
    """Подтягивает ~1.5 года истории, считает кросс по общим датам, кладёт в БД."""
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    rub = fetch_usd_rub_history_cbr(start, today)
    ils = fetch_usd_ils_history_frankfurter(start, today)
    common = sorted(set(rub) & set(ils))
    now = dt.datetime.now().isoformat(timespec="seconds")
    for d in common:
        storage.upsert_rate(d, rub[d], ils[d], rub[d] / ils[d], "backfill:ЦБ+ECB", now)
    return {"rub_points": len(rub), "ils_points": len(ils), "cross_points": len(common)}
