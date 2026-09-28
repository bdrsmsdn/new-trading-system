"""
Test isolation and safety sentinel infrastructure for trading system tests.

Enforces:
1. Temporary state/ledger directories under $TMPDIR / scratch workspace.
2. Path redirection for all persistent state files before any writes occur.
3. Snapshotting and restoration of shared singletons (state, caches, rate limiters).
4. Global denial of unmocked network/socket access (Binance API, Telegram, HTTP, curl).
5. Protection against accidental modification or deletion of production / workspace state files.
"""

import builtins
import copy
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from typing import Any, Callable, Dict, Optional, Set, cast

# Safe import for requests
try:
    import requests
    import requests.sessions
    _HAS_REQUESTS = True
except ImportError:
    requests = None  # type: ignore
    _HAS_REQUESTS = False


class NetworkAccessBlockedError(RuntimeError):
    """Raised when test code attempts an unmocked external network/socket call."""
    pass


class ProductionFileAccessError(RuntimeError):
    """Raised when test code attempts to write to or delete a production state file."""
    pass


# ---------------------------------------------------------------------------
# GLOBAL SENTINEL STATE
# ---------------------------------------------------------------------------
_NETWORK_BLOCKING_ACTIVE = False
_FILE_PROTECTION_ACTIVE = False
_ALLOWED_TEMP_DIRS: Set[str] = set()

# Saved original functions for monkeypatching
_ORIG_SOCKET_CONNECT = socket.socket.connect
_ORIG_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIG_SOCKET_CREATE_CONNECTION = socket.create_connection
_ORIG_URLLIB_OPEN = urllib.request.urlopen
_ORIG_OPENER_OPEN = urllib.request.OpenerDirector.open

_ORIG_REQUESTS_SESSION_SEND: Optional[Callable] = (
    requests.sessions.Session.send if _HAS_REQUESTS and requests is not None else None
)
_ORIG_REQUESTS_SESSION_REQUEST: Optional[Callable] = (
    requests.sessions.Session.request if _HAS_REQUESTS and requests is not None else None
)

_ORIG_SUBPROCESS_RUN = subprocess.run
_ORIG_SUBPROCESS_POPEN = subprocess.Popen
_ORIG_SUBPROCESS_CALL = subprocess.call
_ORIG_SUBPROCESS_CHECK_OUTPUT = subprocess.check_output
_ORIG_SUBPROCESS_CHECK_CALL = subprocess.check_call

_ORIG_OS_REMOVE = os.remove
_ORIG_OS_UNLINK = os.unlink
_ORIG_PATH_UNLINK = pathlib.Path.unlink
_ORIG_BUILTIN_OPEN = builtins.open


# ---------------------------------------------------------------------------
# NETWORK BLOCKING SENTINEL
# ---------------------------------------------------------------------------
def _blocked_socket_connect(self: socket.socket, address: Any) -> None:
    if not _NETWORK_BLOCKING_ACTIVE:
        return _ORIG_SOCKET_CONNECT(self, address)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: socket.connect to {address}"
    )


def _blocked_socket_connect_ex(self: socket.socket, address: Any) -> int:
    if not _NETWORK_BLOCKING_ACTIVE:
        return _ORIG_SOCKET_CONNECT_EX(self, address)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: socket.connect_ex to {address}"
    )


def _blocked_socket_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    if not _NETWORK_BLOCKING_ACTIVE:
        return _ORIG_SOCKET_CREATE_CONNECTION(address, *args, **kwargs)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: socket.create_connection to {address}"
    )


def _blocked_urllib_open(url: Any, *args: Any, **kwargs: Any) -> Any:
    if not _NETWORK_BLOCKING_ACTIVE:
        return _ORIG_URLLIB_OPEN(url, *args, **kwargs)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: urllib.request.urlopen to {url}"
    )


def _blocked_opener_open(self: urllib.request.OpenerDirector, fullurl: Any, *args: Any, **kwargs: Any) -> Any:
    if not _NETWORK_BLOCKING_ACTIVE:
        return _ORIG_OPENER_OPEN(self, fullurl, *args, **kwargs)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: OpenerDirector.open to {fullurl}"
    )


