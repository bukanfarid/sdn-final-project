# energy_app.py — Ryu (OpenFlow 1.3)
# Mode: baseline_sp | baseline_static | energy (konsolidasi adaptif + hysteresis)
import os
import time
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub

# ===== Parameter eksperimen (bisa diubah lewat environment variable) =====
MODE = os.environ.get('MODE', 'energy')
RUN_ID = os.environ.get('RUN_ID', time.strftime('%Y%m%d-%H%M%S'))
LINK_MBPS = float(os.environ.get('LINK_MBPS', '20'))     # kapasitas link bottleneck
INTERVAL = float(os.environ.get('INTERVAL', '1.0'))      # periode polling (detik)
U_HIGH = float(os.environ.get('U_HIGH', '0.7'))          # ambang membangunkan jalur B
U_LOW = float(os.environ.get('U_LOW', '0.4'))            # ambang konsolidasi
K = int(os.environ.get('K', '3'))                        # sampel berturut-turut
MIN_DWELL = float(os.environ.get('MIN_DWELL', '10'))     # jeda minimum antar perubahan (detik)
WAKE_DELAY = float(os.environ.get('WAKE_DELAY', '1.0'))  # emulasi waktu bangun port (detik)
DRAIN = float(os.environ.get('DRAIN', '1.0'))            # jeda sebelum port ditidurkan (detik)
# PLACEHOLDER — ganti dengan nilai dari referensi model daya yang dikutip
P_CHASSIS = float(os.environ.get('P_CHASSIS', '100'))
P_PORT = float(os.environ.get('P_PORT', '1'))
LOG_DIR = os.environ.get('LOG_DIR', '/app/logs')

# ===== Topologi statis (sesuai GNS3) =====
OVS1, OVS2, OVS3, OVS4 = 1, 2, 3, 4
CLIENTS = {'10.1.0.11': 1, '10.1.0.12': 2}   # ip -> port host di OVS1
SERVERS = {'10.1.0.21': 3, '10.1.0.22': 4}   # ip -> port host di OVS4
HOST_PORTS = {OVS1: [1, 2], OVS4: [3, 4]}
EDGE_OUT = {'A': (3, 1), 'B': (4, 2)}        # jalur -> (port OVS1 ke core, port OVS4 ke core)
PATH_PORTS = {
    'A': [(OVS1, 3), (OVS2, 1), (OVS2, 2), (OVS4, 1)],
    'B': [(OVS1, 4), (OVS3, 1), (OVS3, 2), (OVS4, 2)],
}
IPV4 = ether_types.ETH_TYPE_IP


class EnergyAwareApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}
        self.hw = {}            # dpid -> {port_no: hw_addr}
        self.mac_loc = {}       # mac -> (dpid, port)
        self.ready = False
        self.path_up = {'A': True, 'B': True}
        self.pair_path = {}
        self.state = 'INIT'
        self.last_change = 0.0
        self.high_cnt = 0
        self.low_cnt = 0
        self.prev = {}          # (dpid, port) -> (tx_bytes, waktu)
        self.rate = {}          # (dpid, port) -> Mbps
        self.cnt = {'flow_mod': 0, 'port_mod': 0, 'packet_out': 0, 'stats': 0}
        os.makedirs(LOG_DIR, exist_ok=True)
        self.log_path = os.path.join(LOG_DIR, f'{MODE}_{RUN_ID}.csv')
        with open(self.log_path, 'w') as f:
            f.write('t,mode,state,rate_a_mbps,rate_b_mbps,util_a,util_b,'
                    'active_switches,active_ports,power_w,ctrl_msgs,stats_msgs\n')
        hub.spawn(self._monitor)

    # ---------- utilitas OpenFlow ----------
    def _send(self, dp, msg, kind):
        self.cnt[kind] += 1
        dp.send_msg(msg)

    def _flow(self, dp, priority, match, actions):
        p = dp.ofproto_parser
        inst = [p.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        self._send(dp, p.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                    instructions=inst), 'flow_mod')

    def _packet_out(self, dp, port, data):
        p, ofp = dp.ofproto_parser, dp.ofproto
        self._send(dp, p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                      in_port=ofp.OFPP_CONTROLLER,
                                      actions=[p.OFPActionOutput(port)], data=data), 'packet_out')

    # ---------- koneksi switch ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        p, ofp = dp.ofproto_parser, dp.ofproto
        self.dps[dp.id] = dp
        # bersihkan flow lama, lalu table-miss = drop
        self._send(dp, p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                    out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                    match=p.OFPMatch()), 'flow_mod')
        self._flow(dp, 0, p.OFPMatch(), [])
        self._send(dp, p.OFPPortDescStatsRequest(dp, 0), 'stats')

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc(self, ev):
        dp = ev.msg.datapath
        self.hw[dp.id] = {pt.port_no: pt.hw_addr for pt in ev.msg.body}
        if not self.ready and all(d in self.hw for d in (OVS1, OVS2, OVS3, OVS4)):
            self.ready = True
            self._setup()

    def _setup(self):
        # ARP selalu ke controller (hanya di switch edge)
        for dpid in HOST_PORTS:
            dp = self.dps[dpid]
            p, ofp = dp.ofproto_parser, dp.ofproto
            self._flow(dp, 30, p.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP),
                       [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        # pengantaran ke host (statis)
        for dpid, hosts in ((OVS1, CLIENTS), (OVS4, SERVERS)):
            dp = self.dps[dpid]
            p = dp.ofproto_parser
            for ip, port in hosts.items():
                self._flow(dp, 10, p.OFPMatch(eth_type=IPV4, ipv4_dst=ip), [p.OFPActionOutput(port)])
        # switch core: ke client lewat port 1, ke server lewat port 2
        for core in (OVS2, OVS3):
            dp = self.dps[core]
            p = dp.ofproto_parser
            for ip in CLIENTS:
                self._flow(dp, 10, p.OFPMatch(eth_type=IPV4, ipv4_dst=ip), [p.OFPActionOutput(1)])
            for ip in SERVERS:
                self._flow(dp, 10, p.OFPMatch(eth_type=IPV4, ipv4_dst=ip), [p.OFPActionOutput(2)])
        # kondisi awal per mode
        if MODE == 'baseline_sp':
            self._set_path('B', True)
            self._assign('single')
            self.state = 'SP'
        elif MODE == 'baseline_static':
            self._set_path('B', True)
            self._assign('split')
            self.state = 'STATIC'
        else:
            self._assign('single')
            self._set_path('B', False)
            self.state = 'CONSOLIDATED'
        self.last_change = time.time()
        self.logger.info('Setup selesai. MODE=%s, log=%s', MODE, self.log_path)

    # ---------- routing ----------
    def _assign(self, policy):
        for i, c in enumerate(CLIENTS):
            path = 'A' if policy == 'single' else ['A', 'B'][i % 2]
            for s in SERVERS:
                self._route_pair(c, s, path)

    def _route_pair(self, c, s, path):
        self.pair_path[(c, s)] = path
        o1, o4 = EDGE_OUT[path]
        d1, d4 = self.dps[OVS1], self.dps[OVS4]
        p1, p4 = d1.ofproto_parser, d4.ofproto_parser
        # arah balik dulu, lalu arah maju
        self._flow(d4, 20, p4.OFPMatch(eth_type=IPV4, ipv4_src=s, ipv4_dst=c), [p4.OFPActionOutput(o4)])
        self._flow(d1, 20, p1.OFPMatch(eth_type=IPV4, ipv4_src=c, ipv4_dst=s), [p1.OFPActionOutput(o1)])

    def _set_path(self, path, up):
        for dpid, port in PATH_PORTS[path]:
            dp = self.dps[dpid]
            p, ofp = dp.ofproto_parser, dp.ofproto
            hw = self.hw[dpid].get(port)
            if hw is None:
                continue
            cfg = 0 if up else ofp.OFPPC_PORT_DOWN
            self._send(dp, p.OFPPortMod(dp, port, hw, cfg, ofp.OFPPC_PORT_DOWN, 0), 'port_mod')
        self.path_up[path] = up

    # ---------- ARP ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match['in_port']
        eth = packet.Packet(msg.data).get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype != ether_types.ETH_TYPE_ARP:
            return
        if dp.id in HOST_PORTS and in_port in HOST_PORTS[dp.id]:
            self.mac_loc[eth.src] = (dp.id, in_port)
        if eth.dst in self.mac_loc:
            targets = [self.mac_loc[eth.dst]]
        else:
            targets = [(d, pt) for d, ports in HOST_PORTS.items() for pt in ports
                       if (d, pt) != (dp.id, in_port)]
        for d, pt in targets:
            self._packet_out(self.dps[d], pt, msg.data)

    # ---------- monitoring ----------
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        now = time.time()
        dpid = ev.msg.datapath.id
        for st in ev.msg.body:
            key = (dpid, st.port_no)
            if key in self.prev:
                b0, t0 = self.prev[key]
                if now > t0:
                    self.rate[key] = (st.tx_bytes - b0) * 8 / (now - t0) / 1e6
            self.prev[key] = (st.tx_bytes, now)

    def _path_rate(self, path):
        o1, o4 = EDGE_OUT[path]
        return max(self.rate.get((OVS1, o1), 0.0), self.rate.get((OVS4, o4), 0.0))

    def _monitor(self):
        while True:
            if self.ready:
                for dpid in (OVS1, OVS4):
                    dp = self.dps[dpid]
                    self._send(dp, dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY), 'stats')
            hub.sleep(INTERVAL)
            if self.ready:
                if MODE == 'energy':
                    self._decide()
                self._log()

    # ---------- keputusan energy-aware ----------
    def _decide(self):
        now = time.time()
        ua = self._path_rate('A') / LINK_MBPS
        ub = self._path_rate('B') / LINK_MBPS
        dwell_ok = (now - self.last_change) >= MIN_DWELL
        if self.state == 'CONSOLIDATED':
            self.high_cnt = self.high_cnt + 1 if ua > U_HIGH else 0
            if self.high_cnt >= K and dwell_ok:
                self.state, self.high_cnt = 'WAKING', 0
                hub.spawn(self._wake)
        elif self.state == 'SPLIT':
            self.low_cnt = self.low_cnt + 1 if (ua + ub) < U_LOW else 0
            if self.low_cnt >= K and dwell_ok:
                self.state, self.low_cnt = 'SLEEPING', 0
                hub.spawn(self._sleep)

    def _wake(self):
        self._set_path('B', True)
        hub.sleep(WAKE_DELAY)
        self._assign('split')
        self.state, self.last_change = 'SPLIT', time.time()
        self.logger.info('Jalur B dibangunkan, trafik dibagi')

    def _sleep(self):
        self._assign('single')
        hub.sleep(DRAIN)
        self._set_path('B', False)
        self.state, self.last_change = 'CONSOLIDATED', time.time()
        self.logger.info('Trafik dikonsolidasi ke jalur A, jalur B ditidurkan')

    # ---------- energi & log ----------
    def _power(self):
        b = self.path_up['B']
        switches = 3 + (1 if b else 0)
        ports = 4 + len(PATH_PORTS['A']) + (len(PATH_PORTS['B']) if b else 0)
        return switches, ports, switches * P_CHASSIS + ports * P_PORT

    def _log(self):
        ra, rb = self._path_rate('A'), self._path_rate('B')
        sw, pt, pw = self._power()
        ctrl = self.cnt['flow_mod'] + self.cnt['port_mod'] + self.cnt['packet_out']
        with open(self.log_path, 'a') as f:
            f.write(f"{time.time():.3f},{MODE},{self.state},{ra:.3f},{rb:.3f},"
                    f"{ra / LINK_MBPS:.3f},{rb / LINK_MBPS:.3f},{sw},{pt},{pw:.2f},"
                    f"{ctrl},{self.cnt['stats']}\n")
