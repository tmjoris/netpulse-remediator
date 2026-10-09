# NetPulse on-call runbook

Every Prometheus alert in `prometheus/alerts.yml` links to a section here.
Commands assume:

```bash
export NP=http://127.0.0.1:8000
export AUTH="Authorization: Bearer $NETPULSE_API_TOKEN"
```

## First five minutes, whatever fired

```bash
curl -s $NP/v1/status | jq                      # executor mode, automation state, counts
curl -s "$NP/v1/incidents?active=true" | jq     # what NetPulse thinks is broken
curl -s $NP/v1/drains | jq                      # what is out of service, who owns it, why held
```

If NetPulse itself looks wrong (incidents that don't match reality, drains
you can't explain), **stop the automation first and investigate second**:

```bash
curl -s -X PUT $NP/v1/automation -H "$AUTH" -H 'content-type: application/json' \
  -d '{"enabled": false, "actor": "<you>", "reason": "<ticket/incident link>"}'
```

The kill switch stops automated drains and undrains only. Existing drains
stay as they are, operator changes still work, and the setting survives
restarts.

---

## NetPulseIncidentEscalated

**Meaning:** a link has critical loss and automation did not mitigate it. The
link is still carrying traffic.

**Triage:**

```bash
curl -s "$NP/v1/incidents?active=true&state=escalated" | jq '.[] | {id, target, reason, events}'
```

The `escalated` event says which safety checks failed:

| Failed check | Usual cause | Action |
|---|---|---|
| `capacity_headroom` | Survivors can't absorb the traffic | Don't drain. Shift traffic upstream (e.g. de-prefer the path in BGP), or accept the loss and get the link repaired urgently. |
| `min_active_members` | It's the last active member | Same as above; draining would black-hole the path. |
| `fleet_concurrency` | Automation already has its maximum drains active | Check whether the other drains are related (same device or line card?). A correlated failure is a bigger incident than one link. |
| `topology` | Interface is not in any link group | Add it to the config if it has redundancy; otherwise this is expected. |
| `kill_switch` / `maintenance` | Automation was deliberately stopped | Confirm with whoever stopped it. |
| `cooldown` | A recent action (often a failed one) on this interface | Look for a `NetPulseChangeFailed` alert. |
| `post_check` / `execute` | The device change failed | See [NetPulseChangeFailed](#netpulsechangefailed). |

**Manual drain**, if you have decided it is safe (still capacity-checked):

```bash
curl -s -X POST $NP/v1/changes -H "$AUTH" -H 'content-type: application/json' \
  -d '{"action":"drain","device":"<dev>","interface":"<if>","actor":"<you>","reason":"<ticket>"}' | jq
```

`409` means a safety check blocked it; the body lists which ones.

## NetPulseChangeFailed

**Meaning:** a drain or undrain raised an error, or applied but the device
didn't report the expected state afterwards and the change was rolled back.

**Triage:**

```bash
grep '"kind": "change"' /var/lib/netpulse/netpulse.audit.jsonl | tail -5 | jq '.data | {id, action, target, status, checks}'
```

- `execute` failed: device unreachable or rejected the commit. Check device
  reachability and credentials. Automation retries after the cooldown (10m).
- `post_check` failed, `rollback` passed: the change didn't take effect and the
  pre-change state was restored. Check the device for config drift or a stuck
  commit.
- `rollback` failed: **the device may be in an unknown state.** Log in and check
  the interface by hand, then make the state explicit with a manual change.

## NetPulseDrainHeld

**Meaning:** an automated drain is healthy but automation won't undrain it.
Usually `flap_damping` (3 automated drains in 24h). The link is out of service
and the group is running with less headroom.

```bash
curl -s $NP/v1/drains | jq '.[] | select(.hold_reason != null)'
```

A flapping link should be repaired before it goes back into service (clean or
replace the optic, check the patch, open a ticket with the circuit provider).
Once it's fixed:

```bash
curl -s -X POST $NP/v1/changes -H "$AUTH" -H 'content-type: application/json' \
  -d '{"action":"undrain","device":"<dev>","interface":"<if>","actor":"<you>","reason":"optic replaced, <ticket>"}' | jq
```

## NetPulseFleetDrainLimit

**Meaning:** automation has reached `max_concurrent_drains`. The next critical
link will escalate instead of draining.

Check whether the drains share a device, line card or site. If they do, treat it
as one incident with a common cause. Don't raise the limit to make the alert go
away; it exists to cap the damage from bad telemetry or a bug.

## NetPulseAutomationDisabled

**Meaning:** the kill switch has been off for over 30 minutes.

```bash
curl -s $NP/v1/automation | jq     # who turned it off and why
```

If the reason no longer applies, turn it back on with `"enabled": true`. If
it's still needed, leave it off; the alert is a reminder, not an error.

## NetPulseTelemetryStale

**Meaning:** no sample for an interface for 5+ minutes. NetPulse can't detect
problems there, and capacity checks for that link group fail closed (drains
blocked).

Check the collector and the device's telemetry subscription. If the interface
has been decommissioned, remove it from the config. Note that after a NetPulse
restart, this series reappears only when samples arrive again.

## NetPulseDown

**Meaning:** Prometheus can't scrape NetPulse. Nothing is being detected or
remediated. Drains already in place stay as they are.

```bash
docker compose ps netpulse
docker compose logs --tail=100 netpulse
```

If startup fails with `refusing to start with an invalid audit log`, the hash
chain is broken. **Do not delete the log.** Run
`netpulse audit verify <path>` to find the first bad line, keep a copy for
investigation, and escalate. Starting with an empty log would forget the
kill-switch state and drain ownership.

## NetPulseSlowEvaluation

**Meaning:** p99 per-sample evaluation is above 100ms. Ingestion is
serialized, so collectors will see timeouts.

Usual causes: slow disk for the audit log (every append is fsynced), or very
large telemetry batches. Check disk latency on the audit volume first.

---

## Routine operations

**Planned work on a device or interface.** Suppresses automation there. Windows
are capped at 7 days.

```bash
curl -s -X POST $NP/v1/maintenance -H "$AUTH" -H 'content-type: application/json' \
  -d '{"device":"edge-dub-01","interface":null,"duration_minutes":120,"actor":"<you>","reason":"<change ticket>"}' | jq
curl -s -X DELETE "$NP/v1/maintenance/MW-000001?actor=<you>" -H "$AUTH"
```

Maintenance stops automation from acting. It doesn't drain anything, so drain
explicitly first if the work will interrupt traffic.

**Verify the audit trail** (e.g. for a postmortem):

```bash
netpulse audit verify /var/lib/netpulse/netpulse.audit.jsonl
```

**Rehearse a policy change** before deploying it: edit a copy of the config,
validate it, then replay every failure scenario under the new policy and
compare against the current one:

```bash
netpulse check-config new.toml
diff <(netpulse simulate --config current.toml) <(netpulse simulate --config new.toml)
```
