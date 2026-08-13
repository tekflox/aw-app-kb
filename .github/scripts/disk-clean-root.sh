#!/bin/sh
# Runs INSIDE a throwaway container with the host root at /host.
# There is no passwordless sudo on the bare-metal runner, but membership of
# the docker group is root on the host by construction — this is the route.
set +e

echo "-- /var/log before: $(du -sh /host/var/log 2>/dev/null | cut -f1)"
# Rotated copies only; the live files are truncated below so rsyslog keeps
# its file handle instead of writing into a deleted inode.
find /host/var/log -type f \( -name '*.1' -o -name '*.gz' -o -name '*.old' \) -delete
# Archived journals carry '@' in the name; the active ones do not.
find /host/var/log/journal -type f -name '*@*.journal' -delete
: > /host/var/log/syslog
: > /host/var/log/kern.log
echo "-- /var/log after:  $(du -sh /host/var/log 2>/dev/null | cut -f1)"

echo "-- waydroid data: $(du -sh /host/var/lib/waydroid 2>/dev/null | cut -f1)"
rm -rf /host/var/lib/waydroid /host/etc/waydroid /host/usr/share/waydroid \
       /host/home/ubuntu/.local/share/waydroid
echo "-- waydroid purge:"
chroot /host /bin/bash -c "apt-mark unhold waydroid >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get purge -y waydroid 2>&1 | tail -5; DEBIAN_FRONTEND=noninteractive apt-get autoremove -y 2>&1 | tail -2"
echo "-- waydroid binary: $(ls /host/usr/bin/waydroid 2>/dev/null || echo gone)"
