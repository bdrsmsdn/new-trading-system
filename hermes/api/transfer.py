"""Internal Binance transfer helpers for profit isolation and wallet management.
Moves trading profits from Spot (MAIN) to Funding Wallet (FUNDING) to preserve
principal capital and accumulate real cash flow for P2P IDR or operational funds.
"""
from hermes.api.auth import binance_signed_request
from hermes.logging_setup import log
from hermes.notifications.telegram import telegram_send
from hermes.utils import format_price

def transfer_spot_to_funding(asset: str = "USDT", amount: float = 0.0) -> dict:
    """Transfer an asset from Spot (MAIN) to Funding wallet."""
    if amount <= 0:
        return {"error": "Invalid amount"}
    
    # Binance accepts up to 4-8 decimals depending on asset, 4 is safe for USDT
    amount_str = f"{amount:.4f}".rstrip("0").rstrip(".")
    if not amount_str or float(amount_str) <= 0:
        amount_str = f"{amount:.4f}"

    try:
        res = binance_signed_request(
            endpoint="/sapi/v1/asset/transfer",
            params={
                "type": "MAIN_FUNDING",
                "asset": asset.upper(),
                "amount": amount_str
            },
            method="POST"
        )
        if "tranId" in res:
            log.info(f"[TRANSFER] Successfully transferred {amount_str} {asset} from Spot to Funding (tranId: {res['tranId']})")
        else:
            log.warning(f"[TRANSFER] Transfer response: {res}")
        return res
    except Exception as e:
        log.error(f"[TRANSFER] Failed to transfer {amount} {asset} to Funding: {e}")
        return {"error": str(e)}

def transfer_funding_to_spot(asset: str = "USDT", amount: float = 0.0) -> dict:
    """Transfer an asset from Funding to Spot (MAIN) wallet."""
    if amount <= 0:
        return {"error": "Invalid amount"}
    
    amount_str = f"{amount:.4f}".rstrip("0").rstrip(".")
    try:
        res = binance_signed_request(
            endpoint="/sapi/v1/asset/transfer",
            params={
                "type": "FUNDING_MAIN",
                "asset": asset.upper(),
                "amount": amount_str
            },
            method="POST"
        )
        return res
    except Exception as e:
        log.error(f"[TRANSFER] Failed to transfer {amount} {asset} to Spot: {e}")
        return {"error": str(e)}

def sweep_profit_to_funding(profit_usdt: float, min_threshold: float = 0.05, pair: str = "") -> bool:
    """Sweep realized profit from Spot to Funding wallet.
    Preserves principal capital in Spot while isolating gains in Funding.
    """
    if profit_usdt < min_threshold:
        log.info(f"[PROFIT-SWEEP] Realized profit ${profit_usdt:.4f} below sweep threshold ${min_threshold:.2f}, kept in Spot.")
        return False

    res = transfer_spot_to_funding(asset="USDT", amount=profit_usdt)
    if "tranId" in res:
        msg = (
            f"🔒 *HERMES PROFIT AUTO-ISOLATED*\n"
            f"────────────────────\n"
            f"💰 Nominal Profit: *+${profit_usdt:.4f} USDT*\n"
            f"📦 Dari Pair: *{pair.upper() if pair else 'SPOT'}*\n"
            f"🏦 Tujuan: *Funding Wallet (Dompet Pendanaan)*\n"
            f"────────────────────\n"
            f"ℹ️ Modal trading aktif tetap berada di Spot untuk siklus trade berikutnya. Profit ditransfer ke Funding Wallet sesuai aturan alokasi (bukan garansi modal anti-rugi). 🛡️🚀"
        )
        telegram_send(msg)
        return True
    return False


# ---------------------------------------------------------------------------
# DAILY PROFIT COLLECTION MODEL
# ---------------------------------------------------------------------------
# Instead of sweeping every trade's profit (which starves Spot of compounding
# capital), profits accumulate in Spot. A collector runs periodically in the
# daemon; once the accumulated realized profit for the day reaches
# DAILY_PROFIT_TARGET_USDT, exactly that amount is transferred to Funding and
# the day is marked as "collected". Remaining profit compounds in Spot.
# ---------------------------------------------------------------------------
from datetime import datetime, timezone
import json
import os

