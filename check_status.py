import json
from hermes.trading.futures import get_futures_account_overview
from hermes.api.balance import get_spot_account_overview

fut = get_futures_account_overview()
spot = get_spot_account_overview()

print("=== FUTURES OVERVIEW ===")
print("Wallet Balance :", fut.get("total_wallet_balance"))
print("Available      :", fut.get("available_balance"))
print("Unrealized PnL :", fut.get("unrealized_pnl"))
print("Open Positions :", json.dumps(fut.get("open_positions"), indent=2))

print("\n=== SPOT OVERVIEW ===")
print("Total Spot USD :", spot.get("total_portfolio_usdt"))
for h in spot.get("holdings", []):
    print(f"• {h.get('asset')}: {h.get('total')} (${h.get('usdt_value'):.2f})")
