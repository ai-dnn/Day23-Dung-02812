# RTO/RPO evidence — local disaster-recovery drill

Measured on 2026-10-13 in bare mode with `netblock --mock`. Timestamps below are UTC. Inference goes through the edge at port 8080. No service, chaos or measurement code was changed.

## Drill 1 — baseline without recovery

| Metric | Value | Measurement | Evidence |
|---|---|---|---|
| Outage | 2026-10-13T10:28:22.804+00:00 | Region A paused, B alive | `chaos/chaos-events.jsonl:1` |
| First failed request | +0.015s | First failed request after kill | `reports/drill-1-nodr.jsonl:17` |
| Failed requests | 16 of 32 | Requests after outage with `ok:false` | `reports/measure-drill-1.json` |
| Recovery | None in the 40s traffic window | No success after first failure | `reports/drill-1-nodr.jsonl:32` |
| RTO | NO_RECOVERY | Timestamp-based measurement | `reports/measure-drill-1.json` |

## Drill 2 — recovery to Region B

| Milestone | Seconds after outage | Measurement | Evidence |
|---|---|---|---|
| Outage | 0s | 2026-10-13T11:37:20.625+00:00; A paused, B alive | `chaos/chaos-events.jsonl:4` |
| First failed request | +0.050s | First failure after kill | `reports/drill-2-withdr.jsonl:25` |
| Health detection | +14.988s | Three consecutive failed A readiness probes | `reports/health-events.jsonl:2` |
| Incident recorded | +16.121s | Independent outage confirmation complete | `reports/runbook-run.jsonl:2` |
| Snapshot restored | +16.126s | RPO measured against restored SQLite DB | `reports/failover-events.jsonl:2` |
| Standby ready | +22.167s | Readiness 200 after warm-up | `reports/failover-events.jsonl:4` |
| DNS cutover | +22.167s | Routing pointer changed after readiness | `reports/failover-events.jsonl:5` |
| First recovered request | +28.154s | Successful inference served by B | `reports/drill-2-withdr.jsonl:39` |

| Metric | Measured | Target | Verdict | Evidence |
|---|---|---|---|---|
| RTO — inference | 28.2s | 300s | PASS | `reports/measure-drill-2.json` |
| RPO — vector DB at restore | 4.01s / 2 documents | 300s | PASS for seconds; document loss quantified | `reports/failover-events.jsonl:2` |
| Golden signals on B | p95 1.83 ms; error rate 0.0; ten requests | p95 < 500 ms; zero errors | PASS | `reports/runbook-run.jsonl:6` |
| Validity | valid=true; warnings=[] | Recovery from the surviving region | PASS | `reports/measure-drill-2.json` |

## RTO components

| Component | Seconds | Origin | Possible improvement |
|---|---|---|---|
| Lab detection budget | 15.000s | 5s interval × 3 failures in `reports/health-events.jsonl:2` | Shorter interval with a retained failure threshold |
| Detection phase alignment, independent operator confirmation, verify and restore | 1.126s | Restore completion minus outage minus detection budget; restore itself took 0.001s in `reports/failover-events.jsonl:2` | Confirm from a current alert; keep snapshot local and verified |
| Restore-to-scale handoff | 0.000s | Restore to scale in `reports/failover-events.jsonl:3` | Negligible here |
| GPU pool warm-up and readiness polling | 6.040s | Scale to ready; `waited_s`=6.04 in `reports/failover-events.jsonl:4` | Maintain a full standby pool at additional compute cost |
| Ready-to-cutover handoff | 0.001s | Ready to routing write in `reports/failover-events.jsonl:5` | Negligible here |
| DNS cache, in-flight timeout and request scheduling | 5.986s | First B request minus cutover; TTL=5s and edge timeout=2s in `edge/proxy.py` | Reduce TTL and bound timeouts; monitor added lookup/probe load |

The component sum is 28.154s; the measurement tool rounds RTO to one decimal (28.2s). The last component includes cache behavior and traffic timing, so it is not a direct measurement of TTL alone. The lab calls interval × threshold a detection floor; in general the first failed probe's phase and timeout also affect detection time. Observed detection is recorded separately above.

Replication ran every 30s; ingest ran at 0.5 documents/s. Both ran for 115s. Traffic and health checks ran for 100s; chaos occurred 12s after they started, after a 5s replication lead-in. The standby began empty and warm. Default six-second warm-up and five-second edge TTL were retained. The ingest and filesystem replica processes continue accessing A's disk while its serving process is paused; this measures RPO at restore in this simulation, not loss after permanent disk destruction. Embedding version restored: `embed-model=vi-e5-base@v3`.

Recompute with `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`; validate with `python3 -m pytest tests/ -v`. Activate `.venv` first. Baseline measurement deliberately reports missing detection/cutover warnings because those components were absent. The chaos log also retains the earlier recovery attempt; measurement selects the kill inside each traffic file's window. That earlier attempt was repeated because a concurrent unit test changed B's pool state. The final recovery evidence was collected with tests stopped and a fresh standby.
