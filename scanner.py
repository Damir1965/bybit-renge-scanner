"""
Bybit Range Scanner v1.0
- Каждые 3 часа сканирует USDT-Perpetual Bybit
- Ищет боковики (нейтраль) по 6 метрикам
- Строит геометрическую сетку 26 уровней
- Сохраняет сигналы в SQLite + CSV
- Трекер считает виртуальный P&L сетки
- Telegram: @BybitRangeBot
- PAPER_MODE: CSV без TG
"""
from __future__ import annotations

import csv
import gc
import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from logging.handlers import RotatingFileHandler
from typing import Optional

import pandas as pd

import config as cfg
from bybit_client import (
    make_exchange, get_usdt_perpetuals,
    fetch_ohlcv, fetch_spread_pct,
)
from range_strategy import (
    score_symbol, passes_filters, build_signal,
)
from tracker import Database, RangeTracker
from notifications import send_report, send_stats_report
from cleanup import run_cleanup, disk_usage_mb


# ============================================================
# LOG
# ============================================================
os.makedirs(cfg.LOG_DIR, exist_ok=True)
os.makedirs(cfg.CSV_DIR, exist_ok=True)
os.makedirs(cfg.DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        RotatingFileHandler(
            os.path.join(cfg.LOG_DIR, "scanner.log"),
            maxBytes=cfg.LOG_MAX_BYTES,
            backupCount=cfg.LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("scanner")


# ============================================================
# HTTP KEEPALIVE
# ============================================================
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Bybit Range Scanner v1.0 alive")

    def log_message(self, *args):
        pass


def start_http_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"HTTP-сервер на порту {port}")


# ============================================================
# QUIET HOURS
# ============================================================
def is_quiet_hours() -> bool:
    if cfg.QUIET_HOUR_START == cfg.QUIET_HOUR_END:
        return False
    now = datetime.now(timezone.utc)
    return cfg.QUIET_HOUR_START <= now.hour < cfg.QUIET_HOUR_END


# ============================================================
# СКАНИРОВАНИЕ
# ============================================================
def scan_once(exchange, stage_stats: dict) -> list:
    """Один проход по монетам."""
    symbols = get_usdt_perpetuals(exchange)
    log.info(f"USDT-Perpetual найдено: {len(symbols)}")

    # Шаг 1. Отбор по объёму (без свечей — не можем, смотрим через top)
    # Собираем данные по всем, потом отсекаем по TOP_CANDIDATES
    candidates = []

    for i, sym in enumerate(symbols, 1):
        try:
            df = fetch_ohlcv(exchange, sym, cfg.TIMEFRAME, cfg.LOOKBACK_BARS)
            if df.empty:
                continue

            # Объём в USDT
            vol_usdt = float((df["volume"] * df["close"]).mean())
            if vol_usdt < cfg.MIN_VOLUME_USDT:
                continue
            stage_stats["vol_pass"] += 1

            # Спред
            spread = fetch_spread_pct(exchange, sym)
            if spread is None or spread > cfg.MAX_SPREAD_PCT:
                continue
            stage_stats["spread_pass"] += 1

            # Скоринг
            m = score_symbol(df)
            if m is None:
                continue
            stage_stats["score_done"] += 1

            # Фильтры
            if not passes_filters(m):
                continue
            stage_stats["filter_pass"] += 1

            candidates.append({
                "symbol": sym, "df": df, "metrics": m,
                "vol_usdt": vol_usdt, "spread": spread,
            })

        except Exception as e:
            log.warning(f"{sym}: {e}")
            continue

        if i % 20 == 0:
            log.info(f"  ... обработано {i}/{len(symbols)}")

    # Сортируем по score, берём TOP_CANDIDATES
    candidates.sort(key=lambda x: x["metrics"]["score"], reverse=True)
    candidates = candidates[:cfg.TOP_CANDIDATES]

    # Строим сигналы
    signals = []
    for c in candidates:
        try:
            sig = build_signal(
                c["symbol"], c["df"], c["metrics"],
                c["spread"], c["vol_usdt"],
            )
            if sig is not None:
                signals.append(sig)
        except Exception as e:
            log.warning(f"build_signal {c['symbol']}: {e}")

    return signals


# ============================================================
# CSV
# ============================================================
def save_csv(signals: list):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    path = os.path.join(cfg.CSV_DIR, f"range_{ts}.csv")
    rows = []
    for s in signals:
        d = asdict(s)
        d["reasons"] = "; ".join(d["reasons"]) if d["reasons"] else ""
        rows.append(d)
    if rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        log.info(f"CSV сохранён: {path}")


# ============================================================
# MAIN
# ============================================================
db = Database(cfg.DATABASE_PATH)
tracker: Optional[RangeTracker] = None
last_stats_report = 0.0


def run_scan():
    global tracker, last_stats_report
    started = time.time()
    quiet = is_quiet_hours()

    log.info("=" * 60)
    log.info(
        f"🚀 Bybit Range Scanner v1.0"
        f"{' [ТИХИЕ ЧАСЫ]' if quiet else ''}"
        f"{' [PAPER]' if cfg.PAPER_MODE else ''}"
    )

    # Автоочистка
    try:
        run_cleanup()
        log.info(f"💾 /data занято: {disk_usage_mb()} МБ")
    except Exception as e:
        log.warning(f"Cleanup error: {e}")

    # Биржа
    exchange = make_exchange(use_auth=False)

    # Трекер
    if tracker is None:
        def fetch_for_tracker(symbol, tf, limit):
            return fetch_ohlcv(exchange, symbol, tf, limit)
        tracker = RangeTracker(db, fetch_for_tracker)

    # Сканирование
    stage_stats = {
        "vol_pass": 0, "spread_pass": 0,
        "score_done": 0, "filter_pass": 0,
    }
    signals = scan_once(exchange, stage_stats)

    log.info(
        f"📊 Пайплайн: "
        f"vol>{cfg.MIN_VOLUME_USDT/1e6:.0f}M:{stage_stats['vol_pass']} → "
        f"spread<{cfg.MAX_SPREAD_PCT}%:{stage_stats['spread_pass']} → "
        f"scored:{stage_stats['score_done']} → "
        f"filtered:{stage_stats['filter_pass']} → "
        f"signals:{len(signals)}"
    )

    # Фильтр cooldown
    fresh = [s for s in signals
             if db.can_notify(s.symbol, cfg.COOLDOWN_HOURS)]
    log.info(f"После cooldown: {len(fresh)} (было {len(signals)})")

    # Сохранение и отправка
    if fresh:
        for s in fresh[:cfg.MAX_SIGNALS_PER_SCAN]:
            db.save(s)

    if quiet:
        log.info("😴 Тихие часы — TG не отправляется")
    else:
        send_report(fresh[:cfg.MAX_SIGNALS_PER_SCAN],
                    scanned=stage_stats["score_done"],
                    quiet=False)

    if fresh:
        save_csv(fresh)

    # Трекер
    try:
        closed = tracker.run()
        if closed > 0:
            log.info(f"Трекер: закрыто {closed} сигналов")
    except Exception as e:
        log.warning(f"Tracker error: {e}")

    # Статистика раз в 6 часов
    now = time.time()
    if (now - last_stats_report) > cfg.TRACKER_REPORT_INTERVAL_HOURS * 3600:
        try:
            stats = db.get_stats(hours=24)
            if stats["total"] > 0:
                send_stats_report(stats, hours=24)
            last_stats_report = now
        except Exception as e:
            log.warning(f"Stats report error: {e}")

    log.info(f"✅ Готово за {time.time() - started:.1f}с")
    gc.collect()


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    log.info("🔷 Bybit Range Scanner v1.0 запущен")
    log.info(f"PAPER_MODE = {cfg.PAPER_MODE}")
    log.info(f"Интервал: {cfg.SCAN_INTERVAL_HOURS}ч")
    log.info(f"Мин. объём: ${cfg.MIN_VOLUME_USDT:,.0f}")
    log.info(f"Макс. спред: {cfg.MAX_SPREAD_PCT}%")
    if not cfg.TELEGRAM_TOKEN and not cfg.PAPER_MODE:
        log.warning("⚠️ TG_TOKEN не задан, а PAPER_MODE=false")
    start_http_server()
    while True:
        try:
            run_scan()
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
            break
        except Exception as e:
            log.exception(f"Ошибка сканирования: {e}")
        log.info(f"⏳ Следующий скан через {cfg.SCAN_INTERVAL_HOURS}ч")
        try:
            time.sleep(cfg.SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            break
