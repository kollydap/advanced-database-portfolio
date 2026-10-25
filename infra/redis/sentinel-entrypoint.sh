#!/bin/sh
# Sentinel rewrites its own config file, so build a fresh writable one per container.
cat > /tmp/sentinel.conf <<EOF
port 26379
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel monitor squadfire-master redis-master 6379 2
sentinel down-after-milliseconds squadfire-master 5000
sentinel failover-timeout squadfire-master 10000
sentinel parallel-syncs squadfire-master 1
EOF
until redis-cli -h redis-master ping >/dev/null 2>&1; do sleep 1; done
exec redis-sentinel /tmp/sentinel.conf
