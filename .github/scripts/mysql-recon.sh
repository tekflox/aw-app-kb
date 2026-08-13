#!/bin/sh
set +e
echo "=== containers using crispal-mysql ==="
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -i -E 'mysql|crispal'
echo "=== which container mounts /opt/agentic-workspace/data/crispal-mysql ==="
for c in $(docker ps -a --format '{{.Names}}'); do
  docker inspect "$c" --format '{{.Name}} {{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}' 2>/dev/null | grep -q crispal-mysql && echo "  $c"
done
echo "=== binlog dir ==="
ls -lh /opt/agentic-workspace/data/crispal-mysql/binlog.* 2>/dev/null | awk '{print $5"\t"$9}' | tail -20
du -sh /opt/agentic-workspace/data/crispal-mysql 2>/dev/null
echo "=== index file ==="
cat /opt/agentic-workspace/data/crispal-mysql/binlog.index 2>/dev/null | tail -20
echo "=== compose / run config ==="
grep -rn "crispal-mysql" /opt/agentic-workspace/*.yml /opt/agentic-workspace/docker-compose* 2>/dev/null | head -10
