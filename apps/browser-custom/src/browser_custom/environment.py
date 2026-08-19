"""Load project environment variables before runtime settings are consumed."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def load_project_env(path: Path | None = None) -> bool:
    """Load the project .env without overriding variables exported by the shell."""
    return load_dotenv(path or PROJECT_ROOT / ".env", override=False)
