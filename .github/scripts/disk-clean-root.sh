#!/bin/sh
set +e
echo "-- leftover waydroid dir: $(du -sh /host/var/lib/waydroid 2>/dev/null | cut -f1)"
rm -rf /host/var/lib/waydroid
echo "-- after: $(ls -d /host/var/lib/waydroid 2>/dev/null || echo gone)"
echo "-- /var/log now: $(du -sh /host/var/log 2>/dev/null | cut -f1)"
echo "-- journal now:  $(du -sh /host/var/log/journal 2>/dev/null | cut -f1)"
