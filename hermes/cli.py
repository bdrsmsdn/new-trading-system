import argparse
import json
import sys
import asyncio
from hermes.logging_setup import log
from hermes.api.balance import get_balance
from hermes.api.rest import fetch_price_rest, get_rest_budget_status
from hermes.indicators.fear_greed import fetch_fear_greed
from hermes.indicators.signals import get_signal, get_market_regime
from hermes.indicators.rsi import get_multi_rsi
from hermes.config import MAX_TRADE_USDT
from hermes.indicators.strategy_new import get_signal_v2, StrategyV2
from hermes.display.analysis import print_analysis, print_analysis_v2
from hermes.display.dashboard import print_portfolio_dashboard
from hermes.trading.positions import check_open_positions
from hermes.trading.execution import execute_buy, execute_sell
from hermes.daemon.tasks import rank_all_pairs, run_daemon
from hermes.state import state, prices

def to_json(data):
    print(json.dumps(data, indent=2))

def main():
    parser = argparse.ArgumentParser(prog="hermes", description="Hermes Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    
    sub.add_parser("get-balance", help="Get account balance")
    
    p = sub.add_parser("get-price", help="Get current price")
    p.add_argument("pair")
    
    p = sub.add_parser("get-signal", help="Get trading signal")
    p.add_argument("pair")
    
    p = sub.add_parser("analyze", help="Run analysis")
    p.add_argument("--pair", default=None)
    p.add_argument("--all", action="store_true")
    
    p = sub.add_parser("signal-v2", help="Run Strategy V2 (RSI+EMA+Orderbook) analysis")
    p.add_argument("--pair", default=None, help="Specific pair to analyze")
    p.add_argument("--all", action="store_true", help="Analyze all pairs")
    p.add_argument("--risk", type=float, default=0.01, help="Risk percent (default: 0.01 = 1%%)")
    
    sub.add_parser("check-positions", help="Check TP/SL on open positions")
    sub.add_parser("sync-positions", help="Sync and reconcile open positions from Binance myTrades and Spot balance")
    
    p = sub.add_parser("execute-buy", help="Execute buy order")
    p.add_argument("pair")
    p.add_argument("--price", type=float, default=None)
    
    p = sub.add_parser("execute-sell", help="Execute sell order")
    p.add_argument("pair")
    p.add_argument("--qty", type=float, required=True)
    p.add_argument("--price", type=float, default=None)
    
    sub.add_parser("fear-greed", help="Get Fear & Greed index")
    sub.add_parser("portfolio", help="Show portfolio dashboard")
    sub.add_parser("rank-pairs", help="Rank all pairs by score")
    sub.add_parser("market-regime", help="Get market regime")
    sub.add_parser("state", help="Show current state")
    p_news = sub.add_parser("news", help="Get crypto news and sentiment")
    p_news.add_argument("pair", nargs="?", default=None, help="Specific coin (e.g. btc, sol, eth)")
    daemon_parser = sub.add_parser("daemon", help="Run autonomous daemon")
    daemon_parser.add_argument("--dry-run", action="store_true", help="Simulate trading without real orders")
    daemon_parser.add_argument("--with-agent", action="store_true", help="Also start Telegram AI agent in same process")

    one_shot_parser = sub.add_parser("one-shot", help="Run single trading iteration")
    one_shot_parser.add_argument("--dry-run", action="store_true", help="Simulate trading without real orders")
    sub.add_parser("budget", help="Show REST API budget status")
    sub.add_parser("agent", help="Start Telegram AI chatbot agent")

    # Phase 2 Accounting & Rehearsal Operational Subcommands
    p_acc_init = sub.add_parser("accounting-init", help="Initialize isolated SQLite accounting DB and run migrations")
    p_acc_init.add_argument("--db-path", default=None, help="Database path override (defaults to ACCOUNTING_DB_PATH)")
    p_acc_init.add_argument("--account-id", default="default", help="Account ID (default: 'default')")

    p_acc_verify = sub.add_parser("accounting-verify", help="Verify accounting DB integrity, foreign keys, and migration checksums")
    p_acc_verify.add_argument("--db-path", default=None, help="Database path override (defaults to ACCOUNTING_DB_PATH)")

    p_acc_backup = sub.add_parser("accounting-backup", help="Create atomic online backup of accounting DB and state files")
    p_acc_backup.add_argument("--db-path", default=None, help="Database path override (defaults to ACCOUNTING_DB_PATH)")
    p_acc_backup.add_argument("--backup-dir", default=None, help="Directory to store backup files")

    p_acc_restore = sub.add_parser("accounting-restore", help="Restore accounting DB from backup file with preflight integrity checks")
    p_acc_restore.add_argument("--backup-file", required=True, help="Path to backup file to restore")
    p_acc_restore.add_argument("--db-path", default=None, help="Target database path (defaults to ACCOUNTING_DB_PATH)")

    p_acc_reconcile = sub.add_parser("accounting-reconcile", help="Execute dry-run reconciliation rehearsal on archived or live trade data")
    p_acc_reconcile.add_argument("--db-path", default=None, help="Database path override (defaults to ACCOUNTING_DB_PATH)")
    p_acc_reconcile.add_argument("--trades-file", default=None, help="JSON file containing archived Binance myTrades payloads")
    p_acc_reconcile.add_argument("--dry-run", action="store_true", default=True, help="Dry-run mode: zero live orders or transfers (default: True)")

    p_acc_status = sub.add_parser("accounting-status", help="Get summary of accounting cutover, verified PnL, surplus, and blockers")
    p_acc_status.add_argument("--db-path", default=None, help="Database path override (defaults to ACCOUNTING_DB_PATH)")
    p_acc_status.add_argument("--account-id", default="default", help="Account ID (default: 'default')")
    
    args = parser.parse_args()
    
    if args.command == "get-balance":
        to_json(get_balance(use_cache=False))
        
    elif args.command == "get-price":
        price = fetch_price_rest(args.pair)
        to_json({"pair": args.pair, "price": price})
        
    elif args.command == "get-signal":
        price = fetch_price_rest(args.pair)
        if not price:
            to_json({"error": "no_price"})
            return
        multi_rsi = get_multi_rsi(args.pair, price)
        signal, score, reasons = get_signal(args.pair, price, multi_rsi)
        to_json({"pair": args.pair, "signal": signal, "score": score, "reasons": reasons})
        
    elif args.command == "analyze":
        if args.all:
            analyses = print_analysis(get_balance)
            to_json({"analyses": analyses})
        elif args.pair:
            analyses = print_analysis(get_balance, pair=args.pair)
            to_json({"analyses": analyses})
        else:
            to_json({"error": "Specify --pair X or --all"})
            
    elif args.command == "signal-v2":
        # Strategy V2: RSI + EMA + Orderbook analysis
        balance = get_balance(use_cache=True)
        capital = balance.get("usdt", MAX_TRADE_USDT)
        
        if args.pair:
            # Single pair analysis
            signal = get_signal_v2(args.pair, capital=capital, risk_pct=args.risk)
            to_json({"pair": args.pair, "signal": signal})
        elif args.all:
            # Analyze all pairs using display function
            results = print_analysis_v2(get_balance)
            to_json({"results": results})
        else:
            to_json({"error": "Specify --pair X or --all for signal-v2"})
            
    elif args.command == "check-positions":
        # Use WS prices, not REST
        balance = get_balance(use_cache=True)
        for pair in list(state.positions.keys()):
            current_price = prices.get(pair, {}).get("price")
            if not current_price:
                current_price = fetch_price_rest(pair)
            if current_price:
                check_open_positions(current_price, balance, specific_pair=pair)
        to_json({"status": "checked"})

    elif args.command == "sync-positions":
        from hermes.trading.reconcile import reconcile_positions_from_binance
        reconciled = reconcile_positions_from_binance(save_to_state=True)
        to_json({"status": "synced", "positions_count": len(reconciled), "positions": reconciled})
        
    elif args.command == "execute-buy":
        balance = get_balance(use_cache=False)
        usdt = balance.get("usdt", 0)
        price = args.price or fetch_price_rest(args.pair)
        if not price:
            to_json({"error": "no_price"})
            return
        if usdt < MIN_TRADE_USDT:
            to_json({"error": "below_minimum_trade", "message": f"USDT balance (${usdt}) below minimum trade size ($10)"})
            return
        success = execute_buy(args.pair, price, usdt)
        to_json({"success": success})
        
    elif args.command == "execute-sell":
        price = args.price or fetch_price_rest(args.pair)
        if not price:
            to_json({"error": "no_price"})
            return
        qty = args.qty
        usdt_value = qty * price
        if usdt_value < MIN_TRADE_USDT:
            to_json({"error": "below_minimum_trade", "message": f"Sell order value (${usdt_value:.2f}) below minimum ($10)"})
            return
        success, _ = execute_sell(args.pair, price, qty, reason="cli_manual")
        to_json({"success": success})
        
    elif args.command == "fear-greed":
        fg_val, fg_class = fetch_fear_greed()
        to_json({"fear_greed_value": fg_val, "classification": fg_class})
        
    elif args.command == "portfolio":
        dash = print_portfolio_dashboard(get_balance)
        to_json(dash)
        
    elif args.command == "rank-pairs":
        # Use WS-based init, not REST burst
        from hermes.api.rest import fetch_all_prices
        fetch_all_prices()
        rankings = asyncio.run(rank_all_pairs())
        result = [{"pair": p, "score": s, "signal": sig, "daily_pos": dp} for p, s, sig, dp in rankings]
        budget = get_rest_budget_status()
        to_json({"rankings": result, "rest_budget": budget})
        
    elif args.command == "market-regime":
        regime, desc = get_market_regime()
        to_json({"regime": regime, "description": desc})
        
    elif args.command == "state":
        to_json({
            "positions": state.positions,
            "active_pairs": state.active_pairs,
            "fg_value": state.fg_value,
            "fg_class": state.fg_class
        })
    
    elif args.command == "budget":
        to_json(get_rest_budget_status())

    elif args.command == "news":
        from hermes.indicators.news_sentiment import format_news_telegram
        print(format_news_telegram(args.pair))

    elif args.command == "daemon":
        from hermes.config import PID_FILE
        if PID_FILE.exists():
            import os
            try:
                old_pid = int(PID_FILE.read_text().strip())
                os.kill(old_pid, 0)
                print(f"Daemon already running (PID {old_pid}). Exiting.")
                sys.exit(1)
            except OSError:
                pass
        import os
        PID_FILE.write_text(str(os.getpid()))
        try:
            state.dry_run = getattr(args, 'dry_run', False)
            if state.dry_run:
                log.info("DRY_RUN MODE — No real orders will be executed")

            with_agent = getattr(args, 'with_agent', False)

            if with_agent:
                # Run both daemon and agent together
                async def run_with_agent():
                    from hermes.agent.bot import run_telegram_bot
                    daemon_coro = run_daemon(get_balance, dry_run=state.dry_run)
                    agent_coro = run_telegram_bot()
                    await asyncio.gather(daemon_coro, agent_coro)
                asyncio.run(run_with_agent())
            else:
                asyncio.run(run_daemon(get_balance, dry_run=state.dry_run))
        finally:
            if PID_FILE.exists():
                PID_FILE.unlink()
                
    elif args.command == "one-shot":
        from hermes.api.rest import fetch_all_prices
        from hermes.config import MIN_TRADE_USDT, FG_BUY_THRESHOLD, MAX_TRADE_USDT
        from hermes.trading.positions import check_for_entries

        state.dry_run = getattr(args, 'dry_run', False)
        if state.dry_run:
            log.info("DRY_RUN MODE — No real orders will be executed")

        fg_val, fg_class = fetch_fear_greed()
        balance = get_balance(use_cache=False)
        usdt = balance.get("usdt", 0)

        # WS-based price init
        fetch_all_prices()

        # Check positions using WS prices
        for pair in list(state.positions.keys()):
            current_price = prices.get(pair, {}).get("price")
            if not current_price:
                current_price = fetch_price_rest(pair)
            if current_price:
                check_open_positions(current_price, balance, specific_pair=pair)

        if fg_val <= FG_BUY_THRESHOLD:
            for pair in state.active_pairs:
                current_price = prices.get(pair, {}).get("price")
                if not current_price:
                    current_price = fetch_price_rest(pair)
                if current_price:
                    if check_for_entries(pair, current_price, usdt, dry_run=state.dry_run):
                        usdt -= MAX_TRADE_USDT
        state.save()
        budget = get_rest_budget_status()
        to_json({"status": "one-shot completed", "rest_budget": budget})

    elif args.command == "agent":
        from hermes.agent.bot import start_bot
        print("🤖 Starting Hermes AI Trading Agent (Telegram)...")
        print("   Model: MiniMax-M2.7 (Anthropic-compatible)")
        print("   Press Ctrl+C to stop")
        start_bot()

    elif args.command == "accounting-init":
        from hermes.config import ACCOUNTING_DB_PATH
        from hermes.accounting.ops import init_isolated_db
        db = args.db_path or ACCOUNTING_DB_PATH
        res = init_isolated_db(db, account_id=args.account_id)
        to_json(res)

    elif args.command == "accounting-verify":
        from hermes.config import ACCOUNTING_DB_PATH
        from hermes.accounting.ops import verify_db
        db = args.db_path or ACCOUNTING_DB_PATH
        res = verify_db(db)
        to_json(res)

    elif args.command == "accounting-backup":
        from hermes.config import ACCOUNTING_DB_PATH
        from hermes.accounting.ops import backup_accounting_state
        db = args.db_path or ACCOUNTING_DB_PATH
        res = backup_accounting_state(db, backup_dir=args.backup_dir)
        to_json(res)

    elif args.command == "accounting-restore":
        from hermes.config import ACCOUNTING_DB_PATH
        from hermes.accounting.ops import restore_accounting_state
        db = args.db_path or ACCOUNTING_DB_PATH
        res = restore_accounting_state(args.backup_file, target_db_path=db)
        to_json(res)

    elif args.command == "accounting-reconcile":
        from hermes.config import ACCOUNTING_DB_PATH
        from hermes.accounting.ops import rehearse_reconciliation
        db = args.db_path or ACCOUNTING_DB_PATH
        res = rehearse_reconciliation(db, trades_file=args.trades_file, dry_run=args.dry_run)
        to_json(res)

    elif args.command == "accounting-status":
        from hermes.config import ACCOUNTING_DB_PATH
        from hermes.accounting.ops import get_accounting_status
        db = args.db_path or ACCOUNTING_DB_PATH
        res = get_accounting_status(db, account_id=args.account_id)
        to_json(res)

if __name__ == "__main__":
    main()
