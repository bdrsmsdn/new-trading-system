import logging
import sys
import time
import json
from collections import deque, defaultdict
from datetime import datetime
from urllib.parse import urlparse
import traceback
from pathlib import Path

# Provide SCRIPT_DIR dynamically based on cwd to avoid hardcoding path
SCRIPT_DIR = Path.cwd()
TRADE_LOG = SCRIPT_DIR / "hermes_trades.log"
HTTP_LOG_FILE = SCRIPT_DIR / "hermes_http.log"
RATELIMIT_LOG = SCRIPT_DIR / "hermes_ratelimit.log"

class HttpRequestTracker:
    """Tracks all HTTP requests to identify 429 sources."""
    
    def __init__(self):
        self.request_log = deque(maxlen=500)  # Rolling log of last 500 requests
        self.endpoint_counts = defaultdict(int)  # {endpoint: count} per minute
        self.last_reset = time.time()
    
    def log_request(self, url, method, caller, status_code, latency_ms):
        """Log a request and check rate limit proximity."""
        # Reset counts every minute
        if time.time() - self.last_reset > 60:
            self.endpoint_counts.clear()
            self.last_reset = time.time()

        entry = {
            "ts": datetime.now().isoformat(),
            "url": url,
            "method": method, 
            "caller": caller,
            "status": str(status_code),
            "latency_ms": latency_ms,
        }
        self.request_log.append(entry)
        
        # Parse endpoint base
        endpoint = urlparse(url).path
        self.endpoint_counts[endpoint] += 1
        
        # Log to http log
        http_log.info(f"{method} {url} [{status_code}] {latency_ms:.1f}ms - caller: {caller}")
        
        # On 429: dump recent request history for debugging
        if str(status_code) == "429":
            self._dump_429_debug(entry)
    
    def _dump_429_debug(self, trigger_entry):
        """When 429 hits, dump last 60s of requests to identify the culprit."""
        cutoff_iso = datetime.fromtimestamp(time.time() - 60).isoformat()
        recent = [r for r in self.request_log if r["ts"] > cutoff_iso]
        
        by_caller = defaultdict(int)
        for r in recent:
            by_caller[r["caller"]] += 1
        
        ratelimit_log.error(f"[!] HTTP 429 TRIGGERED!")
        ratelimit_log.error(f"   URL: {trigger_entry['url']}")
        ratelimit_log.error(f"   Caller: {trigger_entry['caller']}")
        ratelimit_log.error(f"   Requests in last 60s: {len(recent)}")
        ratelimit_log.error(f"   By caller: {dict(by_caller)}")
        ratelimit_log.error(f"   By endpoint: {dict(self.endpoint_counts)}")
        ratelimit_log.error(f"   Traceback:\n{''.join(traceback.format_stack()[:-2])}")


# ─── ANSI Color Codes ──────────────────────────────────────────────────────────

