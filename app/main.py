from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import dockernet, installer, source
from .config import EDITABLE, Settings, allowed_hosts, load_config
from .fetchers import FetchUnavailable, origin_of
from .models import clean_hit
from .netguard import BlockedRequest, Resolver, ResponseTooLarge
from .pipeline import SearchParams
from .ranking import SIGNALS
from .runtime import Runtime
from .scheduler import INTERVALS, Notifier
from .sources import REGISTRY, CustomSource, SourceError
from .sources.custom import find_arrays, flatten_keys, guess_fields

STATIC = Path(__file__).resolve().parent.parent / "frontend"
log = logging.getLogger("modelseek")

MODE_INFO = {
    "http": "Обычные запросы. Для официальных API и сайтов без защиты.",
    "link": "Не опрашивать — только ссылка «искать на сайте». Надёжно, но без карточек.",
    "impersonate": "Запрос с отпечатком Chrome (curl_cffi). Обходит защиту от ботов — против правил сайтов.",
    "browser": "Настоящий Chromium; проверку проходите сами в окне. Обходит защиту — против правил сайтов.",
}


BAD_INPUT = (ValueError, TypeError, OverflowError)
MAX_INTERVAL_MIN = 525600


def err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def hostname(value: str | None, with_scheme: bool = False) -> str:
    try:
        return (urlsplit(value if with_scheme else f"//{value}").hostname or "").lower()
    except ValueError:
        return ""


def same_origin(origin: str | None, request: Request) -> bool:
    """Origin must name this very server: host and port. 127.0.0.1:8090 is another site for 127.0.0.1:8088."""
    try:
        o = urlsplit(origin or "")
        h = urlsplit(f"//{request.headers.get('host', '')}")
        default = {"http": 80, "https": 443}
        oport = o.port or default.get(o.scheme)
        hport = h.port or default.get(request.url.scheme)
    except ValueError:
        return False
    return (o.scheme in default and bool(o.hostname) and o.hostname.lower() == (h.hostname or "").lower()
            and oport == hport)


SAFE_STATIC = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)*")


class SafeStatic(StaticFiles):
    """Only plain names: Windows also opens INDEX~1.HTM, index.html::$DATA and "file." as other spellings."""

    async def get_response(self, path: str, scope):
        root, full = scope.get("root_path", ""), scope["path"]
        asked = full[len(root):] if root and full.startswith(root) else full  # the URL path, before normpath
        if not SAFE_STATIC.fullmatch(asked.strip("/")):
            return err("нет такого файла", 404)
        return await super().get_response(path, scope)


MAX_BODY = 256 * 1024


class BodyLimit:
    """Refuse request bodies over `limit` bytes, declared or streamed, before anyone reads them.
    `paths` gives single routes their own limit."""

    def __init__(self, app, limit: int = MAX_BODY, paths: dict[str, int] | None = None):
        self.app, self.default, self.paths = app, limit, dict(paths or {})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.paths.get(scope.get("path", ""), self.default)
        declared = dict(scope.get("headers") or []).get(b"content-length", b"0")
        if not declared.isdigit() or int(declared) > limit:
            return await err("слишком большой запрос", 413)(scope, receive, send)
        # Read the whole (small) body up front: an over-long stream is answered here, never handed on.
        chunks, seen = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # client went away
            chunks.append(message.get("body", b""))
            seen += len(chunks[-1])
            if seen > limit:
                return await err("слишком большой запрос", 413)(scope, receive, send)
            if not message.get("more_body"):
                break
        replay = {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        async def buffered():
            nonlocal replay
            if replay is not None:
                message, replay = replay, None
                return message
            return await receive()

        await self.app(scope, buffered, send)


def parse_interval(value: Any) -> int:
    minutes = int(value)
    if not 1 <= minutes <= MAX_INTERVAL_MIN:
        raise ValueError("interval_min вне диапазона")
    return minutes


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


def secret_names(cfg: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for section in ("sources", "custom_sources"):
        for sc in (cfg.get(section) or {}).values():
            for k in ("token_env", "key_env", "user_env"):
                if isinstance(sc, dict) and sc.get(k):
                    names.append(str(sc[k]))
    for path in (("translate", "api_key_env"), ("notify", "token_env"), ("public", "password_env")):
        v = (cfg.get(path[0]) or {}).get(path[1])
        if v:
            names.append(str(v))
    return sorted(set(names))


CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "img-src 'self' https: http: data: blob:",
    "connect-src 'self'",
    "form-action 'self' https:",
    "frame-src 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'none'",
))
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=()",
}


