"""
Логика поиска боковиков (нейтраль) и построения сетки.
Основа взята из GridNeutralBot, адаптирована под v1.0.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

import config as cfg
from indicators import (
    atr, log_returns, trend_slope_proxy,
    mean_reversion_proxy, drift_abs_median,
    two_sided_cross_score,
)


# ============================================================
# СИГНАЛ
# ============================================================
@dataclass
class RangeSignal:
    symbol: str
    score: float
    atr_pct: float
    std_ret: float
    trend_slope_pct: float
    mean_reversion: float
    drift_abs_median: float
    grid_score_20_60h: float
    grid_span_used: float
    vol_usdt_avg: float
    spread_pct: float

    # сетка
    center: float
    lower: float
    upper: float
    step_pct: float
    n_grids: int
    sl_low: float
    sl_high: float

    # виртуальный профит
    net_per_level_pct: float

    reasons: list = field(default_factory=list)
    timestamp: str = ""
    valid_until: str = ""

    trend_1h: str = "RANGE"


# ============================================================
# СКОРИНГ БОКОВИКА
# ============================================================
def score_symbol(df: pd.DataFrame) -> Optional[dict]:
    """
    Оценивает, насколько монета подходит для сетки.
    Возвращает dict с метриками или None, если данных мало.
    """
    if df.empty or len(df) < cfg.MAX_HOURS + 10:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]

    a = atr(high, low, close)
    atr_pct = float((a / close).iloc[-1])

    rets = log_returns(close).dropna()
    std_ret = float(rets.std()) if len(rets) > 1 else 999.0

    slope = trend_slope_proxy(close)
    mr = mean_reversion_proxy(close)
    drift = drift_abs_median(close)

    span = float(np.clip(atr_pct * 1.8,
                         cfg.GRID_SPAN_MIN, cfg.GRID_SPAN_MAX))

    acts = []
    for h in range(cfg.MIN_HOURS, cfg.MAX_HOURS + 1):
        if len(close) < h:
            break
        acts.append(two_sided_cross_score(
            close.iloc[-h:], span, cfg.N_GRIDS
        ))
    gs = ((0.6 * np.median(acts) + 0.4 * np.percentile(acts, 75))
          if acts else 0.0)

    # Итоговый score 0..1
    score = (
        (1 - min(1, atr_pct / cfg.MAX_ATR_PCT))        * 0.30 +
        (1 - min(1, std_ret / cfg.MAX_STD_RET))        * 0.20 +
        (1 - min(1, slope / cfg.MAX_TREND_SLOPE))      * 0.20 +
        min(1, mr)                                     * 0.10 +
        (1 - min(1, drift / cfg.MAX_DRIFT_ABS))        * 0.10 +
        min(1, gs * 12.0)                              * 0.10
    )

    return dict(
        atr_pct=atr_pct,
        std_ret=std_ret,
        trend_slope_pct=slope,
        mean_reversion=mr,
        drift_abs_median=drift,
        grid_span_used=span,
        grid_score_20_60h=gs,
        score=score,
    )


def passes_filters(m: dict) -> bool:
    """Проверяет, проходит ли монета все фильтры боковика."""
    if m["atr_pct"] > cfg.MAX_ATR_PCT:
        return False
    if m["std_ret"] > cfg.MAX_STD_RET:
        return False
    if m["trend_slope_pct"] > cfg.MAX_TREND_SLOPE:
        return False
    if m["mean_reversion"] < cfg.MIN_MEAN_REVERSION:
        return False
    if m["drift_abs_median"] > cfg.MAX_DRIFT_ABS:
        return False
    if m["grid_score_20_60h"] < cfg.MIN_TWO_SIDED_SCORE:
        return False
    return True


# ============================================================
# ГЕОМЕТРИЧЕСКАЯ СЕТКА
# ============================================================
def build_geometric_grid(center: float, span: float,
                         n: Optional[int] = None) -> dict:
    """
    Строит геометрическую сетку вокруг center.
    n — чётное число уровней (26 → 13 вверх + 13 вниз).
    """
    n = n or cfg.N_GRIDS
    if n % 2:
        n += 1
    half = n // 2
    step = span / half
    lc = np.log(center)
    offs = np.arange(-half, half + 1) * np.log(1 + step)
    lv = np.exp(lc + offs)
    return dict(
        center=center,
        span_pct=span,
        step_pct=step,
        n_grids=n,
        levels=lv.tolist(),
        buy_levels=lv[lv < center].tolist(),
        sell_levels=lv[lv >= center].tolist(),
        lower=float(lv[0]),
        upper=float(lv[-1]),
    )


def estimate_grid_profit(grid: dict, fills: int = 1,
                         fee: Optional[float] = None) -> dict:
    """
    Оценка профита с учётом комиссий.
    net_per_level_pct = step% - 2*fee (maker).
    """
    fee = fee if fee is not None else cfg.MAKER_FEE
    net = grid["step_pct"] - 2 * fee
    return dict(
        step_pct=grid["step_pct"],
        net_per_level_pct=net,
        total_net_pct=net * grid["n_grids"] * fills,
        n_levels=grid["n_grids"],
        fee_used=fee,
    )


def check_stop_loss(price: float, grid: dict,
                    sl_pct: Optional[float] = None) -> bool:
    """True если цена вышла за пределы сетки + SL."""
    sl_pct = sl_pct if sl_pct is not None else cfg.GRID_STOP_LOSS_PCT
    return not (grid["lower"] * (1 - sl_pct)
                <= price <= grid["upper"] * (1 + sl_pct))


# ============================================================
# ПОСТРОЕНИЕ СИГНАЛА
# ============================================================
def build_signal(symbol: str, df: pd.DataFrame, m: dict,
                 spread_pct: float, vol_usdt_avg: float) -> Optional[RangeSignal]:
    """Собирает RangeSignal из метрик."""
    if m["score"] < cfg.MIN_SIGNAL_SCORE:
        return None

    center = float(df["close"].iloc[-1])
    grid = build_geometric_grid(center, m["grid_span_used"])
    prof = estimate_grid_profit(grid)

    # SL на 5% за пределами сетки
    sl_low = grid["lower"] * (1 - cfg.GRID_STOP_LOSS_PCT)
    sl_high = grid["upper"] * (1 + cfg.GRID_STOP_LOSS_PCT)

    reasons = [
        f"score {m['score']:.3f}",
        f"ATR {m['atr_pct']*100:.2f}%",
        f"2s {m['grid_score_20_60h']:.4f}",
        f"MR {m['mean_reversion']:.2f}",
    ]
    if prof["net_per_level_pct"] > 0:
        reasons.append(f"шаг {grid['step_pct']*100:.3f}%")
    if spread_pct is not None:
        reasons.append(f"спред {spread_pct:.3f}%")

    now = datetime.now(timezone.utc)
    valid_until = now + timedelta(hours=cfg.MAX_HOLD_HOURS)

    return RangeSignal(
        symbol=symbol,
        score=round(m["score"], 4),
        atr_pct=round(m["atr_pct"], 5),
        std_ret=round(m["std_ret"], 5),
        trend_slope_pct=round(m["trend_slope_pct"], 4),
        mean_reversion=round(m["mean_reversion"], 4),
        drift_abs_median=round(m["drift_abs_median"], 6),
        grid_score_20_60h=round(m["grid_score_20_60h"], 6),
        grid_span_used=round(m["grid_span_used"], 5),
        vol_usdt_avg=round(vol_usdt_avg, 2),
        spread_pct=round(spread_pct, 4) if spread_pct is not None else 999.0,
        center=round(center, 8),
        lower=round(grid["lower"], 8),
        upper=round(grid["upper"], 8),
        step_pct=round(grid["step_pct"], 6),
        n_grids=grid["n_grids"],
        sl_low=round(sl_low, 8),
        sl_high=round(sl_high, 8),
        net_per_level_pct=round(prof["net_per_level_pct"], 6),
        reasons=reasons[:5],
        timestamp=now.isoformat(),
        valid_until=valid_until.isoformat(),
                           )
