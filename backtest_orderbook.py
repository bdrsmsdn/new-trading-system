#!/usr/bin/env python3
"""
Backtest: Orderbook + RSI + EMA Crossover Strategy
===================================================
Strategy:
  LONG  = RSI oversold zone (<45) + price recovering above EMA9 + volume spike
  SHORT = price rejected at EMA9 + thick orderbook (volume spike)
  EXIT  = price crosses back below EMA9 (for longs)

Orderbook Proxy: Volume spike > 1.0x 14-day avg = thick = support/resistance holding
"""

import numpy as np
from datetime import datetime, timedelta

np.random.seed(42)

# === SYNTHETIC DATA ===
start_date = datetime(2025, 10, 1)
n_days = 198
dates = np.array([start_date + timedelta(days=i) for i in range(n_days)])
base_price = 1500
returns = np.random.normal(0.0012, 0.06, n_days)
close = base_price * np.cumprod(1 + returns)
high = close * (1 + np.abs(np.random.normal(0, 0.025, n_days)))
low = close * (1 - np.abs(np.random.normal(0, 0.025, n_days)))
volumes = np.random.lognormal(15, 1.2, n_days)

# === INDICATORS ===
def calc_rsi(prices, period=14):
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    rsi = np.zeros(len(prices))
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    rsi[period] = 100 - (100 / (1 + avg_gain / (avg_loss + 1e-10)))
    for i in range(period+1, len(prices)):
        avg_gain = (avg_gain * (period-1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period-1) + losses[i-1]) / period
        rsi[i] = 100 - (100 / (1 + avg_gain / (avg_loss + 1e-10)))
    return rsi

