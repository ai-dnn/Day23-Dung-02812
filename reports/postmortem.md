# Postmortem — Region A recovery drill

## Timeline

| UTC timestamp | Event | Evidence |
|---|---|---|
| 2026-10-13T11:37:20.625+00:00 | Region A serving process paused; B liveness verified | `chaos/chaos-events.jsonl:4` |
| 2026-10-13T11:37:20.675+00:00 | First user request that fails begins | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-13T11:37:35.613+00:00 | Three-failure health alert | `reports/health-events.jsonl:2` |
| 2026-10-13T11:37:36.746+00:00 | Incident recorded, 16.121s notification delay | `reports/runbook-run.jsonl:2` |
| 2026-10-13T11:37:36.746+00:00 | CI/local-drill auto confirmation, not a human response | `reports/runbook-run.jsonl:3` |
| 2026-10-13T11:37:36.751+00:00 | Snapshot and embedding version restored | `reports/failover-events.jsonl:2` |
| 2026-10-13T11:37:42.792+00:00 | Region B readiness verified after pool warm-up | `reports/failover-events.jsonl:4` |
| 2026-10-13T11:37:42.792+00:00 | Edge routing changed to B | `reports/failover-events.jsonl:5` |
| 2026-10-13T11:37:48.779+00:00 | First successful request served by B | `reports/drill-2-withdr.jsonl:39` |

## RTO/RPO and gap analysis

RTO target: 300s; measured: 28.2s; gap (measured − target): -271.8s. Recovery used B, and the measurement has no validity warnings. The baseline had 16 failed requests and NO_RECOVERY. RPO target: 300s; measured at restore: 4.01s / 2 documents; gap: -295.99s. No explicit document-count target was specified.

The largest component is detection/confirmation: the nominal detection budget is 15.0s, and restoration completes 16.126s after outage. Six-second compute warm-up and DNS cache/in-flight traffic add further delay. Exact phase durations are in `reports/rto-evidence.md`. Ten direct B requests had p95 1.83 ms and zero errors; edge recovery is measured independently from traffic logs. Default interactive approval may add human response time beyond this automated drill.

## Root cause — five whys

1. Why did inference fail? Edge requests went to a paused A process and waited for timeout.
2. Why did edge continue using A? It follows a cached routing pointer and has no autonomous recovery mechanism.
3. Why could B not immediately serve? The warm standby initially lacked vectors and model weights and required pool promotion.
4. Why was recovery impossible in the baseline? No health checker, state restoration or readiness-gated routing procedure was running.
5. Why is a stateful runbook required? Serving availability depends on a compatible embedding model, restored vectors, compute readiness and routing together; process liveness alone does not verify these dependencies.

The completed automation bounds network waits, confirms consecutive failures, restores the state/version together, waits for readiness, then changes routing. Remaining operational gaps: current alerts are not connected to a production incident system; snapshots have no transactional multi-artifact publication or integrity checks; permanent primary disk loss would prevent the local comparison used to count lost documents; failback needs reconciliation. A concurrent unit test changed B's pool during the first recovery attempt. The final drill was repeated with tests stopped and a fresh warm, empty B; this is a workflow isolation issue, not operator blame.

## Action items

| Action item | Owner | Deadline | Expected effect and validation |
|---|---|---|---|
| Isolate tests and drills in separate working/state directories | Lab maintainer | 2026-10-14 | Prevent test writes from changing warm-up; verify standby is warm and empty before chaos |
| Evaluate 1s readiness interval while retaining three consecutive failures | Serving on-call | 2026-10-16 | Nominal detection budget drops 15s to 3s, potentially saving 12s; measure under packet loss and adjust timeout |
| Add consistent SQLite backup, manifest integrity and atomic snapshot publication | State platform owner | 2026-10-20 | Protect restore correctness; repeat drill while ingest is active and verify DB/model consistency |
| Define permanent-loss RPO accounting and reconcile failback writes | State platform owner + incident commander | 2026-10-23 | Count acknowledged writes absent from replica without primary disk; prove zero avoidable loss on approved failback |

## Required reflection

1. Interval × threshold = 5s × 3 = 15s, or 53.2% of measured RTO. This is the lab's nominal detection budget. Actual consecutive-probe detection depends on where outage occurs between polls and the bounded timeout; the real observed delay was 14.988s. For a 300s budget, allowing 2s timeout, 6s warm-up and 5s TTL gives an approximate maximum interval of (300 − 2 − 6 − 5) / 3 = 95.7s before restore, operator time and safety margin. Keep 5s here; that ceiling is not an operational recommendation.
2. A 1s interval nominally saves 12s (15s → 3s), but timeout=2s can make a sequential probe cycle longer than 1s. It produces more probe traffic and reacts more readily to short interruptions; test it with suitable timeouts and the retained threshold. Warm-up and TTL remain. This reduction is a hypothesis, not another measured drill.
3. With a six-hour permanent primary loss, the 2 documents counted at this restore are customer writes missing from this replica. They require upstream replay or customer re-entry, otherwise they are lost. Later acknowledged writes beyond this restore point would also need accounting. This paused-service drill leaves A's disk accessible and ingestion/replication active, so its measured RPO does not establish a guarantee for permanent region storage loss.

The checker is a separate process and imports no serving implementation, so pausing serving does not pause detection. Recheck the claim using `reports/measure-drill-2.json` and its raw evidence paths.
