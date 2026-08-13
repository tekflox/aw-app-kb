#!/bin/sh
set +e
echo "-- waydroid dpkg state:"
chroot /host dpkg -l waydroid 2>&1 | tail -3
echo "-- leftover files:"
ls -d /host/usr/bin/waydroid /host/usr/lib/waydroid /host/var/lib/waydroid /host/etc/waydroid 2>/dev/null || echo "   none of the usual paths remain"
echo "-- is dpkg locked?"
chroot /host fuser -v /var/lib/dpkg/lock-frontend 2>&1 | tail -3
chroot /host ps -eo pid,args 2>/dev/null | grep -E "[a]pt|[d]pkg|unattended" | head -5 || echo "   no apt/dpkg process running"
echo "-- retry purge:"
chroot /host /bin/bash -c "apt-mark unhold waydroid >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get purge -y waydroid 2>&1 | tail -6"
echo "-- final dpkg state:"
chroot /host dpkg -l waydroid 2>&1 | tail -2
