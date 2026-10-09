# NetPulse architecture

## The problem in operational terms

A link that is hard down is the network's easy case: routing protocols notice
and converge. The hard case is a *gray failure*: a link that is up, passes
BFD and keeps its routing adjacencies, but drops or delays a fraction of
packets. Users feel it; routing doesn't see it. The usual fix is to **drain**
the link (move its traffic onto parallel links) and investigate.

Automating that is attractive and dangerous in equal measure. NetPulse's design
takes the position that the remediation itself is the easy part; the
engineering is in deciding when **not** to remediate, and in being able to
explain every decision afterwards.

## Components

```text
                ┌────────────── server.py (FastAPI, one lock) ───────────────┐
POST /v1/telemetry                                                           │
   │            │                                                            │
   ▼            │   engine.py                                                │
Sample ──► Detector ──► Evaluation ──► incident lifecycle                    │
            (window,     verdict:        open / mitigated /                  │
             M-of-N,     healthy|hold|   escalated / resolved                │
             hysteresis) warning|critical     │                              │
                                              ▼                              │
                                SafetyChecker(policy, topology, ControlState)│
                                              │ all checks pass?             │
                                   no ◄───────┴───────► yes                  │
                           BLOCKED change          Executor.drain/undrain    │
                           + escalate              post-check: drained()?    │
                                                   mismatch ► rollback       │
                                              │                              │
                                              ▼                              │
                                 AuditLog (hash chain) + metrics + JSON logs │
                └────────────────────────────────────────────────────────────┘
```

| Module | Owns |
|---|---|
| `detector.py` | Per-interface windows; classifying each window |
| `engine.py` | Incidents, change orchestration, replay and reconciliation |
| `safety.py` | Check logic and `ControlState` (kill switch, drains, history, maintenance) |
| `topology.py` | Which interfaces can carry each other's traffic |
| `executors.py` | The only code that would touch a device |
| `audit.py` | Durable, tamper-evident record of every decision |

## Detection

Each sample is classified against three tiers:

| Tier | Default condition |
|---|---|
| critical breach | loss ≥ 10% |
| breach | loss ≥ 2% **or** latency ≥ 100 ms |
| clear | loss < 0.5% **and** latency < 80 ms |

Once the window (5 samples) is full, the verdict is the first match of:
≥ 4 critical → `CRITICAL`; ≥ 4 breaches → `WARNING`; ≥ 4 clear → `HEALTHY`;
otherwise `HOLD`.

- **M-of-N rather than averages.** The original version averaged the window,
  so a single 60%-loss sample could push the mean over threshold. Counting
  breaching samples is robust to one outlier and easy to explain.
- **Hysteresis.** Clear thresholds sit below raise thresholds, and `HOLD`
  changes nothing. A link oscillating around 2% loss produces one incident,
  not a stream of open/resolve pairs.
- **Event time.** All policy timing (cooldown, soak, flap window) uses sample
  timestamps, not the wall clock. That's what makes the simulator
  deterministic and makes replayed telemetry behave like live telemetry. The
  cost is that NetPulse trusts collector clocks; out-of-order samples are
  rejected and counted (`result="out_of_order"`).

## Incident lifecycle

```text
            WARNING/CRITICAL verdict
   (none) ───────────────────────────► OPEN ──── drain applied ────► MITIGATED
                                         │                              │
                                         │ drain blocked / failed       │
                                         ▼                              │
                                     ESCALATED ── later drain ok ───────┤
                                         │                              │
                                         └──────── HEALTHY verdict ─────┴──► RESOLVED
```

One unresolved incident per interface. Severity only goes up while it is open.
Every transition is appended to the incident's timeline and the audit log.

Only **critical** incidents drain automatically. Warnings open an incident and
alert, because latency at warning level is frequently congestion, and draining
a congested link makes its neighbours more congested.

## Safety model

All checks are evaluated and reported, not just the first failure, so a
blocked change tells the on-call engineer everything that is wrong at once.

| Check | Applies to | Why |
|---|---|---|
| `kill_switch` | automated drain + undrain | One switch to stop the robot during an incident or bad deploy |
| `maintenance` | automated drain + undrain | Someone is working on it; don't fight them |
| `cooldown` | automated drain + undrain | Prevents oscillation and retry storms against a failing device |
| `fleet_concurrency` | automated drain | Caps blast radius: a bad telemetry feed can't drain the network |
| `topology` | every drain | Unknown redundancy means unknown risk (fail closed) |
| `min_active_members` | every drain | Never drain the last path |
| `capacity_headroom` | every drain | Survivors must stay ≤ 80% after absorbing the traffic |
| `soak` | automated undrain | Healthy continuously for 15 min, not just at this moment |
| `flap_damping` | automated undrain | After 3 automated drains in 24 h, hold drained for a human |

