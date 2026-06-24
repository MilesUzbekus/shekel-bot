"""Демо-прогон ядра на ЖИВЫХ данных, без секретов.

Запуск из корня проекта:  python demo.py
Делает: init БД -> backfill истории -> текущий снимок -> конусы 1н/2н/мес ->
bias -> рекомендация на МОК-инвентаре -> печать отчёта (его же будет слать бот).
"""
import datetime as dt

from src import data, storage, analytics, config


def main():
    storage.init()

    print("Подгружаю историю (ЦБ + ECB)...")
    info = data.backfill(days=540)
    print(f"  USD/RUB точек: {info['rub_points']}, USD/ILS точек: {info['ils_points']}, "
          f"кросс по общим датам: {info['cross_points']}")

    snap = data.current_snapshot()
    storage.upsert_rate(snap["date"], snap["usd_rub"], snap["usd_ils"], snap["ils_rub"], snap["source"])
    print(f"Текущий кросс: {snap['ils_rub']:.4f} руб/шек ({snap['source']})")

    rows = storage.get_series(540)
    cross = [r["ils_rub"] for r in rows]
    rets = analytics.log_returns(cross)
    sigma = analytics.ewma_vol(rets)
    print(f"Дневная волатильность кросса (EWMA): {sigma * 100:.3f}%  "
          f"(годовая ~{sigma * (252 ** 0.5) * 100:.1f}%)")

    bias = analytics.compute_bias(rows)

    cones = {}
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for label, days in config.HORIZONS.items():
        cones[label] = analytics.cone(snap["ils_rub"], sigma, days)

    # МОК-инвентарь (заменится реальной таблицей): рубли у тебя, шекели у партнёра, средняя цена.
    mock_pos = {"rub_balance": 600_000.0, "ils_balance": 18_000.0, "avg_cost": 24.10}
    rec = analytics.recommend(mock_pos, snap["ils_rub"], bias)

    # сохраняем прогнозы (фундамент RAG + будущая самопроверка)
    for label, c in cones.items():
        storage.insert_forecast(ts, label, snap["ils_rub"], c, bias["score"],
                                rec["action"], "; ".join(rec["reasons"]))

    print("\n" + "=" * 64)
    print(analytics.format_report(snap, cones, bias, rec, config.ASSUMPTIONS))
    print("=" * 64)
    print("\n[демо на МОК-инвентаре: рубли 600 000, шекели 18 000, средняя 24.10 — заменится реальной таблицей]")


if __name__ == "__main__":
    main()
