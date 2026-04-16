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
        
        ratelimit_log.error(f"🚨 HTTP 429 TRIGGERED!")
        ratelimit_log.error(f"   URL: {trigger_entry['url']}")
        ratelimit_log.error(f"   Caller: {trigger_entry['caller']}")
        ratelimit_log.error(f"   Requests in last 60s: {len(recent)}")
        ratelimit_log.error(f"   By caller: {dict(by_caller)}")
        ratelimit_log.error(f"   By endpoint: {dict(self.endpoint_counts)}")
        ratelimit_log.error(f"   Traceback:\n{''.join(traceback.format_stack()[:-2])}")

def setup_logging():
    """Configure main trading logger."""
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("hermes_trader")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        # File handler
        fh = logging.FileHandler(TRADE_LOG)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        # Console handler
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger

def setup_http_logging():
    """Configure logger for HTTP traffic."""
    logger = logging.getLogger("hermes_http")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(HTTP_LOG_FILE)
        fh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(fh)
    return logger

def setup_ratelimit_logging():
    """Configure logger for Ratelimit debugging."""
    logger = logging.getLogger("hermes_ratelimit")
    if not logger.handlers:
        logger.setLevel(logging.ERROR)
        fh = logging.FileHandler(RATELIMIT_LOG)
        fh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(fh)
    return logger

log = setup_logging()
http_log = setup_http_logging()
ratelimit_log = setup_ratelimit_logging()

tracker = HttpRequestTracker()
