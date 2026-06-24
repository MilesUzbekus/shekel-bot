"""ИИ-прогноз ILS/RUB.

НЕ скрипт: нейросеть (OpenAI) рассуждает по ДИНАМИКЕ курса + СВЕЖИМ НОВОСТЯМ
(макроэкономика, нефть, ставки ЦБ, санкции) и даёт направление/сценарии на 1нед и 1мес.
Числовой коридор берём от волатильности (чтобы ИИ не выдумывал точные цифры).
«Обучение» — честное, на фактах: каждый прогноз сохраняется и потом сверяется с
тем, что реально произошло (evaluate_due); накопленный track-record подаётся ИИ
обратно, чтобы он калибровался на собственных промахах.
"""
import json
import datetime as dt

from src import config, storage, analytics, llm, news


def _pct(a, b):
    return (a / b - 1) * 100 if (a and b) else 0.0


def dynamics(rows):
    cross = [r["ils_rub"] for r in rows if r.get("ils_rub")]
    if not cross:
        return {"spot": 0, "chg_1w": 0, "chg_1m": 0, "chg_3m": 0,
                "vol_annual_pct": 0, "lo60": 0, "hi60": 0, "n": 0}
    spot = cross[-1]

    def back(n):
        return cross[-1 - n] if len(cross) > n else cross[0]

    sigma_d = analytics.ewma_vol(analytics.log_returns(cross))
    win = cross[-60:] if len(cross) >= 60 else cross
    return {
        "spot": spot,
        "chg_1w": _pct(spot, back(5)),
        "chg_1m": _pct(spot, back(21)),
        "chg_3m": _pct(spot, back(63)),
        "vol_annual_pct": sigma_d * (252 ** 0.5) * 100,
        "lo60": min(win), "hi60": max(win), "n": len(cross),
    }


SYS = ("Ты — валютный аналитик пары ILS/RUB (шекель к рублю; рост кросса = шекель "
       "крепчает к рублю). Тебе дают: динамику курса, диапазон по волатильности, "
       "СВЕЖИЕ НОВОСТИ (макроэкономика, нефть, ставки ЦБ, санкции) и track-record твоих прошлых "
       "прогнозов. Дай ОБОСНОВАННЫЙ прогноз на 1 неделю и 1 месяц.\n"
       "Честность: недельный FX близок к случайному блужданию — НЕ притворяйся, что "
       "знаешь точно; если новости нейтральны/скудны — ставь flat и confidence low. "
       "Числа держи в пределах данного коридора волатильности, точные значения не "
       "выдумывай. Опирайся на КОНКРЕТНЫЕ новости из списка. Ответ — СТРОГО валидный "
       "JSON по схеме, без текста вне JSON.")

SCHEMA_HINT = ('{"dir_1w":"up|down|flat","dir_1m":"up|down|flat",'
               '"confidence":"low|medium|high",'
               '"drivers":["конкретный драйвер из новостей/макро","..."],'
               '"scenarios":{"bull":"что нужно для роста кросса","base":"...","bear":"..."},'
               '"rationale":"2-4 предложения почему","watch":["что отслеживать"]}')


def _track_line(tr):
    if not tr["n"] or (tr["h1w"] + tr["m1w"] + tr["h1m"] + tr["m1m"]) == 0:
        return "пока нет проверенных прогнозов"
    parts = []
    if tr["h1w"] + tr["m1w"]:
        parts.append(f"{tr['h1w']}/{tr['h1w'] + tr['m1w']} угадано на 1нед")
    if tr["h1m"] + tr["m1m"]:
        parts.append(f"{tr['h1m']}/{tr['h1m'] + tr['m1m']} на 1мес")
    return ", ".join(parts) if parts else "пока нет проверенных прогнозов"


def build_forecast(snap, rows):
    dyn = dynamics(rows)
    spot = snap["ils_rub"]
    sigma_d = analytics.ewma_vol(analytics.log_returns([r["ils_rub"] for r in rows if r.get("ils_rub")]))
    cones = {label: analytics.cone(spot, sigma_d, days) for label, days in config.HORIZONS.items()}
    items, sources = news.fetch_headlines()
    tr = storage.ai_track_record()
    tr_line = _track_line(tr)

    user = (
        f"ТЕКУЩИЙ КУРС ILS/RUB: {spot:.3f} руб/шек ({snap['date']})\n"
        f"ДИНАМИКА: за неделю {dyn['chg_1w']:+.1f}%, за месяц {dyn['chg_1m']:+.1f}%, "
        f"за 3 мес {dyn['chg_3m']:+.1f}%; годовая волатильность ~{dyn['vol_annual_pct']:.0f}%; "
        f"коридор за 60 дней {dyn['lo60']:.2f}–{dyn['hi60']:.2f}.\n"
        "ДИАПАЗОН ПО ВОЛАТИЛЬНОСТИ (держись внутри):\n"
        + "".join(f"  {h}: 68% [{c['low68']:.2f}–{c['high68']:.2f}], 95% [{c['low95']:.2f}–{c['high95']:.2f}]\n"
                 for h, c in cones.items())
        + f"ТВОЙ TRACK-RECORD: {tr_line}\n\n"
        f"СВЕЖИЕ НОВОСТИ (новые сверху):\n{news.headlines_text(items)}\n\n"
        f"Верни JSON строго по схеме:\n{SCHEMA_HINT}"
    )
    raw = llm.chat([{"role": "system", "content": SYS}, {"role": "user", "content": user}],
                   model=config.AI_FORECAST_MODEL, json_mode=True, max_tokens=900, temperature=0.4)
    try:
        p = json.loads(raw)
    except Exception:
        p = {"dir_1w": "flat", "dir_1m": "flat", "confidence": "low",
             "drivers": [], "scenarios": {}, "rationale": "ИИ вернул неразборчивый ответ.", "watch": []}

    now = dt.datetime.now().isoformat(timespec="seconds")
    storage.save_ai_forecast(
        now, snap["date"], spot, p.get("dir_1w", "flat"), p.get("dir_1m", "flat"),
        p.get("confidence", "low"),
        json.dumps(p.get("drivers", []), ensure_ascii=False),
        json.dumps(p.get("scenarios", {}), ensure_ascii=False),
        (p.get("rationale") or "")[:1500], news.headlines_text(items)[:4000])
    return format_report(snap, dyn, cones, p, sources, tr_line)


