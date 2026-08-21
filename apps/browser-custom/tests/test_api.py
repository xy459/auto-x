from pathlib import Path

from fastapi.testclient import TestClient

from browser_custom import api
from browser_custom.app import app
from browser_custom.config import ConfigStore


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, reference):
        return self.values.get(reference)

    def set(self, reference, value):
        self.values[reference] = value

    def delete(self, reference):
        self.values.pop(reference, None)


class FakeRegistry:
    def __init__(self):
        self.running = set()

    def is_running(self, acc):
        return acc in self.running

    def status(self, accounts):
        return [{
            "acc": account.acc, "name": account.display_name,
            "status": "running" if account.acc in self.running else "stopped",
            "browserRunning": account.acc in self.running, "userDataDir": account.userDataDir,
            "proxy": account.proxy, "fingerprint": None,
            "mainPids": [], "processCount": 0,
        } for account in accounts]

    async def ensure_started(self, account, _config):
        self.running.add(account.acc)

    async def open(self, account, _config):
        activated = account.acc in self.running
        self.running.add(account.acc)
        return {"running": True, "activated": activated, "windowActivated": True}

    async def close(self, account):
        was_running = account.acc in self.running
        self.running.discard(account.acc)
        return {"closed": was_running, "closeError": None, "killed": []}

    async def restart(self, account, _config):
        self.running.add(account.acc)

    async def close_all(self, _accounts):
        self.running.clear()


