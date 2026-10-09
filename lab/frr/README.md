# FRR lab: NetPulse against real routers

A containerised network where NetPulse acts on **real routing software**: two
[FRRouting](https://frrouting.org) routers running OSPF over two parallel
links, with equal-cost multipath (ECMP) between them.

```text
        10.0.1.0/24 (lnk1)
   r1 ======================= r2
        10.0.2.0/24 (lnk2)
   lo 10.255.0.1         lo 10.255.0.2
```

| Piece | What's real |
|---|---|
| Routing | FRR 10.2 `zebra` + `ospfd`; routes land in the Linux kernel FIB |
| Fault | `tc netem` packet loss on the link: the link stays up and OSPF keeps the adjacency (a gray failure) |
| Telemetry | `netpulse collect`: `ping` out of each specific interface for loss and latency, plus interface byte counters for utilization |
| Drain | `FrrExecutor` sets `ip ospf cost 65535` on **both ends** of the link via `vtysh`, then waits until the routing table on the device stops using it; if it doesn't within 15s, it restores the cost and fails the change |
| Undrain | restores `ip ospf cost 10` on both ends after the soak period |
| Drain state | read back from the routers (`show ip ospf interface … json`), never assumed |

## Run it

Requires Docker and the `sch_netem` kernel module (standard on most Linux
distributions; `sudo modprobe sch_netem` if needed).

```bash
lab/frr/demo.sh          # ~2 minutes; exits non-zero if any step fails
KEEP=1 lab/frr/demo.sh   # leave the routers running afterwards
```

Output, abridged:

```text
== Start two FRR routers with two parallel OSPF links
r1 -> r2 loopback:
    10.255.0.2 nhid 14 proto ospf metric 20
    	nexthop via 10.0.1.3 dev lnk1 weight 1
    	nexthop via 10.0.2.3 dev lnk2 weight 1

== Inject 30% packet loss on r1:lnk1 (tc netem); routing still sees the link up
r1 -> r2 loopback:
    10.255.0.2 nhid 16 via 10.0.2.3 dev lnk2 proto ospf metric 20
OSPF cost on lnk1: r1=65535 r2=65535

== Repair the link; NetPulse undrains after the soak period
r1 -> r2 loopback:
    10.255.0.2 nhid 14 proto ospf metric 20
    	nexthop via 10.0.1.3 dev lnk1 weight 1
    	nexthop via 10.0.2.3 dev lnk2 weight 1

== Timeline (from the collector)
T+  0m16s  opened     INC-000001 r1:lnk1   warning: 4/5 samples breaching (3 critical, need 4); mean loss 20.00% ...
T+  0m19s  change     CHG-000001 r1:lnk1   drain -> applied
T+  0m19s  severity   INC-000001 r1:lnk1   escalated to critical: 5/5 samples breaching (4 critical, need 4) ...
T+  0m19s  mitigated  INC-000001 r1:lnk1   drained by netpulse (applied)
T+  0m27s  resolved   INC-000001 r1:lnk1   healthy: 4/5 samples below clear thresholds; interface stays drained ...
T+  0m47s  change     CHG-000002 r1:lnk1   undrain -> applied
T+  0m47s  undrained  INC-000001 r1:lnk1   undrain applied
```

## Drive it by hand

```bash
KEEP=1 lab/frr/demo.sh                                   # or: docker compose -f lab/frr/docker-compose.yml up -d
netpulse serve   --config lab/frr/netpulse.toml --port 8001 &
netpulse collect --config lab/frr/netpulse.toml --target http://127.0.0.1:8001 --verbose

docker exec netpulse-lab-r1 tc qdisc add dev lnk1 root netem loss 30%   # break lnk1
docker exec netpulse-lab-r1 tc qdisc del dev lnk1 root                  # repair it
docker exec netpulse-lab-r1 vtysh -c "show ip route 10.255.0.2"         # watch ECMP change
```

Worth trying:

- **Last-path protection:** cost out `lnk2` by hand
  (`vtysh -c "conf t" -c "int lnk2" -c "ip ospf cost 65535"`), restart
  `netpulse serve`, then break `lnk1`. NetPulse adopts the manual drain as
  operator-owned on startup, and refuses to drain `lnk1` because no capacity
  would remain.
- **Kill switch:** `curl -X PUT localhost:8001/v1/automation -d '{"enabled":false,"actor":"me","reason":"test"}' -H 'content-type: application/json'`,
  then break a link. The incident is escalated, not drained.
- **Integration tests:** `NETPULSE_FRR_LAB=1 pytest tests/test_frr_lab.py` with the lab running.

## From lab to production

The same executor works on real FRR-based routers (FRR on Linux, SONiC,
Cumulus) by switching the transport in the config:

```toml
[devices]
transport = "ssh"
ssh_user = "netpulse"
[devices.hosts]
edge-dub-01 = "edge-dub-01.mgmt.example.net"
```

Still missing for production use: per-device credentials management, reading
`normal_cost` from the source of truth instead of config, and running changes
asynchronously so a slow device doesn't block telemetry ingestion (today a
drain holds the API lock for up to `converge_timeout_s`).