def _blocked_requests_send(self: Any, request: Any, **kwargs: Any) -> Any:
    if not _NETWORK_BLOCKING_ACTIVE:
        if _ORIG_REQUESTS_SESSION_SEND:
            return _ORIG_REQUESTS_SESSION_SEND(self, request, **kwargs)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: requests to {getattr(request, 'url', request)}"
    )


def _blocked_requests_request(self: Any, method: str, url: str, **kwargs: Any) -> Any:
    if not _NETWORK_BLOCKING_ACTIVE:
        if _ORIG_REQUESTS_SESSION_REQUEST:
            return _ORIG_REQUESTS_SESSION_REQUEST(self, method, url, **kwargs)
    raise NetworkAccessBlockedError(
        f"[TEST ISOLATION] Unmocked network access blocked: requests {method} {url}"
    )


def _is_network_subprocess_command(args: Any) -> bool:
    """Check if subprocess command is invoking curl, wget, nc or network tools."""
    if isinstance(args, (list, tuple)) and len(args) > 0:
        cmd0 = str(args[0]).lower()
        base_cmd = os.path.basename(cmd0)
        return base_cmd in ("curl", "wget", "nc", "netcat", "http", "https")
    elif isinstance(args, str):
        parts = args.strip().split()
        if parts:
            base_cmd = os.path.basename(parts[0].lower())
            return base_cmd in ("curl", "wget", "nc", "netcat", "http", "https")
    return False


def _blocked_subprocess_run(args: Any, *pargs: Any, **kwargs: Any) -> Any:
    if _NETWORK_BLOCKING_ACTIVE and _is_network_subprocess_command(args):
        raise NetworkAccessBlockedError(
            f"[TEST ISOLATION] Unmocked network subprocess blocked: {args}"
        )
    return _ORIG_SUBPROCESS_RUN(args, *pargs, **kwargs)


def _blocked_subprocess_popen(args: Any, *pargs: Any, **kwargs: Any) -> Any:
    if _NETWORK_BLOCKING_ACTIVE and _is_network_subprocess_command(args):
        raise NetworkAccessBlockedError(
            f"[TEST ISOLATION] Unmocked network subprocess blocked: {args}"
        )
    return _ORIG_SUBPROCESS_POPEN(args, *pargs, **kwargs)


def _blocked_subprocess_call(args: Any, *pargs: Any, **kwargs: Any) -> Any:
    if _NETWORK_BLOCKING_ACTIVE and _is_network_subprocess_command(args):
        raise NetworkAccessBlockedError(
            f"[TEST ISOLATION] Unmocked network subprocess blocked: {args}"
        )
    return _ORIG_SUBPROCESS_CALL(args, *pargs, **kwargs)


def _blocked_subprocess_check_output(args: Any, *pargs: Any, **kwargs: Any) -> Any:
    if _NETWORK_BLOCKING_ACTIVE and _is_network_subprocess_command(args):
        raise NetworkAccessBlockedError(
            f"[TEST ISOLATION] Unmocked network subprocess blocked: {args}"
        )
    return _ORIG_SUBPROCESS_CHECK_OUTPUT(args, *pargs, **kwargs)


def _blocked_subprocess_check_call(args: Any, *pargs: Any, **kwargs: Any) -> Any:
    if _NETWORK_BLOCKING_ACTIVE and _is_network_subprocess_command(args):
        raise NetworkAccessBlockedError(
            f"[TEST ISOLATION] Unmocked network subprocess blocked: {args}"
        )
    return _ORIG_SUBPROCESS_CHECK_CALL(args, *pargs, **kwargs)


def enable_network_blocking() -> None:
    """Globally enable network blocking for all tests."""
    global _NETWORK_BLOCKING_ACTIVE
    _NETWORK_BLOCKING_ACTIVE = True

    socket.socket.connect = _blocked_socket_connect  # type: ignore
    socket.socket.connect_ex = _blocked_socket_connect_ex  # type: ignore
    socket.create_connection = _blocked_socket_create_connection  # type: ignore
    urllib.request.urlopen = _blocked_urllib_open  # type: ignore
    urllib.request.OpenerDirector.open = _blocked_opener_open  # type: ignore

    if _HAS_REQUESTS and requests is not None:
        requests.sessions.Session.send = _blocked_requests_send  # type: ignore
        requests.sessions.Session.request = _blocked_requests_request  # type: ignore

    subprocess.run = _blocked_subprocess_run  # type: ignore
    subprocess.Popen = _blocked_subprocess_popen  # type: ignore
    subprocess.call = _blocked_subprocess_call  # type: ignore
    subprocess.check_output = _blocked_subprocess_check_output  # type: ignore
    subprocess.check_call = _blocked_subprocess_check_call  # type: ignore


