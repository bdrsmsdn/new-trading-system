import json
import time
import subprocess
import inspect
from hermes.config import API_KEY, API_SECRET, NONCE_FILE
from hermes.logging_setup import log, tracker

# Global cooldown
_global_rate_limit_until = 0.0

# In-memory nonce guard — ensures monotonicity even when file I/O fails
_last_nonce = 0

def get_nonce() -> int:
    """Get and increment nonce for API calls."""
    global _last_nonce
    try:
        if NONCE_FILE.exists():
            with open(NONCE_FILE, "r") as f:
                content = f.read().strip()
            try:
                current = int(content) if content else 0
            except ValueError:
                log.warning(f"Nonce file corrupted ('{content}'), resetting")
                current = 0
        else:
            current = 0

        ms = int(time.time() * 1000)
        new_nonce = max(ms, current + 1, _last_nonce + 1)
        _last_nonce = new_nonce

        with open(NONCE_FILE, "w") as f:
            f.write(str(new_nonce))
        return new_nonce
    except Exception as e:
        log.error(f"Nonce error: {e}")
        fallback = max(int(time.time() * 1000), _last_nonce + 1)
        _last_nonce = fallback  # persist in memory so next call cannot reuse it
        return fallback

def sign_request(params_str: str) -> str:
    """Generate HMAC-SHA512 signature."""
    import hmac
    import hashlib
    return hmac.new(
        API_SECRET.encode(),
        params_str.encode(),
        hashlib.sha512
    ).hexdigest()

def api_call(method: str, **params) -> dict:
    """Make authenticated API call to Indodax with retry on rate limit."""
    global _global_rate_limit_until
    
    # Get caller function for logging
    caller = inspect.stack()[1].function
    
    nonce = get_nonce()
    extra = "&".join(f"{k}={v}" for k, v in params.items())
    params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
    signature = sign_request(params_str)
    
    max_retries = 3
    base_delay = 2

    url = "https://indodax.com/tapi"

    for attempt in range(max_retries):
        try:
            start_time = time.time()
            result = subprocess.run([
                "curl", "-s", "-X", "POST", url,
                "-H", f"Key: {API_KEY}",
                "-H", f"Sign: {signature}",
                "-d", params_str,
                "-H", "User-Agent: Mozilla/5.0",
                "-w", "\n%{http_code}",
            ], capture_output=True, text=True, timeout=15)
            
            latency = (time.time() - start_time) * 1000
            
            parts = result.stdout.rsplit("\n", 1)
            body = parts[0] if len(parts) == 2 else result.stdout
            http_status = parts[1].strip() if len(parts) == 2 else "200"
            
            # Log request via tracker
            tracker.log_request(url, f"POST/{method}", caller, http_status, latency)

            if http_status == "429":
                _global_rate_limit_until = time.time() + 60
                delay = base_delay * (2 ** attempt)
                log.warning(f"[API] HTTP 429 from Indodax TAPI — global cooldown 60s, retry in {delay}s")
                time.sleep(delay)
                nonce = get_nonce()
                params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
                signature = sign_request(params_str)
                continue

            data = json.loads(body)

            error_msg = str(data.get("error", "")).lower()
            if data.get("success") == 0 and ("too_many_requests" in error_msg or "rate" in error_msg):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning(f"Rate limited by Indodax, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    nonce = get_nonce()
                    params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
                    signature = sign_request(params_str)
                    continue
                else:
                    log.error("Rate limit exceeded after all retries")
                    return data

            return data
        except json.JSONDecodeError as e:
            log.warning(f"[API] Non-JSON response on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                nonce = get_nonce()
                params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
                signature = sign_request(params_str)
                continue
            return {"success": 0, "error": "non-json response"}
        except Exception as e:
            log.error(f"API call failed: {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            return {"success": 0, "error": str(e)}

    return {"success": 0, "error": "max retries exceeded"}
