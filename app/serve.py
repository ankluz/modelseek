"""Запуск modelseek: админский профиль и, если включён, внешний — в одном процессе на двух портах.

    python -m app.serve

Админский профиль слушает только 127.0.0.1 (порт MODELSEEK_PORT, по умолчанию 8088).
Внешний профиль включается в config.yaml, раздел public.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import signal
import sys

from collections import Counter

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .config import Settings, load_config, validate_public
from . import dockernet
from .dockernet import IN_DOCKER
from .main import create_app
from .public import inside, networks, parse_ip
from .runtime import Runtime

log = logging.getLogger("modelseek.serve")


class Server(uvicorn.Server):
    """Signals are handled once for both servers in serve()."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def admin_address() -> tuple[str, int]:
    port = int(os.environ.get("MODELSEEK_PORT") or 8088)
    return ("0.0.0.0" if IN_DOCKER else "127.0.0.1"), port


def public_address(pub: dict) -> tuple[str, int]:
    host = os.environ.get("MODELSEEK_PUBLIC_HOST") or str(pub.get("host") or "0.0.0.0")
    port = int(os.environ.get("MODELSEEK_PUBLIC_PORT") or pub.get("port") or 8090)
    return host, port


class GuestH11(H11Protocol):
    """HTTP for the public port: a request must arrive whole in REQUEST_TIMEOUT seconds (no slowloris),
    and one address keeps at most PER_ADDRESS connections. Exempt from the cap: a configured reverse proxy,
    and Docker's gateway, behind which all guests share one address (uvicorn's overall limit still holds)."""

    REQUEST_TIMEOUT = 10.0
    PER_ADDRESS = 24
    proxies: list = []
    open_by_ip: Counter = Counter()

    def connection_made(self, transport):
        super().connection_made(transport)
        self._deadline = None
        self._ip = (self.client or ("?",))[0]
        exempt = inside(parse_ip(self._ip), self.proxies)
        self._counted = not exempt
        if self._counted:
            GuestH11.open_by_ip[self._ip] += 1
            if GuestH11.open_by_ip[self._ip] > self.PER_ADDRESS:
                transport.close()
                return
        self._arm()

    def _arm(self):
        self._disarm()
        self._deadline = self.loop.call_later(self.REQUEST_TIMEOUT, self._too_slow)

    def _disarm(self):
        if getattr(self, "_deadline", None) is not None:
            self._deadline.cancel()
            self._deadline = None

    def _too_slow(self):
        self._deadline = None
        if self.cycle is None or self.cycle.response_complete:
            self.transport.close()

    def handle_events(self):
        super().handle_events()
        if self.cycle is not None and not self.cycle.response_complete:
            self._disarm()

    def on_response_complete(self):
        super().on_response_complete()
        if not self.transport.is_closing():
            self._arm()

    def connection_lost(self, exc):
        self._disarm()
        if getattr(self, "_counted", False):
            self._counted = False
            GuestH11.open_by_ip[self._ip] -= 1
            if GuestH11.open_by_ip[self._ip] <= 0:
                del GuestH11.open_by_ip[self._ip]
        super().connection_lost(exc)


def server(app, host: str, port: int, connections: int = 100, guests: bool = False) -> Server:
    """connections: open connections at most; beyond that uvicorn answers 503 instead of piling them up."""
    extra = {"http": GuestH11} if guests else {}
    return Server(uvicorn.Config(app, host=host, port=port, lifespan="off", access_log=False,
                                 proxy_headers=False, server_header=False, date_header=False, log_level="info",
                                 limit_concurrency=connections, timeout_keep_alive=5,
                                 h11_max_incomplete_event_size=16 * 1024, **extra))


async def serve() -> None:
    cfg = load_config()
    pub = cfg.get("public") or {}
    validate_public(pub)
    rt = Runtime(Settings(None, None, cfg["server"]["data_dir"]))
    await rt.start()
    try:
        ahost, aport = admin_address()
        servers = [server(create_app(runtime=rt), ahost, aport)]
        where = f"порт {aport} в контейнере" if IN_DOCKER else f"http://127.0.0.1:{aport}"
        print(f"[modelseek] админский профиль: {where}  (только с этого компьютера)", flush=True)
        if pub.get("enabled"):
            from .public import create_public_app

            phost, pport = public_address(pub)
            if pport == aport:
                raise SystemExit(f"public.port {pport} совпадает с портом админского профиля — выберите другой")
            GuestH11.proxies = networks(pub.get("trusted_proxies")) + [
                ipaddress.ip_network(g) for g in dockernet.gateways()]
            servers.append(server(create_public_app(rt), phost, pport, connections=200, guests=True))
            rt.public_running = True
            where = f"порт {pport} в контейнере" if IN_DOCKER else f"{phost}:{pport}"
            print(f"[modelseek] внешний профиль: {where}  (вход: {pub.get('auth', 'none')})", flush=True)
            if IN_DOCKER:
                print("[modelseek] Docker: чтобы пустить другие устройства, задайте MODELSEEK_PUBLIC_BIND=0.0.0.0 в .env "
                      "и перезапустите контейнер", flush=True)

        def stop(*_):
            for s in servers:
                s.should_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, stop)
        tasks = [asyncio.ensure_future(s.serve()) for s in servers]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        failed = [s for s in servers if not s.started]
        if failed:
            raise SystemExit("не удалось открыть порт — возможно, он занят другой программой")
    finally:
        await rt.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve())
    except ValueError as e:
        print(f"[modelseek] ошибка в config.yaml: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