def disable_network_blocking() -> None:
    """Disable network blocking."""
    global _NETWORK_BLOCKING_ACTIVE
    _NETWORK_BLOCKING_ACTIVE = False

    socket.socket.connect = _ORIG_SOCKET_CONNECT  # type: ignore
    socket.socket.connect_ex = _ORIG_SOCKET_CONNECT_EX  # type: ignore
    socket.create_connection = _ORIG_SOCKET_CREATE_CONNECTION  # type: ignore
    urllib.request.urlopen = _ORIG_URLLIB_OPEN  # type: ignore
    urllib.request.OpenerDirector.open = _ORIG_OPENER_OPEN  # type: ignore

    if _HAS_REQUESTS and requests is not None:
        if _ORIG_REQUESTS_SESSION_SEND:
            requests.sessions.Session.send = _ORIG_REQUESTS_SESSION_SEND  # type: ignore
        if _ORIG_REQUESTS_SESSION_REQUEST:
            requests.sessions.Session.request = _ORIG_REQUESTS_SESSION_REQUEST  # type: ignore

    subprocess.run = _ORIG_SUBPROCESS_RUN  # type: ignore
    subprocess.Popen = _ORIG_SUBPROCESS_POPEN  # type: ignore
    subprocess.call = _ORIG_SUBPROCESS_CALL  # type: ignore
    subprocess.check_output = _ORIG_SUBPROCESS_CHECK_OUTPUT  # type: ignore
    subprocess.check_call = _ORIG_SUBPROCESS_CHECK_CALL  # type: ignore


# ---------------------------------------------------------------------------
# PRODUCTION FILE PROTECTION SENTINEL
# ---------------------------------------------------------------------------
_PROTECTED_FILENAMES = {
    "daily_profit_state.json",
    "hermes_trader_state.json",
    "hermes_prices.json",
    "hermes_agent_memory.json",
    "daemon.pid",
    ".env",
    "hermes_ledger.db",
    "hermes_ledger.sqlite",
}


def _is_path_in_allowed_temp(path_str: str) -> bool:
    """Check if the path resides inside an explicitly registered temp directory or TMPDIR."""
    try:
        resolved = str(pathlib.Path(path_str).resolve())
    except Exception:
        resolved = str(path_str)

    tmpdir = os.environ.get("TMPDIR", tempfile.gettempdir())
    try:
        resolved_tmpdir = str(pathlib.Path(tmpdir).resolve())
    except Exception:
        resolved_tmpdir = tmpdir

    if resolved.startswith(resolved_tmpdir):
        return True

    for allowed in _ALLOWED_TEMP_DIRS:
        try:
            resolved_allowed = str(pathlib.Path(allowed).resolve())
            if resolved.startswith(resolved_allowed):
                return True
        except Exception:
            pass

    return False


def _check_file_deletion_safety(path: Any) -> None:
    """Ensure tests cannot delete production state files outside temp dirs."""
    if not _FILE_PROTECTION_ACTIVE:
        return

    path_str = str(path)
    base_name = os.path.basename(path_str)

    if base_name in _PROTECTED_FILENAMES:
        if not _is_path_in_allowed_temp(path_str):
            raise ProductionFileAccessError(
                f"[TEST ISOLATION] Deletion of protected production file blocked: {path_str}"
            )


def _check_file_write_safety(path: Any, mode: str) -> None:
    """Ensure tests cannot write/modify protected state files outside temp dirs."""
    if not _FILE_PROTECTION_ACTIVE:
        return

    if any(m in mode for m in ("w", "a", "+", "x")):
        path_str = str(path)
        base_name = os.path.basename(path_str)
        if base_name in _PROTECTED_FILENAMES:
            if not _is_path_in_allowed_temp(path_str):
                raise ProductionFileAccessError(
                    f"[TEST ISOLATION] Write to protected production file blocked: {path_str}"
                )


def _guarded_os_remove(path: Any, *args: Any, **kwargs: Any) -> None:
    _check_file_deletion_safety(path)
    _ORIG_OS_REMOVE(path, *args, **kwargs)