def calc_ema(prices, period):
    ema = np.zeros_like(prices)
    ema[0] = prices[0]
    mult = 2 / (period + 1)
    for i in range(1, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * mult + ema[i-1]
    return ema

rsi   = calc_rsi(close)
ema9  = calc_ema(close, 9)
ema21 = calc_ema(close, 21)
vol_sma = np.convolve(volumes, np.ones(14)/14, mode='same')

# === PARAMS ===
RSI_OVERSOLD   = 45
RSI_OVERBOUGHT = 55
VOL_THRESH     = 1.0
INITIAL_IDR    = 1_000_000
WARMUP         = 30

# === BACKTEST ===
position = 0
coin = 0
idr = INITIAL_IDR
equity = []
trades = []
cross_recent = 0  # bars since last EMA cross

for i in range(WARMUP, n_days):
    c = close[i]
    r = rsi[i]
    vol_r = volumes[i] / (vol_sma[i] + 1e-10)
    e9 = ema9[i]
    e21 = ema21[i]
    is_last = (i == n_days - 1)
    thick = vol_r > VOL_THRESH

    # Detect cross this bar
    cross = 0
    if i > WARMUP:
        if ema9[i] > ema21[i] and ema9[i-1] <= ema21[i-1]:
            cross = 1
        elif ema9[i] < ema21[i] and ema9[i-1] >= ema21[i-1]:
            cross = -1
    if cross != 0:
        cross_recent = 3
    elif cross_recent > 0:
        cross_recent -= 1

    # === ENTRY ===
    if position == 0:
        # LONG: RSI oversold + price > EMA9 (recovering) + thick vol
        long_sig = (r < RSI_OVERSOLD) and (c > e9) and thick
        # Also: recent bullish cross + still in oversold zone + thick
        long_sig = long_sig or (cross_recent > 0 and cross == 1 and r < RSI_OVERBOUGHT and thick)

        if long_sig and not is_last:
            coin = idr / c
            idr = 0
            position = 1
            trades.append({
                'action': 'BUY', 'date': dates[i], 'price': c,
                'rsi': r, 'vol_r': vol_r,
                'ema_diff': (e9-e21)/e21*100
            })

    # === EXIT ===
    elif position == 1:
        # Exit: price crosses below EMA9, or RSI overbought, or end
        exit_sig = (c < e9) or (r > RSI_OVERBOUGHT + 10) or is_last
        if exit_sig:
            idr = coin * c
            coin = 0
            position = 0
            trades.append({
                'action': 'SELL', 'date': dates[i], 'price': c,
                'rsi': r, 'vol_r': vol_r,
                'ema_diff': (e9-e21)/e21*100,
                'pnl_idr': idr - INITIAL_IDR,
                'ret_pct': (c - trades[-1]['price']) / trades[-1]['price'] * 100
            })

    # Equity
    equity.append(idr + coin * c if position == 1 else idr)

# === STATS ===
eq = np.array(equity)
closed = [t for t in trades if t['action'] == 'SELL']
open_trades = [t for t in trades if t['action'] == 'BUY']
wins = [t for t in closed if t.get('pnl_idr', 0) > 0]
final_ret = (eq[-1] - INITIAL_IDR) / INITIAL_IDR * 100
bh_ret = (close[-1] - close[WARMUP]) / close[WARMUP] * 100

print(f"""
======================================================================
📊 BACKTEST: Orderbook + RSI + EMA Strategy
======================================================================
Pair         : DOGEIDR (synthetic, Oct 2025 - Apr 2026)
RSI Zones    : <{RSI_OVERSOLD} oversold | >{RSI_OVERBOUGHT} overbought
Orderbook    : Volume > {VOL_THRESH}x 14-day avg = thick (support/resistance)
EMA          : 9 vs 21
Initial IDR  : {INITIAL_IDR:,}
======================================================================
📈 RESULTS
----------------------------------------------------------------------
Strategy Return : {final_ret:+.2f}%
Buy & Hold      : {bh_ret:+.2f}%
Alpha           : {final_ret - bh_ret:+.2f}%
----------------------------------------------------------------------
Trades Closed   : {len(closed)}
Open Positions  : {len(open_trades)}
Win Rate        : {len(wins)/len(closed)*100:.0f}% ({len(wins)}/{len(closed)})
""")

if closed:
    pnls = [t['pnl_idr'] for t in closed]
    print(f"Best Trade      : Rp {max(pnls):+,.0f}  ({max(t['ret_pct'] for t in closed):+.1f}%)")
    print(f"Worst Trade     : Rp {min(pnls):+,.0f}  ({min(t['ret_pct'] for t in closed):+.1f}%)")
    print(f"Avg Trade       : Rp {np.mean(pnls):+,.0f}")

print(f"""
======================================================================
📜 TRADE LOG
======================================================================""")
for t in trades:
    if t['action'] == 'BUY':
        print(f"  BUY  {str(t['date'])[:10]}  Rp {t['price']:8,.0f}  RSI:{t['rsi']:4.0f}  VolR:{t['vol_r']:.2f}  EMAΔ:{t['ema_diff']:+.1f}%")
    else:
        pnl_str = f"  PnL: Rp {t['pnl_idr']:+,.0f} ({t['ret_pct']:+.1f}%)"
        print(f"  SELL {str(t['date'])[:10]}  Rp {t['price']:8,.0f}  RSI:{t['rsi']:4.0f}  VolR:{t['vol_r']:.2f}  EMAΔ:{t['ema_diff']:+.1f}%  {pnl_str}")

print(f"""
======================================================================
📉 EQUITY CURVE
======================================================================""")
step = max(1, len(eq)//15)
for j, idx in enumerate(range(0, len(eq), step)):
    pct = eq[idx] / INITIAL_IDR * 100
    bar = "█" * max(0, int((pct - 85) / 15 * 25))
    print(f"  {str(dates[WARMUP+idx])[:10]}  {pct:+7.2f}%  {bar}")

print(f"""
======================================================================
⚠️  NOTES:
  - Synthetic data (no real orderbook - Indodax REST API doesn't expose it)
  - Volume spike = orderbook thickness PROXY (high vol = defended levels)
  - Real BID/ASK depth requires WebSocket connection to Indodax
  - Try tuning RSI thresholds for different asset volatility profiles
======================================================================""")
