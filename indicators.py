"""
Индикаторы для Bybit Range Scanner.
Включает стандартные (EMA, ATR) и специализированные для боковика:
- mean_reversion_proxy
- two_sided_cross_score
- trend_slope_proxy
- drift_abs_median
"""
import numpy as np
import pandas as pd

import config as cfg


# ============================================================
# БАЗОВЫЕ
# ============================================================
def ema(s: pd.Series, span: int = 50) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - pc).abs(), (low - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


# ============================================================
# СПЕЦИАЛЬНЫЕ ДЛЯ БОКОВИКА
# ============================================================
def trend_slope_proxy(close: pd.Series) -> float:
    """
    Наклон логарифмической цены (proxy тренда).
    Чем меньше — тем более нейтральная монета.
    """
    y = np.log(close.values + 1e-12)
    if len(y) < 80:
        return 999.0
    x = np.arange(len(y))
    return float(abs(np.polyfit(x, y, 1)[0]) * 100.0)


def mean_reversion_proxy(close: pd.Series, span: int = 50,
                         dev: float = 0.004) -> float:
    """
    Измеряет склонность к возврату к EMA.
    0.0 = тренд (цена уходит), 1.0 = боковик (цена возвращается).
    """
    e = ema(close, span)
    z = (close - e) / e
    d = z.abs() > dev
    if d.sum() == 0:
        return 0.0
    dn = (close - e).abs()
    nx = (close.shift(-1) - e.shift(-1)).abs()
    return float((nx < dn)[d].mean())


def drift_abs_median(close: pd.Series) -> float:
    """Медиана модуля лог-доходности. Чем меньше — тем лучше."""
    r = log_returns(close).dropna()
    return float(r.abs().median()) if len(r) else 999.0


def two_sided_cross_score(win: pd.Series, span: float,
                          n: int = 26) -> float:
    """
    Насколько цена ходит в обе стороны (для сетки важно).
    0.0 = только в одну сторону, >0.008 = хороший боковик.
    """
    start = float(win.iloc[0])
    if start <= 0:
        return 0.0
    lo = start * (1 - span / 2)
    hi = start * (1 + span / 2)
    if lo <= 0:
        return 0.0
    lv = np.linspace(lo, hi, n + 1)
    pi = int(np.digitize([start], lv)[0])
    up = dn = tot = 0
    for p in win.iloc[1:]:
        i = int(np.digitize([float(p)], lv)[0])
        if i != pi:
            tot += 1
            up += i > pi
            dn += i < pi
            pi = i
    if tot == 0:
        return 0.0
    sym = 1.0 - abs(up - dn) / tot
    return float((tot / max(1, len(win) - 1)) * max(0.0, sym))


def max_drawdown_pct(eq: np.ndarray) -> float:
    """Максимальная просадка в % (для бэктеста/трекера)."""
    if len(eq) == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / np.where(peak == 0, 1, peak)
    return float(dd.min() * 100.0)
