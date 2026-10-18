#!/bin/bash
# Variabel dari GNS3: MGMT_IP (mis. 10.0.0.11/24), DPID (mis. 0000000000000001), CONTROLLER (mis. tcp:10.0.0.100:6653)
mkdir -p /var/run/openvswitch /etc/openvswitch
[ -f /etc/openvswitch/conf.db ] || ovsdb-tool create /etc/openvswitch/conf.db /usr/share/openvswitch/vswitch.ovsschema
/usr/share/openvswitch/scripts/ovs-ctl start --system-id=random

[ -n "$MGMT_IP" ] && ip addr add "$MGMT_IP" dev eth0 && ip link set eth0 up

ovs-vsctl --may-exist add-br br0 \
  -- set bridge br0 protocols=OpenFlow13 fail_mode=secure \
  -- set bridge br0 other-config:datapath-id=${DPID:-0000000000000001}

for i in 1 2 3 4; do
  if [ -e /sys/class/net/eth$i ]; then
    ip link set eth$i up
    ovs-vsctl --may-exist add-port br0 eth$i -- set interface eth$i ofport_request=$i
  fi
done

[ -n "$CONTROLLER" ] && ovs-vsctl set-controller br0 "$CONTROLLER"
echo "OVS siap. DPID=$DPID CONTROLLER=$CONTROLLER"
exec bash
