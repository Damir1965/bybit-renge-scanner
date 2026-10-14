"""
Обёртка ccxt для Bybit USDT-Perpetual.
Только чтение — мы не торгуем, собираем данные для сигналов.
"""
import logging
import time
from typing import Optional

import ccxt
import pandas as pd

import config as cfg

log = logging.getLogger("bybit")


def make_exchange(use_auth: bool = False):
    """
    Создаёт объект биржи Bybit.
    use_auth=False — только чтение (по умолчанию).
    use_auth=True — если когда-нибудь понадобится торговля.
    """
    params = {
        "options": {
            "defaultType": cfg.MARKET_TYPE,
            "adjustForTimeDifference": True,
        },
        "enableRateLimit": True,
        "timeout": cfg.REQUEST_TIMEOUT * 1000,
    }

    if use_auth:
        params["apiKey"] = cfg.BYBIT_API_KEY
        params["secret"] = cfg.BYBIT_API_SECRET

    return ccxt.bybit(params)


def get_usdt_perpetuals(exchange) -> list:
    """Возвращает список USDT-Perpetual символов Bybit."""
    try:
        markets = exchange.load_markets()
    except Exception as e:
        log.warning(f"load_markets error: {e}")
        return []

    out = []
    for sym, m in markets.items():
        if not m.get("swap"):
            continue
        if not m.get("linear"):
            continue
        if m.get("quote") != cfg.SYMBOLS_QUOTE:
            continue
        if not m.get("active", True):
            continue
        out.append(sym)
    return out


def fetch_ohlcv(exchange, symbol: str, timeframe: str,
                limit: int = 500) -> pd.DataFrame:
    """Загружает свечи. Возвращает DataFrame с ts/open/high/low/close/volume."""
    try:
        o = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        log.debug(f"{symbol}: fetch_ohlcv error: {e}")
        return pd.DataFrame()

    if not o or len(o) < 100:
        return pd.DataFrame()

    df = pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def fetch_spread_pct(exchange, symbol: str,
                     levels: int = 5) -> Optional[float]:
    """
    Возвращает спред в %: (ask - bid) / mid * 100.
    None — если не удалось получить.
    """
    try:
        ob = exchange.fetch_order_book(symbol, limit=levels)
    except Exception as e:
        log.debug(f"{symbol}: order_book error: {e}")
        return None

    if not ob or not ob.get("bids") or not ob.get("asks"):
        return None

    try:
        bid = float(ob["bids"][0][0])
        ask = float(ob["asks"][0][0])
    except (IndexError, TypeError, ValueError):
        return None

    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    mid = (bid + ask) / 2
    return round((ask - bid) / mid * 100, 4)


def safe_call(fn, *args, **kwargs):
    """Обёртка с повтором при network error."""
    for attempt in range(3):
        try:
            time.sleep(cfg.API_DELAY)
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            log.debug(f"Network error ({attempt+1}/3): {e}")
            time.sleep(1 + attempt)
        except ccxt.RateLimitExceeded:
            log.warning("Rate limit — пауза 2с")
            time.sleep(2 + attempt)
        except Exception as e:
            log.debug(f"Unexpected error: {e}")
            return None
    return None
