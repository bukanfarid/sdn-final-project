#!/bin/bash
# Set MTU 1500 dan matikan offload di semua interface container GNS3
for n in $(docker ps --format '{{.Names}}' | grep '^GNS3\.'); do
  pid=$(docker inspect -f '{{.State.Pid}}' "$n")
  for i in $(sudo nsenter -t "$pid" -n ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep '^eth'); do
    sudo nsenter -t "$pid" -n ip link set "$i" mtu 1500
    if sudo nsenter -t "$pid" -n ethtool -K "$i" tx off rx off tso off gso off gro off 2>/tmp/ethtool.err; then
      echo "OK   $n $i (mtu 1500)"
    else
      echo "GAGAL $n $i: $(cat /tmp/ethtool.err)"
    fi
  done
done
