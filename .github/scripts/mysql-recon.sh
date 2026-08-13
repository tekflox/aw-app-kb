#!/bin/sh
set +e
echo "=== BEFORE ==="; df -h / | tail -1; docker system df
echo "=== what will go: stopped containers ==="
docker ps -a --filter status=exited --format '{{.Names}}\t{{.Status}}' | head -20
echo "   total stopped: $(docker ps -aq --filter status=exited | wc -l)"
echo "=== unused images ==="
docker images --format '{{.Size}}\t{{.Repository}}:{{.Tag}}' | sort -rh | head -10
if [ "$APPLY" = "true" ]; then
  echo "=== prune ==="
  docker system prune -a -f 2>&1 | tail -6
  echo "=== AFTER ==="; df -h / | tail -1; docker system df
fi
