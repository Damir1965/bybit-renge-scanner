"""
Telegram-уведомления для @BybitRangeBot.
- format_signal() — одиночный сигнал (боковик).
- send_report() — сводка за скан.
- send_stats_report() — статистика за 24ч.
"""
import logging
from datetime import datetime, timezone

import requests

import config as cfg

log = logging.getLogger("notifications")


# ============================================================
# ОТПРАВКА
# ============================================================
def send_telegram(text: str) -> None:
    if cfg.PAPER_MODE:
        log.info("[PAPER] TG не отправлен")
        return
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        log.warning("Telegram не настроен")
        return
    try:
        url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(
            url,
            data={
                "chat_id": cfg.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not r.ok:
            log.error(f"Telegram: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ============================================================
# ФОРМАТИРОВАНИЕ
# ============================================================
def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8g}"


def format_signal(s) -> str:
    """
    Формат сигнала боковика для Telegram.
    s — RangeSignal из range_strategy.py
    """
    # score 0..1 → 0..100 для читаемости
    score100 = int(s.score * 100)

    # Шаг сетки в bp (базисные пункты)
    step_bp = s.step_pct * 10000
    net_bp = s.net_per_level_pct * 10000

    lines = [
        f"🔷 <b>[BYBIT] RANGE {s.symbol}</b>",
        f"Score: <b>{score100}/100</b> · шаг {step_bp:.1f}bp · net {net_bp:+.1f}bp",
        f"📍 Центр {fmt_price(s.center)} · "
        f"[{fmt_price(s.lower)} … {fmt_price(s.upper)}]",
        f"⚙ Сетка: {s.n_grids} ур. · span {s.grid_span_used*100:.2f}%",
        f"🛑 SL: ниже {fmt_price(s.sl_low)} / выше {fmt_price(s.sl_high)}",
    ]

    ctx = [
        f"ATR {s.atr_pct*100:.2f}%",
        f"2s {s.grid_score_20_60h:.4f}",
        f"MR {s.mean_reversion:.2f}",
        f"slope {s.trend_slope_pct:.3f}",
    ]
    lines.append("📊 " + " · ".join(ctx))

    if s.spread_pct and s.spread_pct < 999:
        lines.append(f"💧 Спред {s.spread_pct:.3f}% · объём ${s.vol_usdt_avg:,.0f}")

    if s.reasons:
        lines.append("💬 " + ", ".join(s.reasons[:4]))

    return "\n".join(lines) + "\n"


def send_report(signals: list, scanned: int, quiet: bool = False):
    """Сводка за скан."""
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    quiet_note = " · 😴 тихие часы" if quiet else ""
    paper_note = " · 📝 PAPER" if cfg.PAPER_MODE else ""
    header = (
        f"🔷 <b>Bybit Range Scanner v1.0</b> · {ts}{quiet_note}{paper_note}\n"
        f"Просканировано: {scanned} · найдено: {len(signals)}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    parts = [header]

    if signals:
        for s in signals[:cfg.MAX_SIGNALS_PER_SCAN]:
            parts.append("")
            parts.append(format_signal(s))
    else:
        parts.append("\n<i>Нет подходящих боковиков. Следим.</i>")

    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3900] + "\n…"
    send_telegram(text)


def send_stats_report(stats: dict, hours: int = 24):
    """Отчёт статистики за N часов."""
    if stats["total"] == 0:
        return
    ts = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
    text = (
        f"📊 <b>Bybit Range · Статистика за {hours}ч</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Всего сигналов: <b>{stats['total']}</b>\n"
        f"🟢 Profitable: {stats['profitable']}\n"
        f"🔴 SL_HIT: {stats['sl_hit']}\n"
        f"⏰ Expired: {stats['expired']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Winrate: <b>{stats['winrate']}%</b>\n"
        f"Средний результат: <b>{stats['avg_pct']:+.3f}%</b>\n"
        f"Среднее crossings: <b>{stats['avg_crossings']}</b>\n"
        f"Max profit: <b>{stats['avg_max_profit']:+.3f}%</b>\n"
        f"Max DD: <b>{stats['avg_max_dd']:+.3f}%</b>\n"
        f"Среднее время: <b>{stats['avg_time']}ч</b>"
    )
    send_telegram(text)
