#!/usr/bin/env bash
# End-to-end demo on real routers: FRR + OSPF ECMP, tc netem packet loss,
# NetPulse detecting it from active probes and draining the link by OSPF
# cost-out, then undraining after recovery. Exits non-zero if any step fails,
# so CI runs it too.
#
#   lab/frr/demo.sh            # run and tear down
#   KEEP=1 lab/frr/demo.sh     # leave the lab running afterwards
set -euo pipefail

cd "$(dirname "$0")/../.."
NETPULSE=${NETPULSE:-$(command -v netpulse || echo .venv/bin/netpulse)}
PORT=${PORT:-8001}
API=http://127.0.0.1:$PORT
LOGS=$(mktemp -d)
COMPOSE=(docker compose -f lab/frr/docker-compose.yml)
PIDS=()

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
cleanup() {
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
    if [[ "${KEEP:-0}" != 1 ]]; then "${COMPOSE[@]}" down --volumes >/dev/null 2>&1 || true; fi
}
fail() { echo "FAIL: $*" >&2; echo "--- server log"; tail -20 "$LOGS/serve.log"; echo "--- collector log"; tail -20 "$LOGS/collect.log"; exit 1; }
trap cleanup EXIT
r1() { docker exec netpulse-lab-r1 "$@"; }
routes() { echo "r1 -> r2 loopback:"; r1 ip route show 10.255.0.2 | sed 's/^/    /'; }
wait_for() {  # wait_for <seconds> <description> <command...>
    local deadline=$((SECONDS + $1)) what=$2; shift 2
    until "$@"; do ((SECONDS < deadline)) || fail "timed out waiting for $what"; sleep 1; done
}
drained() { curl -fsS "$API/v1/drains" | grep -q '"r1:lnk1"'; }
ecmp() { [[ $(r1 ip route show 10.255.0.2 | grep -c nexthop) == 2 ]]; }
ready() { curl -fsS -o /dev/null "$API/readyz" 2>/dev/null; }

step "Start two FRR routers with two parallel OSPF links"
"${COMPOSE[@]}" up -d --wait >/dev/null 2>&1
wait_for 60 "OSPF ECMP routes" ecmp
routes

step "Start NetPulse (FRR executor) and the probe collector"
rm -f runtime/frr-lab.audit.jsonl
"$NETPULSE" serve --config lab/frr/netpulse.toml --port "$PORT" >"$LOGS/serve.log" 2>&1 &
PIDS+=($!)
wait_for 30 "NetPulse readiness" ready
"$NETPULSE" collect --config lab/frr/netpulse.toml --target "$API" --interval 2 >"$LOGS/collect.log" 2>&1 &
PIDS+=($!)
sleep 6

step "Inject 30% packet loss on r1:lnk1 (tc netem); routing still sees the link up"
r1 tc qdisc add dev lnk1 root netem loss 30%
wait_for 90 "NetPulse to drain r1:lnk1" drained
routes
echo "OSPF cost on lnk1: r1=$(r1 vtysh -c 'show ip ospf interface lnk1 json' | grep -o '"cost":[0-9]*' | cut -d: -f2)" \
     "r2=$(docker exec netpulse-lab-r2 vtysh -c 'show ip ospf interface lnk1 json' | grep -o '"cost":[0-9]*' | cut -d: -f2)"
r1 ip route show 10.255.0.2 | grep -q lnk1 && fail "traffic still uses lnk1 after the drain"

step "Repair the link; NetPulse undrains after the soak period"
r1 tc qdisc del dev lnk1 root
wait_for 120 "NetPulse to undrain r1:lnk1" bash -c "! curl -fsS $API/v1/drains | grep -q '\"r1:lnk1\"'"
routes
ecmp || fail "ECMP not restored"

step "Timeline (from the collector)"
grep -E '^T\+' "$LOGS/collect.log"

step "Audit log"
"$NETPULSE" audit verify runtime/frr-lab.audit.jsonl
echo -e "\nPASS: detected, drained, verified, undrained on real routers"
