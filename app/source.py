"""Исходный код запущенной копии для пользователей по сети (AGPL-3.0, раздел 13).

Архив собирается из файлов этой установки, поэтому в него попадают и ваши правки кода.
Данные, ключи и личный config.yaml в архив не входят.
"""
from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = ("app/**/*.py", "frontend/*.*", "frontend/fonts/*", "tests/**/*.py", "requirements*.txt", "*.sh", "*.bat", "*.md",
            "LICENSE", "Dockerfile", "docker-compose.yml", ".env.example", ".dockerignore", ".gitignore")
SKIP_PARTS = {"data", ".venv", "__pycache__", ".git", ".claude", "node_modules"}

_lock = threading.Lock()
_cache: bytes | None = None


def files(root: Path = ROOT) -> list[Path]:
    out: set[Path] = set()
    for pattern in PATTERNS:
        for p in root.glob(pattern):
            rel = p.relative_to(root)
            if p.is_file() and not p.is_symlink() and not SKIP_PARTS & set(rel.parts):
                out.add(rel)
    return sorted(out)


def archive(root: Path = ROOT) -> bytes:
    global _cache
    with _lock:
        if _cache is None or root != ROOT:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for rel in files(root):
                    z.write(root / rel, "modelseek/" + rel.as_posix())
            data = buf.getvalue()
            if root != ROOT:
                return data
            _cache = data
        return _cache