_ARROW = {"up": "↑ вверх (шекель крепчает к рублю)",
          "down": "↓ вниз (шекель слабеет к рублю)",
          "flat": "→ вбок (без явного направления)"}


def format_report(snap, dyn, cones, p, sources, tr_line):
    L = [f"ИИ-прогноз ILS/RUB на {snap['date']}",
         f"Курс сейчас: {snap['ils_rub']:.3f} руб/шек  [{snap.get('source', '?')}]",
         f"Динамика: нед {dyn['chg_1w']:+.1f}%, мес {dyn['chg_1m']:+.1f}%, 3мес {dyn['chg_3m']:+.1f}%",
         "",
         f"Прогноз — неделя: {_ARROW.get(p.get('dir_1w'), '→')}",
         f"Прогноз — месяц:  {_ARROW.get(p.get('dir_1m'), '→')}",
         f"Уверенность: {p.get('confidence', 'low')}"]
    if p.get("drivers"):
        L.append("\nКлючевые драйверы (из новостей/макро):")
        L += [f"  • {d}" for d in p["drivers"][:5]]
    sc = p.get("scenarios") or {}
    if sc:
        L.append("\nСценарии:")
        for k, name in (("bull", "Рост"), ("base", "База"), ("bear", "Падение")):
            if sc.get(k):
                L.append(f"  {name}: {sc[k]}")
    if p.get("rationale"):
        L.append(f"\nОбоснование: {p['rationale']}")
    if p.get("watch"):
        L.append("\nСледить за: " + "; ".join(p["watch"][:4]))
    L.append("\nДиапазон по волатильности (честные коридоры, не предсказание):")
    for h, c in cones.items():
        L.append(f"  {h}: 68% [{c['low68']:.2f}–{c['high68']:.2f}]  •  95% [{c['low95']:.2f}–{c['high95']:.2f}]")
    L.append(f"\nTrack-record прогнозов: {tr_line}")
    L.append(f"Новости из: {', '.join(sources) if sources else 'ленты сейчас недоступны'}")
    L.append("FX на неделю близок к случайному блужданию — это обоснованная ставка, не гарантия.")
    return "\n".join(L)


# ---------- сверка прошлых прогнозов с фактом (обучение на результатах) ----------

def _rate_near(rows, target_date, slack=4):
    best, bestdiff = None, 1e9
    for r in rows:
        try:
            d = dt.date.fromisoformat(str(r["date"])[:10])
        except Exception:
            continue
        diff = abs((d - target_date).days)
        if diff <= slack and diff < bestdiff and r.get("ils_rub"):
            best, bestdiff = r["ils_rub"], diff
    return best


def _hit(spot0, spot_then, direction):
    chg = spot_then - spot0
    if direction == "up":
        return 1 if chg > 0 else 0
    if direction == "down":
        return 1 if chg < 0 else 0
    return 1 if abs(chg) / spot0 < 0.005 else 0   # flat: попал, если ушло мало


def evaluate_due(rows=None):
    """Проставить факт для прогнозов, чей горизонт наступил. Возвращает число обновлённых."""
    rows = rows or storage.get_series(540)
    today = dt.date.today()
    updated = 0
    for f in storage.ai_forecasts_unevaluated():
        try:
            made = dt.date.fromisoformat(str(f["made_date"])[:10])
        except Exception:
            continue
        kw = {}
        if f["hit_1w"] is None and (today - made).days >= 7:
            s = _rate_near(rows, made + dt.timedelta(days=7))
            if s is not None:
                kw.update(spot_1w=s, hit_1w=_hit(f["spot"], s, f["dir_1w"]))
        if f["hit_1m"] is None and (today - made).days >= 30:
            s = _rate_near(rows, made + dt.timedelta(days=30), slack=6)
            if s is not None:
                kw.update(spot_1m=s, hit_1m=_hit(f["spot"], s, f["dir_1m"]))
        if kw:
            storage.set_ai_outcome(f["id"], **kw)
            updated += 1
    return updated