def page_html(profile: str) -> HTMLResponse:
    """index.html with the profile marked on <html>, so boot.js picks the right storage before any request."""
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    text = text.replace('<html lang="ru">', f'<html lang="ru" data-profile="{profile}">', 1)
    return HTMLResponse(text, headers={"Cache-Control": "no-cache"})


def source_response() -> Response:
    return Response(source.archive(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="modelseek-source.zip"'})


PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip")


def from_this_machine(request: Request) -> bool:
    """The admin profile answers only to this computer, and never through a proxy.

    In Docker the port is published on the host's 127.0.0.1 only and arrives from Docker's gateway;
    other containers on the same network have their own addresses and are refused.
    """
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True  # unix socket or in-process test client
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    if dockernet.IN_DOCKER:
        gws = dockernet.gateways()
        return ip in gws if gws else True
    return False


def create_app(cfg: dict[str, Any] | None = None, transport: httpx.AsyncBaseTransport | None = None,
               db_path: str | None = None, start_scheduler: bool = True,
               config_path: str | None = None, data_dir: str | None = None,
               resolver: Resolver | None = None, runtime: Runtime | None = None) -> Starlette:
    """The admin profile. With `runtime` the caller owns it (app.serve shares one runtime with the public app)."""
    if runtime is None:
        base_for_dir = cfg if cfg is not None else load_config(config_path)
        settings = Settings(cfg, config_path, data_dir or base_for_dir["server"]["data_dir"])
        rt = Runtime(settings, transport=transport, db_path=db_path, start_scheduler=start_scheduler,
                     resolver=resolver)
    else:
        rt, settings = runtime, runtime.settings

    @asynccontextmanager
    async def lifespan(app):
        if runtime is not None:
            yield
            return
        await rt.start()
        try:
            yield
        finally:
            await rt.close()

    S = lambda: rt.storage

    async def api_sources(request: Request):
        out = []
        for s in rt.sources.values():
            info = s.info()
            until = rt.engine.breaker.is_open(s.name)
            info["paused_until"] = until.strftime("%H:%M") if until else None
            out.append(info)
        return JSONResponse({"sources": out, "browser": rt.engine.browser.status()})

    features = rt.features

    def public_status() -> dict[str, Any]:
        p = rt.cfg.get("public") or {}
        name = str(p.get("password_env") or "")
        return {"enabled": bool(p.get("enabled")), "host": p.get("host"), "port": p.get("port"),
                "auth": p.get("auth", "none"), "allow_from": p.get("allow_from") or [],
                "password_env": name, "password_set": bool(name and os.environ.get(name)),
                "searches_per_minute": p.get("searches_per_minute"), "sources": p.get("sources") or [],
                "lend_keys": bool(p.get("lend_keys")), "docker": dockernet.IN_DOCKER,
                "addresses_hidden": bool(getattr(rt, "public_addresses_hidden", False)),
                "running": bool(getattr(rt, "public_running", False))}

    async def api_config(request: Request):
        r = rt.cfg["ranking"]
        return JSONResponse({
            "profile": "admin",
            "source_url": str((rt.cfg.get("public") or {}).get("source_url") or "") or "/source.zip",
            "weights": {k: r["weights"].get(k, 0) for k in SIGNALS},
            "freshness_half_life_days": r["freshness_half_life_days"],
            "filters": rt.cfg["filters"],
            "source_weights": {n: s.weight for n, s in rt.sources.items()},
            "features": features(),
            "intervals": [{"minutes": m, "label": l} for m, l in INTERVALS.items()],
            "version": rt.version,
        })

    async def api_cache_clear(request: Request):
        rt.engine.cache.clear()
        rt.engine.breaker.reset()
        return JSONResponse({"ok": True})

    async def api_source_reset(request: Request):
        rt.engine.breaker.reset(request.path_params["name"])
        return JSONResponse({"ok": True})

    def settings_payload() -> dict[str, Any]:
        eff = rt.cfg
        builtin = []
        for name, cls in REGISTRY.items():
            builtin.append({"name": name, "label": cls.label, "homepage": cls.homepage, "color": cls.color,
                            "allowed_modes": list(cls.allowed_modes), "default_mode": cls.default_mode,
                            "secret_keys": list(cls.secret_keys), "official_api": cls.official_api})
        return {
            "config": {k: eff.get(k) for k in EDITABLE},
            "overrides": settings.overrides(),
            "secrets": {n: bool(os.environ.get(n)) for n in secret_names(eff)},
            "builtin": builtin,
            "modes": MODE_INFO,
            "features": features(),
            "public": public_status(),
            "paths": {"config": str(settings.config_path.resolve()),
                      "overrides": str(settings.overrides_path.resolve()),
                      "secrets": str(settings.secrets_path.resolve())},
            "version": rt.version,
        }

    async def api_settings(request: Request):
        if request.method == "GET":
            return JSONResponse(settings_payload())
        b = await body(request)
        patch = b.get("patch")
        if not isinstance(patch, dict):
            return err("нужен объект patch")
        try:
            settings.patch(patch)
        except (ValueError, TypeError) as e:
            return err(str(e))
        await rt.reload()
        return JSONResponse(settings_payload())

    async def api_settings_reset(request: Request):
        section = (await body(request)).get("section")
        if section is not None and section not in EDITABLE:
            return err("нет такого раздела")
        settings.reset(section)
        await rt.reload()
        return JSONResponse(settings_payload())

    async def api_secrets(request: Request):
        values = (await body(request)).get("values")
        if not isinstance(values, dict):
            return err("нужен объект values")
        try:
            settings.set_secrets({str(k): (None if v is None else str(v)) for k, v in values.items()})
        except ValueError as e:
            return err(str(e))
        await rt.reload()
        return JSONResponse(settings_payload())

    async def api_source_test(request: Request):
        b = await body(request)
        conf = b.get("config")
        query = (b.get("query") or "benchy").strip()[:100]
        if not isinstance(conf, dict):
            return err("нужен объект config")
        src = CustomSource(str(b.get("name") or "test"), {**conf, "mode": conf.get("mode", "http")})
        if src.mode == "link":
            return JSONResponse({"ok": True, "link": src.search_link(query), "count": 0, "sample": []})
        out: dict[str, Any] = {"ok": False}
        try:
            rt.guard.trust(str(conf.get("url") or ""), str(conf.get("homepage") or ""))
            fetcher = rt.engine.fetcher_for(src)
            vals = {"q": query, "limit": 10, "page": 1, "page0": 0, "offset": 0}
            from .sources.custom import _fill_body, _fill_url
            url = _fill_url(str(conf.get("url", "")), vals)
            headers = {str(k): str(v) for k, v in (conf.get("headers") or {}).items()}
            if str(conf.get("method", "GET")).upper() == "POST":
                tpl = json.loads(conf.get("body") or "{}") if isinstance(conf.get("body"), str) else conf.get("body") or {}
                resp = await fetcher.post_json(url, _fill_body(tpl, vals), headers=headers)
            else:
                resp = await fetcher.get(url, headers=headers)
            out["http_status"] = resp.status
            data = src.check(resp)
            arrays = find_arrays(data)
            out["arrays"] = arrays
            try:
                items = src.items(data)
            except SourceError as e:
                out["error"] = str(e)
                return JSONResponse(out)
            first_item = next((i for i in items if isinstance(i, dict)), None)
            out["fields_available"] = flatten_keys(first_item) if first_item else {}
            out["fields_guess"] = guess_fields(first_item) if first_item else {}
            hits = [src.parse(it, i) for i, it in enumerate(items[:6]) if isinstance(it, dict)]
            hits = [h for h in map(clean_hit, hits) if h is not None]
            out.update(ok=True, count=len(items), sample=[h.to_dict() for h in hits])
        except (SourceError, FetchUnavailable, ValueError, BlockedRequest, ResponseTooLarge) as e:
            out["error"] = str(e)
        except httpx.HTTPError as e:
            out["error"] = f"сеть: {type(e).__name__}"
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"[:200]
        return JSONResponse(out)

    async def api_notify_test(request: Request):
        n: Notifier = rt.notifier
        if not n.enabled:
            return err("уведомления не настроены")
        e = await n.send("modelseek", "Тестовое уведомление: всё работает 👍")
        return err(e, 502) if e else JSONResponse({"ok": True})

    def job_started(job) -> JSONResponse:
        return JSONResponse({"job": job.to_dict()})

    def reload_after(job):
        if job.status == "ok":
            rt.reload_soon()

    async def api_extras(request: Request):
        return JSONResponse(installer.extras_status())

    async def api_extras_install(request: Request):
        keys = [k for k in (await body(request)).get("keys") or [] if k in installer.EXTRAS]
        if not keys:
            return err("выберите, что установить")
        job = rt.jobs.start("extras", "Установка: " + ", ".join(keys),
                            lambda j: installer.install_extras(j, keys), on_done=reload_after)
        return job_started(job)

    async def api_chromium(request: Request):
        return job_started(rt.jobs.start("chromium", "Скачивание Chromium", installer.install_chromium))

    async def api_semantic_warm(request: Request):
        if not rt.embedder.available:
            return err(rt.embedder.status().get("error") or "смысловой поиск выключен")
        emb = rt.embedder
        return job_started(rt.jobs.start("semantic", "Загрузка модели смыслового поиска",
                                         lambda j: installer.warm_embeddings(j, emb)))

    async def api_ollama(request: Request):
        import asyncio
        st = await asyncio.to_thread(installer.ollama_status, rt.cfg["translate"]["url"])
        st["model"] = rt.cfg["translate"]["model"]
        st["enabled"] = bool(rt.cfg["translate"]["enabled"])
        return JSONResponse(st)

    async def api_ollama_action(request: Request):
        action = request.path_params["action"]
        url = rt.cfg["translate"]["url"]
        if action == "install":
            job = rt.jobs.start("ollama-install", "Установка Ollama", installer.install_ollama)
        elif action == "start":
            job = rt.jobs.start("ollama-start", "Запуск Ollama", lambda j: installer.start_ollama(j, url))
        elif action == "pull":
            model = ((await body(request)).get("model") or rt.cfg["translate"]["model"]).strip()
            if not installer.MODEL_NAME.match(model):
                return err("некорректное имя модели")
            job = rt.jobs.start("ollama-pull", f"Скачивание {model}", lambda j: installer.pull_model(j, url, model))
        else:
            return err("неизвестное действие", 404)
        return job_started(job)

    async def api_job(request: Request):
        job = rt.jobs.get(request.path_params["jid"])
        if not job:
            return err("нет такой задачи", 404)
        since = int(request.query_params.get("since") or 0)
        return JSONResponse(job.to_dict(since))

    def params(request: Request) -> SearchParams | JSONResponse:
        try:
            sp = SearchParams.from_mapping(request.query_params, rt.cfg)
        except BAD_INPUT as e:
            return err(f"некорректный параметр запроса: {e}")
        if not sp.query:
            return err("пустой запрос")
        if len(sp.query) > 200:
            return err("слишком длинный запрос")
        return sp

    async def api_search(request: Request):
        sp = params(request)
        if isinstance(sp, JSONResponse):
            return sp
        return JSONResponse(await rt.pipeline.final(sp))

    async def api_search_stream(request: Request):
        sp = params(request)
        if isinstance(sp, JSONResponse):
            return sp
        pipeline = rt.pipeline

        async def gen():
            async for snap in pipeline.run(sp):
                if await request.is_disconnected():
                    break
                yield f"event: update\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def api_browser_open(request: Request):
        name = (await body(request)).get("source")
        src = rt.sources.get(name or "")
        if not src:
            return err("нет такого источника")
        try:
            await rt.engine.browser.open_for_user(origin_of(src.homepage))
        except FetchUnavailable as e:
            return err(str(e), 503)
        except Exception as e:
            return err(f"не удалось открыть браузер: {type(e).__name__}: {e}"[:200], 500)
        rt.engine.breaker.reset(src.name)
        return JSONResponse({"ok": True, "browser": rt.engine.browser.status()})

    async def api_browser_done(request: Request):
        await rt.engine.browser.finish_user()
        rt.engine.breaker.reset()
        rt.engine.cache.clear()
        return JSONResponse({"ok": True, "browser": rt.engine.browser.status()})

    async def api_browser_status(request: Request):
        return JSONResponse(rt.engine.browser.status())

    async def api_favorites(request: Request):
        if request.method == "GET":
            c = request.query_params.get("collection") or None
            return JSONResponse({"items": S().favorites(c), "collections": S().collections()})
        b = await body(request)
        if request.method == "POST":
            hit = b.get("hit")
            if not isinstance(hit, dict) or not hit.get("source") or not hit.get("id"):
                return err("нужен объект hit")
            if not isinstance(hit.get("title"), str) or not hit["title"]:
                return err("у hit нужно название")
            if not str(hit.get("url") or "").startswith(("http://", "https://")):
                return err("у hit нужна ссылка http(s)://")
            if not isinstance(hit.get("tags", []), list):
                return err("tags должен быть списком")
            S().add_favorite(hit, b.get("collection") or "Избранное", b.get("note") or "")
        elif request.method == "PATCH":
            if not b.get("key"):
                return err("нужен key")
            S().update_favorite(b["key"], b.get("collection"), b.get("note"))
        elif request.method == "DELETE":
            key = request.query_params.get("key") or b.get("key")
            if not key:
                return err("нужен key")
            S().remove_favorite(key)
        return JSONResponse({"ok": True, "collections": S().collections()})

    async def api_hidden(request: Request):
        if request.method == "GET":
            return JSONResponse({"items": S().hidden()})
        b = await body(request)
        key = request.query_params.get("key") or b.get("key")
        if not key:
            return err("нужен key")
        if request.method == "POST":
            S().hide(key, b.get("title") or "")
        else:
            S().unhide(key)
        return JSONResponse({"ok": True})

    async def api_seen(request: Request):
        key = (await body(request)).get("key")
        if key:
            S().mark_seen(key)
        return JSONResponse({"ok": True})

    async def api_history(request: Request):
        if request.method == "DELETE":
            S().delete_history(request.query_params.get("query"))
            return JSONResponse({"ok": True})
        return JSONResponse({"items": S().history()})

    async def api_presets(request: Request):
        if request.method == "GET":
            return JSONResponse({"items": S().presets()})
        if request.method == "DELETE":
            S().delete_preset(int(request.path_params.get("pid", 0)))
            return JSONResponse({"ok": True})
        b = await body(request)
        if not (b.get("name") or "").strip():
            return err("нужно имя")
        S().save_preset(b["name"], b.get("filters") or {})
        return JSONResponse({"ok": True, "items": S().presets()})

    async def api_subs(request: Request):
        if request.method == "GET":
            return JSONResponse({"items": S().subs(), "new_total": S().total_new()})
        b = await body(request)
        query = (b.get("query") or "").strip()
        if not query:
            return err("нужен запрос")
        try:
            interval = parse_interval(b.get("interval_min") or 1440)
            sp = SearchParams.from_dict({**(b.get("params") or {}), "query": query, "page": 1})
        except BAD_INPUT as e:
            return err(f"некорректные параметры подписки: {e}")
        sid = S().add_sub(query, sp.to_dict(), interval)
        res = await rt.scheduler.run_sub(sid)
        return JSONResponse({"ok": True, "id": sid, "run": res, "sub": S().sub(sid)})

    async def api_sub(request: Request):
        sid = int(request.path_params["sid"])
        if not S().sub(sid):
            return err("нет такой подписки", 404)
        if request.method == "DELETE":
            S().delete_sub(sid)
            return JSONResponse({"ok": True})
        if request.method == "PATCH":
            b = await body(request)
            try:
                interval = None if b.get("interval_min") is None else parse_interval(b["interval_min"])
            except BAD_INPUT as e:
                return err(f"некорректные параметры подписки: {e}")
            S().update_sub(sid, interval, b.get("enabled"))
        return JSONResponse({"sub": S().sub(sid)})

    async def api_sub_run(request: Request):
        sid = int(request.path_params["sid"])
        return JSONResponse({"run": await rt.scheduler.run_sub(sid), "sub": S().sub(sid)})

    async def api_sub_items(request: Request):
        sid = int(request.path_params["sid"])
        favs = S().favorite_keys()
        items = S().sub_items(sid, only_new=request.query_params.get("new") == "1")
        for it in items:
            it["favorite"] = favs.get(it["key"])
        return JSONResponse({"items": items, "sub": S().sub(sid)})

    async def api_sub_seen(request: Request):
        S().mark_sub_seen(int(request.path_params["sid"]))
        return JSONResponse({"ok": True})

    async def index(request: Request):
        return page_html("admin")

    async def source_zip(request: Request):
        return source_response()

    routes = [
        Route("/", index),
        Route("/source.zip", source_zip),
        Route("/api/search", api_search),
        Route("/api/search/stream", api_search_stream),
        Route("/api/sources", api_sources),
        Route("/api/sources/test", api_source_test, methods=["POST"]),
        Route("/api/sources/{name}/reset", api_source_reset, methods=["POST"]),
        Route("/api/config", api_config),
        Route("/api/settings", api_settings, methods=["GET", "PATCH"]),
        Route("/api/settings/reset", api_settings_reset, methods=["POST"]),
        Route("/api/secrets", api_secrets, methods=["PUT"]),
        Route("/api/cache/clear", api_cache_clear, methods=["POST"]),
        Route("/api/extras", api_extras),
        Route("/api/extras/install", api_extras_install, methods=["POST"]),
        Route("/api/extras/chromium", api_chromium, methods=["POST"]),
        Route("/api/semantic/warm", api_semantic_warm, methods=["POST"]),
        Route("/api/ollama", api_ollama),
        Route("/api/ollama/{action}", api_ollama_action, methods=["POST"]),
        Route("/api/jobs/{jid}", api_job),
        Route("/api/browser/open", api_browser_open, methods=["POST"]),
        Route("/api/browser/done", api_browser_done, methods=["POST"]),
        Route("/api/browser/status", api_browser_status),
        Route("/api/favorites", api_favorites, methods=["GET", "POST", "PATCH", "DELETE"]),
        Route("/api/hidden", api_hidden, methods=["GET", "POST", "DELETE"]),
        Route("/api/seen", api_seen, methods=["POST"]),
        Route("/api/history", api_history, methods=["GET", "DELETE"]),
        Route("/api/presets", api_presets, methods=["GET", "POST"]),
        Route("/api/presets/{pid:int}", api_presets, methods=["DELETE"]),
        Route("/api/subs", api_subs, methods=["GET", "POST"]),
        Route("/api/subs/{sid:int}", api_sub, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/subs/{sid:int}/run", api_sub_run, methods=["POST"]),
        Route("/api/subs/{sid:int}/items", api_sub_items),
        Route("/api/subs/{sid:int}/seen", api_sub_seen, methods=["POST"]),
        Route("/api/notify/test", api_notify_test, methods=["POST"]),
        Mount("/static", SafeStatic(directory=STATIC), name="static"),
    ]

    class RequestGuard(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await self.check(request, call_next)
            response.headers.update(SECURITY_HEADERS)
            return response

        async def check(self, request: Request, call_next):
            if not from_this_machine(request):
                return err("админский профиль открывается только с этого компьютера", 403)
            allowed = allowed_hosts(rt.cfg)
            host = request.headers.get("host")
            if hostname(host) not in allowed:
                return err(f"адрес {host} не разрешён: добавьте его в server.allowed_hosts", 403)
            if request.url.path.startswith("/api/"):
                if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                    return err("запрос с другого сайта отклонён", 403)
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if request.headers.get("x-modelseek") != "1":
                        return err("missing X-Modelseek header", 403)
                    origin = request.headers.get("origin")
                    if origin and not same_origin(origin, request):
                        return err(f"источник {origin[:80]} не разрешён", 403)
            return await call_next(request)

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(BodyLimit), Middleware(RequestGuard)])
    app.state.runtime = rt
    app.state.profile = "admin"
    return app


def __getattr__(name: str):
    """`uvicorn app.main:app` still works (admin profile only); importing the module builds nothing."""
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
