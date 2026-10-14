"""
Трекер Range-сигналов.
- Хранит сигналы в SQLite.
- Проверяет виртуальный P&L сетки на 1h-свечах.
- Собирает статистику: средний профит, winrate, время до SL.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

import config as cfg

log = logging.getLogger("tracker")


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS range_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                valid_until TEXT,
                symbol TEXT NOT NULL,
                score REAL NOT NULL,
                center REAL, lower REAL, upper REAL,
                step_pct REAL, n_grids INTEGER,
                sl_low REAL, sl_high REAL,
                net_per_level_pct REAL,
                atr_pct REAL, std_ret REAL, trend_slope_pct REAL,
                mean_reversion REAL, drift_abs_median REAL,
                grid_score_20_60h REAL, grid_span_used REAL,
                vol_usdt_avg REAL, spread_pct REAL,
                reasons TEXT,

                result TEXT, result_price REAL, result_time TEXT,
                result_pct REAL,
                n_crossings INTEGER,
                max_profit_pct REAL, max_drawdown_pct REAL,
                time_to_result_hours REAL
            );
            CREATE INDEX IF NOT EXISTS idx_lookup
                ON range_signals(symbol, ts);
        """)
        self.db.commit()

    def can_notify(self, symbol: str, cooldown_hours: int) -> bool:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=cooldown_hours)).isoformat()
        row = self.db.execute(
            "SELECT 1 FROM range_signals WHERE symbol=? AND ts>=? LIMIT 1",
            (symbol, cutoff),
        ).fetchone()
        return row is None

    def save(self, s):
        self.db.execute(
            """INSERT INTO range_signals
            (ts, valid_until, symbol, score,
             center, lower, upper, step_pct, n_grids,
             sl_low, sl_high, net_per_level_pct,
             atr_pct, std_ret, trend_slope_pct,
             mean_reversion, drift_abs_median,
             grid_score_20_60h, grid_span_used,
             vol_usdt_avg, spread_pct, reasons)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                s.timestamp, s.valid_until, s.symbol, s.score,
                s.center, s.lower, s.upper, s.step_pct, s.n_grids,
                s.sl_low, s.sl_high, s.net_per_level_pct,
                s.atr_pct, s.std_ret, s.trend_slope_pct,
                s.mean_reversion, s.drift_abs_median,
                s.grid_score_20_60h, s.grid_span_used,
                s.vol_usdt_avg, s.spread_pct,
                json.dumps(s.reasons, ensure_ascii=False),
            ),
        )
        self.db.commit()

    def get_open(self, lookback_hours: int, limit: int) -> list:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=lookback_hours)).isoformat()
        rows = self.db.execute(
            """SELECT * FROM range_signals
               WHERE ts >= ? AND result IS NULL
               ORDER BY ts DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_result(self, sig_id: int, data: dict):
        self.db.execute(
            """UPDATE range_signals SET
                result=?, result_price=?, result_time=?, result_pct=?,
                n_crossings=?, max_profit_pct=?, max_drawdown_pct=?,
                time_to_result_hours=?
               WHERE id=?""",
            (
                data["result"], data["result_price"], data["result_time"],
                data["result_pct"], data["n_crossings"],
                data["max_profit_pct"], data["max_drawdown_pct"],
                data["time_to_result_hours"], sig_id,
            ),
        )
        self.db.commit()

    def get_stats(self, hours: int) -> dict:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=hours)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM range_signals WHERE ts >= ? AND result IS NOT NULL",
            (cutoff,),
        ).fetchall()

        s = {"total": 0, "profitable": 0, "sl_hit": 0, "expired": 0,
             "sum_pct": 0.0, "sum_crossings": 0,
             "sum_max_profit": 0.0, "sum_max_dd": 0.0, "sum_time": 0.0}
        for r in rows:
            s["total"] += 1
            if r["result"] == "PROFITABLE":
                s["profitable"] += 1
            elif r["result"] == "SL_HIT":
                s["sl_hit"] += 1
            elif r["result"] == "EXPIRED":
                s["expired"] += 1
            if r["result_pct"] is not None:
                s["sum_pct"] += float(r["result_pct"])
            if r["n_crossings"] is not None:
                s["sum_crossings"] += int(r["n_crossings"])
            if r["max_profit_pct"] is not None:
                s["sum_max_profit"] += float(r["max_profit_pct"])
            if r["max_drawdown_pct"] is not None:
                s["sum_max_dd"] += float(r["max_drawdown_pct"])
            if r["time_to_result_hours"] is not None:
                s["sum_time"] += float(r["time_to_result_hours"])

        n = s["total"]
        s["winrate"] = round(s["profitable"] / n * 100, 1) if n else 0.0
        s["avg_pct"] = round(s["sum_pct"] / n, 3) if n else 0.0
        s["avg_crossings"] = round(s["sum_crossings"] / n, 1) if n else 0.0
        s["avg_max_profit"] = round(s["sum_max_profit"] / n, 3) if n else 0.0
        s["avg_max_dd"] = round(s["sum_max_dd"] / n, 3) if n else 0.0
        s["avg_time"] = round(s["sum_time"] / n, 1) if n else 0.0
        return s

    def close(self):
        self.db.close()