def _guarded_os_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
    _check_file_deletion_safety(path)
    _ORIG_OS_UNLINK(path, *args, **kwargs)


def _guarded_path_unlink(self: pathlib.Path, *args: Any, **kwargs: Any) -> None:
    _check_file_deletion_safety(self)
    _ORIG_PATH_UNLINK(self, *args, **kwargs)


def _guarded_builtin_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    if isinstance(file, (str, bytes, os.PathLike, pathlib.Path)):
        _check_file_write_safety(file, mode)
    return _ORIG_BUILTIN_OPEN(file, mode, *args, **kwargs)


def install_file_protection() -> None:
    """Install file protection sentinels."""
    global _FILE_PROTECTION_ACTIVE
    _FILE_PROTECTION_ACTIVE = True

    os.remove = _guarded_os_remove  # type: ignore
    os.unlink = _guarded_os_unlink  # type: ignore
    pathlib.Path.unlink = _guarded_path_unlink  # type: ignore
    builtins.open = _guarded_builtin_open  # type: ignore


def uninstall_file_protection() -> None:
    """Uninstall file protection sentinels."""
    global _FILE_PROTECTION_ACTIVE
    _FILE_PROTECTION_ACTIVE = False

    os.remove = _ORIG_OS_REMOVE  # type: ignore
    os.unlink = _ORIG_OS_UNLINK  # type: ignore
    pathlib.Path.unlink = _ORIG_PATH_UNLINK  # type: ignore
    builtins.open = _ORIG_BUILTIN_OPEN  # type: ignore