class _C:
    """ANSI color constants."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    BLACK   = "\033[30m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"

    BR_RED     = "\033[91m"
    BR_GREEN   = "\033[92m"
    BR_YELLOW  = "\033[93m"
    BR_BLUE    = "\033[94m"
    BR_MAGENTA = "\033[95m"
    BR_CYAN    = "\033[96m"
    BR_WHITE   = "\033[97m"

    BG_RED    = "\033[41m"
    BG_GREEN  = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"


def _supports_color() -> bool:
    """Check whether the terminal supports ANSI colors."""
    # Windows: enable VT sequences via kernel32
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-12), 7)
            return True
        except Exception:
            return False
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


_COLOR_ENABLED = _supports_color()


class ColorFormatter(logging.Formatter):
    """
    Colorized console formatter for Hermes trading logs.

    Level colors:
        DEBUG   → dim white
        INFO    → bright cyan
        WARNING → bright yellow  (bold)
        ERROR   → bright red     (bold)
        CRITICAL→ white on red bg (bold)

    Keyword highlights (within message):
        ✅ / SUCCESS  → green
        ❌ / FAILED   → red
        BUY           → bright green
        SELL          → bright red / magenta
        Rp xxx,xxx    → yellow
        [TAG]         → magenta brackets with cyan tag
        +X.X%         → green,  -X.X% → red
    """

    LEVEL_STYLES = {
        logging.DEBUG:    (_C.DIM + _C.WHITE,               "DBG"),
        logging.INFO:     (_C.BR_CYAN,                      "INF"),
        logging.WARNING:  (_C.BOLD + _C.BR_YELLOW,          "WRN"),
        logging.ERROR:    (_C.BOLD + _C.BR_RED,             "ERR"),
        logging.CRITICAL: (_C.BOLD + _C.BG_RED + _C.WHITE, "CRT"),
    }

    # Keyword → color  (order matters — more specific first)
    KEYWORD_HIGHLIGHTS = [
        ("STRONG_BUY",   _C.BOLD + _C.BR_GREEN),
        ("STRONG_SELL",  _C.BOLD + _C.BR_RED),
        ("STRONG BUY",   _C.BOLD + _C.BR_GREEN),
        ("STRONG SELL",  _C.BOLD + _C.BR_RED),
        ("BUY SUCCESS",  _C.BOLD + _C.BR_GREEN),
        ("SELL SUCCESS", _C.BOLD + _C.BR_GREEN),
        ("BUY FAILED",   _C.BOLD + _C.BR_RED),
        ("SELL FAILED",  _C.BOLD + _C.BR_RED),
        ("✅",           _C.BR_GREEN),
        ("❌",           _C.BR_RED),
        ("⚠️",           _C.BR_YELLOW),
        ("🎯",           _C.BR_GREEN),
        ("🛑",           _C.BR_RED),
        ("🟢",           _C.BR_GREEN),
        ("🔴",           _C.BR_RED),
        ("🌅",           _C.BR_YELLOW),
        (" BUY",         _C.BR_GREEN),
        (" SELL",        _C.BR_MAGENTA),
        ("LONG",         _C.BR_GREEN),
        ("SHORT",        _C.BR_RED),
        ("BULL",         _C.BR_GREEN),
        ("BEAR",         _C.BR_RED),
        ("SIDEWAYS",     _C.BR_YELLOW),
        ("starting",     _C.DIM + _C.BR_CYAN),
        ("Starting",     _C.DIM + _C.BR_CYAN),
        ("Daemon",       _C.DIM + _C.BR_CYAN),
        ("daemon",       _C.DIM + _C.BR_CYAN),
        ("Error:",       _C.BR_RED),
        ("error:",       _C.BR_RED),
        ("FAILED",       _C.BR_RED),
        ("SKIPPED",      _C.BR_YELLOW),
        ("WARNING",      _C.BR_YELLOW),
    ]

    def _colorize_message(self, msg: str) -> str:
        """Apply keyword-based highlights to message text."""
        import re

        # [TAG] brackets → dim magenta [ ] with cyan tag content
        msg = re.sub(
            r'\[([A-Z0-9\-_/\.]+)\]',
            lambda m: (
                f"{_C.DIM}{_C.MAGENTA}[{_C.RESET}"
                f"{_C.BR_CYAN}{m.group(1)}{_C.RESET}"
                f"{_C.DIM}{_C.MAGENTA}]{_C.RESET}"
            ),
            msg
        )

        # Rp amounts → yellow
        msg = re.sub(
            r'(Rp[\s\u00a0][\d,\.]+)',
            lambda m: f"{_C.BR_YELLOW}{m.group(1)}{_C.RESET}",
            msg
        )

        # +X.X% → green, -X.X% → red
        msg = re.sub(
            r'(\+\d+\.?\d*%)',
            lambda m: f"{_C.BR_GREEN}{m.group(1)}{_C.RESET}",
            msg
        )
        msg = re.sub(
            r'(?<!\033\[9)(\-\d+\.?\d*%)',
            lambda m: f"{_C.BR_RED}{m.group(1)}{_C.RESET}",
            msg
        )

        # Keyword highlights
        for keyword, color in self.KEYWORD_HIGHLIGHTS:
            if keyword in msg:
                msg = msg.replace(keyword, f"{color}{keyword}{_C.RESET}")

        return msg

    def format(self, record: logging.LogRecord) -> str:
        if not _COLOR_ENABLED:
            # Plain fallback (file handlers / no-color terminals)
            plain_fmt = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            return plain_fmt.format(record)

        level_color, level_tag = self.LEVEL_STYLES.get(
            record.levelno, (_C.WHITE, "???")
        )

        # Timestamp → dim grey
        ts = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
        ts_str = f"{_C.DIM}{ts}{_C.RESET}"

        # Level tag → colored pill [INF] [WRN] etc.
        level_str = f"{level_color}[{level_tag}]{_C.RESET}"

        # Message
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        colored_msg = self._colorize_message(msg)

        return f"{ts_str} {level_str} {colored_msg}"


def setup_logging():
    """Configure main trading logger with colorized console output."""
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("hermes_trader")
    if not logger.handlers:
        logger.setLevel(logging.INFO)

        # ── File handler: plain text (no ANSI codes in log files) ──
        fh = logging.FileHandler(TRADE_LOG, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))

        # ── Console handler: colorized ──
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(ColorFormatter())

        logger.addHandler(fh)
        logger.addHandler(ch)

    return logger

def setup_http_logging():
    """Configure logger for HTTP traffic."""
    logger = logging.getLogger("hermes_http")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(HTTP_LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(fh)
    return logger

def setup_ratelimit_logging():
    """Configure logger for Ratelimit debugging."""
    logger = logging.getLogger("hermes_ratelimit")
    if not logger.handlers:
        logger.setLevel(logging.ERROR)
        fh = logging.FileHandler(RATELIMIT_LOG, encoding="utf-8")
        fh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(fh)
    return logger

log = setup_logging()
http_log = setup_http_logging()
ratelimit_log = setup_ratelimit_logging()

tracker = HttpRequestTracker()
