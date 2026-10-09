#!/bin/sh
# Give the Docker-assigned ethN interfaces stable names (lnk1/lnk2, by subnet)
# so the FRR config and NetPulse topology can refer to them, then start FRR.
set -eu
rename() {
    subnet_prefix=$1 name=$2
    dev=$(ip -o -4 addr show | awk -v p="$subnet_prefix" '$4 ~ "^"p {print $2; exit}')
    [ -n "$dev" ] || { echo "no interface in $subnet_prefix" >&2; exit 1; }
    [ "$dev" = "$name" ] && return
    ip link set dev "$dev" down
    ip link set dev "$dev" name "$name"
    ip link set dev "$name" up
}
rename 10.0.1. lnk1
rename 10.0.2. lnk2
ip addr add "$LO_ADDR" dev lo 2>/dev/null || true
install -o frr -g frr -m 640 /lab/frr.conf /etc/frr/frr.conf
install -o frr -g frr -m 640 /lab/vtysh.conf /etc/frr/vtysh.conf
exec /sbin/tini -- /usr/lib/frr/docker-start
