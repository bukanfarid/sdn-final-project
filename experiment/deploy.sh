#!/bin/bash
# Salin kode controller ke container Ryu
c() { docker ps --format '{{.Names}}' | grep "\.$1\." | head -1; }
docker exec $(c Ryu-1) mkdir -p /app/logs
docker cp ~/sdn-final-project/controller/energy_app.py $(c Ryu-1):/app/energy_app.py
echo "Controller disalin ke $(c Ryu-1)"
