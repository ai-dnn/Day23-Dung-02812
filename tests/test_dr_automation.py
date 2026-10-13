"""Isolated checks of readiness gates and the operator runbook."""
import json
import pathlib

import httpx
import pytest

from dr import failover as fo
from dr import health_checker as hc
from dr import runbook as rb


@pytest.mark.parametrize("status,body,ready", [
    (200, {"ready": True}, True),
    (200, {"ready": False}, False),
    (503, {"ready": True}, False),
])
def test_probe_requires_http_success_and_readiness(monkeypatch, status, body, ready):
    def get(url, timeout):
        assert url.endswith("/readyz")
        assert timeout == 0.1
        return httpx.Response(status, json=body)
    monkeypatch.setattr(hc.httpx, "get", get)
    assert hc.probe("a", 0.1)[0] is ready


def test_probe_timeout_is_a_failed_check(monkeypatch):
    def get(*args, **kwargs):
        raise httpx.ReadTimeout("paused region")
    monkeypatch.setattr(hc.httpx, "get", get)
    assert hc.probe("a", 0.1) == (False, "ReadTimeout: paused region")


def test_failover_completes_in_order_and_returns_replica(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pathlib.Path("state/region-b").mkdir(parents=True)
    state = {"region": "b", "weights": True, "count": 200, "pool_state": "full"}
    monkeypatch.setattr(fo, "state_of", lambda region: state)
    monkeypatch.setattr(fo.snapshot, "get", lambda *args: {"embed_model_version": "v3"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *args: {"rpo_seconds": 2, "docs_lost": 1})
    def ready(*args, **kwargs):
        assert not pathlib.Path("edge/active_region").exists()
        return httpx.Response(200, json={"ready": True})
    monkeypatch.setattr(fo.httpx, "get", ready)
    result = fo.failover("b", "fs", 1)
    assert result["ok"] and result["state"]["count"] == 200
    assert pathlib.Path("edge/active_region").read_text() == "b"
    events = [json.loads(line) for line in fo.LOG.read_text().splitlines()]
    assert [event["step"] for event in events] == [
        "1_verify_target", "2_restore_snapshot", "3_scale_pool", "4_wait_ready", "5_dns_cutover"]


def test_missing_snapshot_aborts_without_cutover(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pathlib.Path("edge").mkdir()
    pathlib.Path("edge/active_region").write_text("a")
    monkeypatch.setattr(fo, "state_of", lambda region: {"region": region})
    result = fo.failover("b", "fs", 1)
    assert not result["ok"]
    assert pathlib.Path("edge/active_region").read_text() == "a"


@pytest.mark.parametrize("approve", [False, True])
def test_runbook_calls_failover_once_only_after_confirmation(monkeypatch, tmp_path, approve):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rb, "probe", lambda *args: (False, "timeout"))
    monkeypatch.setattr(rb.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rb, "confirm", lambda *args: approve)
    monkeypatch.setattr(rb.httpx, "get", lambda *args, **kwargs: httpx.Response(200))
    calls = []
    def failover(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "cutover": True, "state": {"count": 200, "weights": True},
                "replica": {"embed_model_version": "v3"}, "rpo_seconds": 2, "docs_lost": 1}
    monkeypatch.setattr(rb.fo, "failover", failover)
    def infer(self, *args, **kwargs):
        return httpx.Response(200, json={"region": "b"})
    monkeypatch.setattr(rb.httpx.Client, "get", infer)
    result = rb.run("a", "b", "fs", False)
    assert len(calls) == int(approve)
    assert result["ok"] is approve
    if approve:
        events = [json.loads(line) for line in rb.LOG.read_text().splitlines()]
        assert [event["step"] for event in events] == list(range(1, 8))
        assert events[5]["requests"] == 10


def test_confirmation_defaults_to_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert not rb.confirm(False, "Proceed?")
    assert rb.confirm(True, "Proceed?")