# ---------------------------------------------------------------------------
# SINGLETON STATE SNAPSHOT & RESTORATION
# ---------------------------------------------------------------------------
class SingletonSnapshot:
    """Captures and restores the shared singleton state of the application."""

    def __init__(self) -> None:
        self.state_positions: Any = None
        self.state_futures_positions: Any = None
        self.state_last_trade_time: Any = None
        self.state_price_history: Any = None
        self.state_rsi_state: Any = None
        self.state_active_pairs: Any = None
        self.state_fg_value: Any = None
        self.state_fg_class: Any = None
        self.state_balance_cache: Any = None
        self.state_balance_cache_time: Any = None
        self.state_dry_run: Any = None
        self.state_last_regime: Any = None

        self.global_prices: Any = None
        self.global_ticker_cache: Any = None
        self.global_candle_cache: Any = None
        self.global_multi_rsi_cache: Any = None

        self.rest_prices: Any = None
        self.rest_ticker_cache: Any = None
        self.rest_candle_cache: Any = None

        self.telegram_last_send: Any = None

    def capture(self) -> None:
        """Capture deep copies of singletons."""
        if "hermes.state" in sys.modules:
            st = sys.modules["hermes.state"]
            if hasattr(st, "state") and st.state is not None:
                self.state_positions = copy.deepcopy(getattr(st.state, "positions", {}))
                self.state_futures_positions = copy.deepcopy(getattr(st.state, "futures_positions", {}))
                self.state_last_trade_time = copy.deepcopy(getattr(st.state, "last_trade_time", {}))
                self.state_price_history = copy.deepcopy(getattr(st.state, "price_history", {}))
                self.state_rsi_state = copy.deepcopy(getattr(st.state, "rsi_state", {}))
                self.state_active_pairs = copy.deepcopy(getattr(st.state, "active_pairs", []))
                self.state_fg_value = getattr(st.state, "fg_value", 50)
                self.state_fg_class = getattr(st.state, "fg_class", "Neutral")
                self.state_balance_cache = copy.deepcopy(getattr(st.state, "balance_cache", None))
                self.state_balance_cache_time = getattr(st.state, "balance_cache_time", 0)
                self.state_dry_run = getattr(st.state, "dry_run", False)
                self.state_last_regime = getattr(st.state, "_last_regime", "SIDEWAYS")

            if hasattr(st, "prices") and isinstance(st.prices, dict):
                self.global_prices = copy.deepcopy(st.prices)
            if hasattr(st, "_ticker_cache") and isinstance(st._ticker_cache, dict):
                self.global_ticker_cache = copy.deepcopy(st._ticker_cache)
            if hasattr(st, "_candle_cache") and isinstance(st._candle_cache, dict):
                self.global_candle_cache = copy.deepcopy(st._candle_cache)
            if hasattr(st, "_multi_rsi_cache") and isinstance(st._multi_rsi_cache, dict):
                self.global_multi_rsi_cache = copy.deepcopy(st._multi_rsi_cache)

        if "hermes.api.rest" in sys.modules:
            rest = sys.modules["hermes.api.rest"]
            if hasattr(rest, "prices") and isinstance(rest.prices, dict):
                self.rest_prices = copy.deepcopy(rest.prices)
            if hasattr(rest, "_ticker_cache") and isinstance(rest._ticker_cache, dict):
                self.rest_ticker_cache = copy.deepcopy(rest._ticker_cache)
            if hasattr(rest, "_candle_cache") and isinstance(rest._candle_cache, dict):
                self.rest_candle_cache = copy.deepcopy(rest._candle_cache)

        if "hermes.notifications.telegram" in sys.modules:
            tg = sys.modules["hermes.notifications.telegram"]
            if hasattr(tg, "_telegram_last_send"):
                self.telegram_last_send = getattr(tg, "_telegram_last_send", 0.0)

    def restore(self) -> None:
        """Restore singletons to the captured state."""
        if "hermes.state" in sys.modules:
            st = sys.modules["hermes.state"]
            if hasattr(st, "state") and st.state is not None:
                if self.state_positions is not None:
                    st.state.positions = copy.deepcopy(self.state_positions)
                if hasattr(self, "state_futures_positions") and self.state_futures_positions is not None:
                    st.state.futures_positions = copy.deepcopy(self.state_futures_positions)
                if self.state_last_trade_time is not None:
                    st.state.last_trade_time = copy.deepcopy(self.state_last_trade_time)
                if self.state_price_history is not None:
                    st.state.price_history = copy.deepcopy(self.state_price_history)
                if self.state_rsi_state is not None:
                    st.state.rsi_state = copy.deepcopy(self.state_rsi_state)
                if self.state_active_pairs is not None:
                    st.state.active_pairs = copy.deepcopy(self.state_active_pairs)
                if self.state_fg_value is not None:
                    st.state.fg_value = self.state_fg_value
                if self.state_fg_class is not None:
                    st.state.fg_class = self.state_fg_class
                if self.state_balance_cache is not None:
                    st.state.balance_cache = copy.deepcopy(self.state_balance_cache)
                if self.state_balance_cache_time is not None:
                    st.state.balance_cache_time = self.state_balance_cache_time
                if self.state_dry_run is not None:
                    st.state.dry_run = self.state_dry_run
                if self.state_last_regime is not None:
                    setattr(st.state, "_last_regime", self.state_last_regime)

            if hasattr(st, "prices") and self.global_prices is not None and isinstance(st.prices, dict):
                st.prices.clear()
                st.prices.update(copy.deepcopy(self.global_prices))
            if hasattr(st, "_ticker_cache") and self.global_ticker_cache is not None and isinstance(st._ticker_cache, dict):
                st._ticker_cache.clear()
                st._ticker_cache.update(copy.deepcopy(self.global_ticker_cache))
            if hasattr(st, "_candle_cache") and self.global_candle_cache is not None and isinstance(st._candle_cache, dict):
                st._candle_cache.clear()
                st._candle_cache.update(copy.deepcopy(self.global_candle_cache))
            if hasattr(st, "_multi_rsi_cache") and self.global_multi_rsi_cache is not None and isinstance(st._multi_rsi_cache, dict):
                st._multi_rsi_cache.clear()
                st._multi_rsi_cache.update(copy.deepcopy(self.global_multi_rsi_cache))

        if "hermes.api.rest" in sys.modules:
            rest = sys.modules["hermes.api.rest"]
            if hasattr(rest, "prices") and self.rest_prices is not None and isinstance(rest.prices, dict):
                rest.prices.clear()
                rest.prices.update(copy.deepcopy(self.rest_prices))
            if hasattr(rest, "_ticker_cache") and self.rest_ticker_cache is not None and isinstance(rest._ticker_cache, dict):
                rest._ticker_cache.clear()
                rest._ticker_cache.update(copy.deepcopy(self.rest_ticker_cache))
            if hasattr(rest, "_candle_cache") and self.rest_candle_cache is not None and isinstance(rest._candle_cache, dict):
                rest._candle_cache.clear()
                rest._candle_cache.update(copy.deepcopy(self.rest_candle_cache))

        if "hermes.notifications.telegram" in sys.modules:
            tg = sys.modules["hermes.notifications.telegram"]
            if hasattr(tg, "_telegram_last_send") and self.telegram_last_send is not None:
                setattr(tg, "_telegram_last_send", self.telegram_last_send)


