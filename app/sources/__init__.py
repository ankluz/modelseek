from __future__ import annotations

from typing import Any

from .base import ChallengeError, LinkSource, Source, SourceError
from .cults3d import Cults3D
from .custom import CustomSource
from .makerworld import MakerWorld
from .myminifactory import MyMiniFactory
from .printables import Printables
from .searxng import SearXNG
from .thangs import Thangs
from .thingiverse import Thingiverse

REGISTRY: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (Printables, MakerWorld, Thingiverse, Thangs, Cults3D, MyMiniFactory, SearXNG)
}


def build_sources(cfg: dict[str, Any]) -> dict[str, Source]:
    out: dict[str, Source] = {}
    for name, c in (cfg.get("sources") or {}).items():
        cls = REGISTRY.get(name)
        if cls:
            out[name] = cls(c or {})
    for name, cls in REGISTRY.items():
        out.setdefault(name, cls({"enabled": False}))
    for name, c in (cfg.get("custom_sources") or {}).items():
        if name not in out and isinstance(c, dict):
            out[name] = CustomSource(name, c)
    for name, c in (cfg.get("links") or {}).items():
        if name not in out and isinstance(c, dict) and c.get("url"):
            out[name] = LinkSource(name, c)
    return out


__all__ = ["REGISTRY", "ChallengeError", "CustomSource", "LinkSource", "Source", "SourceError", "build_sources"]
