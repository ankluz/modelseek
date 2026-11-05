"""Внешний профиль: отдельный порт для доступа извне.

Здесь есть только поиск. Избранное, история, подписки, ключи, установка компонентов и настройки сервера
остаются в админском профиле, и маршрутов к ним на этом порту нет совсем. Свои настройки и избранное
гость хранит у себя в браузере.

Гость ищет со своими ключами: они лежат в куки его браузера и приходят с каждым поиском. Ключи владельца
гостю достаются, только если в config.yaml задано public.lend_keys: true. Сайт, который отвечает
скриптам с чужих страниц (CORS), браузер гостя опрашивает сам, а сервер только разбирает и ранжирует ответ.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route

from . import dockernet
from .config import as_list, validate_public
from .fetchers import FetchResult, PlannedRequest, origin_of
from .main import (BAD_INPUT, CSP, SECURITY_HEADERS, STATIC, BodyLimit, SafeStatic, body, err, hostname,
                   page_html, same_origin, source_response)
from .pipeline import SearchParams
from .ranking import SIGNALS
from .runtime import Runtime
from .sources import GUEST, CustomSource, GuestAccess
from .storage import BUILTIN_PRESETS

log = logging.getLogger("modelseek.public")

SESSION_COOKIE = "ms_public_session"
GUEST_ID_COOKIE = "ms_guest_id"      # random, only to count limits when the guest's address is hidden
GUEST_KEYS_COOKIE = "ms_guest_keys"  # set by the guest's own page, see frontend/util.js
SESSION_DAYS = 30
MAX_PUBLIC_BODY = 4096  # a password, or a few small fields
MAX_FETCHED_TEXT = 2_000_000  # one site answer relayed by the guest's browser
MAX_RELAY_BODY = 3 * 1024 * 1024
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = 600
FAIL_DELAY = 0.5  # seconds after a wrong password
# Behind Docker Desktop every guest arrives from the same gateway: limits then go per browser,
# with an overall cap so a client that drops its cookie cannot get around them
HIDDEN_LOGIN_ATTEMPTS = 30
HIDDEN_SEARCH_FACTOR = 5
OPEN_PATHS = ("/", "/api/session", "/api/login", "/api/logout", "/source.zip", "/favicon.ico")
GUEST_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
KEY_VALUE = re.compile(r"[\x21-\x7e]{1,256}")
Net = ipaddress.IPv4Network | ipaddress.IPv6Network
Addr = ipaddress.IPv4Address | ipaddress.IPv6Address


def networks(values: Any) -> list[Net]:
    return [ipaddress.ip_network(v, strict=False) for v in as_list(values)]


def parse_ip(value: str | None) -> Addr | None:
    try:
        ip = ipaddress.ip_address((value or "").strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def inside(ip: Addr | None, nets: list[Net]) -> bool:
    return ip is not None and any(ip.version == n.version and ip in n for n in nets)


def client_ip(request: Request, proxies: list[Net]) -> Addr | None:
    """The visitor's address. X-Forwarded-For is believed only from a configured proxy, right to left."""
    peer = parse_ip(request.client.host if request.client else None)
    if not proxies or not inside(peer, proxies):
        return peer
    chain = [parse_ip(p) for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    for ip in reversed(chain):
        if ip is None:
            return peer
        if not inside(ip, proxies):
            return ip
    return chain[0] if chain else peer


def visitor_key(ip: Addr | None) -> str:
    if ip is None:
        return "?"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class RateLimit:
    """Sliding window: at most `limit` events per `window` seconds for each key."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.events: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        now = self.clock()
        q = self.events.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(self.events) > 10000:
            for k in [k for k, v in self.events.items() if not v or now - v[-1] >= self.window]:
                self.events.pop(k, None)
        return q

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def hit(self, key: str) -> bool:
        """Count one event; False when the key is over the limit (the event is not counted then)."""
        q = self._recent(key)
        if len(q) >= self.limit:
            return False
        q.append(self.clock())
        self.events[key] = q
        return True

    def retry_after(self, key: str) -> int:
        q = self._recent(key)
        return max(1, int(self.window - (self.clock() - q[0])) + 1) if q else 1


class Sessions:
    """Signed cookie after the shared password. Changing the password logs everyone out."""

    def __init__(self, key_path: Path):
        self.key = self._load_key(key_path)

    @staticmethod
    def _load_key(path: Path) -> bytes:
        try:
            data = bytes.fromhex(path.read_text(encoding="ascii").strip())
            if len(data) >= 32:
                return data
        except (OSError, ValueError):
            pass
        data = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data.hex(), encoding="ascii")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data

    def _sign(self, expires: int, password: str) -> str:
        pw = hashlib.sha256(password.encode()).hexdigest()
        return hmac.new(self.key, f"{expires}:{pw}".encode(), hashlib.sha256).hexdigest()

    def issue(self, password: str, now: float | None = None) -> str:
        expires = int((now or time.time()) + SESSION_DAYS * 86400)
        return f"{expires}.{self._sign(expires, password)}"

    def valid(self, token: str | None, password: str, now: float | None = None) -> bool:
        if not token or not password or "." not in token:
            return False
        exp, _, sig = token.partition(".")
        if not exp.isdigit() or int(exp) < (now or time.time()):
            return False
        return hmac.compare_digest(sig, self._sign(int(exp), password))


class Relay:
    """Site answers a guest's browser fetched itself, waiting for the search they were planned for.

    An entry belongs to one visitor and one search, lives `ttl` seconds and is used once. Memory is capped
    per visitor, in entries, and in bytes overall.
    """

    def __init__(self, ttl: float = 120.0, per_visitor: int = 4, max_entries: int = 500,
                 max_bytes: int = 32 * 1024 * 1024, clock=time.monotonic):
        self.ttl, self.per_visitor, self.max_entries, self.max_bytes = ttl, per_visitor, max_entries, max_bytes
        self.clock = clock
        self.entries: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _size(self) -> int:
        return sum(len(r.text) for e in self.entries.values() for r in e["results"].values())

    def _expire(self) -> None:
        now = self.clock()
        for token in [t for t, e in self.entries.items() if now - e["at"] > self.ttl]:
            self.entries.pop(token, None)

    def open(self, who: str, sig: tuple, plans: dict[str, PlannedRequest]) -> str:
        self._expire()
        mine = [t for t, e in self.entries.items() if e["who"] == who]
        for t in mine[:max(0, len(mine) - self.per_visitor + 1)]:
            self.entries.pop(t, None)
        while len(self.entries) >= self.max_entries:
            self.entries.popitem(last=False)
        token = secrets.token_urlsafe(18)
        self.entries[token] = {"who": who, "sig": sig, "plans": dict(plans), "results": {}, "at": self.clock()}
        return token

    def put(self, token: str, who: str, source: str, status: int, text: str) -> str | None:
        """Store one answer; returns why it was refused, or None."""
        self._expire()
        e = self.entries.get(token)
        if e is None or e["who"] != who:
            return "поиск устарел — повторите"
        if source not in e["plans"] or source in e["results"]:
            return "этот ответ не ожидался"
        e["results"][source] = FetchResult(status, text, e["plans"][source].url)
        while self._size() > self.max_bytes and len(self.entries) > 1:
            oldest = next(iter(self.entries))
            if oldest == token:
                break
            self.entries.pop(oldest, None)
        return None

    def take(self, token: str | None, who: str, sig: tuple) -> dict[str, tuple[PlannedRequest, FetchResult]]:
        self._expire()
        e = self.entries.get(token or "")
        if e is None or e["who"] != who:
            return {}
        self.entries.pop(token, None)
        if e["sig"] != sig:
            return {}
        return {name: (e["plans"][name], result) for name, result in e["results"].items()}


def signature(sp: SearchParams) -> tuple:
    return sp.query, sp.page, sp.site_order


def create_public_app(rt: Runtime) -> Starlette:
    validate_public(rt.cfg.get("public") or {})
    sessions = Sessions(Path(rt.settings.data_dir) / "public-session.key")
    logins = RateLimit(LOGIN_ATTEMPTS, LOGIN_WINDOW)
    hidden_logins = RateLimit(HIDDEN_LOGIN_ATTEMPTS, LOGIN_WINDOW)
    relay = Relay()
    limits: dict[str, Any] = {"version": None}
    visible_cache: dict[str, Any] = {"version": None, "names": []}

    def pub() -> dict[str, Any]:
        return rt.cfg.get("public") or {}

    def lend() -> bool:
        return bool(pub().get("lend_keys"))

    def password() -> str:
        name = str(pub().get("password_env") or "")
        return os.environ.get(name, "").strip() if name else ""

    def searches() -> tuple[RateLimit, RateLimit]:
        if limits["version"] != rt.version:
            per = int(pub().get("searches_per_minute") or 20)
            old = limits.get("rl")
            limits["rl"] = RateLimit(per) if old is None or old.limit != per else old
            limits["hidden"] = RateLimit(per * HIDDEN_SEARCH_FACTOR)
            limits["sem"] = limits.get("sem") or asyncio.Semaphore(int(pub().get("max_parallel_searches") or 4))
            limits["version"] = rt.version
        return limits["rl"], limits["hidden"]

    def logged_in(request: Request) -> bool:
        return sessions.valid(request.cookies.get(SESSION_COOKIE), password())

    def who(request: Request) -> str:
        """Whom limits count: the address, or the browser when Docker hides the address."""
        if request.state.hidden:
            return "id:" + (request.state.guest_id or "-")
        return visitor_key(request.state.ip)

    async def visible() -> list[str]:
        """Sources a visitor may use: enabled ones, limited by public.sources, without the owner's local sites.
        A source that needs keys is offered too: the guest can bring their own."""
        if visible_cache["version"] == rt.version:
            return visible_cache["names"]
        only = set(as_list(pub().get("sources")))
        names = []
        for name, src in rt.sources.items():
            if not src.enabled or (only and name not in only):
                continue
            if isinstance(src, CustomSource):
                if src.cfg.get("headers") and not lend():
                    continue  # its headers may carry the owner's key
                urls = [str(src.cfg.get(k) or "") for k in ("url", "search_page", "homepage")]
                if not all([await rt.guard.allowed(u, use_trust=False) for u in urls if u]):
                    continue
            names.append(name)
        visible_cache.update(version=rt.version, names=names)
        return names

    def direct_origins() -> set[str]:
        """Sites a guest's browser may ask itself (they allow it with CORS)."""
        only = set(as_list(pub().get("sources")))
        return {origin_of(s.homepage) for n, s in rt.sources.items()
                if s.browser_direct and s.enabled and s.mode == "http" and s.homepage and (not only or n in only)}

    def guest_keys(request: Request) -> dict[str, dict[str, str]]:
        raw = request.cookies.get(GUEST_KEYS_COOKIE)
        if not raw:
            return {}
        try:
            data = json.loads(unquote(raw))
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for name, fields in list(data.items())[:32]:
            src = rt.sources.get(name) if isinstance(name, str) else None
            if src is None or not isinstance(fields, dict):
                continue
            good = {k: v.strip() for k, v in fields.items()
                    if k in src.secret_keys and isinstance(v, str) and KEY_VALUE.fullmatch(v.strip())}
            if good:
                out[name] = good
        return out

    def access(request: Request, fetched: dict | None = None) -> GuestAccess:
        return GuestAccess(keys=guest_keys(request), lend=lend(), fetched=fetched or {})

    def source_info(src, acc: GuestAccess) -> dict[str, Any]:
        token = GUEST.set(acc)  # "configured" as this guest sees it
        try:
            i = src.info()
            owner_has = False
            if lend() and src.secret_keys:
                GUEST.set(GuestAccess(lend=True))
                owner_has = src.configured
        finally:
            GUEST.reset(token)
        out = {k: i.get(k) for k in ("name", "label", "homepage", "color", "enabled", "configured", "weight",
                                     "official_api", "mode", "link_only", "search_page")}
        out["missing"] = []  # the owner's variable names stay private
        out["key_fields"] = list(src.secret_keys) if src.mode != "link" else []
        out["own_keys"] = sorted(acc.keys.get(src.name, {}))
        out["owner_key"] = owner_has
        out["browser_direct"] = bool(src.browser_direct and src.mode == "http")
        until = rt.engine.breaker.is_open(src.name)
        out["paused_until"] = until.strftime("%H:%M") if until else None
        return out

    def auth_info(request: Request) -> dict[str, Any]:
        mode = pub().get("auth", "none")
        return {"auth": mode, "logged_in": mode != "password" or logged_in(request),
                "password_set": bool(password()) if mode == "password" else None}

    def source_url() -> str:
        return str(pub().get("source_url") or "") or "/source.zip"

    async def api_session(request: Request):
        return JSONResponse(auth_info(request))

    async def api_login(request: Request):
        if pub().get("auth") != "password":
            return JSONResponse(auth_info(request))
        k, hidden = who(request), request.state.hidden
        if logins.blocked(k) or (hidden and hidden_logins.blocked("all")):
            wait = max(logins.retry_after(k), hidden_logins.retry_after("all") if hidden else 0)
            return JSONResponse({"error": "слишком много попыток, подождите несколько минут"}, status_code=429,
                                headers={"Retry-After": str(wait)})
        pw = password()
        given = str((await body(request)).get("password") or "")
        if not pw:
            return err("пароль ещё не задан владельцем", 503)
        if not hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(pw.encode()).digest()):
            logins.hit(k)
            if hidden:
                hidden_logins.hit("all")
            await asyncio.sleep(FAIL_DELAY)
            return err("неверный пароль", 403)
        resp = JSONResponse({**auth_info(request), "logged_in": True})
        resp.set_cookie(SESSION_COOKIE, sessions.issue(pw), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")
        return resp

    async def api_logout(request: Request):
        resp = JSONResponse({**auth_info(request), "logged_in": pub().get("auth") != "password"})
        resp.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")
        return resp

    async def api_sources(request: Request):
        names = await visible()
        acc = access(request)
        return JSONResponse({"sources": [source_info(rt.sources[n], acc) for n in names if n in rt.sources],
                             "browser": None, "lend_keys": lend()})

    async def api_config(request: Request):
        r = rt.cfg["ranking"]
        names = await visible()
        f = rt.features()
        weights = {k: r["weights"].get(k, 0) for k in SIGNALS}
        weights["personal"] = 0
        return JSONResponse({
            "profile": "public",
            "weights": weights,
            "freshness_half_life_days": r["freshness_half_life_days"],
            "filters": rt.cfg["filters"],
            "source_weights": {n: rt.sources[n].weight for n in names if n in rt.sources},
            "features": {
                "semantic": {"available": bool(f["semantic"].get("available")), "error": None},
                "translate": {"enabled": bool(f["translate"].get("enabled")), "error": None, "model": ""},
                "image_dedupe": {k: bool(f["image_dedupe"].get(k)) for k in ("enabled", "available")},
                "notify": {"enabled": False, "kind": ""},
            },
            "intervals": [],
            "version": rt.version,
            "source_url": source_url(),
            "lend_keys": lend(),
            **auth_info(request),
        })

    async def api_presets(request: Request):
        return JSONResponse({"items": [{"id": None, **p, "builtin": True} for p in BUILTIN_PRESETS]})

    async def params(request: Request) -> SearchParams | Response:
        try:
            sp = SearchParams.from_mapping(request.query_params, rt.cfg)
        except BAD_INPUT as e:
            return err(f"некорректный параметр запроса: {e}")
        if not sp.query:
            return err("пустой запрос")
        if len(sp.query) > 200:
            return err("слишком длинный запрос")
        names = await visible()
        sp.sources = names if sp.sources is None else [n for n in sp.sources if n in names]
        sp.weights["personal"] = 0
        sp.filters.pop("hide_seen", None)
        return sp

    def limited(request: Request) -> str | None:
        k = who(request)
        rl, hidden_rl = searches()
        if request.state.hidden and hidden_rl.blocked("all"):
            return f"слишком много поисков от гостей — попробуйте через {hidden_rl.retry_after('all')} с"
        if not rl.hit(k):
            return f"слишком много поисков подряд — попробуйте через {rl.retry_after(k)} с"
        if request.state.hidden:
            hidden_rl.hit("all")
        return None

    def fetched_for(request: Request, sp: SearchParams) -> dict:
        return relay.take(request.query_params.get("pf"), who(request), signature(sp))

    async def api_plan(request: Request):
        """What the guest's browser may fetch itself for this search, before it opens the result stream."""
        sp = await params(request)
        if isinstance(sp, Response):
            return sp
        allowed = direct_origins()
        plans = {n: r for n, r in (await rt.pipeline.browser_plan(sp, access(request))).items()
                 if origin_of(r.url) in allowed}
        if not plans:
            return JSONResponse({"token": None, "requests": []})
        token = relay.open(who(request), signature(sp), plans)
        return JSONResponse({"token": token, "requests": [{"source": n, **r.to_dict()} for n, r in plans.items()]})

    async def api_relay(request: Request):
        """One answer the guest's browser got from a site; used for that guest's next search only."""
        data = await body(request)
        token, source, status, text = data.get("token"), data.get("source"), data.get("status"), data.get("text")
        if not (isinstance(token, str) and isinstance(source, str) and isinstance(text, str)
                and isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599):
            return err("некорректный ответ")
        if len(text) > MAX_FETCHED_TEXT:
            return err("слишком большой ответ", 413)
        why = relay.put(token, who(request), source, status, text)
        if why:
            return err(why, 409)
        return JSONResponse({"ok": True})

    async def api_search(request: Request):
        sp = await params(request)
        if isinstance(sp, Response):
            return sp
        if why := limited(request):
            return err(why, 429)
        acc = access(request, fetched_for(request, sp))
        async with limits["sem"]:
            return JSONResponse(await rt.pipeline.final(sp, guest=acc))

    async def api_search_stream(request: Request):
        sp = await params(request)
        if isinstance(sp, Response):
            return sp
        why = limited(request)
        acc = access(request, fetched_for(request, sp))
        pipeline, sem = rt.pipeline, limits["sem"]

        async def gen():
            if why:
                yield f"event: fail\ndata: {json.dumps({'error': why}, ensure_ascii=False)}\n\n"
                return
            async with sem:
                async for snap in pipeline.run(sp, guest=acc):
                    if await request.is_disconnected():
                        break
                    yield f"event: update\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def index(request: Request):
        return page_html("public")

    async def source_zip(request: Request):
        return source_response()

    routes = [
        Route("/", index),
        Route("/source.zip", source_zip),
        Route("/api/session", api_session),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
        Route("/api/config", api_config),
        Route("/api/sources", api_sources),
        Route("/api/presets", api_presets),
        Route("/api/search", api_search),
        Route("/api/search/plan", api_plan),
        Route("/api/search/relay", api_relay, methods=["POST"]),
        Route("/api/search/stream", api_search_stream),
        Mount("/static", SafeStatic(directory=STATIC), name="static"),
    ]

    def page_csp() -> str:
        extra = sorted(direct_origins())
        return CSP.replace("connect-src 'self'", "connect-src 'self' " + " ".join(extra)) if extra else CSP

    class PublicGuard(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            gid = request.cookies.get(GUEST_ID_COOKIE) or ""
            request.state.guest_id = gid if GUEST_ID.fullmatch(gid) else None
            response = await self.check(request, call_next)
            response.headers.update(SECURITY_HEADERS)
            response.headers["Content-Security-Policy"] = page_csp()
            if request.state.guest_id is None:
                response.set_cookie(GUEST_ID_COOKIE, secrets.token_urlsafe(18), max_age=365 * 86400, httponly=True,
                                    samesite="strict", secure=request.url.scheme == "https", path="/")
            return response

        async def check(self, request: Request, call_next):
            p = pub()
            ip = client_ip(request, networks(p.get("trusted_proxies")))
            request.state.ip = ip
            request.state.hidden = dockernet.is_gateway(ip)
            if request.state.hidden and not getattr(rt, "public_addresses_hidden", False):
                rt.public_addresses_hidden = True
                log.warning("Docker не показывает адреса гостей (все приходят с %s): лимиты считаются по браузеру, "
                            "allow_from и auth: subnet пропустить гостей не смогут", ip)
            allow = networks(p.get("allow_from"))
            if allow and not inside(ip, allow):
                return err("доступ с этого адреса закрыт", 403)
            hosts = {h.lower() for h in as_list(p.get("allowed_hosts"))}
            host = hostname(request.headers.get("host"))
            if hosts and host not in hosts:
                return err("неизвестный адрес сайта", 403)
            path = request.url.path
            if path.startswith("/api/"):
                if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                    return err("запрос с другого сайта отклонён", 403)
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if request.headers.get("x-modelseek") != "1":
                        return err("missing X-Modelseek header", 403)
                    origin = request.headers.get("origin")
                    if origin and not same_origin(origin, request):
                        return err("запрос с другого сайта отклонён", 403)
            if p.get("auth") == "password" and not (path in OPEN_PATHS or path.startswith("/static/")):
                if not password():
                    return err("вход по паролю включён, но пароль ещё не задан владельцем", 503)
                if not logged_in(request):
                    return JSONResponse({"error": "нужен пароль", "login": True}, status_code=401)
            response = await call_next(request)
            response.headers.setdefault("Cache-Control", "no-store" if path.startswith("/api/") else "no-cache")
            return response

    app = Starlette(routes=routes, middleware=[
        Middleware(BodyLimit, limit=MAX_PUBLIC_BODY, paths={"/api/search/relay": MAX_RELAY_BODY}),
        Middleware(PublicGuard),
    ])
    app.state.runtime = rt
    app.state.profile = "public"
    return app
