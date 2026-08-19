from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "x_ops.app:app",
        host=os.environ.get("X_OPS_HOST", "127.0.0.1"),
        port=int(os.environ.get("X_OPS_PORT", "8790")),
        log_level=os.environ.get("X_OPS_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
