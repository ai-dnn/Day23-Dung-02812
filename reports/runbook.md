# Region A outage runbook

Owner: serving on-call. Incident commander authorizes cutover and rollback. Run from the repository root with `source .venv/bin/activate`; install dependencies with `python3 -m pip install -r requirements.txt` on a new machine. Ports: Region A 8001, Region B 8002, edge 8080. Filesystem snapshots are in `state/_replica/dr-artifacts/`.

Prerequisites: both serving processes and the edge are running (`bash scripts/up_bare.sh`), and replication has completed at least one cycle. Start `python3 state/replicate.py --every 30 --duration 115 --backend fs` before the drill. Run `python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl` alongside traffic. A warm, empty Region B returning 503 from readiness is expected before restoration; its liveness must return 200.

Execute the recovery once:

```bash
python3 dr/runbook.py --primary a --target b --backend fs
```

The command performs all seven steps below. Answer `y` only after reviewing the confirmed outage. Use `--auto` only for the local graded drill or CI. The incident announcement is written to a local log. Logs append across runs; the measurement tool selects events within each traffic window.

| # | Step and copy-pasteable verification command | Completion signal | Owner |
|---|---|---|---|
| 1 | Confirm outage: `python3 chaos/kill_region.py status --backend bare` | Automation confirms three consecutive failed A readiness checks, five seconds apart, and checks B liveness each time. Runbook step 1 has `ok:true`. | Serving on-call |
| 2 | Record incident: `cat reports/runbook-run.jsonl` | Step 2 records incident timestamp, outage timestamp and notification delay; operator approves the prompt. | Incident commander |
| 3 | Restore state and scale compute: `cat reports/failover-events.jsonl` | Steps 1–3 show target state, restored embedding version, measured RPO/documents lost, then `pool_state:full`. Automation calls failover exactly once. | Serving on-call |
| 4 | Verify replica and readiness: `curl -fsS http://127.0.0.1:8002/v1/state` and `curl -fsS http://127.0.0.1:8002/readyz` | Nonzero count, weights present, full pool, readiness 200 after six-second warm-up; runbook step 4 records returned state. | Serving on-call |
| 5 | Verify DNS/LB cutover: `curl -fsS http://127.0.0.1:8080/edge/state` | `active_region:b` after up to five seconds of cached routing; failover step 5 follows successful readiness. | Serving on-call |
| 6 | Verify golden signals: `cat reports/runbook-run.jsonl` and `curl -fsS http://127.0.0.1:8080/v1/infer` | Step 6 has ten requests to B, zero error rate and p95 below 500 ms. Edge response has `region:b`. Check edge traffic logs for user-visible recovery. | Serving on-call |
| 7 | Measure and review: `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, empty warnings, `rto_verdict:PASS`, RPO seconds and documents lost present. Update evidence and postmortem from logs. | Incident commander |

If restoration, target verification or readiness fails, automation aborts without changing routing and exits nonzero. Investigate the `abort` event; verify the target process and snapshot before retrying. If golden signals fail after cutover, keep the incident open and investigate B before considering rollback.

Rollback requires incident commander approval, Region A responding consistently to readiness, compatible weights/index, and reconciliation of data written in both regions. A resumed process alone does not justify rollback. For this paused-process lab, `python3 chaos/kill_region.py restore --region a --backend bare` resumes A. Confirm its readiness three times, five seconds apart, with `curl -fsS http://127.0.0.1:8001/readyz`. Reconcile the replica before routing traffic back. Only after approval and readiness, set routing with `printf a > edge/active_region`, wait for the edge cache to refresh and verify `curl -fsS http://127.0.0.1:8080/v1/infer`. Production failback needs an explicit data reconciliation procedure; restoring the older A-to-B snapshot into A would discard newer data.

After the lab, stop its processes with `bash scripts/down_bare.sh`.
