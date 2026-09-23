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
            f"ℹ️ Modal pokok tetap aman di Spot untuk compound/trade berikutnya. Profit bersih telah diamankan ke Funding Wallet siap untuk ditarik / cadangan server! 🛡️🚀"
        )
        telegram_send(msg)
        return True
    return False
