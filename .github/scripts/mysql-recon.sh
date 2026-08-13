#!/bin/sh
set +e
C=aw-custom-crispal-db
PW=$(docker inspect "$C" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MYSQL_ROOT_PASSWORD=' | cut -d= -f2-)
[ -z "$PW" ] && { echo "FATAL: no MYSQL_ROOT_PASSWORD in the container env"; exit 1; }
q() { docker exec "$C" mysql -uroot -p"$PW" -N -B -e "$1" 2>&1 | grep -v "password on the command line"; }

echo "=== current binlog settings ==="
q "SELECT @@log_bin AS log_bin, @@binlog_expire_logs_seconds AS expire_secs, @@max_binlog_size AS max_size;"
echo "=== replicas attached? (purging is unsafe if any) ==="
REPL=$(q "SHOW REPLICAS;")
if [ -n "$REPL" ]; then echo "REPLICAS PRESENT:"; echo "$REPL"; else echo "none"; fi
echo "=== binlog files now ==="
q "SHOW BINARY LOGS;" | tail -5
echo "=== data dir: $(du -sh /opt/agentic-workspace/data/crispal-mysql 2>/dev/null | cut -f1) ==="

if [ "$APPLY" = "true" ]; then
  if [ -n "$REPL" ]; then
    echo "!! replicas attached — setting expiry but NOT purging"
  else
    echo "=== purging binlogs older than 3 days ==="
    # This is the DEV clone (production is sapatariacrispal.com, hosted
    # elsewhere), so binlogs here buy no point-in-time recovery — the ~1.9 G/day
    # is dominated by full-DB dev restores writing themselves into the log.
    q "PURGE BINARY LOGS BEFORE DATE_SUB(NOW(), INTERVAL 3 DAY);"
  fi
  echo "=== persisting expiry = 3 days ==="
  # SET PERSIST writes mysqld-auto.cnf into the data dir, which is a bind
  # mount — so this survives both a restart and a container recreate. There
  # is no compose file for this container to edit instead.
  q "SET PERSIST binlog_expire_logs_seconds = 259200;"
  echo "=== after ==="
  q "SELECT @@binlog_expire_logs_seconds AS expire_secs;"
  q "SHOW BINARY LOGS;" | tail -4
  echo "data dir: $(du -sh /opt/agentic-workspace/data/crispal-mysql 2>/dev/null | cut -f1)"
  df -h / | tail -1
fi