# ---------------------------------------------------------------------------
# TEST ENVIRONMENT ISOLATION CONTEXT / BASE TEST CASE
# ---------------------------------------------------------------------------
class EnvironmentIsolation:
    """Manages the isolated temporary directory, state-path patches, and safety guards."""

    def __init__(self, temp_dir: Optional[str] = None) -> None:
        self.custom_temp_dir = temp_dir
        self.temp_dir_obj: Optional[tempfile.TemporaryDirectory] = None
        self.temp_dir_path: Optional[pathlib.Path] = None
        self.snapshot = SingletonSnapshot()
        self._orig_env: Dict[str, Optional[str]] = {}
        self._orig_paths: Dict[str, Any] = {}

    def start(self) -> pathlib.Path:
        # Enable global network blocking and file protection
        enable_network_blocking()
        install_file_protection()

        # Capture singleton states
        self.snapshot.capture()

        # Set up isolated scratch directory
        scratch_base = os.environ.get("TMPDIR", tempfile.gettempdir())
        os.makedirs(scratch_base, exist_ok=True)

        if self.custom_temp_dir:
            self.temp_dir_path = pathlib.Path(self.custom_temp_dir)
            self.temp_dir_path.mkdir(parents=True, exist_ok=True)
        else:
            self.temp_dir_obj = tempfile.TemporaryDirectory(
                prefix="hermes_test_iso_", dir=scratch_base
            )
            self.temp_dir_path = pathlib.Path(self.temp_dir_obj.name)

        _ALLOWED_TEMP_DIRS.add(str(self.temp_dir_path))

        # Environment variables
        self._orig_env["HERMES_TMPDIR"] = os.environ.get("HERMES_TMPDIR")
        self._orig_env["HERMES_STATE_DIR"] = os.environ.get("HERMES_STATE_DIR")
        self._orig_env["HERMES_LEDGER_PATH"] = os.environ.get("HERMES_LEDGER_PATH")

        os.environ["HERMES_TMPDIR"] = str(self.temp_dir_path)
        os.environ["HERMES_STATE_DIR"] = str(self.temp_dir_path)
        os.environ["HERMES_LEDGER_PATH"] = str(self.temp_dir_path / "hermes_ledger.db")

        # Patch state paths across modules
        self._patch_module_paths()

        return self.temp_dir_path

    def _patch_module_paths(self) -> None:
        if self.temp_dir_path is None:
            return

        isolated_state = self.temp_dir_path / "hermes_trader_state.json"
        isolated_price_cache = self.temp_dir_path / "hermes_prices.json"
        isolated_agent_mem = self.temp_dir_path / "hermes_agent_memory.json"
        isolated_pid = self.temp_dir_path / "daemon.pid"
        isolated_daily_profit = str(self.temp_dir_path / "daily_profit_state.json")
        isolated_ledger = self.temp_dir_path / "hermes_ledger.db"

        # hermes.config
        if "hermes.config" in sys.modules:
            cfg = sys.modules["hermes.config"]
            self._orig_paths["config.STATE_FILE"] = getattr(cfg, "STATE_FILE", None)
            self._orig_paths["config.PRICE_CACHE"] = getattr(cfg, "PRICE_CACHE", None)
            self._orig_paths["config.AGENT_MEMORY_FILE"] = getattr(cfg, "AGENT_MEMORY_FILE", None)
            self._orig_paths["config.PID_FILE"] = getattr(cfg, "PID_FILE", None)
            self._orig_paths["config.LEDGER_PATH"] = getattr(cfg, "LEDGER_PATH", None)
            self._orig_paths["config.ACCOUNTING_DB_PATH"] = getattr(cfg, "ACCOUNTING_DB_PATH", None)

            setattr(cfg, "STATE_FILE", isolated_state)
            setattr(cfg, "PRICE_CACHE", isolated_price_cache)
            setattr(cfg, "AGENT_MEMORY_FILE", isolated_agent_mem)
            setattr(cfg, "PID_FILE", isolated_pid)
            setattr(cfg, "LEDGER_PATH", isolated_ledger)
            setattr(cfg, "ACCOUNTING_DB_PATH", isolated_ledger)

        # hermes.state
        if "hermes.state" in sys.modules:
            st = sys.modules["hermes.state"]
            self._orig_paths["state.STATE_FILE"] = getattr(st, "STATE_FILE", None)
            setattr(st, "STATE_FILE", isolated_state)

        # hermes.api.transfer
        if "hermes.api.transfer" in sys.modules:
            tf = sys.modules["hermes.api.transfer"]
            self._orig_paths["transfer._DAILY_STATE_PATH"] = getattr(tf, "_DAILY_STATE_PATH", None)
            setattr(tf, "_DAILY_STATE_PATH", isolated_daily_profit)

        # hermes.api.rest
        if "hermes.api.rest" in sys.modules:
            rest = sys.modules["hermes.api.rest"]
            self._orig_paths["rest.PRICE_CACHE"] = getattr(rest, "PRICE_CACHE", None)
            setattr(rest, "PRICE_CACHE", isolated_price_cache)

        # hermes.agent.memory
        if "hermes.agent.memory" in sys.modules:
            mem = sys.modules["hermes.agent.memory"]
            self._orig_paths["memory.MEMORY_FILE"] = getattr(mem, "MEMORY_FILE", None)
            setattr(mem, "MEMORY_FILE", isolated_agent_mem)

    def stop(self) -> None:
        # Restore module paths
        if "hermes.config" in sys.modules:
            cfg = sys.modules["hermes.config"]
            for k in ("STATE_FILE", "PRICE_CACHE", "AGENT_MEMORY_FILE", "PID_FILE", "LEDGER_PATH", "ACCOUNTING_DB_PATH"):
                orig = self._orig_paths.get(f"config.{k}")
                if orig is not None:
                    setattr(cfg, k, orig)

        if "hermes.state" in sys.modules:
            st = sys.modules["hermes.state"]
            orig = self._orig_paths.get("state.STATE_FILE")
            if orig is not None:
                setattr(st, "STATE_FILE", orig)

        if "hermes.api.transfer" in sys.modules:
            tf = sys.modules["hermes.api.transfer"]
            orig = self._orig_paths.get("transfer._DAILY_STATE_PATH")
            if orig is not None:
                setattr(tf, "_DAILY_STATE_PATH", orig)

        if "hermes.api.rest" in sys.modules:
            rest = sys.modules["hermes.api.rest"]
            orig = self._orig_paths.get("rest.PRICE_CACHE")
            if orig is not None:
                setattr(rest, "PRICE_CACHE", orig)

        if "hermes.agent.memory" in sys.modules:
            mem = sys.modules["hermes.agent.memory"]
            orig = self._orig_paths.get("memory.MEMORY_FILE")
            if orig is not None:
                setattr(mem, "MEMORY_FILE", orig)

        # Restore environment variables
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        # Restore singletons
        self.snapshot.restore()

        # Remove from allowed temp dirs and clean up directory
        if self.temp_dir_path:
            _ALLOWED_TEMP_DIRS.discard(str(self.temp_dir_path))

        if self.temp_dir_obj:
            try:
                self.temp_dir_obj.cleanup()
            except Exception:
                pass


class isolate_test_environment:
    """Context manager for running code under test isolation."""

    def __init__(self, temp_dir: Optional[str] = None) -> None:
        self.env_iso = EnvironmentIsolation(temp_dir=temp_dir)

    def __enter__(self) -> pathlib.Path:
        return self.env_iso.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.env_iso.stop()


class IsolatedTestCase(unittest.TestCase):
    """Base TestCase class providing automatic state isolation, path redirection, and safety sentinels."""

    iso_env: Optional[EnvironmentIsolation] = None
    isolated_dir: Optional[pathlib.Path] = None

    def setUp(self) -> None:
        super().setUp()
        self.iso_env = EnvironmentIsolation()
        self.isolated_dir = self.iso_env.start()

    def tearDown(self) -> None:
        if self.iso_env:
            self.iso_env.stop()
        super().tearDown()


# Automatically enable network blocking and file protection at module load
enable_network_blocking()
install_file_protection()
