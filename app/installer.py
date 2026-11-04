from __future__ import annotations

import importlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

EXTRAS: dict[str, tuple[str, str, str]] = {
    "playwright": ("playwright", "playwright>=1.45", "режим browser: настоящий Chromium"),
    "curl_cffi": ("curl_cffi", "curl_cffi>=0.7", "режим impersonate: отпечаток Chrome"),
    "fastembed": ("fastembed", "fastembed>=0.3", "смысловой поиск (эмбеддинги на CPU)"),
    "pillow": ("PIL", "Pillow>=10", "склейка дублей по картинке"),
}
MODEL_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,79}$")
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IN_DOCKER = Path("/.dockerenv").exists() or os.environ.get("MODELSEEK_IN_DOCKER") == "1"


def _ensure_user_site():
    import site
    if os.environ.get("PIP_USER") in ("1", "true", "yes"):
        user_site = site.getusersitepackages()
        if user_site not in sys.path:
            Path(user_site).mkdir(parents=True, exist_ok=True)
            site.addsitedir(user_site)


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.title = kind, title
        self.status = "running"
        self.log: list[str] = []
        self.progress: float | None = None
        self.started = time.time()
        self.ended: float | None = None
        self._lock = threading.Lock()

    def write(self, line: str):
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            if self.log and line.split(" ")[0] == self.log[-1].split(" ")[0] and "%" in line and "%" in self.log[-1]:
                self.log[-1] = line
            else:
                self.log.append(line[:400])
            if len(self.log) > 600:
                del self.log[:100]

    def done(self, ok: bool, msg: str | None = None):
        if msg:
            self.write(msg)
        self.status = "ok" if ok else "error"
        self.ended = time.time()

    def to_dict(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                    "progress": self.progress, "log": self.log[since:], "log_total": len(self.log),
                    "started": self.started, "ended": self.ended}


class Jobs:
    def __init__(self):
        self.jobs: dict[str, Job] = {}

    def running(self, kind: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.kind == kind and j.status == "running"), None)

    def start(self, kind: str, title: str, fn: Callable[[Job], None], on_done: Callable[[Job], None] | None = None) -> Job:
        existing = self.running(kind)
        if existing:
            return existing
        job = Job(kind, title)
        self.jobs[job.id] = job
        if len(self.jobs) > 30:
            for old in sorted(self.jobs.values(), key=lambda j: j.started)[:10]:
                if old.status != "running":
                    self.jobs.pop(old.id, None)

        def run():
            try:
                fn(job)
                if job.status == "running":
                    job.done(True)
            except Exception as e:
                job.done(False, f"Ошибка: {type(e).__name__}: {e}")
            finally:
                if on_done:
                    try:
                        on_done(job)
                    except Exception:
                        pass

        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return job

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)


def run_cmd(job: Job, cmd: list[str], env: dict | None = None, timeout: float = 1800) -> int:
    job.write("$ " + " ".join(cmd))
    flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env={**os.environ, **(env or {})},
                            creationflags=flags)
    t0 = time.time()
    assert proc.stdout is not None
    for line in proc.stdout:
        for part in line.replace("\r", "\n").split("\n"):
            job.write(part)
        if time.time() - t0 > timeout:
            proc.kill()
            job.write("Превышено время ожидания")
            break
    return proc.wait()


def _installed(module: str) -> bool:
    _ensure_user_site()
    importlib.invalidate_caches()
    return importlib.util.find_spec(module) is not None


def playwright_browsers_dir() -> Path:
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        return Path(custom)
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ms-playwright"
    if IS_MAC:
        return Path.home() / "Library/Caches/ms-playwright"
    return Path.home() / ".cache/ms-playwright"


def chromium_installed() -> bool:
    d = playwright_browsers_dir()
    return d.exists() and any(p.name.startswith(("chromium-", "chromium_headless_shell-")) for p in d.iterdir())


def extras_status() -> dict[str, Any]:
    items = []
    for key, (module, spec, why) in EXTRAS.items():
        items.append({"key": key, "package": spec.split(">=")[0], "why": why, "installed": _installed(module)})
    pw = next(i for i in items if i["key"] == "playwright")
    pw["chromium"] = chromium_installed() if pw["installed"] else False
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return {"items": items, "python": sys.version.split()[0], "executable": sys.executable, "venv": in_venv,
            "docker": IN_DOCKER}


def install_extras(job: Job, keys: list[str]):
    specs = [EXTRAS[k][1] for k in keys if k in EXTRAS]
    if not specs:
        job.done(False, "Нечего устанавливать")
        return
    code = run_cmd(job, [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                         "--progress-bar", "off", *specs])
    if code != 0:
        job.done(False, f"pip завершился с кодом {code}. Если нет прав на запись — запустите install-extras.bat.")
        return
    _ensure_user_site()
    importlib.invalidate_caches()
    if "playwright" in keys:
        job.write("Скачиваю Chromium для режима browser (~150 МБ)…")
        code = run_cmd(job, [sys.executable, "-m", "playwright", "install", "chromium"])
        if code != 0:
            job.done(False, f"Не удалось скачать Chromium (код {code})")
            return
    job.done(True, "Готово. Новые возможности включаются в настройках без перезапуска.")


