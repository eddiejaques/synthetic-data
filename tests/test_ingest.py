"""Parallel ingestion engine: limits, resume, retries."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ingest  # noqa: E402


def _files(tmp_path, n_files=3, per_file=25):
    d = tmp_path / "in"
    d.mkdir()
    for i in range(n_files):
        (d / f"batch_{i:05d}.jsonl").write_text(
            "\n".join(json.dumps({"event": "x", "i": i * 100 + k, "pad": "y" * 50}) for k in range(per_file)) + "\n")
    return sorted(d.glob("*.jsonl"))


def test_chunks_respect_item_and_byte_limits(tmp_path):
    files = _files(tmp_path)
    chunks = ingest.chunk_records(files, max_items=10, max_bytes=600)
    assert sum(len(c.records) for c in chunks) == 75
    assert all(len(c.records) <= 10 and c.nbytes <= 600 for c in chunks)
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in ingest.chunk_records(files, 10, 600)]  # stable ids
    with pytest.raises(ValueError):
        ingest.chunk_records(files, max_items=10, max_bytes=20)


def test_parallel_run_resumes_and_never_resends(tmp_path):
    chunks = ingest.chunk_records(_files(tmp_path), 10, 10_000)
    sent: list[str] = []
    prog = ingest.Progress(tmp_path / "state.json", "dest")
    lim = ingest.RateLimiter(1000, 10_000)

    def flaky(c):
        if c.chunk_id.endswith("#1") and not any(x.endswith("#1") for x in sent):
            sent.append("fail:" + c.chunk_id)
            raise ingest.FatalUpload("boom")
        sent.append(c.chunk_id)

    with pytest.raises(SystemExit):
        ingest.run(chunks, flaky, workers=1, limiter=lim, progress=prog, label="t")
    done_first = set(prog.done)
    assert done_first and len(done_first) < len(chunks)
    prog2 = ingest.Progress(tmp_path / "state.json", "dest")
    ingest.run(chunks, lambda c: sent.append(c.chunk_id), workers=4, limiter=lim, progress=prog2, label="t")
    ok = [s for s in sent if not s.startswith("fail:")]
    assert sorted(ok) == sorted(c.chunk_id for c in chunks)            # each chunk exactly once
    assert ingest.Progress(tmp_path / "state.json", "other-dest").done == set()   # new destination starts fresh


class _Resp:
    def __init__(self, code, headers=None):
        self.status_code, self.headers, self.text, self.content = code, headers or {}, "", b""


def test_retry_on_429_then_success(monkeypatch):
    calls = []

    class S:
        def post(self, url, timeout=None, **kw):
            calls.append(1)
            return _Resp(429, {"Retry-After": "0"}) if len(calls) < 3 else _Resp(200)

    monkeypatch.setattr(ingest, "session", lambda: S())
    assert ingest.post_with_retry("http://x").status_code == 200 and len(calls) == 3


def test_hard_4xx_is_fatal(monkeypatch):
    class S:
        def post(self, url, timeout=None, **kw):
            return _Resp(400)

    monkeypatch.setattr(ingest, "session", lambda: S())
    with pytest.raises(ingest.FatalUpload):
        ingest.post_with_retry("http://x")
