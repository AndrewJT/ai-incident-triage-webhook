# Disk full

## Symptoms
"No space left on device", `ENOSPC`, "disk usage above 90%", write failures, databases going read-only,
log rotation failing, or pods evicted for ephemeral-storage pressure.

## Diagnosis
- Identify the largest directories (`du -xh --max-depth=2 /var | sort -h`).
- Check for runaway log files, core dumps, or unrotated temp files.
- Check for deleted files still held open by a process (`lsof +L1`).

## Remediation
1. Free space immediately: compress or delete old logs and temp files.
2. Restart the process holding deleted-but-open files.
3. Expand the volume if the growth is legitimate.
4. Fix log rotation or retention so usage stays below 80%.
5. Add a disk-usage alert at 80% and 90%.
