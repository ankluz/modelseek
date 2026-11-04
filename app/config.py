from __future__ import annotations

import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "server": {
        "cache_ttl_seconds": 900,
        "request_timeout_seconds": 10,
        "per_source_limit": 24,
        "user_agent": "modelseek/0.3 (local model search)",
        "data_dir": "data",
        "allowed_hosts": [],
    },
    "breaker": {"max_failures": 3, "cooldown_minutes": 10},
    "browser": {"headless": True, "executable_path": "", "nav_timeout_seconds": 25},
    "impersonate": {"target": "chrome"},
    "ranking": {
        "weights": {"relevance": 0.40, "semantic": 0.15, "source_rank": 0.15,
                    "popularity": 0.20, "freshness": 0.05, "personal": 0.05},
        "freshness_half_life_days": 365,
        "downloads_factor": 0.3,
        "dedupe": True,
        "dedupe_images": False,
    },
    "semantic": {"enabled": False, "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"},
    "translate": {"enabled": False, "url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:3b", "api_key_env": ""},
    "filters": {"exclude_words": [], "min_likes": 0, "free_only": False},
    "subscriptions": {"check_every_seconds": 60, "max_new_kept": 200},
    "notify": {"kind": "", "url": "", "token_env": "", "chat_id": "", "open_url": "http://127.0.0.1:8088"},
    "sources": {},
    "custom_sources": {},
    "links": {},
}

EDITABLE = ("server", "breaker", "browser", "impersonate", "ranking", "semantic", "translate", "filters",
            "subscriptions", "notify", "sources", "custom_sources", "links")

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOCKED = (("browser", "executable_path"), ("server", "allowed_hosts"), ("server", "data_dir"))


def allowed_hosts(cfg: dict[str, Any]) -> set[str]:
    extra = (cfg.get("server") or {}).get("allowed_hosts") or []
    if isinstance(extra, str):
        extra = extra.split(",")
    return set(LOOPBACK_HOSTS) | {str(h).strip().lower() for h in extra if str(h).strip()}


def _merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался словарь верхнего уровня")
    return data


ENV_PREFIX = "MODELSEEK__"


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (environ if environ is not None else os.environ).items():
        if not k.startswith(ENV_PREFIX) or not v:
            continue
        parts = [p.lower() for p in k[len(ENV_PREFIX):].split("__") if p]
        if not parts:
            continue
        try:
            value = yaml.safe_load(v)
        except yaml.YAMLError:
            value = v
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("MODELSEEK_CONFIG", "config.yaml"))
    return _merge(_merge(DEFAULTS, _read_yaml(path)), env_overrides())


def env(name: str | None) -> str | None:
    if not name:
        return None
    val = os.environ.get(name, "").strip()
    return val or None


