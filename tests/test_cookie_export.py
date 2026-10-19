from tools.export_taobao_cookie import output_path, upload_state
import io
import zipfile
import json
import subprocess
from pathlib import Path

import pytest
from backend import collector, store as s

from tests.test_system import client


def test_export_filters_unrelated_cookies_and_origins():
    state = upload_state(
        {
            "cookies": [
                {"name": "unb", "value": "x", "domain": ".taobao.com"},
                {"name": "other", "value": "y", "domain": ".example.com"},
            ],
            "origins": [
                {"origin": "https://www.taobao.com", "localStorage": []},
                {"origin": "https://example.com", "localStorage": []},
            ],
        }
    )
    assert [c["name"] for c in state["cookies"]] == ["unb"]
    assert [o["origin"] for o in state["origins"]] == ["https://www.taobao.com"]


def test_export_default_path_is_downloads_when_desktop_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.export_taobao_cookie.Path.home", lambda: tmp_path)
    path = output_path(None)
    assert path.parent == tmp_path / "Downloads"
    assert path.name.startswith("taobao-cookie-") and path.suffix == ".json"


@pytest.mark.parametrize("platform", ["extension", "windows", "mac"])
def test_download_is_complete_runtime_free_extension(client, platform):
    response = client.get(f"/api/session/export-script/{platform}")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["action"]["default_popup"] in names
        assert set(manifest["permissions"]) == {"cookies", "downloads", "scripting"}
        assert manifest["version"] == "1.2.0"
        assert manifest["host_permissions"] == ["https://*.taobao.com/*", "https://*.tmall.com/*"]
    assert names == {"manifest.json", "popup.html", "popup.css", "popup.js", "cookies.mjs", "使用说明.txt"}
    assert response.headers["content-type"] == "application/zip"
    assert "attachment;" in response.headers["content-disposition"]


def test_extension_output_upload_roundtrip(client):
    root = Path(__file__).resolve().parent.parent
    code = """
      import { normalizeCookies } from './tools/cookie-extension/cookies.mjs';
      const out = normalizeCookies([
        {name:'unb',value:'fixture',domain:'.taobao.com',path:'/',session:true,httpOnly:true,secure:true,sameSite:'no_restriction'},
        {name:'host',value:'fixture',domain:'login.taobao.com',path:'/',session:true,secure:true,sameSite:'strict'},
        {name:'unrelated',value:'fixture',domain:'evil-taobao.com',session:true}
      ]);
      process.stdout.write(JSON.stringify(out.state));
    """
    exported = subprocess.check_output(["node", "--input-type=module", "-e", code], cwd=root, text=True)
    response = client.post("/api/session/import", json={"content": exported})
    assert response.status_code == 200
    cookies = json.loads(collector.SESSION.read_text())["cookies"]
    assert len(cookies) == 2
    assert cookies[0]["httpOnly"] and cookies[0]["sameSite"] == "None"
    assert cookies[1]["domain"] == "login.taobao.com"
    assert cookies[1]["sameSite"] == "Strict"
    assert "fixture" not in response.text


def test_unknown_extension_platform_returns_404(client):
    assert client.get("/api/session/export-script/unknown").status_code == 404


def test_partitioned_cookies_and_storage_survive_upload(client):
    cookie = {"name": "auth", "value": "fixture", "domain": ".taobao.com", "path": "/", "httpOnly": True, "secure": True, "sameSite": "None", "expires": -1}
    incoming = {"cookies": [cookie,
        {**cookie, "partitionKey": "https://tmall.com", "_crHasCrossSiteAncestor": True},
        {**cookie, "partitionKey": "https://tmall.com", "_crHasCrossSiteAncestor": False}],
        "origins": [{"origin": "https://www.taobao.com", "localStorage": [{"name": "auth", "value": "fixture"}]}]}
    response = client.post("/api/session/import", json={"content": json.dumps(incoming)})
    assert response.status_code == 200
    state = json.loads(collector.SESSION.read_text())
    assert state == incoming
    assert "2 条分区记录" in response.json()["message"]
    assert "fixture" not in response.text


def test_import_logs_only_safe_export_coverage(client):
    incoming = {
        "cookies": [{"name": "unb", "value": "fixture-secret", "domain": ".taobao.com"}],
        "origins": [{"origin": "https://login.taobao.com", "localStorage": [{"name": "fixture-secret", "value": "fixture-secret"}]}],
        "export_info": {
            "tool": "guidajia-cookie-extension", "version": "1.2.0", "cookie_count": 2,
            "storage_read_failures": 1, "storage_frames_read": 3,
            "auth_markers": {"taobao.com": ["fixture-secret"]},
            "storage_origins": ["https://fixture-secret.example"], "error": "fixture-secret",
        },
    }
    response = client.post("/api/session/import", json={"content": json.dumps(incoming)})
    assert response.status_code == 200
    assert "v1.2.0" in response.json()["message"]
    assert "读取失败" in response.json()["message"]
    with s.connection() as db:
        row = db.execute("select level,message,details from logs where message like '已导入淘宝 Cookie%' order by created_at desc limit 1").fetchone()
    details = json.loads(row["details"])["session_import"]
    assert row["level"] == "warning"
    assert details["cookie_count_mismatch"] is True
    assert details["auth_markers"]["taobao.com"] == ["unb"]
    assert details["missing_auth_storage_origins"] == ["https://passport.taobao.com"]
    assert details["reported_storage_frames_read"] == 3
    assert "fixture-secret" not in row["details"] + row["message"] + response.text
    assert json.loads(collector.SESSION.read_text())["cookies"][0]["value"] == "fixture-secret"


def test_import_ignores_untrusted_metadata_values(client):
    state = {"cookies": [{"name": "test", "value": "fixture", "domain": ".taobao.com"}], "origins": []}
    coverage = collector.session_state.import_diagnostics(json.dumps({
        **state, "export_info": {"tool": "guidajia-cookie-extension", "version": "fixture-secret", "cookie_count": True,
            "storage_read_failures": "fixture-secret", "storage_frames_read": -4},
    }), state)
    assert coverage["source_version"] is None
    assert coverage["reported_storage_read_failures"] is None
    assert coverage["reported_storage_frames_read"] is None
    assert coverage["cookie_count_mismatch"] is False
    assert "fixture-secret" not in json.dumps(coverage)
