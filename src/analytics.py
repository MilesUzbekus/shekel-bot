"""Аналитика.

Принцип честности:
  - Вероятностный КОНУС считается без выдуманного направления (нулевой снос) —
    это математически корректные диапазоны от волатильности.
  - Направленный BIAS показывается ОТДЕЛЬНО и прозрачно (вклад каждого сигнала),
    как обоснованный наклон, а не как точная вероятность.
  - Рекомендация (покупать/продавать/держать) считается от ТВОЕГО инвентаря
    (баланс рублей/шекелей + средняя цена) с поправкой на наклон рынка.
"""
import math
import datetime as dt

from src import config


# ---------- волатильность и конус ----------

def log_returns(values):
    out = []
    for i in range(1, len(values)):
        a, b = values[i - 1], values[i]
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def ewma_vol(returns, lam=0.94):
    """Дневная сигма по RiskMetrics EWMA."""
    if not returns:
        return 0.0
    seed = returns[: min(20, len(returns))]
    var = sum(r * r for r in seed) / len(seed)
    for r in returns:
        var = lam * var + (1 - lam) * r * r
    return math.sqrt(var)


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def cone(spot, sigma_daily, days, drift_daily=0.0):
    """Лог-нормальный конус. drift_daily=0 -> честный диапазон без направления."""
    sig = sigma_daily * math.sqrt(days)
    mu = drift_daily * days
    p_up = (1 - _phi((-mu) / sig)) if sig > 0 else 0.5

    def q(z):
        return spot * math.exp(mu + z * sig)

    return {
        "sigma_h": sig,
        "p_up": p_up,
        "low68": q(-1.0), "high68": q(1.0),
        "low95": q(-1.96), "high95": q(1.96),
    }


# ---------- направленный bias ----------

def zscore(current, series):
    n = len(series)
    if n < 30:
        return 0.0
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / (n - 1)
    sd = math.sqrt(var)
    return (current - mean) / sd if sd > 0 else 0.0


def compute_bias(rows):
    """rows: список dict с usd_rub/usd_ils/ils_rub (старые->новые)."""
    cross = [r["ils_rub"] for r in rows]
    rub = [r["usd_rub"] for r in rows]
    ils = [r["usd_ils"] for r in rows]
    comp = {}

    # рубль крепче среднего (USD/RUB ниже) -> z<0 -> склонность к ослаблению -> кросс ВВЕРХ
    z_rub = zscore(rub[-1], rub[-250:])
    comp["rub_reversion"] = -math.tanh(z_rub)

    # шекель крепче среднего (USD/ILS ниже) -> z<0 -> склонность к ослаблению шекеля -> кросс ВНИЗ
    z_ils = zscore(ils[-1], ils[-250:])
    comp["ils_reversion"] = math.tanh(z_ils)

    # моментум: знак 20-дневного изменения кросса
    comp["momentum"] = math.tanh((cross[-1] / cross[-21] - 1) * 10) if len(cross) > 21 else 0.0

    # carry: высокая ставка ЦБ держит рубль -> лёгкий наклон кросс ВНИЗ
    diff = config.ASSUMPTIONS["cbr_key_rate"] - config.ASSUMPTIONS["ils_cb_rate"]
    comp["carry"] = -math.tanh(diff / 20.0)

    # сезонность
    comp["seasonality"] = config.SEASONALITY.get(dt.date.today().month, 0.0)

    score = sum(config.BIAS_WEIGHTS[k] * v for k, v in comp.items())
    score = max(-1.0, min(1.0, score))
    return {"score": score, "components": comp, "z_rub": z_rub, "z_ils": z_ils}


def bias_to_drift(score, monthly_cap=0.02):
    """Наклон [-1;1] -> дневной снос. ±1 ≈ ±2% за месяц. Используется только для 'склонности', не для коридоров."""
    return (score * monthly_cap) / 21.0


# ---------- инвентарная рекомендация ----------

def recommend(pos, spot, bias, target_share=None):
    """Рекомендация от рыночного наклона + подсказка по текущей позиции.
    Баланс шекелей — чистая торговая позиция (может быть отрицательной)."""
    ils = pos.get("ils_balance", 0.0) or 0.0
    rub = pos.get("rub_balance", 0.0) or 0.0
    score = bias["score"]
    reasons = []

    if score > 0.15:
        action = "склонность ПОКУПАТЬ шекели"
        reasons.append("Рынок склонен к росту кросса (шекель дорожает к рублю) — докупать сейчас выгоднее, чем позже.")
    elif score < -0.15:
        action = "склонность ПРОДАВАТЬ шекели"
        reasons.append("Рынок склонен к падению кросса (шекель дешевеет к рублю) — продавать сейчас выгоднее.")
    else:
        action = "рынок нейтрален"
        reasons.append("Явного перевеса по рынку нет — работай от спреда и спроса клиентов.")

    if ils < -3000:
        reasons.append(f"У партнёра крупная короткая позиция по шекелям ({ils:,.0f} ₪) — есть смысл докупить, чтобы её сократить.")
    elif ils > 5000:
        reasons.append(f"У партнёра накоплено шекелей ({ils:,.0f} ₪) — можно продавать в спрос.")
    if rub < 50000:
        reasons.append(f"Рублей у тебя немного ({rub:,.0f} ₽) — следи, чтобы хватало на покупки.")

    return {"action": action, "score": score, "reasons": reasons}


# ---------- форматирование сообщения (его же шлёт бот) ----------

def format_report(snap, cones, bias, rec, assumptions):
    if bias["score"] > 0.10:
        arrow = "вверх (шекель крепче к рублю)"
    elif bias["score"] < -0.10:
        arrow = "вниз (шекель слабее к рублю)"
    else:
        arrow = "нейтрально"

    L = []
    L.append(f"ILS/RUB на {snap['date']}")
    L.append(f"Текущий кросс: {snap['ils_rub']:.3f} руб/шек")
    L.append(f"(USD/RUB {snap['usd_rub']:.2f} / USD/ILS {snap['usd_ils']:.4f}; источник: {snap['source']})")
    L.append("")
    L.append("Диапазон по волатильности (без направления):")
    for h, c in cones.items():
        L.append(f"  {h}: 68% [{c['low68']:.2f}–{c['high68']:.2f}]  •  95% [{c['low95']:.2f}–{c['high95']:.2f}]")
    L.append("")
    L.append(f"Наклон модели: {arrow}  (score {bias['score']:+.2f})")
    comp = bias["components"]
    L.append("  " + ", ".join(f"{k} {v:+.2f}" for k, v in comp.items()))
    L.append("")
    L.append(f"РЕКОМЕНДАЦИЯ: {rec['action']}")
    for r in rec["reasons"]:
        L.append(f"  - {r}")
    L.append("")
    L.append(f"Допущения bias на {assumptions['as_of']}: ставка ЦБ {assumptions['cbr_key_rate']}%, "
             f"ЦБ-ILS {assumptions['ils_cb_rate']}%, консенсус USD/RUB к концу года ~{assumptions['usdrub_yearend_consensus']}.")
    L.append("Диапазоны — это волатильность, а не предсказание. Точного направления у недельного FX нет.")
    return "\n".join(L)
