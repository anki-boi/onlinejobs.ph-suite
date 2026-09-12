"""
tests/test_config.py — W1.4: app/config.py owns the live config.
A changed config.local.json takes effect via POST /api/config/reload with
no restart (LLM tailoring is the motivating case).
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_live():
    """app.config caches its live state per process — reset per test."""
    from app import config as appconfig
    appconfig._live = None
    yield
    appconfig._live = None


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point the config files at a temp dir (config.json + local overlay)."""
    from db import connection as dbconn
    import json
    (tmp_path / "config.json").write_text(json.dumps({"base_url": "https://www.onlinejobs.ph"}))
    (tmp_path / "config.local.json").write_text(json.dumps({}))
    monkeypatch.setattr(dbconn, "BASE_DIR", tmp_path)
    return tmp_path


class TestConfigModule:
    def test_local_overlay_merges_over_defaults(self, cfg_home):
        from app import config as appconfig
        import json
        (cfg_home / "config.local.json").write_text(json.dumps({"llm_base_url": "http://llm"}))
        assert appconfig.get()["llm_base_url"] == "http://llm"
        assert appconfig.get()["base_url"] == "https://www.onlinejobs.ph"

    def test_reload_picks_up_file_changes(self, cfg_home):
        from app import config as appconfig
        import json
        assert appconfig.get().get("llm_model") is None
        (cfg_home / "config.local.json").write_text(json.dumps({"llm_model": "gpt-x"}))
        assert appconfig.get().get("llm_model") is None  # cached until reload
        assert appconfig.reload()["llm_model"] == "gpt-x"
        assert appconfig.get()["llm_model"] == "gpt-x"

    def test_redacted_masks_secrets(self, cfg_home):
        from app import config as appconfig
        import json
        (cfg_home / "config.local.json").write_text(
            json.dumps({"llm_api_key": "sk-secret", "oj_cookies": "c=1"})
        )
        r = appconfig.redacted()
        assert r["llm_api_key"] == "***"
        assert r["oj_cookies"] == "***"
        assert "sk-secret" not in str(r)

    def test_features_off_lists_llm_and_clears_when_set(self, cfg_home):
        from app import config as appconfig
        import json
        assert any("LLM tailoring is off" in m for m in appconfig.features_off())
        (cfg_home / "config.local.json").write_text(json.dumps({"llm_base_url": "http://llm"}))
        appconfig.reload()
        assert appconfig.features_off() == []

    def test_missing_files_degrade_to_empty(self, cfg_home):
        from app import config as appconfig
        (cfg_home / "config.json").unlink()
        (cfg_home / "config.local.json").unlink()
        assert appconfig.reload() == {}


class TestConfigApi:
    def test_get_config_redacted_and_features_off(self, client, cfg_home):
        r = client.get("/api/config")
        assert r.status_code == 200
        body = r.json()
        assert body["config"]["base_url"] == "https://www.onlinejobs.ph"
        assert any("LLM tailoring is off" in m for m in body["features_off"])

    def test_reload_endpoint_makes_new_config_live(self, client, cfg_home):
        import json
        # acceptance: write cfg → reload → new values live without a restart.
        # GET first so the live state holds the old (empty) overlay;
        # only then does the file change show as `changed`.
        client.get("/api/config")
        (cfg_home / "config.local.json").write_text(
            json.dumps({"llm_base_url": "http://llm", "llm_model": "m1", "llm_api_key": "k"})
        )
        r = client.post("/api/config/reload")
        assert r.status_code == 200
        assert r.json()["changed"] is True
        assert r.json()["features_off"] == []
        body = client.get("/api/config").json()
        assert body["config"]["llm_model"] == "m1"
        assert body["config"]["llm_api_key"] == "***"

    def test_reload_with_no_changes_reports_changed_false(self, client, cfg_home):
        r = client.post("/api/config/reload")
        assert r.json()["changed"] is False

    def test_live_config_reaches_callers_without_restart(self, client, cfg_home):
        """server's _cfg is a live view: after reload, callers see the new
        LLM config in the same process (the tailor endpoint no longer
        reports 'No LLM configured')."""
        from app import server as srv
        assert srv._cfg.get("llm_base_url") is None
        import json
        (cfg_home / "config.local.json").write_text(json.dumps({"llm_base_url": "http://llm"}))
        client.post("/api/config/reload")
        assert srv._cfg.get("llm_base_url") == "http://llm"


class TestSchedulerDelegation:
    def test_dbconn_load_config_delegates_to_live_state(self, cfg_home):
        from db import connection as dbconn
        import json
        (cfg_home / "config.local.json").write_text(json.dumps({"llm_model": "m9"}))
        from app import config as appconfig
        appconfig.reload()
        assert dbconn.load_config()["llm_model"] == "m9"
