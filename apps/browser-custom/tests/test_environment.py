import os

from browser_custom import __main__ as main_module
from browser_custom.environment import load_project_env


def test_load_project_env_without_overriding_shell_value(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("BROWSER_CUSTOM_PORT=9001\nBROWSER_CUSTOM_CONFIG_DIR=/tmp/from-dotenv\n")
    monkeypatch.delenv("BROWSER_CUSTOM_PORT", raising=False)
    monkeypatch.setenv("BROWSER_CUSTOM_CONFIG_DIR", "/tmp/from-shell")

    assert load_project_env(path) is True
    assert os.environ["BROWSER_CUSTOM_PORT"] == "9001"
    assert os.environ["BROWSER_CUSTOM_CONFIG_DIR"] == "/tmp/from-shell"


def test_main_loads_environment_before_reading_port(monkeypatch):
    captured = {}

    def fake_load():
        monkeypatch.setenv("BROWSER_CUSTOM_PORT", "9002")

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.delenv("BROWSER_CUSTOM_PORT", raising=False)
    monkeypatch.setattr(main_module, "load_project_env", fake_load)
    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    main_module.main()

    assert captured["app"] == "browser_custom.app:app"
    assert captured["port"] == 9002
