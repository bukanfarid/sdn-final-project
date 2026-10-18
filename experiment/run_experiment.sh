#!/bin/bash
# run_experiment.sh — menjalankan matriks eksperimen (mode × skenario × pengulangan)
# Pemakaian:
#   ./run_experiment.sh                                    -> 5 run × 3 skenario × 3 mode
#   REPS=1 MODES=energy SCENARIOS=S3 ./run_experiment.sh   -> uji cepat
set -u
REPS=${REPS:-5}
MODES=${MODES:-"baseline_sp baseline_static energy"}
SCENARIOS=${SCENARIOS:-"S1 S2 S3"}
DUR=${DUR:-60}             # durasi pengukuran per run (detik)
WARMUP=${WARMUP:-12}       # jeda setelah controller siap (> MIN_DWELL)
LOW_RATE=${LOW_RATE:-5M}   # trafik rendah (~25% kapasitas 20 Mbps)
BASE=~/sdn-final-project
OUT=$BASE/results/raw

c() { docker ps --format '{{.Names}}' | grep "\.$1\." | head -1; }

# ---- cek semua node berjalan ----
for n in OVS1 OVS2 OVS3 OVS4 Ryu-1 Client1 Client2 Server1 Server2; do
  [ -n "$(c $n)" ] || { echo "ERROR: $n tidak berjalan. Start all di GNS3 dulu."; exit 1; }
done
RYU=$(c Ryu-1); C1=$(c Client1); C2=$(c Client2); SV1=$(c Server1); SV2=$(c Server2)

# ---- persiapan sekali di awal ----
$BASE/experiment/prepare.sh || exit 1
for n in OVS1 OVS2 OVS3 OVS4; do
  docker exec $(c $n) ovs-vsctl set controller br0 max_backoff=1000   # reconnect cepat ke controller
done
mkdir -p $OUT
{ echo "tanggal: $(date)"
  echo "REPS=$REPS DUR=$DUR WARMUP=$WARMUP LOW_RATE=$LOW_RATE"
  echo "MODES=$MODES"; echo "SCENARIOS=$SCENARIOS"; } > $OUT/params.txt

kill_ryu() {
  docker exec $RYU python3 -c "
import os, signal
me = os.getpid()
for p in os.listdir('/proc'):
    if p.isdigit() and int(p) != me:
        try:
            if b'ryu-manager' in open('/proc/%s/cmdline' % p, 'rb').read():
                os.kill(int(p), signal.SIGTERM)
        except Exception:
            pass
"
  sleep 2
}

clean_hosts() {
  for n in $C1 $C2 $SV1 $SV2; do
    docker exec $n sh -c 'pkill iperf3; pkill ping; rm -f /tmp/iperf.json /tmp/ping.txt' 2>/dev/null
  done
}

run_one() {
  local mode=$1 sc=$2 rep=$3
  local id="${sc}_r${rep}" dir="$OUT/$mode/$sc/run$rep"
  rm -rf "$dir"; mkdir -p "$dir"
  echo "[$(date +%T)] mode=$mode skenario=$sc run=$rep"

  kill_ryu; clean_hosts
  docker exec $RYU sh -c "rm -f /app/logs/${mode}_${id}.csv /app/logs/ryu_${mode}_${id}.out"
  docker exec -d $RYU sh -c "cd /app && PYTHONUNBUFFERED=1 MODE=$mode RUN_ID=$id ryu-manager energy_app.py > /app/logs/ryu_${mode}_${id}.out 2>&1"

  local ok=0
  for i in $(seq 1 30); do
    if docker exec $RYU grep -q "Setup selesai" /app/logs/ryu_${mode}_${id}.out 2>/dev/null; then ok=1; break; fi
    sleep 1
  done
  [ $ok -eq 1 ] || { echo "  GAGAL: controller tidak siap, run dilewati"; return 1; }

  docker exec $SV1 iperf3 -s -D
  docker exec $SV2 iperf3 -s -D
  docker exec $C1 ping -c 2 -W 1 10.1.0.21 >/dev/null   # pemanasan ARP
  docker exec $C2 ping -c 2 -W 1 10.1.0.22 >/dev/null
  sleep $WARMUP

  # ping kontinu (delay, jitter, loss) di kedua jalur client
  docker exec -d $C1 sh -c "ping -D -i 0.1 -w $((DUR+2)) 10.1.0.21 > /tmp/ping.txt"
  docker exec -d $C2 sh -c "ping -D -i 0.1 -w $((DUR+2)) 10.1.0.22 > /tmp/ping.txt"

  # pola trafik per skenario
  case $sc in
    S1) docker exec -d $C1 sh -c "iperf3 -c 10.1.0.21 -t $DUR -b $LOW_RATE -J > /tmp/iperf.json" ;;
    S2) docker exec -d $C1 sh -c "iperf3 -c 10.1.0.21 -t $DUR -J > /tmp/iperf.json"
        docker exec -d $C2 sh -c "iperf3 -c 10.1.0.22 -t $DUR -J > /tmp/iperf.json" ;;
    S3) docker exec -d $C1 sh -c "iperf3 -c 10.1.0.21 -t $DUR -b $LOW_RATE -J > /tmp/iperf.json"
        docker exec -d $C2 sh -c "sleep $((DUR/3)); iperf3 -c 10.1.0.22 -t $((DUR/3)) -J > /tmp/iperf.json" ;;
  esac

  # sampling CPU & memori container setiap ~3 detik
  ( end=$((SECONDS+DUR)); while [ $SECONDS -lt $end ]; do
      docker stats --no-stream --format "$(date +%s),{{.Name}},{{.CPUPerc}},{{.MemUsage}}" >> "$dir/docker_stats.csv"
      sleep 3
    done ) &
  local sampler=$!
  sleep $((DUR+4))
  wait $sampler 2>/dev/null

  # kumpulkan hasil
  docker cp $C1:/tmp/iperf.json "$dir/iperf_c1.json" 2>/dev/null
  docker cp $C2:/tmp/iperf.json "$dir/iperf_c2.json" 2>/dev/null
  docker cp $C1:/tmp/ping.txt "$dir/ping_c1.txt" 2>/dev/null
  docker cp $C2:/tmp/ping.txt "$dir/ping_c2.txt" 2>/dev/null
  docker exec $(c OVS1) ovs-ofctl -O OpenFlow13 dump-flows br0 > "$dir/flows_ovs1.txt"
  docker exec $(c OVS4) ovs-ofctl -O OpenFlow13 dump-flows br0 > "$dir/flows_ovs4.txt"
  kill_ryu
  docker cp $RYU:/app/logs/${mode}_${id}.csv "$dir/ctrl.csv" 2>/dev/null
  docker cp $RYU:/app/logs/ryu_${mode}_${id}.out "$dir/ryu.out" 2>/dev/null
}

total=0; fail=0
for rep in $(seq 1 $REPS); do          # pengulangan di luar: mode diselang-seling
  for sc in $SCENARIOS; do
    for mode in $MODES; do
      run_one $mode $sc $rep || fail=$((fail+1))
      total=$((total+1))
    done
  done
done
kill_ryu
echo "Selesai: $total run, $fail gagal. Hasil di $OUT"