class Settings:

    def __init__(self, base: dict[str, Any] | None, config_path: str | os.PathLike | None, data_dir: str | Path):
        self._fixed_base = deepcopy(base) if base is not None else None
        self.config_path = Path(config_path or os.environ.get("MODELSEEK_CONFIG", "config.yaml"))
        self.data_dir = Path(data_dir)
        self.overrides_path = self.data_dir / "settings.yaml"
        self.secrets_path = self.data_dir / "secrets.env"
        self._lock = threading.Lock()
        self.load_secrets()

    def base(self) -> dict[str, Any]:
        return deepcopy(self._fixed_base) if self._fixed_base is not None else load_config(self.config_path)

    def overrides(self) -> dict[str, Any]:
        return _read_yaml(self.overrides_path)

    def effective(self) -> dict[str, Any]:
        return _merge(self.base(), self.overrides())

    def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        bad = [k for k in patch if k not in EDITABLE]
        if bad:
            raise ValueError("нельзя менять разделы: " + ", ".join(bad))
        bad = [k for k, v in patch.items() if not isinstance(v, dict)]
        if bad:
            raise ValueError("раздел должен быть объектом: " + ", ".join(bad))
        with self._lock:
            current = self.effective()
            ov = self.overrides()
            ov = _merge_keep_null(ov, patch)
            candidate = _merge(self.base(), ov)
            validate(candidate)
            self._check_protected(current, candidate)
            self._write_overrides(ov)
        return candidate

    def _check_protected(self, current: dict[str, Any], candidate: dict[str, Any]) -> None:
        def get(cfg: dict[str, Any], section: str, key: str) -> Any:
            sec = cfg.get(section)
            return (sec.get(key) if isinstance(sec, dict) else None) or ""

        for section, key in LOCKED:
            if get(current, section, key) != get(candidate, section, key):
                raise ValueError(f"{section}.{key}: задаётся только в config.yaml или переменной окружения")

        site_keys = {str(sc[k]) for sc in (candidate.get("sources") or {}).values() if isinstance(sc, dict)
                     for k in ("token_env", "key_env", "user_env") if sc.get(k)}
        own_keys = set(self._read_secrets()) | self._dotenv_names()
        sinks = (("translate", "api_key_env", ("url",)), ("notify", "token_env", ("url", "kind")))
        for section, key, fields in sinks:
            name = str(get(candidate, section, key))
            if not name or (section == "notify" and get(candidate, section, "kind") == "telegram"):
                continue
            if all(get(current, section, f) == get(candidate, section, f) for f in (key, *fields)):
                continue
            if name in site_keys:
                raise ValueError(f"{section}.{key}: ключ сайта нельзя отправлять на другой адрес")
            if os.environ.get(name) and name not in own_keys:
                raise ValueError(f"{section}.{key}: {name} — переменная окружения системы, а не ключ modelseek. "
                                 "Укажите другое имя или задайте её в config.yaml")

    def _dotenv_names(self) -> set[str]:
        path = self.config_path.parent / ".env"
        if not path.exists():
            return set()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {ln.split("=", 1)[0].strip() for ln in lines if "=" in ln and not ln.lstrip().startswith("#")}

    def reset(self, section: str | None = None) -> None:
        with self._lock:
            if section is None:
                self._write_overrides({})
            else:
                ov = self.overrides()
                ov.pop(section, None)
                self._write_overrides(ov)

    def _write_overrides(self, ov: dict[str, Any]) -> None:
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.overrides_path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(ov, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.overrides_path)

    def _read_secrets(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.secrets_path.exists():
            for line in self.secrets_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def load_secrets(self) -> None:
        for k, v in self._read_secrets().items():
            if ENV_NAME.match(k) and v and not os.environ.get(k):
                os.environ[k] = v

    def set_secrets(self, values: dict[str, str | None]) -> None:
        with self._lock:
            cur = self._read_secrets()
            for k, v in values.items():
                if not ENV_NAME.match(k):
                    raise ValueError(f"недопустимое имя переменной: {k}")
                v = (v or "").strip()
                if "\n" in v or "\r" in v:
                    raise ValueError("значение не может содержать перевод строки")
                if v:
                    cur[k] = v
                    os.environ[k] = v
                else:
                    cur.pop(k, None)
                    os.environ.pop(k, None)
            self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(f"{k}={v}\n" for k, v in sorted(cur.items()))
            self.secrets_path.write_text(body, encoding="utf-8")
            try:
                os.chmod(self.secrets_path, 0o600)
            except OSError:
                pass


def _merge_keep_null(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_keep_null(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def validate(cfg: dict[str, Any]) -> None:
    def num(path: str, lo: float, hi: float):
        cur: Any = cfg
        for p in path.split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        try:
            v = float(cur)
        except (TypeError, ValueError):
            raise ValueError(f"{path}: нужно число") from None
        if not lo <= v <= hi:
            raise ValueError(f"{path}: допустимо от {lo:g} до {hi:g}")

    num("server.request_timeout_seconds", 1, 120)
    num("server.per_source_limit", 1, 100)
    num("server.cache_ttl_seconds", 0, 86400)
    num("breaker.max_failures", 0, 100)
    num("breaker.cooldown_minutes", 0, 1440)
    num("subscriptions.check_every_seconds", 0, 86400)
    num("ranking.freshness_half_life_days", 1, 10000)
    for k, v in (cfg.get("ranking", {}).get("weights") or {}).items():
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"ranking.weights.{k}: нужно число ≥ 0")
    for section in ("sources", "custom_sources", "links"):
        for name, sc in (cfg.get(section) or {}).items():
            if not re.match(r"^[a-z0-9][a-z0-9_-]{0,39}$", str(name)):
                raise ValueError(f"{section}.{name}: имя — латиница в нижнем регистре, цифры, - и _")
            if not isinstance(sc, dict):
                raise ValueError(f"{section}.{name}: ожидались настройки")
            if "weight" in sc:
                num(f"{section}.{name}.weight", 0, 10)
            for key in ("token_env", "key_env", "user_env"):
                if sc.get(key) and not ENV_NAME.match(str(sc[key])):
                    raise ValueError(f"{section}.{name}.{key}: имя переменной ЗАГЛАВНЫМИ_БУКВАМИ")
    for name, sc in (cfg.get("custom_sources") or {}).items():
        mode = sc.get("mode", "http")
        if mode != "link" and not str(sc.get("url", "")).startswith(("http://", "https://")):
            raise ValueError(f"custom_sources.{name}.url: нужен адрес http(s)://")
        if mode == "link" and "{q}" not in str(sc.get("search_page", "")):
            raise ValueError(f"custom_sources.{name}.search_page: нужен адрес с {{q}}")
        if sc.get("search_page") and not str(sc["search_page"]).startswith(("http://", "https://")):
            raise ValueError(f"custom_sources.{name}.search_page: нужен адрес http(s)://")
    for name, sc in (cfg.get("links") or {}).items():
        if "{q}" not in str(sc.get("url", "")):
            raise ValueError(f"links.{name}.url: адрес должен содержать {{q}} — туда подставится запрос")
        if not str(sc.get("url", "")).startswith(("http://", "https://")):
            raise ValueError(f"links.{name}.url: нужен адрес http(s)://")
    for section, key in (("translate", "api_key_env"), ("notify", "token_env")):
        name = (cfg.get(section) or {}).get(key)
        if name and not ENV_NAME.match(str(name)):
            raise ValueError(f"{section}.{key}: имя переменной ЗАГЛАВНЫМИ_БУКВАМИ")
