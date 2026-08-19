from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("BROWSER_CUSTOM_PORT", "8787"))
    uvicorn.run("browser_custom.app:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()

