from pathlib import Path

from fastapi.testclient import TestClient

from browser_custom import api
from browser_custom.app import app
from browser_custom.config import ConfigStore


def test_batch_add_accounts(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    monkeypatch.setattr(api, "store", config_store)

    with TestClient(app) as client:
        response = client.post("/api/accounts/batch", json={"accounts": [
            {"name": "01", "userDataDir": str(tmp_path / "profiles" / "01")},
            {
                "name": "02",
                "userDataDir": str(tmp_path / "profiles" / "02"),
                "network": {
                    "proxy": {"server": "socks5://127.0.0.1:1080"},
                    "regionMode": "auto",
                    "strictProxy": True,
                },
            },
        ]})

    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert [account.name for account in config_store.accounts.accounts] == ["01", "02"]
    assert len({account.acc for account in config_store.accounts.accounts}) == 2


def test_batch_add_accounts_is_atomic_on_validation_error(tmp_path: Path, monkeypatch):
    config_store = ConfigStore(tmp_path / "config")
    monkeypatch.setattr(api, "store", config_store)

    with TestClient(app) as client:
        response = client.post("/api/accounts/batch", json={"accounts": [
            {"name": "valid", "userDataDir": str(tmp_path / "profiles" / "valid")},
            {"name": "invalid", "userDataDir": "relative/path"},
        ]})

    assert response.status_code == 400
    assert response.json()["detail"]["line"] == 2
    assert config_store.accounts.accounts == []