Design points:

- **Operators skip the automation guards but not the physics.** A human drain
  skips the kill switch, cooldown and fleet limit, which exist to constrain
  automation, but still has to pass topology, minimum-members and capacity
  checks. Humans make capacity mistakes too.
- **Ownership.** Automation only undrains drains it made. An operator drain,
  or a drain found on the device that NetPulse didn't make, is never undone
  automatically.
- **Capacity math counts in-flight traffic.** When two members of a bundle
  fail in the same polling cycle, the first one's traffic hasn't shown up on
  the survivors yet. The check therefore sums every member's last observed
  load, including members drained moments ago, rather than only the active
  ones. Without this, the second drain would undercount by a full member's
  traffic. (Found while building the `correlated-failure` scenario; covered by
  `test_recently_drained_member_traffic_is_still_counted`.)
- **Stale data fails closed.** Utilization older than 5 minutes is treated as
  unknown, and unknown blocks the drain.
- **The safe resting state is "drained".** Flap damping holds a link drained
  rather than putting it back into service, because the capacity check already
  proved the survivors can carry it.
- **Blocked attempts are deduplicated.** A blocked drain is re-evaluated on
  every sample (so it proceeds as soon as conditions allow) but only recorded
  when the set of failing checks changes. Undrain checks that mean "not yet"
  (`soak`, `cooldown`) are never recorded.

## Change execution

```text
execute(change):
    executor.drain|undrain(target)          ── raises ──► FAILED (escalate)
    post-check: target in executor.drained()
        matches  ──► APPLIED (or DRY_RUN)
        mismatch ──► rollback (inverse op) ──► ROLLED_BACK, or FAILED if rollback raised
    always: record last automated action (so failures wait out the cooldown)
            sync ControlState from executor.drained()
            append change record to audit log
```

### Executors

`Executor` is a four-member protocol: `name`, `success_status`,
`drain/undrain(ref, change_id)` and `drained()`. `drained()` is the source of
truth for device state; the engine never assumes a change worked.

- `DryRunExecutor`: shadow mode. Tracks the would-be drained set so capacity
  and concurrency checks are faithful, and records `dry_run` changes.
- `LabExecutor`: simulated device with fault injection (`fail_on` raises,
  `ignore_on` silently doesn't take effect). Used by the simulator and tests.

A production adapter would, at minimum: authenticate per device; drain
gracefully (raise the IGP metric / shut the BGP session, wait for traffic to
leave, then disable); be idempotent on `change_id`; and read back real state
for `drained()`.

## State and restarts

| State | Where it lives | After restart |
|---|---|---|
| Kill switch, maintenance windows | audit log | replayed |
| Drains, owners, flap history, cooldowns | audit log, reconciled with `executor.drained()` | replayed, then executor wins |
| ID sequences (`INC-`, `CHG-`, `MW-`) | audit log | continue where they left off |
| Detection windows, healthy-since | memory | rebuilt from the next 5 samples |
| Incident objects | memory (bounded, 1000) | lost; audit log keeps their events |

The replay is the important part: without it, restarting the process would
silently **re-enable automation an operator had disabled**, and forget which
drains automation owns.

**Reconciliation** runs at startup. If the audit log says a link is drained but
the executor says it isn't, the executor wins (logged + audited as drift). If
the executor reports a drain NetPulse didn't make, it is adopted as
operator-owned (`actor="unknown"`) and never auto-undrained.

The audit log refuses to load if its hash chain is broken, so NetPulse won't
make decisions on top of a history it can't trust.

## Concurrency and scale

The server serializes all engine access behind one lock. Evaluation is
O(window) per sample plus O(group size) for a safety check, and every audit
append is an `fsync`. That is fine for a lab; it has not been load-tested, and
it is a single-writer design. Scaling out would mean sharding interfaces
across workers by link group (so a capacity check never spans shards) with
one leader per shard holding the change lock.

## Trade-offs worth discussing

- **Thresholds are static.** Per-link baselines (a transoceanic link's normal
  latency is not a metro link's) would reduce false positives. Static
  thresholds were kept because they are explainable on a 3am page.
- **Interface-scoped incidents.** A device or line-card failure opens N
  incidents. The fleet concurrency limit stops that from causing N drains, but
  correlation (by device, line card or shared-risk group) would page once.
- **Audit log as state store.** Replaying a JSONL file is simple and
  inspectable but grows without bound; production would snapshot state and
  ship the log to durable storage.
