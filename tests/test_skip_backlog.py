"""The dashboard's "Skip backlog" button: delete everything behind live."""

import os
import time

from app.streaming.backlog import skip_backlog


def _touch(d, name, age):
    (d / name).write_bytes(b"x")
    t = time.time() - age
    os.utime(d / name, (t, t))


def test_keeps_live_edge_and_in_progress(tmp_path):
    _touch(tmp_path, "s1_000000.ts", 3600)
    _touch(tmp_path, "s1_000001.ts", 3600)
    _touch(tmp_path, "s2_000000.ts", 300)   # in playlist
    _touch(tmp_path, "s2_000001.ts", 5)     # being written, not in playlist yet
    (tmp_path / "live.m3u8").write_text("#EXTM3U\n#EXTINF:6,\ns2_000000.ts\n")

    assert skip_backlog(tmp_path) == 2
    assert sorted(p.name for p in tmp_path.glob("*.ts")) == ["s2_000000.ts", "s2_000001.ts"]


def test_leaves_already_uploaded_segments_alone(tmp_path):
    import json
    _touch(tmp_path, "s1_000000.ts", 3600)  # uploaded
    _touch(tmp_path, "s1_000001.ts", 3600)  # never sent
    (tmp_path / "segments.json").write_text(json.dumps({"segments": {"s1_000000.ts": 1.0}}))
    assert skip_backlog(tmp_path) == 1
    assert (tmp_path / "s1_000000.ts").exists()


def test_no_playlist_still_keeps_fresh_files(tmp_path):
    _touch(tmp_path, "s1_000000.ts", 3600)
    _touch(tmp_path, "s1_000001.ts", 1)
    assert skip_backlog(tmp_path) == 1
    assert (tmp_path / "s1_000001.ts").exists()


def test_endpoint_requires_login(tmp_path, monkeypatch):
    from flask import Flask

    from app.web import api as api_mod

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(api_mod.api)
    monkeypatch.setattr(api_mod.manager, "load", lambda: {"hls": {"segment_dir": str(tmp_path)}})
    _touch(tmp_path, "s1_000000.ts", 3600)

    client = app.test_client()
    assert client.post("/api/uploader/skip-backlog").status_code == 401
    assert (tmp_path / "s1_000000.ts").exists()

    with client.session_transaction() as s:
        s["authenticated"] = True
    resp = client.post("/api/uploader/skip-backlog")
    assert resp.status_code == 200 and resp.get_json()["deleted"] == 1
