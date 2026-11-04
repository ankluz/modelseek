from __future__ import annotations

import re
from typing import Any

import httpx

from .config import env

CYRILLIC = re.compile(r"[Ѐ-ӿ]")

PROMPT = ("Translate this search query for a 3D-printing model catalog into short English search "
          "keywords. Reply with the keywords only, no quotes, no explanation.")


class Translator:
    def __init__(self, cfg: dict[str, Any], client: httpx.AsyncClient):
        self.enabled = bool(cfg.get("enabled"))
        self.url = str(cfg.get("url", "")).rstrip("/")
        self.model = cfg.get("model")
        self.key = env(cfg.get("api_key_env"))
        self.client = client
        self._cache: dict[str, str] = {}
        self.error: str | None = None

    @staticmethod
    def needs(query: str) -> bool:
        return bool(CYRILLIC.search(query))

    async def to_english(self, query: str) -> str | None:
        if not self.enabled or not self.needs(query):
            return None
        q = query.strip().lower()
        if q in self._cache:
            return self._cache[q]
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        payload = {"model": self.model, "temperature": 0, "max_tokens": 40,
                   "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": query}]}
        try:
            r = await self.client.post(f"{self.url}/chat/completions", json=payload, headers=headers, timeout=20)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:200]
            return None
        lines = text.strip().splitlines() if isinstance(text, str) else []
        text = lines[0].strip().strip('"\'`').strip()[:120] if lines else ""
        if not text or CYRILLIC.search(text):
            return None
        self.error = None
        self._cache[q] = text
        return text

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "model": self.model, "url": self.url, "error": self.error}
