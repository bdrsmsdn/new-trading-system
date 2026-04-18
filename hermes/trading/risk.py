"""
Risk management tools: Monte Carlo simulation and Kelly criterion position sizing.
"""
import random
from typing import List, Dict
from hermes.logging_setup import log


def monte_carlo_sim(trades: List[Dict], n_simulations: int = 1000) -> Dict:
    """
    Simulate equity curves by randomly shuffling trade outcomes.

    Args:
        trades: List[dict] — each dict has {pnl_pct: float, hold_hours: float}
        n_simulations: Number of Monte Carlo runs (default 1000)

    Returns:
        {
            'p10': float   — 10th percentile final equity
            'p50': float   — 50th percentile (median)
            'p90': float   — 90th percentile
            'max_drawdown_pct': float — avg max drawdown across sims
            'avg_trades': float       — average number of trades per sim
        }
    """
    if not trades:
        return {"p10": 0, "p50": 0, "p90": 0, "max_drawdown_pct": 0, "avg_trades": 0}

    pnl_list = [t["pnl_pct"] for t in trades]
    initial = 1_000_000

    final_equities: List[float] = []
    max_drawdowns: List[float] = []

    for _ in range(n_simulations):
        equity = float(initial)
        peak = equity
        max_dd = 0.0

        # Shuffle trade order for this simulation
        order = list(range(len(pnl_list)))
        random.shuffle(order)

        for idx in order:
            pnl = pnl_list[idx]
            equity *= (1 + pnl)
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        final_equities.append(equity)
        max_drawdowns.append(max_dd)

    final_equities.sort()
    n = len(final_equities)
    p10_idx = max(0, int(n * 0.10) - 1)
    p50_idx = max(0, int(n * 0.50) - 1)
    p90_idx = max(0, int(n * 0.90) - 1)

    return {
        "p10": final_equities[p10_idx],
        "p50": final_equities[p50_idx],
        "p90": final_equities[p90_idx],
        "max_drawdown_pct": sum(max_drawdowns) / len(max_drawdowns),
        "avg_trades": float(len(trades)),
    }


def kelly_criterion(win_rate: float, avg_win: float, avg_loss: float, safety: float = 0.25) -> float:
    """
    Calculate Kelly bet size fraction.

    Kelly % = (win_rate * avg_win - avg_loss) / avg_win
    safety = 0.25 means use 1/4 Kelly (recommended for trading).

    Returns: fraction of capital to risk (e.g., 0.15 = 15%)
    """
    if avg_win <= 0 or avg_loss <= 0:
        return 0.0

    raw_kelly = (win_rate * avg_win - avg_loss) / avg_win
    if raw_kelly <= 0:
        return 0.0

    return raw_kelly * safety


def calc_position_size_kelly(
    capital: float,
    risk_pct: float,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
) -> Dict:
    """
    Calculate position size using Kelly criterion.

    Returns:
        {
            'kelly_pct': float       — raw Kelly %
            'safe_kelly_pct': float — safe Kelly % (with 0.25x safety factor)
            'position_rp': float    — Rp amount to risk
            'max_loss_rp': float   — max loss if stop hit
        }
    """
    kelly_pct = kelly_criterion(win_rate, avg_win, avg_loss, safety=1.0)
    safe_kelly_pct = kelly_criterion(win_rate, avg_win, avg_loss, safety=0.25)

    risk_amount = capital * risk_pct
    position_rp = risk_amount / safe_kelly_pct if safe_kelly_pct > 0 else 0
    max_loss_rp = risk_amount

    return {
        "kelly_pct": kelly_pct * 100,
        "safe_kelly_pct": safe_kelly_pct * 100,
        "position_rp": min(position_rp, capital),
        "max_loss_rp": max_loss_rp,
    }
