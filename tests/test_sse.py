"""
tests/test_sse.py — W2.2: every streaming endpoint is a resilient "run" —
run id announced up front, tagged on every event, and the stream always
ends with error + done when the work raises mid-stream.
"""


def _parse(sse_text: str) -> list[dict]:
    """Parse an SSE stream into [{id, event, data}, ...]."""
    out = []
    for block in sse_text.strip().split("\n\n"):
        ev = {"id": None, "event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("id: "):
                ev["id"] = line[4:]
            elif line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = line[6:]
        if ev["event"] is not None:
            out.append(ev)
    return out


class TestSseFormat:
    def test_run_id_becomes_id_line_and_payload_field(self):
        from app.sse import sse
        msg = sse("progress", {"n": 1}, "abc123")
        assert "id: abc123\n" in msg
        assert '"run_id": "abc123"' in msg
        assert msg.endswith("}\n\n")

    def test_no_run_id_no_id_line(self):
        from app.sse import sse
        msg = sse("progress", {"n": 1})
        assert not msg.startswith("id: ")
        assert "run_id" not in msg

    def test_string_data_unchanged(self):
        from app.sse import sse
        assert sse("done", "complete", "abc") == "id: abc\nevent: done\ndata: complete\n\n"


class TestHardened:
    def test_run_started_first_and_all_events_tagged(self):
        from app.sse import hardened, sse

        def gen():
            yield sse("log", "working")
            yield sse("done", "complete")

        events = _parse("".join(hardened(gen(), "t")))
        assert events[0]["event"] == "run_started"
        run_id = events[0]["id"]
        assert run_id and events[0]["data"].find(run_id) != -1
        assert all(e["id"] == run_id for e in events)
        assert [e["event"] for e in events] == ["run_started", "log", "done"]

    def test_exception_yields_error_then_done(self):
        from app.sse import hardened, sse

        def gen():
            yield sse("log", "halfway")
            raise RuntimeError("site is down")

        events = _parse("".join(hardened(gen(), "t")))
        assert [e["event"] for e in events] == ["run_started", "log", "error", "done"]
        assert "site is down" in events[2]["data"]
        assert events[3]["data"] == "failed"
        assert all(e["id"] == events[0]["id"] for e in events)

    def test_empty_gen_still_announces_run(self):
        from app.sse import hardened

        def gen():
            if False:
                yield 0

        events = _parse("".join(hardened(gen(), "t")))
        assert [e["event"] for e in events] == ["run_started"]


class TestPipelineEndpoint:
    def test_mid_stream_failure_terminates_cleanly(self, client, monkeypatch):
        """Acceptance: an exception inside the generator emits a `done`
        event after `error`, and every event carries the same run id."""
        from app import server as srv

        def boom(client_, **kwargs):
            yield type("E", (), {"type": "log", "message": "started"})()
            raise RuntimeError("boom mid-harvest")

        monkeypatch.setattr(srv, "harvest", boom)
        resp = client.post("/api/pipeline/run", json={})
        assert resp.status_code == 200
        events = _parse(resp.text)
        assert events[0]["event"] == "run_started"
        assert events[-1]["event"] == "done"
        assert events[-2]["event"] == "error"
        assert "boom mid-harvest" in events[-2]["data"]
        assert all(e["id"] == events[0]["id"] and e["id"] for e in events)

    def test_pipeline_check_failure_terminates_cleanly(self, client, monkeypatch):
        from app import server as srv

        def boom(*args, **kwargs):
            raise RuntimeError("enrich exploded")

        monkeypatch.setattr(srv, "enrich", boom)
        # a job the check path will actually try to enrich (domain-guarded)
        from db import connection as dbconn
        c = dbconn.get_conn()
        c.execute(
            "INSERT OR IGNORE INTO jobs (job_id, job_url, title) VALUES "
            "(777, 'https://www.onlinejobs.ph/jobseekers/job/x-777', 'Check me')"
        )
        c.commit()
        c.close()
        resp = client.post("/api/pipeline/check", json={})
        assert resp.status_code == 200
        events = _parse(resp.text)
        assert events[-1]["event"] == "done" and events[-1]["data"] == "failed"
        assert "enrich exploded" in events[-2]["data"]
