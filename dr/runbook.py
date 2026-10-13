"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
import math

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    record = {**kw, "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "step": n, "name": name}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("Choose distinct primary and target regions a/b")
    started = time.monotonic()
    # Three failed checks five seconds apart, independent of the alert process.
    for attempt in range(3):
        ready, reason = probe(primary, 2)
        try:
            alive = httpx.get(f"{URL[target]}/healthz", timeout=2).status_code == 200
        except httpx.HTTPError:
            alive = False
        if ready or not alive:
            step(1, "xac_nhan_outage", ok=False, primary_ready=ready, target_alive=alive)
            return {"ok": False, "error": "Outage unconfirmed or target unreachable"}
        if attempt < 2:
            time.sleep(5)
    step(1, "xac_nhan_outage", ok=True, primary=primary, target=target,
         consecutive_fails=3, reason=reason)
    events = pathlib.Path("chaos/chaos-events.jsonl")
    kills = [json.loads(line) for line in events.read_text().splitlines() if line.strip()] if events.exists() else []
    outage = next((e for e in reversed(kills) if e.get("action") == "kill"
                   and e.get("region") == primary and e["ts"] <= time.time()), None)
    incident = step(2, "thong_bao_incident", t_outage=outage["ts"] if outage else None,
                    notification_delay_s=time.time() - outage["ts"] if outage else None,
                    message="Local incident recorded; operator confirmation required", auto=auto)
    if not confirm(auto, f"Fail over {primary} to {target}?"):
        step(3, "scale_gpu_pool", ok=False, reason="Operator declined")
        return {"ok": False, "error": "Operator declined"}
    step(3, "scale_gpu_pool", operator_confirmed_at=time.time(), incident_ts=incident["ts"])
    result = fo.failover(target, backend, wait=60)
    if not result["ok"]:
        step(3, "scale_gpu_pool", ok=False, error=result["error"])
        return result
    step(4, "verify_state_replica", **result["state"],
         embed_model_version=result["replica"]["embed_model_version"],
         rpo_seconds=result["rpo_seconds"], docs_lost=result["docs_lost"])
    step(5, "dns_cutover", ok=result["cutover"], target=target)
    latencies, errors = [], 0
    with httpx.Client(timeout=3) as client:
        for _ in range(10):
            t = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer")
                if response.status_code != 200 or response.json().get("region") != target:
                    errors += 1
            except (httpx.HTTPError, ValueError):
                errors += 1
            latencies.append((time.monotonic() - t) * 1000)
    p95 = sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1]
    signals_ok = errors == 0 and p95 < 500
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=round(p95, 2),
         error_rate=errors / 10, ok=signals_ok)
    step(7, "post_incident", elapsed_s=round(time.monotonic() - started, 3),
         ok=signals_ok, measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {**result, "ok": signals_ok, "p95_latency_ms": round(p95, 2), "error_rate": errors / 10}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
