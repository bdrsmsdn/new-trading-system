"""Support package for test isolation and safety sentinels."""
from tests.support.isolation import (
    IsolatedTestCase,
    NetworkAccessBlockedError,
    ProductionFileAccessError,
    isolate_test_environment,
    enable_network_blocking,
    disable_network_blocking,
    install_file_protection,
    uninstall_file_protection,
)

__all__ = [
    "IsolatedTestCase",
    "NetworkAccessBlockedError",
    "ProductionFileAccessError",
    "isolate_test_environment",
    "enable_network_blocking",
    "disable_network_blocking",
    "install_file_protection",
    "uninstall_file_protection",
]
