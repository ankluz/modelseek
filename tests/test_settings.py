import json
import os
import time
from copy import deepcopy

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from app import installer
from app.config import Settings, validate, _merge, DEFAULTS
from app.main import create_app

from .fixtures import router as base_router
from .test_api import CFG, H

CUSTOM_API = {
    "result": {"total": 2, "list": [
        {"uid": "a1", "name": "Gear box", "slug": "gear-box", "pic": "/img/a1.png", "owner": {"nick": "zed"},
         "stats": {"hearts": 77, "dl": 900}, "created": "2025-05-05T00:00:00Z", "keywords": ["gear", "box"]},
        {"uid": "a2", "name": "Gear wheel", "slug": "gear-wheel", "pic": "https://cdn.x/a2.png",
         "owner": {"nick": "amy"}, "stats": {"hearts": 5, "dl": 10}},
    ]}}

CUSTOM_CONF = {
    "label": "Gears", "color": "#123456", "mode": "http",
    "url": "https://api.gears.test/find?term={q}&n={limit}&from={offset}",
    "search_page": "https://gears.test/s?q={q}",
    "items_path": "result.list",
    "url_prefix": "https://gears.test",
    "fields": {"id": "uid", "title": "name", "url": "https://gears.test/m/{uid}-{slug}", "thumbnail": "pic",
               "author": "owner.nick", "likes": "stats.hearts", "downloads": "stats.dl",
               "published": "created", "tags": "keywords"},
}


def router_with_custom(seen=None):
    inner = base_router()

    def handler(req):
        if req.url.host == "api.gears.test":
            if seen is not None:
                seen.append(str(req.url))
            return httpx.Response(200, json=CUSTOM_API)
        if req.url.host == "post.gears.test":
            body = json.loads(req.content)
            if seen is not None:
                seen.append(body)
            return httpx.Response(200, json=CUSTOM_API)
        return inner.handler(req)

    return httpx.MockTransport(handler)


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_TV_TOKEN", "t")
    monkeypatch.setenv("TEST_MMF_KEY", "k")
    seen: list = []
    app = create_app(deepcopy(CFG), transport=router_with_custom(seen), db_path=":memory:",
                     start_scheduler=False, data_dir=str(tmp_path))
    with TestClient(app) as c:
        c.seen = seen
        c.tmp = tmp_path
        yield c



def test_overrides_layer_and_null_deletes(tmp_path):
    base = _merge(DEFAULTS, {"links": {"yeggi": {"url": "https://y/{q}"}, "keep": {"url": "https://k/{q}"}}})
    st = Settings(base, None, tmp_path)
    eff = st.patch({"links": {"yeggi": None}, "server": {"per_source_limit": 10}})
    assert "yeggi" not in eff["links"] and "keep" in eff["links"]
    assert eff["server"]["per_source_limit"] == 10
    saved = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
    assert saved["links"]["yeggi"] is None
    st.reset("links")
    assert "yeggi" in st.effective()["links"]
    with pytest.raises(ValueError):
        st.patch({"server": {"per_source_limit": 1000}})
    with pytest.raises(ValueError):
        st.patch({"evil": {}})


def test_validate_rejects_bad_sources():
    good = _merge(DEFAULTS, {})
    validate(good)
    for bad in ({"links": {"x": {"url": "https://no-placeholder"}}},
                {"custom_sources": {"x": {"url": "ftp://x"}}},
                {"sources": {"Bad Name": {}}},
                {"sources": {"printables": {"token_env": "lower"}}},
                {"ranking": {"weights": {"relevance": -1}}}):
        with pytest.raises(ValueError):
            validate(_merge(DEFAULTS, bad))


def test_secrets_file_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MS_TEST_TOKEN", raising=False)
    st = Settings(_merge(DEFAULTS, {}), None, tmp_path)
    st.set_secrets({"MS_TEST_TOKEN": "abc"})
    assert os.environ["MS_TEST_TOKEN"] == "abc"
    assert "MS_TEST_TOKEN=abc" in (tmp_path / "secrets.env").read_text()
    monkeypatch.delenv("MS_TEST_TOKEN")
    Settings(_merge(DEFAULTS, {}), None, tmp_path)
    assert os.environ["MS_TEST_TOKEN"] == "abc"
    st.set_secrets({"MS_TEST_TOKEN": ""})
    assert "MS_TEST_TOKEN" not in os.environ
    with pytest.raises(ValueError):
        st.set_secrets({"bad name": "x"})
    with pytest.raises(ValueError):
        st.set_secrets({"OK_NAME": "a\nB=c"})



