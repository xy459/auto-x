"""Standalone CloakBrowser profile manager."""

import sys

if sys.version_info < (3, 12):
    raise RuntimeError(
        f"browser-custom requires Python 3.12+, current version is "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )

__version__ = "0.1.0"
