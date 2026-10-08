# NetPulse architecture

## The problem in operational terms

Suppose an interface reports one packet-loss spike. Acting immediately may
move traffic unnecessarily. Ignoring a sustained loss pattern may allow an
incident to grow. NetPulse demonstrates a middle path: retain a bounded recent
history, calculate a simple policy signal, and produce a reviewable action.

## Request lifecycle

```text
POST /telemetry
      |
      v
Pydantic validation
      |
      v
Sample(device, interface, timestamp, measurements)
      |
      v
RemediationEngine.evaluate()
      |
      +--> no sustained degradation: accepted
      |
      +--> degradation: Incident
                         |
                         v
                   DryRunExecutor
                         |
                         +--> JSONL audit record
                         +--> response plan
```

## State and restart behavior

The engine stores sample windows and cooldown timestamps in process memory.
The server stores recent incidents in a process-local list. Therefore:

- restarting the process clears the sample windows;
- restarting the process clears the in-memory incident list;
- the audit file is the only durable output in the current implementation;
- the default audit path is `runtime/netpulse.audit.jsonl`.

This is appropriate for a small lab, not for a highly available production
controller. A production implementation would need durable state, identity,
authorization, bounded retention, and coordinated workers.

## Safety boundary

The executor returns a plan and writes an audit record. It does not apply a
change. Replacing it with a live adapter would require, at minimum:

1. authenticated device access;
2. an explicit allow-list of permitted actions;
3. pre-change and post-change validation;
4. rollback behavior;
5. approval and rate limiting;
6. structured logs and alerting.

The separation between engine and executor is deliberate so those controls do
not become implicit side effects of incident detection.

## Limitations

The policy uses arithmetic averages, not percentiles or time-weighted
measurements. It does not model topology, correlated failures, device
maintenance, or traffic demand. It is a transparent starting point for
experimentation, not a claim that five samples and fixed thresholds are
universally correct.