# ============================================================
# ТРЕКЕР
# ============================================================
class RangeTracker:
    """
    Симулирует работу сетки на исторических 1h-свечах.
    - Считает пересечения уровней (crossings).
    - Умножает на net_per_level_pct — получает виртуальный P&L.
    - Смотрит, не вышел ли SL.
    """
    def __init__(self, database: Database, fetch_ohlcv_fn):
        self.db = database
        self.fetch_ohlcv = fetch_ohlcv_fn

    @staticmethod
    def _count_crossings(levels: np.ndarray,
                         prices: np.ndarray) -> int:
        """Сколько раз цена пересекла уровни сетки."""
        if len(levels) < 2 or len(prices) < 2:
            return 0
        total = 0
        for i in range(1, len(prices)):
            lo, hi = sorted([prices[i - 1], prices[i]])
            total += int(np.sum((levels > lo) & (levels <= hi)))
        return total

    def check_signal(self, sig: dict) -> Optional[dict]:
        symbol = sig["symbol"]
        center = float(sig["center"])
        lower = float(sig["lower"])
        upper = float(sig["upper"])
        sl_low = float(sig["sl_low"])
        sl_high = float(sig["sl_high"])
        n_grids = int(sig["n_grids"])
        span = float(sig["grid_span_used"])
        net_level = float(sig["net_per_level_pct"])
        ts = datetime.fromisoformat(sig["ts"])
        valid_until = datetime.fromisoformat(sig["valid_until"])

        df = self.fetch_ohlcv(symbol, cfg.TIMEFRAME, cfg.MAX_HOURS + 5)
        if df.empty:
            return None

        df = df[df["ts"] >= ts]
        if df.empty:
            return None

        # Строим уровни один раз
        from range_strategy import build_geometric_grid
        grid = build_geometric_grid(center, span, n_grids)
        levels = np.array(grid["levels"])

        prices = df["close"].values
        highs = df["high"].values
        lows = df["low"].values

        # Считаем P&L накопительно
        crossings = self._count_crossings(levels, prices)

        # Проверка SL по high/low (а не только close)
        sl_hit_idx = None
        for i in range(len(df)):
            if highs[i] > sl_high or lows[i] < sl_low:
                sl_hit_idx = i
                break

        # Виртуальный P&L (в % от депозита на одну сетку)
        virtual_pnl_pct = crossings * net_level * 100.0

        # Максимальная просадка (по close)
        cum = np.zeros(len(prices))
        for i in range(1, len(prices)):
            lo, hi = sorted([prices[i - 1], prices[i]])
            step_cross = np.sum((levels > lo) & (levels <= hi))
            cum[i] = cum[i - 1] + step_cross * net_level * 100.0
        peak = np.maximum.accumulate(cum)
        dd = cum - peak
        max_dd = float(dd.min()) if len(dd) else 0.0
        max_profit = float(cum.max()) if len(cum) else 0.0

        now = datetime.now(timezone.utc)

        if sl_hit_idx is not None:
            result = "SL_HIT"
            result_time = df.iloc[sl_hit_idx]["ts"]
            result_price = sl_high if highs[sl_hit_idx] > sl_high else sl_low
            result_pct = float(cum[sl_hit_idx]) - 100 * cfg.GRID_STOP_LOSS_PCT
        elif now >= valid_until:
            result = "PROFITABLE" if virtual_pnl_pct > 0 else "EXPIRED"
            result_time = df.iloc[-1]["ts"]
            result_price = float(prices[-1])
            result_pct = virtual_pnl_pct
        else:
            return None  # ещё активно

        rt = result_time
        rt_dt = rt.to_pydatetime() if isinstance(rt, pd.Timestamp) else rt
        time_hours = round((rt_dt - ts).total_seconds() / 3600, 2)

        return {
            "result": result,
            "result_price": float(result_price),
            "result_time": rt_dt.isoformat(),
            "result_pct": round(result_pct, 4),
            "n_crossings": crossings,
            "max_profit_pct": round(max_profit, 4),
            "max_drawdown_pct": round(max_dd, 4),
            "time_to_result_hours": time_hours,
        }

    def run(self) -> int:
        signals = self.db.get_open(
            lookback_hours=cfg.TRACKER_LOOKBACK_HOURS,
            limit=cfg.TRACKER_BATCH,
        )
        if not signals:
            return 0
        closed = 0
        for sig in signals:
            try:
                result = self.check_signal(sig)
            except Exception as e:
                log.warning(f"Tracker {sig['symbol']}: {e}")
                continue
            if result is None:
                continue
            self.db.update_result(sig["id"], result)
            closed += 1
            log.info(
                f"📌 {sig['symbol']} → {result['result']} "
                f"@ {result['result_price']} "
                f"({result['result_pct']:+.3f}%) "
                f"cross={result['n_crossings']}"
            )
        return closed
