from copy import deepcopy

import httpx
import pytest
from starlette.testclient import TestClient

from app.config import DEFAULTS, Settings, _merge, allowed_hosts
from app.main import create_app

from .fixtures import router
from .test_api import CFG, H


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(deepcopy(CFG), transport=router(), db_path=":memory:", start_scheduler=False,
                               data_dir=str(tmp_path))) as c:
        yield c


@pytest.mark.parametrize("host", ["127.0.0.1:8088", "localhost:8088", "[::1]:8088", "LOCALHOST", "testserver"])
def test_local_hosts_are_allowed(client, host):
    assert client.get("/api/sources", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/api/settings", "/static/app.js"])
def test_foreign_host_is_rejected_everywhere(client, path):
    r = client.get(path, headers={"Host": "evil.example"})
    assert r.status_code == 403 and "allowed_hosts" in r.json()["error"]


def test_dns_rebinding_cannot_change_settings(client):
    r = client.patch("/api/settings", headers={"Host": "evil.example:8088", "Origin": "http://evil.example:8088", **H},
                     json={"patch": {"notify": {"kind": "ntfy", "url": "https://evil.example/x"}}})
    assert r.status_code == 403
    assert client.get("/api/settings").json()["config"]["notify"]["url"] == ""


def test_extra_host_comes_from_config(tmp_path):
    cfg = deepcopy(CFG)
    cfg["server"]["allowed_hosts"] = "testserver, modelseek.lan"
    with TestClient(create_app(cfg, transport=router(), db_path=":memory:", start_scheduler=False,
                               data_dir=str(tmp_path))) as c:
        assert c.get("/api/sources", headers={"Host": "modelseek.lan:8088"}).status_code == 200
        assert c.get("/api/sources", headers={"Host": "other.lan"}).status_code == 403
    assert allowed_hosts({}) == {"127.0.0.1", "localhost", "::1"}


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "not a url"])
def test_foreign_origin_cannot_change_anything(client, origin):
    assert client.post("/api/cache/clear", headers={"Origin": origin, **H}).status_code == 403


def test_same_origin_post_works(client):
    r = client.post("/api/cache/clear", headers={"Host": "127.0.0.1:8088", "Origin": "http://127.0.0.1:8088", **H})
    assert r.status_code == 200


def test_other_sites_cannot_trigger_api_with_get(client):
    q = {"q": "headphone"}
    assert client.get("/api/search", params=q, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/history").json()["items"] == []
    assert client.get("/api/search", params=q, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


@pytest.mark.parametrize("patch", [{"browser": {"executable_path": r"\\evil\share\x.exe"}},
                                   {"server": {"allowed_hosts": ["evil.example"]}},
                                   {"server": {"data_dir": "/tmp/x"}}])
def test_locked_settings_cannot_be_changed_by_api(client, patch):
    r = client.patch("/api/settings", headers=H, json={"patch": patch})
    assert r.status_code == 400 and "config.yaml" in r.json()["error"]


def test_saving_browser_section_without_path_still_works(client):
    r = client.patch("/api/settings", headers=H, json={"patch": {"browser": {"headless": False}}})
    assert r.status_code == 200 and r.json()["config"]["browser"]["headless"] is False


def test_section_must_be_an_object(client):
    assert client.patch("/api/settings", headers=H, json={"patch": {"browser": "x"}}).status_code == 400
    assert client.get("/api/settings").status_code == 200


def settings(tmp_path, **sources):
    return Settings(_merge(DEFAULTS, {"sources": sources}), tmp_path / "config.yaml", tmp_path / "data")


def test_system_env_var_cannot_be_sent_to_custom_address(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t")
    st = settings(tmp_path)
    for patch in ({"notify": {"kind": "ntfy", "url": "https://x.test/t", "token_env": "AWS_SECRET_ACCESS_KEY"}},
                  {"translate": {"url": "https://x.test/v1", "api_key_env": "AWS_SECRET_ACCESS_KEY"}}):
        with pytest.raises(ValueError, match="AWS_SECRET_ACCESS_KEY"):
            st.patch(patch)


def test_site_key_cannot_be_sent_to_custom_address(tmp_path, monkeypatch):
    monkeypatch.setenv("TV_TOKEN", "x")
    st = settings(tmp_path, thingiverse={"token_env": "TV_TOKEN"})
    with pytest.raises(ValueError, match="ключ сайта"):
        st.patch({"translate": {"url": "https://x.test/v1", "api_key_env": "TV_TOKEN"}})


def test_own_keys_keep_working(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_NEW_KEY", raising=False)
    monkeypatch.setenv("FROM_DOTENV", "x")
    monkeypatch.delenv("FROM_UI", raising=False)
    (tmp_path / ".env").write_text("# comment\nFROM_DOTENV=x\n", encoding="utf-8")
    st = settings(tmp_path)
    st.patch({"translate": {"url": "https://x.test/v1", "api_key_env": "MY_NEW_KEY"}})
    st.patch({"translate": {"api_key_env": "FROM_DOTENV"}})
    st.set_secrets({"FROM_UI": "y"})
    st.patch({"notify": {"kind": "ntfy", "url": "https://x.test/t", "token_env": "FROM_UI"}})
    monkeypatch.delenv("FROM_UI", raising=False)


def test_telegram_token_goes_only_to_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    st = settings(tmp_path)
    st.patch({"notify": {"kind": "telegram", "token_env": "TELEGRAM_BOT_TOKEN", "chat_id": "1"}})
    with pytest.raises(ValueError):
        st.patch({"notify": {"kind": "ntfy", "url": "https://x.test/t"}})


def test_existing_config_with_system_var_stays_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("CORP_LLM_KEY", "x")
    st = Settings(_merge(DEFAULTS, {"translate": {"api_key_env": "CORP_LLM_KEY"}}), tmp_path / "config.yaml", tmp_path / "data")
    st.patch({"translate": {"enabled": True}})


def test_links_must_be_web_addresses(client):
    r = client.patch("/api/settings", headers=H, json={"patch": {"links": {"x": {"label": "X", "url": "javascript:alert('{q}')"}}}})
    assert r.status_code == 400