def test_account_crud_and_browser_actions(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    registry = FakeRegistry()
    monkeypatch.setattr(api, "store", config_store)
    monkeypatch.setattr(api, "session_registry", registry)

    with TestClient(app) as client:
        created = client.post("/api/accounts", json={
            "name": "A", "userDataDir": str(tmp_path / "profiles" / "a"),
            "proxy": "socks5://127.0.0.1:1080",
        })
        assert created.status_code == 200
        acc = created.json()["account"]["acc"]

        opened = client.post(f"/api/browser/{acc}/open")
        assert opened.status_code == 200
        assert opened.json()["activated"] is False
        assert opened.json()["windowActivated"] is True
        reopened = client.post(f"/api/browser/{acc}/open")
        assert reopened.status_code == 200
        assert reopened.json()["activated"] is True
        assert reopened.json()["windowActivated"] is True
        assert client.get("/api/accounts").json()["accounts"][0]["runtime"]["status"] == "running"

        blocked = client.put(f"/api/accounts/{acc}", json={
            "name": "B", "userDataDir": str(tmp_path / "profiles" / "a")
        })
        assert blocked.status_code == 409

        assert client.post(f"/api/browser/{acc}/close").status_code == 200
        updated = client.put(f"/api/accounts/{acc}", json={
            "name": "B", "userDataDir": str(tmp_path / "profiles" / "a")
        })
        assert updated.status_code == 200
        assert updated.json()["account"]["name"] == "B"

        assert client.delete(f"/api/accounts/{acc}").status_code == 200
        assert client.get("/api/accounts").json()["accounts"] == []


def test_running_account_can_save_network_check_only(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    registry = FakeRegistry()
    monkeypatch.setattr(api, "store", config_store)
    monkeypatch.setattr(api, "session_registry", registry)

    with TestClient(app) as client:
        created = client.post("/api/accounts", json={
            "name": "A", "userDataDir": str(tmp_path / "profiles" / "a"),
        }).json()["account"]
        acc = created["acc"]
        assert client.post(f"/api/browser/{acc}/open").status_code == 200

        payload = {
            key: created[key]
            for key in (
                "name", "userDataDir", "browserPath", "geolocation", "fpPlatform",
                "platformVersion", "brandVersion", "releaseChannel", "browserVersion",
                "cloakArgs", "humanPreset", "humanize", "headless",
            )
        }
        payload["network"] = {
            **created["network"],
            "lastCheck": {
                "exitIp": "203.0.113.8",
                "detectedTimezone": "Asia/Tokyo",
                "detectedLocale": "ja-JP",
                "appliedTimezone": "Asia/Tokyo",
                "appliedLocale": "ja-JP",
                "timezoneSource": "auto",
                "localeSource": "auto",
                "webrtcIp": "203.0.113.8",
                "checkedAt": "2026-08-20T15:00:00Z",
                "proxySignature": api.cloak.proxy_signature(None),
                "latencyMs": 42,
                "stale": False,
            },
        }

        saved = client.put(f"/api/accounts/{acc}", json=payload)
        assert saved.status_code == 200
        assert registry.is_running(acc)
        assert saved.json()["account"]["network"]["lastCheck"]["exitIp"] == "203.0.113.8"

        payload["name"] = "B"
        blocked = client.put(f"/api/accounts/{acc}", json=payload)
        assert blocked.status_code == 409
        assert "修改其他配置前请先关闭浏览器" in blocked.json()["detail"]


def test_health_and_static_console(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api, "store", ConfigStore(tmp_path / "config"))
    monkeypatch.setattr(api, "session_registry", FakeRegistry())
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["architecture"] == "cloakbrowser-playwright-persistent"
        console = client.get("/")
        assert console.status_code == 200
        assert "Browser Custom" in console.text
        options = client.get("/api/cloak/options").json()
        assert {item["value"] for item in options["platforms"]} == {"auto", "windows", "macos"}
        assert {item["value"] for item in options["releaseChannels"]} == {"stable", "preview"}
        assert options["hostPlatformLabel"]
        assert any(item["value"] == "Asia/Shanghai" for item in options["timezones"])
        assert any(item["value"] == "en-US" for item in options["locales"])
        assert "批量打开" in console.text
        assert "selectAll" in console.text
        assert "statusRefresh" in console.text
        assert "globalExtensionPaths" in console.text
        assert "批量重启" in console.text
        assert "自动生成一致身份" in console.text
        assert "高级指纹覆盖" in console.text
        assert "指纹信息" in console.text

        extension = tmp_path / "extension"
        updated = client.put("/api/settings", json={
            "cloakBrowserPath": "",
            "cloakUserDataBase": str(tmp_path / "profiles"),
            "extensionPaths": [str(extension)],
        })
        assert updated.status_code == 200
        assert client.get("/api/settings").json()["extensionPaths"] == [str(extension)]
        created = client.post("/api/accounts", json={
            "name": "extension-account",
            "userDataDir": str(tmp_path / "profiles" / "extension-account"),
        })
        assert created.status_code == 200
        assert client.get("/api/settings").json()["extensionPaths"] == [str(extension)]


def test_browser_info_uses_official_channel_and_version(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api, "store", ConfigStore(tmp_path / "config"))
    monkeypatch.setattr(api, "session_registry", FakeRegistry())

    def fake_info(channel, version):
        assert channel == "preview"
        assert version == "145.0.7632.109.2"
        return {
            "version": version, "tier": "pro", "platform": "darwin-arm64",
            "binaryPath": "/tmp/cloak", "installed": True,
            "releaseChannel": channel, "requestedVersion": version,
        }

    monkeypatch.setattr(api.cloak, "browser_binary_info", fake_info)
    with TestClient(app) as client:
        response = client.get(
            "/api/cloak/browser-info",
            params={"releaseChannel": "preview", "browserVersion": "145.0.7632.109.2"},
        )
        assert response.status_code == 200
        assert response.json()["version"] == "145.0.7632.109.2"
        invalid = client.get("/api/cloak/browser-info", params={"browserVersion": "145.0"})
        assert invalid.status_code == 400


def test_account_list_includes_fingerprint_summary(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    monkeypatch.setattr(api, "store", config_store)
    monkeypatch.setattr(api, "session_registry", FakeRegistry())
    monkeypatch.setattr(api.cloak, "browser_binary_info", lambda *_args: {
        "version": "145.0.7632.109.2",
    })

    with TestClient(app) as client:
        created = client.post("/api/accounts", json={
            "name": "fingerprint",
            "userDataDir": str(tmp_path / "profiles" / "fingerprint"),
            "network": {"lastCheck": {
                "exitIp": "203.0.113.8",
                "webrtcIp": "203.0.113.8",
                "appliedTimezone": "Asia/Tokyo",
                "appliedLocale": "ja-JP",
                "checkedAt": "2026-08-19T10:00:00Z",
            }},
        })
        assert created.status_code == 200

        account = client.get("/api/accounts").json()["accounts"][0]

    fingerprint = account["fingerprint"]
    assert fingerprint["source"] == "lastCheck"
    assert fingerprint["webrtcIp"] == "203.0.113.8"
    assert fingerprint["region"] == "日本（东京）"
    assert fingerprint["locale"] == "ja-JP"
    assert fingerprint["timezone"] == "Asia/Tokyo"
    assert fingerprint["browserVersion"] == "145.0.7632.109.2"
    assert "Chrome/145.0.0.0" in fingerprint["userAgent"]
    assert fingerprint["userAgentSource"] == "projected"


def test_batch_open_and_close(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    registry = FakeRegistry()
    monkeypatch.setattr(api, "store", config_store)
    monkeypatch.setattr(api, "session_registry", registry)

    with TestClient(app) as client:
        accounts = []
        for name in ("A", "B"):
            created = client.post("/api/accounts", json={
                "name": name,
                "userDataDir": str(tmp_path / "profiles" / name.lower()),
            })
            assert created.status_code == 200
            accounts.append(created.json()["account"]["acc"])

        opened = client.post("/api/browser/batch", json={
            "action": "open", "accounts": [*accounts, accounts[0], "missing"],
        })
        assert opened.status_code == 200
        assert opened.json()["ok"] is False
        assert len(opened.json()["results"]) == 3
        assert registry.running == set(accounts)

        restarted = client.post("/api/browser/batch", json={
            "action": "restart", "accounts": accounts,
        })
        assert restarted.status_code == 200
        assert restarted.json()["ok"] is True

        closed = client.post("/api/browser/batch", json={
            "action": "close", "accounts": accounts,
        })
        assert closed.status_code == 200
        assert closed.json()["ok"] is True
        assert registry.running == set()


def test_proxy_password_is_write_only_and_preserved_on_edit(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    config_store.secret_store = MemorySecrets()
    monkeypatch.setattr(api, "store", config_store)
    monkeypatch.setattr(api, "session_registry", FakeRegistry())

    with TestClient(app) as client:
        created = client.post("/api/accounts", json={
            "name": "secure", "userDataDir": str(tmp_path / "profiles" / "secure"),
            "network": {"proxy": {
                "server": "socks5://127.0.0.1:1080", "username": "user", "password": "secret",
            }},
        })
        assert created.status_code == 200
        account = created.json()["account"]
        acc = account["acc"]
        assert "password" not in account["network"]["proxy"]
        assert account["network"]["proxy"]["hasPassword"] is True
        assert "secret" not in config_store.path.read_text()

        updated = client.put(f"/api/accounts/{acc}", json={
            "name": "secure-2", "userDataDir": str(tmp_path / "profiles" / "secure"),
            "network": {"proxy": {
                "server": "socks5://127.0.0.1:1080", "username": "user",
            }},
        })
        assert updated.status_code == 200
        assert config_store.accounts.get(acc).network.proxy.password == "secret"


def test_network_probe_uses_combined_identity_result(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    monkeypatch.setattr(api, "store", config_store)
    monkeypatch.setattr(api, "session_registry", FakeRegistry())

    async def fake_probe(proxy, timezone_override, locale_override):
        assert proxy.server == "socks5://127.0.0.1:1080"
        assert timezone_override == "Asia/Tokyo"
        assert locale_override is None
        return {
            "ok": True, "exitIp": "203.0.113.8", "detectedTimezone": "Asia/Shanghai",
            "detectedLocale": "zh-CN", "appliedTimezone": "Asia/Tokyo",
            "appliedLocale": "zh-CN", "timezoneSource": "custom", "localeSource": "auto",
            "webrtcIp": "203.0.113.8", "latencyMs": 12, "proxySignature": "sig",
        }

    monkeypatch.setattr(api.cloak, "probe_network_identity", fake_probe)
    with TestClient(app) as client:
        response = client.post("/api/cloak/network-test", json={
            "network": {
                "proxy": {"server": "socks5://127.0.0.1:1080"},
                "timezoneOverride": "Asia/Tokyo",
            },
        })
        assert response.status_code == 200
        assert response.json()["exitIp"] == "203.0.113.8"
        assert response.json()["appliedTimezone"] == "Asia/Tokyo"
        assert response.json()["checkedAt"]


def test_network_probe_invalid_proxy_returns_json_safe_400(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api, "store", ConfigStore(tmp_path / "config"))
    monkeypatch.setattr(api, "session_registry", FakeRegistry())

    with TestClient(app) as client:
        response = client.post("/api/cloak/network-test", json={
            "network": {"proxy": {"server": "", "username": None}},
        })

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["server"]
    assert "代理格式" in detail[0]["msg"]
    assert "ctx" not in detail[0]
