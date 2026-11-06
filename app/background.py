"""Запуск без консоли: pythonw не даёт stdout и stderr, поэтому они пишутся в файл."""
from __future__ import annotations

import sys
from pathlib import Path

from .config import load_config

MAX_LOG_BYTES = 5_000_000


def open_log(data_dir: str | Path):
    path = Path(data_dir) / "modelseek.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        path.replace(path.with_suffix(".log.1"))
    return path.open("a", encoding="utf-8", buffering=1)


def main() -> None:
    try:
        data_dir = load_config()["server"]["data_dir"]
    except Exception:
        data_dir = "data"
    sys.stdout = sys.stderr = open_log(data_dir)

    from .serve import main as serve

    serve()


if __name__ == "__main__":
    main()