def test_settings_patch_applies_without_restart(app_client):
    c = app_client
    s = c.get("/api/settings").json()
    assert s["config"]["sources"]["printables"]["mode"] == "http"
    assert s["secrets"]["TEST_TV_TOKEN"] is True and s["secrets"]["TEST_CULTS_KEY"] is False
    assert any(b["name"] == "makerworld" and "browser" in b["allowed_modes"] for b in s["builtin"])

    r = c.patch("/api/settings", json={"patch": {"sources": {"makerworld": {"mode": "link"}}}}, headers=H)
    assert r.status_code == 200 and r.json()["config"]["sources"]["makerworld"]["mode"] == "link"
    st = {x["name"]: x for x in c.get("/api/search", params={"q": "lamp"}).json()["sources"]}
    assert st["makerworld"]["status"] == "link"

    bad = c.patch("/api/settings", json={"patch": {"server": {"request_timeout_seconds": 0}}}, headers=H)
    assert bad.status_code == 400 and "request_timeout_seconds" in bad.json()["error"]

    c.post("/api/settings/reset", json={"section": "sources"}, headers=H)
    assert c.get("/api/settings").json()["config"]["sources"]["makerworld"]["mode"] == "http"
    assert c.patch("/api/settings", json={"patch": {}}).status_code == 403


def test_secrets_api_enables_source(app_client, monkeypatch):
    c = app_client
    monkeypatch.delenv("TEST_CULTS_USER", raising=False)
    monkeypatch.delenv("TEST_CULTS_KEY", raising=False)
    st = {x["name"]: x for x in c.get("/api/search", params={"q": "skull"}).json()["sources"]}
    assert st["cults3d"]["status"] == "skipped"
    s = c.put("/api/secrets", json={"values": {"TEST_CULTS_USER": "me", "TEST_CULTS_KEY": "k"}}, headers=H).json()
    assert s["secrets"]["TEST_CULTS_KEY"] is True
    assert "k" not in json.dumps(s["secrets"])
    st = {x["name"]: x for x in c.get("/api/search", params={"q": "skull"}).json()["sources"]}
    assert st["cults3d"]["status"] == "ok"
    c.put("/api/secrets", json={"values": {"TEST_CULTS_USER": "", "TEST_CULTS_KEY": ""}}, headers=H)
    assert "TEST_CULTS_KEY" not in os.environ


def test_custom_source_test_endpoint_and_search(app_client):
    c = app_client
    r = c.post("/api/sources/test", json={"config": {**CUSTOM_CONF, "items_path": "", "fields": {}},
                                          "query": "gear"}, headers=H).json()
    assert r["ok"] is False and {"path": "result.list", "count": 2} in r["arrays"]
    r = c.post("/api/sources/test", json={"config": {**CUSTOM_CONF, "fields": {}}, "query": "gear"}, headers=H).json()
    assert r["ok"] and r["count"] == 2
    assert r["fields_available"]["owner.nick"] == "zed" and r["fields_guess"]["title"] == "name"
    r = c.post("/api/sources/test", json={"config": CUSTOM_CONF, "query": "gear"}, headers=H).json()
    a = r["sample"][0]
    assert a["url"] == "https://gears.test/m/a1-gear-box" and a["thumbnail"] == "https://gears.test/img/a1.png"
    assert a["author"] == "zed" and a["likes"] == 77 and a["tags"] == ["gear", "box"]
    assert "term=gear&n=10&from=0" in c.seen[-1]

    c.patch("/api/settings", json={"patch": {"custom_sources": {"gears": CUSTOM_CONF}}}, headers=H)
    srcs = {s["name"]: s for s in c.get("/api/sources").json()["sources"]}
    assert srcs["gears"]["custom"] and srcs["gears"]["label"] == "Gears"
    data = c.get("/api/search", params={"q": "gear"}).json()
    st = {x["name"]: x for x in data["sources"]}
    assert st["gears"]["status"] == "ok" and st["gears"]["count"] == 2
    assert any(h["source"] == "gears" for h in data["results"])

    c.patch("/api/settings", json={"patch": {"custom_sources": {"gears": None}}}, headers=H)
    assert "gears" not in {s["name"] for s in c.get("/api/sources").json()["sources"]}


def test_custom_source_post_body_template(app_client):
    conf = {**CUSTOM_CONF, "url": "https://post.gears.test/q", "method": "POST",
            "body": '{"search": {"text": "{q}", "size": "{limit}", "label": "q={q}"}}'}
    r = app_client.post("/api/sources/test", json={"config": conf, "query": "gear"}, headers=H).json()
    assert r["ok"]
    assert app_client.seen[-1] == {"search": {"text": "gear", "size": 10, "label": "q=gear"}}


