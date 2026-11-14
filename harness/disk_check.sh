#!/usr/bin/env bash
# Diagnose the study host's disk after a suspected drive failure.
#
#   harness/disk_check.sh                 # print a report
#   harness/disk_check.sh > report.txt    # or keep it
#
# Written after the GPU host's NVMe controller went to state=dead mid-run and
# ext4 remounted / as emergency_ro. The interesting question afterwards is not
# "is it mounted" but "did the device come back, and did the kernel see errors
# on the boot that failed" -- and the answers live in places that need no root
# except smartctl.
#
# Everything here is read-only.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/env.sh" 2>/dev/null || true

say() { printf '\n== %s ==\n' "$1"; }

say "host"
echo "$(hostname)  $(uname -r)"
echo "uptime:$(cat /proc/uptime | cut -d' ' -f1) s"

say "mounted filesystems (emergency_ro means ext4 shut itself down)"
grep -vE ' (proc|sysfs|cgroup|cgroup2|devpts|securityfs|pstore|bpf|tracefs|debugfs|hugetlbfs|mqueue|configfs|fusectl|devtmpfs|binfmt_misc) ' /proc/mounts
if grep -q emergency_ro /proc/mounts; then
  echo
  echo "!! a filesystem is mounted emergency_ro / read-only: the kernel or ext4"
  echo "!! aborted it after an I/O error. Reads of uncached pages will return EIO,"
  echo "!! so even binaries start failing to load. Reboot, do not force writes."
fi

say "block devices"
if command -v lsblk >/dev/null 2>&1; then lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT 2>&1; else
  for d in /sys/block/*; do printf '%s size_blocks=' "$(basename "$d")"; cat "$d/size"; done
fi

say "nvme controllers (state=dead or size=0 means the device left the bus)"
for c in /sys/class/nvme/nvme*; do
  [ -d "$c" ] || continue
  printf '%s: state=' "$(basename "$c")"
  cat "$c/state" 2>/dev/null || printf 'unknown\n'
  printf '  model=%s serial=%s\n' "$(cat "$c/model" 2>/dev/null)" "$(cat "$c/serial" 2>/dev/null)"
done
for b in /sys/block/nvme*; do
  [ -d "$b" ] || continue
  printf '%s: size(512b sectors)=%s\n' "$(basename "$b")" "$(cat "$b/size" 2>/dev/null)"
done

say "smartctl"
if command -v smartctl >/dev/null 2>&1; then
  ok=0
  for d in /dev/nvme0n1 /dev/nvme0; do
    [ -e "$d" ] || continue
    if sudo -n smartctl -a "$d" 2>/dev/null; then ok=1; break; fi
    # Some distros allow NVMe SMART reads without root; try the plain call too.
    if smartctl -a "$d" 2>/dev/null; then ok=1; break; fi
  done
  [ "$ok" = 1 ] || echo "smartctl needs root (sudo -n failed). Run: sudo smartctl -a /dev/nvme0n1"
else
  echo "smartctl not installed: sudo dnf install -y smartmontools"
fi

say "kernel messages for the failed boot (previous boot)"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -k -b -1 --no-pager 2>/dev/null \
    | grep -iE "nvme|ext4|i/o error|reset|abort|critical medium|media error" | tail -40 \
    || echo "(previous boot not available, or journalctl needs privileges)"
else
  echo "journalctl unavailable; try: dmesg | grep -iE 'nvme|ext4|error'"
fi

say "kernel messages for this boot"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -k -b 0 --no-pager 2>/dev/null \
    | grep -iE "nvme|ext4|i/o error|recovery|fsck" | tail -30 \
    || echo "(journalctl needs privileges; try: dmesg | tail -60)"
fi

say "filesystem errors recorded at last mount"
for m in / /home /boot; do
  grep -E " $m " /proc/mounts >/dev/null 2>&1 || continue
  dev=$(grep -E " $m " /proc/mounts | head -1 | cut -d' ' -f1)
  echo "$m ($dev):"
  if command -v tune2fs >/dev/null 2>&1; then
    sudo -n tune2fs -l "$dev" 2>/dev/null | grep -iE "filesystem state|mount count|last checked|error" \
      || echo "  (needs root: sudo tune2fs -l $dev)"
  fi
done

say "verdict hints"
echo "  state=dead or size=0        -> the device did not come back; it needs a cold"
echo "                                 power cycle, and if it stays dead, replacement"
echo "  mounted emergency_ro        -> that boot's I/O already failed; reboot and"
echo "                                 fsck before trusting the working tree"
echo "  smartctl reallocated/media  -> replace the drive, do not re-run the study on it"
