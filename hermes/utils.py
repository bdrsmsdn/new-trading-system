"""Utility functions for Hermes Trading System.
Provides dynamic price and quantity formatters for all crypto asset types
(from high-cap BTC/ETH to micro meme coins like PEPE/FLOKI/SHIB).
"""

def format_price(price, include_dollar: bool = True) -> str:
    """Format price dynamically based on magnitude.
    Prevents micro-price meme coins (PEPE, FLOKI, SHIB) from truncating to $0.0000.
    """
    if price is None:
        return "$0.00" if include_dollar else "0.00"
    try:
        p = float(price)
    except (ValueError, TypeError):
        return str(price)
    
    abs_p = abs(p)
    prefix = "$" if include_dollar else ""
    if abs_p == 0:
        return f"{prefix}0.00"
    elif abs_p >= 100:
        return f"{prefix}{p:,.2f}"
    elif abs_p >= 1:
        return f"{prefix}{p:,.4f}"
    elif abs_p >= 0.01:
        return f"{prefix}{p:.4f}"
    elif abs_p >= 0.0001:
        return f"{prefix}{p:.6f}"
    elif abs_p >= 0.000001:
        return f"{prefix}{p:.8f}"
    else:
        val_str = f"{p:.10f}".rstrip("0")
        if val_str.endswith("."):
            val_str += "00"
        return f"{prefix}{val_str}"


def format_qty(qty) -> str:
    """Format asset quantity dynamically.
    Avoids trailing excessive decimals for large quantities (e.g. 345,833.000000 PEPE -> 345,833 PEPE).
    """
    if qty is None:
        return "0"
    try:
        q = float(qty)
    except (ValueError, TypeError):
        return str(qty)
    
    if q == 0:
        return "0"
    abs_q = abs(q)
    if abs_q >= 100:
        if q == int(q):
            return f"{int(q):,}"
        return f"{q:,.2f}"
    elif abs_q >= 1:
        if q == int(q):
            return f"{int(q):,}"
        return f"{q:,.4f}".rstrip("0").rstrip(".")
    elif abs_q >= 0.001:
        return f"{q:,.6f}".rstrip("0").rstrip(".")
    else:
        return f"{q:.8f}".rstrip("0").rstrip(".")