def test_link_added_from_settings(app_client):
    c = app_client
    bad = c.patch("/api/settings", json={"patch": {"links": {"pins": {"label": "Pinshape", "url": "https://pinshape.com"}}}}, headers=H)
    assert bad.status_code == 400
    c.patch("/api/settings", json={"patch": {"links": {"pins": {"label": "Pinshape", "url": "https://pinshape.com/search?q={q}"}}}}, headers=H)
    st = {x["name"]: x for x in c.get("/api/search", params={"q": "vase"}).json()["sources"]}
    assert st["pins"]["status"] == "link" and st["pins"]["link"] == "https://pinshape.com/search?q=vase"



def test_extras_status_shape(app_client):
    d = app_client.get("/api/extras").json()
    keys = {i["key"] for i in d["items"]}
    assert keys == {"playwright", "curl_cffi", "fastembed", "pillow"}
    assert next(i for i in d["items"] if i["key"] == "pillow")["installed"] is True


def test_install_job_runs_whitelisted_only(app_client, monkeypatch):
    calls = []

    def fake_install(job, keys):
        calls.append(keys)
        job.write("Collecting curl_cffi")
        job.done(True, "ok")

    monkeypatch.setattr(installer, "install_extras", fake_install)
    assert app_client.post("/api/extras/install", json={"keys": ["rm -rf /"]}, headers=H).status_code == 400
    r = app_client.post("/api/extras/install", json={"keys": ["curl_cffi", "evil"]}, headers=H).json()
    jid = r["job"]["id"]
    for _ in range(50):
        j = app_client.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "ok" and calls == [["curl_cffi"]]
    assert j["log"][0] == "Collecting curl_cffi"
    assert app_client.get(f"/api/jobs/{jid}", params={"since": 1}).json()["log"] == ["ok"]


def test_ollama_status_when_absent(app_client, monkeypatch):
    monkeypatch.setattr(installer, "ollama_binary", lambda: None)
    c = app_client
    c.patch("/api/settings", json={"patch": {"translate": {"url": "http://127.0.0.1:9/v1"}}}, headers=H)
    d = c.get("/api/ollama").json()
    assert d["running"] is False and d["binary"] is None and d["base"] == "http://127.0.0.1:9"
    assert "command" in d["install"]
    assert c.post("/api/ollama/pull", json={"model": "bad name;rm"}, headers=H).status_code == 400


def test_ollama_pull_progress(monkeypatch):
    lines = [{"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 40},
             {"status": "downloading", "total": 100, "completed": 100}, {"status": "success"}]

    def handler(req):
        assert req.url.path == "/api/pull" and json.loads(req.content)["model"] == "qwen2.5:3b"
        return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())

    real = httpx.Client
    monkeypatch.setattr(installer.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    job = installer.Job("t", "t")
    installer.pull_model(job, "http://ollama.test/v1", "qwen2.5:3b")
    assert job.status == "ok" and job.progress == 1.0
    assert job.log[0].startswith("Скачиваю") and any("100%" in l for l in job.log)


def test_custom_source_without_id_keeps_models_apart():
    from app.sources import CustomSource
    src = CustomSource("x", {"url": "https://a.test/?q={q}", "fields": {"title": "name", "url": "link"}})
    a = src.parse({"name": "A", "link": "/m/1"}, 0)
    b = src.parse({"name": "B", "link": "/m/2"}, 1)
    assert a.key != b.key and a.url == "https://a.test/m/1"
    c = src.parse({"uid": "u1", "name": "C"}, 2)
    assert c.id == "u1"


def test_env_overrides_for_docker(monkeypatch, tmp_path):
    from app.config import env_overrides, load_config
    env = {"MODELSEEK__TRANSLATE__URL": "http://ollama:11434/v1", "MODELSEEK__SERVER__PER_SOURCE_LIMIT": "12",
           "MODELSEEK__SOURCES__SEARXNG__ENABLED": "true", "OTHER": "x", "MODELSEEK__EMPTY": ""}
    assert env_overrides(env) == {"translate": {"url": "http://ollama:11434/v1"}, "server": {"per_source_limit": 12},
                                  "sources": {"searxng": {"enabled": True}}}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(tmp_path / "none.yaml")
    assert cfg["translate"]["url"] == "http://ollama:11434/v1" and cfg["server"]["per_source_limit"] == 12