from hermes.config import DAILY_PROFIT_COLLECTION, DAILY_PROFIT_TARGET_USDT

_DAILY_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daily_profit_state.json")


def _today_key() -> str:
    """UTC date key for the current day (YYYY-MM-DD)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_daily_state() -> dict:
    try:
        with open(_DAILY_STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_daily_state(state: dict) -> None:
    try:
        with open(_DAILY_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"[DAILY-PROFIT] Failed to save state: {e}")


def track_realized_profit(pair: str, pnl_usdt: float) -> None:
    """Accumulate realized profit into today's ledger (no transfer)."""
    if pnl_usdt <= 0:
        return
    state = _load_daily_state()
    today = _today_key()
    day = state.setdefault(today, {"collected_profit": 0.0, "target_met": False})
    if day.get("target_met"):
        return  # daily target already collected; let extra profit compound
    day["collected_profit"] = round(day.get("collected_profit", 0.0) + pnl_usdt, 6)
    _save_daily_state(state)
    log.info(f"[DAILY-PROFIT] {today} accumulated: ${day['collected_profit']:.4f} / ${DAILY_PROFIT_TARGET_USDT:.2f} (from {pair})")


def run_daily_profit_collector() -> bool:
    """Once accumulated profit >= daily target, transfer exactly the target
    amount to Funding Wallet and mark the day as collected.

    Returns True if a collection transfer was executed.
    """
    if not DAILY_PROFIT_COLLECTION:
        return False

    state = _load_daily_state()
    today = _today_key()
    day = state.get(today)
    if not day or day.get("target_met"):
        return False

    accumulated = float(day.get("collected_profit", 0.0))
    if accumulated < DAILY_PROFIT_TARGET_USDT:
        return False

    # Ensure Spot USDT balance can cover the transfer
    try:
        from hermes.api.balance import get_balance
        balances = get_balance(use_cache=False)
        spot_usdt = float(balances.get("usdt", 0) or balances.get("USDT", 0))
        log.info(f"[DAILY-PROFIT] Spot USDT balance: ${spot_usdt:.2f}")
    except Exception as e:
        log.error(f"[DAILY-PROFIT] Balance check failed: {e}")
        return False

    if spot_usdt < DAILY_PROFIT_TARGET_USDT:
        log.info(f"[DAILY-PROFIT] Spot USDT ${spot_usdt:.2f} below target ${DAILY_PROFIT_TARGET_USDT:.2f}, deferring collection.")
        return False

    res = transfer_spot_to_funding(asset="USDT", amount=DAILY_PROFIT_TARGET_USDT)
    if "tranId" not in res:
        log.error(f"[DAILY-PROFIT] Transfer failed: {res}")
        return False

    day["target_met"] = True
    _save_daily_state(state)

    surplus = accumulated - DAILY_PROFIT_TARGET_USDT
    msg = (
        f"🎯 *HERMES DAILY PROFIT TARGET TERCAPAI!*\n"
        f"────────────────────\n"
        f"📅 Tanggal (UTC): *{today}*\n"
        f"💰 Profit Terkumpul Hari Ini: *${accumulated:.4f} USDT*\n"
        f"🏦 Dikirim ke Funding Wallet: *${DAILY_PROFIT_TARGET_USDT:.2f} USDT*\n"
        f"♻️ Sisa Profit Compound di Spot: *${surplus:.4f} USDT*\n"
        f"────────────────────\n"
        f"✅ Target harian 1 USDT/hari tercapai! Sisa profit tetap compound sebagai modal trading di Spot. 🚀🛡️"
    )
    telegram_send(msg)
    log.info(f"[DAILY-PROFIT] ✅ Daily target collected: ${DAILY_PROFIT_TARGET_USDT:.2f} to Funding (accumulated: ${accumulated:.4f})")
    return True