def install_chromium(job: Job):
    code = run_cmd(job, [sys.executable, "-m", "playwright", "install", "chromium"])
    job.done(code == 0, "Chromium установлен" if code == 0 else f"Ошибка (код {code})")


def ollama_base(url: str) -> str:
    u = (url or "http://127.0.0.1:11434").rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def ollama_binary() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    if IS_WIN:
        p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        if p.exists():
            return str(p)
    if IS_MAC and Path("/Applications/Ollama.app").exists():
        return "/Applications/Ollama.app/Contents/Resources/ollama"
    return None


def install_method() -> dict[str, Any]:
    if IN_DOCKER:
        return {"auto": False, "how": "docker", "command": "docker compose --profile ollama up -d"}
    if IS_WIN and shutil.which("winget"):
        return {"auto": True, "how": "winget", "command": "winget install -e --id Ollama.Ollama"}
    if IS_MAC and shutil.which("brew"):
        return {"auto": True, "how": "brew", "command": "brew install ollama"}
    if IS_WIN:
        return {"auto": False, "how": "manual", "command": "https://ollama.com/download/windows"}
    if IS_MAC:
        return {"auto": False, "how": "manual", "command": "https://ollama.com/download/mac"}
    return {"auto": False, "how": "terminal", "command": "curl -fsSL https://ollama.com/install.sh | sh"}


def ollama_status(url: str) -> dict[str, Any]:
    base = ollama_base(url)
    out: dict[str, Any] = {"base": base, "binary": ollama_binary(), "running": False, "version": None,
                           "models": [], "install": install_method(), "os": platform.system()}
    try:
        with httpx.Client(timeout=2) as c:
            r = c.get(f"{base}/api/version")
            if r.status_code == 200:
                out["running"] = True
                out["version"] = r.json().get("version")
                tags = c.get(f"{base}/api/tags").json().get("models", [])
                out["models"] = [{"name": m.get("name"), "size": m.get("size")} for m in tags]
    except (httpx.HTTPError, ValueError):
        pass
    return out


def install_ollama(job: Job):
    m = install_method()
    if not m["auto"]:
        job.done(False, f"Автоматическая установка недоступна. Установите вручную: {m['command']}")
        return
    if m["how"] == "winget":
        cmd = ["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
               "--accept-source-agreements", "--accept-package-agreements"]
    else:
        cmd = ["brew", "install", "ollama"]
    code = run_cmd(job, cmd)
    if code != 0 and not ollama_binary():
        job.done(False, f"Установка завершилась с кодом {code}")
        return
    job.done(True, "Ollama установлена. Нажмите «Запустить», если она не запустилась сама.")


def start_ollama(job: Job, url: str):
    binary = ollama_binary()
    if not binary:
        job.done(False, "Ollama не найдена — сначала установите её")
        return
    if ollama_status(url)["running"]:
        job.done(True, "Ollama уже запущена")
        return
    job.write(f"$ {binary} serve")
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([binary, "serve"], **kwargs)
    for _ in range(30):
        time.sleep(1)
        if ollama_status(url)["running"]:
            job.done(True, "Ollama запущена")
            return
    job.done(False, "Ollama не ответила за 30 секунд")


def pull_model(job: Job, url: str, model: str):
    if not MODEL_NAME.match(model or ""):
        job.done(False, "Некорректное имя модели")
        return
    base = ollama_base(url)
    job.write(f"Скачиваю модель {model}…")
    last = ""
    with httpx.Client(timeout=httpx.Timeout(30, read=None)) as c:
        with c.stream("POST", f"{base}/api/pull", json={"model": model, "stream": True}) as r:
            if r.status_code != 200:
                job.done(False, f"Ollama ответила HTTP {r.status_code}: {r.read()[:200]!r}")
                return
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    job.done(False, "Ошибка: " + str(ev["error"]))
                    return
                status = ev.get("status", "")
                total, done = ev.get("total"), ev.get("completed")
                if total and done is not None:
                    job.progress = round(done / total, 3)
                    job.write(f"{status} {job.progress * 100:.0f}%")
                elif status != last:
                    job.write(status)
                last = status
    job.progress = 1.0
    job.done(True, f"Модель {model} готова")


def warm_embeddings(job: Job, embedder) -> None:
    job.write(f"Загружаю {embedder.model_name} (≈220 МБ при первом запуске)…")
    embedder._load()
    embedder._embed(["test"])
    job.done(True, "Модель смыслового поиска готова")
