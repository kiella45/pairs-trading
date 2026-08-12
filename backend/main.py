"""
Pairs Trading Backend
Binance Futures USDT-M perpetual data
Dollar-neutral: log spread = ln(A) - ln(B)
Local SQLite cache for fast reloads
"""

import logging
import asyncio
import pickle
import sqlite3
import os
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from enum import Enum

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Pairs Trading API", version="1.4")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BINANCE_FAPI = "https://fapi.binance.com"
MIN_LISTING_DAYS = 730
PERIODS_PER_DAY = {"15m": 96, "1h": 24, "4h": 6}
CACHE_MAX_AGE_MINUTES = 60

CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")


def init_cache():
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT,
            interval TEXT,
            limit_val INTEGER,
            data BLOB,
            updated_at TIMESTAMP,
            PRIMARY KEY (symbol, interval, limit_val)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS symbols_cache (
            key TEXT PRIMARY KEY,
            data BLOB,
            updated_at TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_to_cache(symbol: str, interval: str, limit: int, df: pd.DataFrame):
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    blob = pickle.dumps(df)
    cursor.execute("""
        INSERT OR REPLACE INTO price_cache (symbol, interval, limit_val, data, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (symbol, interval, limit, blob, datetime.utcnow()))
    conn.commit()
    conn.close()


def load_from_cache(symbol: str, interval: str, limit: int) -> Optional[pd.DataFrame]:
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT data, updated_at FROM price_cache
        WHERE symbol = ? AND interval = ? AND limit_val = ?
    """, (symbol, interval, limit))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    data, updated_at = row
    updated = datetime.fromisoformat(updated_at)
    age = datetime.utcnow() - updated

    if age > timedelta(minutes=CACHE_MAX_AGE_MINUTES):
        logger.info(f"Cache expired for {symbol}/{interval}, age={age}")
        return None

    logger.debug(f"Cache hit for {symbol}/{interval}, age={age}")
    return pickle.loads(data)


def save_symbols_cache(key: str, data: list):
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    blob = pickle.dumps(data)
    cursor.execute("""
        INSERT OR REPLACE INTO symbols_cache (key, data, updated_at)
        VALUES (?, ?, ?)
    """, (key, blob, datetime.utcnow()))
    conn.commit()
    conn.close()


def load_symbols_cache(key: str) -> Optional[list]:
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT data, updated_at FROM symbols_cache WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    data, updated_at = row
    updated = datetime.fromisoformat(updated_at)
    age = datetime.utcnow() - updated

    if age > timedelta(minutes=CACHE_MAX_AGE_MINUTES):
        return None

    return pickle.loads(data)


init_cache()


# ─── Models ─────────────────────────────────────────────────────────────────

class Timeframe(str, Enum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"

class PairResult(BaseModel):
    pair: str
    symbol_a: str
    symbol_b: str
    correlation_6m: float
    cointegration_pvalue: float
    adf_pvalue: float
    correlation_short: float
    z_score: float
    half_life_days: float
    signal: str
    score: float
    last_price_a: float
    last_price_b: float
    spread_deviation_pct: float
    spread_mean: float
    spread_std: float
    timeframe: str


# ─── Binance API ────────────────────────────────────────────────────────────

async def fetch(endpoint: str, params: dict = None) -> dict:
    url = f"{BINANCE_FAPI}{endpoint}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()

async def get_perpetual_symbols() -> List[dict]:
    cached = load_symbols_cache("perpetual")
    if cached is not None:
        logger.info(f"Using cached symbols list ({len(cached)} symbols)")
        return cached

    logger.info("Fetching symbols from Binance...")
    data = await fetch("/fapi/v1/exchangeInfo")
    symbols = []
    now = datetime.utcnow()
    for s in data.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            onboard_ms = s.get("onboardDate", 0)
            if onboard_ms:
                listing_date = datetime.utcfromtimestamp(onboard_ms / 1000)
                age_days = (now - listing_date).days
                if age_days >= MIN_LISTING_DAYS:
                    symbols.append({
                        "symbol": s["symbol"],
                        "base": s["baseAsset"],
                        "age_days": age_days,
                    })

    save_symbols_cache("perpetual", symbols)
    logger.info(f"Saved {len(symbols)} symbols to cache")
    return symbols

async def get_klines(symbol: str, interval: str = "1h", limit: int = 500) -> pd.DataFrame:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = await fetch("/fapi/v1/klines", params)
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["close"] = pd.to_numeric(df["close"])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df[["close"]].dropna()


# ─── Analytics ──────────────────────────────────────────────────────────────

def half_life_in_periods(spread: pd.Series) -> float:
    lag = spread.shift(1).dropna()
    diff = spread.diff().dropna()
    idx = lag.index.intersection(diff.index)
    if len(idx) < 10:
        return 999.0
    y = diff.loc[idx]
    X = add_constant(lag.loc[idx])
    try:
        model = OLS(y, X, missing="drop").fit()
        theta = model.params.iloc[1] if len(model.params) > 1 else model.params[0]
        if theta >= 0 or np.isnan(theta):
            return 999.0
        hl = -np.log(2) / theta
        return min(hl, 999.0) if hl > 0 else 999.0
    except Exception:
        return 999.0

def calc_correlation(pa: pd.Series, pb: pd.Series) -> float:
    common = pa.index.intersection(pb.index)
    if len(common) < 30:
        return 0.0
    ra = np.log(pa.loc[common] / pa.loc[common].shift(1)).dropna()
    rb = np.log(pb.loc[common] / pb.loc[common].shift(1)).dropna()
    return float(ra.corr(rb))

def analyze_pair(sym_a, sym_b, pa_short, pb_short, daily_a, daily_b, tf):
    common_short = pa_short.index.intersection(pb_short.index)
    if len(common_short) < 200:
        return None
    pa_s = pa_short.loc[common_short]
    pb_s = pb_short.loc[common_short]

    corr_short = calc_correlation(pa_s, pb_s)

    log_spread_short = np.log(pa_s) - np.log(pb_s)
    spread_mean_short = float(log_spread_short.mean())
    spread_std_short = float(log_spread_short.std())
    current_spread_short = float(log_spread_short.iloc[-1])
    z = (current_spread_short - spread_mean_short) / spread_std_short if spread_std_short > 0 else 0.0

    hl_periods = half_life_in_periods(log_spread_short)
    periods_per_day = PERIODS_PER_DAY.get(tf, 24)
    hl_days = hl_periods / periods_per_day if hl_periods < 999 else 999.0

    spread_deviation_pct = ((current_spread_short - spread_mean_short) / abs(spread_mean_short)) * 100 if spread_mean_short != 0 else 0.0

    if daily_a is None or daily_b is None:
        return None

    common_daily = daily_a.index.intersection(daily_b.index)
    if len(common_daily) < 100:
        return None
    pa_d = daily_a.loc[common_daily]
    pb_d = daily_b.loc[common_daily]

    corr_6m = calc_correlation(pa_d, pb_d)

    log_spread_daily = np.log(pa_d) - np.log(pb_d)

    try:
        _, pval, _ = coint(np.log(pa_d), np.log(pb_d), trend="c", autolag="aic")
        coint_p = float(pval)
    except Exception:
        coint_p = 1.0

    try:
        adf_p = float(adfuller(log_spread_daily.dropna(), autolag="AIC")[1])
    except Exception:
        adf_p = 1.0

    if z > 2.0:
        sig = "SHORT"
    elif z < -2.0:
        sig = "LONG"
    elif abs(z) < 0.5:
        sig = "CLOSE"
    else:
        sig = "HOLD"

    score = 0.0
    score += min(abs(corr_6m) * 25, 25)
    score += max(0, (1 - coint_p) * 25)
    score += max(0, (1 - adf_p) * 15)
    if 0.5 <= hl_days <= 10:
        score += 15
    elif 10 < hl_days <= 30:
        score += 15 * (30 - hl_days) / 20
    score += max(0, min(10, 10 - abs(spread_mean_short) * 100))
    score = min(100, max(0, score))

    return {
        "pair": f"{sym_a}/{sym_b}",
        "symbol_a": sym_a,
        "symbol_b": sym_b,
        "correlation_6m": round(corr_6m, 4),
        "cointegration_pvalue": round(coint_p, 4),
        "adf_pvalue": round(adf_p, 4),
        "correlation_short": round(corr_short, 4),
        "z_score": round(z, 4),
        "half_life_days": round(hl_days, 2),
        "signal": sig,
        "score": round(score, 1),
        "last_price_a": round(float(pa_s.iloc[-1]), 4),
        "last_price_b": round(float(pb_s.iloc[-1]), 4),
        "spread_deviation_pct": round(spread_deviation_pct, 2),
        "spread_mean": round(spread_mean_short, 6),
        "spread_std": round(spread_std_short, 6),
        "timeframe": tf,
    }


# ─── Cache ──────────────────────────────────────────────────────────────────

async def get_cached_klines(symbol: str, interval: str, limit: int = 500, force: bool = False) -> pd.DataFrame:
    if not force:
        cached = load_from_cache(symbol, interval, limit)
        if cached is not None:
            return cached

    logger.info(f"Fetching {symbol}/{interval} from Binance...")
    df = await get_klines(symbol, interval, limit)
    save_to_cache(symbol, interval, limit, df)
    return df


# ─── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/pairs")
async def get_pairs(
    timeframe: Timeframe = Query(Timeframe.H1),
    min_correlation: float = Query(0.5, ge=-1, le=1),
    max_half_life: float = Query(30, ge=1),
    min_score: float = Query(0, ge=0, le=100),
    top_n: int = Query(50, ge=1, le=200),
    force_refresh: bool = Query(False, description="Ignore cache and fetch fresh data from Binance"),
):
    symbols_info = await get_perpetual_symbols()
    symbols = [s["symbol"] for s in symbols_info]
    logger.info(f"Analyzing {len(symbols)} symbols on {timeframe.value}")

    sem = asyncio.Semaphore(10)

    async def fetch_one(s, interval, limit):
        async with sem:
            try:
                return s, await get_cached_klines(s, interval, limit, force=force_refresh)
            except Exception as e:
                logger.warning(f"Failed {s}: {e}")
                return s, None

    tf_tasks = [fetch_one(s, timeframe.value, 500) for s in symbols]
    daily_tasks = [fetch_one(s, "1d", 180) for s in symbols]

    tf_results = await asyncio.gather(*tf_tasks)
    daily_results = await asyncio.gather(*daily_tasks)

    tf_prices = {s: df for s, df in tf_results if df is not None}
    daily_prices = {s: df for s, df in daily_results if df is not None}

    if len(tf_prices) < 2:
        raise HTTPException(400, "Insufficient data from Binance")

    syms = list(tf_prices.keys())
    pairs = []
    total = len(syms) * (len(syms) - 1) // 2

    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            r = analyze_pair(
                syms[i], syms[j],
                tf_prices[syms[i]]["close"], tf_prices[syms[j]]["close"],
                daily_prices.get(syms[i], {}).get("close") if daily_prices.get(syms[i]) is not None else None,
                daily_prices.get(syms[j], {}).get("close") if daily_prices.get(syms[j]) is not None else None,
                timeframe.value
            )
            if r and r["correlation_6m"] >= min_correlation and r["half_life_days"] <= max_half_life and r["score"] >= min_score:
                pairs.append(r)

    pairs.sort(key=lambda x: x["score"], reverse=True)

    return {
        "pairs": pairs[:top_n],
        "total_analyzed": total,
        "total_passed": len(pairs),
        "timeframe": timeframe.value,
        "symbols_count": len(syms),
    }


@app.get("/pair/{symbol_a}/{symbol_b}")
async def get_pair_detail(
    symbol_a: str,
    symbol_b: str,
    timeframe: Timeframe = Query(Timeframe.H1),
    limit: int = Query(500, ge=50, le=1000),
    force_refresh: bool = Query(False, description="Ignore cache and fetch fresh data from Binance"),
):
    for sym in [symbol_a, symbol_b]:
        if not sym.endswith("USDT"):
            sym += "USDT"

    df_a = await get_cached_klines(symbol_a, timeframe.value, limit, force=force_refresh)
    df_b = await get_cached_klines(symbol_b, timeframe.value, limit, force=force_refresh)
    daily_a = await get_cached_klines(symbol_a, "1d", 180, force=force_refresh)
    daily_b = await get_cached_klines(symbol_b, "1d", 180, force=force_refresh)

    r = analyze_pair(symbol_a, symbol_b, df_a["close"], df_b["close"], daily_a["close"], daily_b["close"], timeframe.value)
    if not r:
        raise HTTPException(400, "Insufficient data")

    pa = df_a["close"]
    pb = df_b["close"]
    common = pa.index.intersection(pb.index)
    pa = pa.loc[common]
    pb = pb.loc[common]

    log_spread = np.log(pa) - np.log(pb)
    spread_mean = float(log_spread.mean())
    spread_std = float(log_spread.std())
    z_hist = ((log_spread - spread_mean) / spread_std).tolist()

    spread_dev_hist = []
    for val in log_spread:
        if spread_mean != 0:
            dev = ((val - spread_mean) / abs(spread_mean)) * 100
        else:
            dev = 0.0
        spread_dev_hist.append(dev)

    dates = [d.strftime("%Y-%m-%d %H:%M") for d in log_spread.index]

    return {
        **r,
        "dates": dates,
        "spread_deviation_pct_history": spread_dev_hist,
        "z_score_history": z_hist,
        "price_a": pa.tolist(),
        "price_b": pb.tolist(),
        "upper_2": [2.0] * len(dates),
        "lower_2": [-2.0] * len(dates),
        "zero": [0.0] * len(dates),
    }


@app.get("/coins")
async def get_coins():
    symbols = await get_perpetual_symbols()
    return {"coins": [s["base"] for s in symbols], "count": len(symbols)}


@app.get("/cache/status")
async def cache_status():
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM price_cache")
    price_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM symbols_cache")
    sym_count = cursor.fetchone()[0]
    cursor.execute("SELECT symbol, interval, updated_at FROM price_cache ORDER BY updated_at DESC LIMIT 5")
    recent = cursor.fetchall()
    conn.close()
    return {
        "price_cache_entries": price_count,
        "symbols_cache_entries": sym_count,
        "cache_db_path": CACHE_DB,
        "max_age_minutes": CACHE_MAX_AGE_MINUTES,
        "recent_updates": [{"symbol": r[0], "interval": r[1], "updated": r[2]} for r in recent]
    }


@app.post("/cache/update")
async def start_update(timeframe: Timeframe = Query(Timeframe.H1)):
    symbols_info = await get_perpetual_symbols()
    symbols = [s["symbol"] for s in symbols_info]
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    now = datetime.utcnow()
    deleted = 0
    for s in symbols:
        for interval, limit in [(timeframe.value, 500), ("1d", 180)]:
            cursor.execute(
                "SELECT updated_at FROM price_cache WHERE symbol = ? AND interval = ? AND limit_val = ?",
                (s, interval, limit)
            )
            row = cursor.fetchone()
            if row is not None:
                updated = datetime.fromisoformat(row[0])
                if now - updated > timedelta(minutes=CACHE_MAX_AGE_MINUTES):
                    cursor.execute(
                        "DELETE FROM price_cache WHERE symbol = ? AND interval = ? AND limit_val = ?",
                        (s, interval, limit)
                    )
                    deleted += 1
    conn.commit()
    conn.close()
    return {"deleted": deleted, "message": f"Removed {deleted} stale entries. Refreshing data..."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
