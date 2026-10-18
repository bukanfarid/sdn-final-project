#!/bin/bash
c() { docker ps --format '{{.Names}}' | grep "\.$1\." | head -1; }
cap() { docker exec $(c $1) timeout 6 tcpdump -nli $2 -c 3 'tcp port 5201' 2>/dev/null | sed "s/^/[$1 $2] /" & }
cap Client1 eth0; cap OVS1 eth1; cap OVS1 eth3
cap OVS2 eth1;    cap OVS2 eth2
cap OVS4 eth1;    cap OVS4 eth3; cap Server1 eth0
sleep 1
docker exec $(c Client1) timeout 4 iperf3 -c 10.1.0.21 -t 2 >/dev/null 2>&1
wait
echo "=== Flow di OVS1 ==="
docker exec $(c OVS1) ovs-ofctl -O OpenFlow13 dump-flows br0
