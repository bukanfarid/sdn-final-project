#!/bin/bash
# Bottleneck 20 Mbps di link menuju core (kedua arah)
c() { docker ps --format '{{.Names}}' | grep "\.$1\." | head -1; }
for i in eth3 eth4; do
  docker exec $(c OVS1) tc qdisc replace dev $i root tbf rate 20mbit burst 16kb latency 50ms
done
for i in eth1 eth2; do
  docker exec $(c OVS4) tc qdisc replace dev $i root tbf rate 20mbit burst 16kb latency 50ms
done
echo "Bottleneck 20 Mbps terpasang"
