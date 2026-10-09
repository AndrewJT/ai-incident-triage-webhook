# Out-of-memory kills (OOM)

## Symptoms
`java.lang.OutOfMemoryError: Java heap space`, `OOMKilled` pod status with exit code 137, "Out of memory: Killed process",
container restart loops, or steadily growing memory before a crash. GC overhead and long pauses may precede it.

## Diagnosis
- Check memory graphs for a sawtooth or monotonic growth (leak) versus a sudden spike (large request or batch).
- Inspect container memory limits versus JVM / runtime heap settings.
- Capture a heap dump or profile before restarting, if possible.

## Remediation
1. Restart or reschedule the affected pods to restore service.
2. Raise the memory limit or heap size as a temporary mitigation.
3. Capture a heap dump from a surviving instance and compare against a healthy baseline.
4. Roll back the latest release if memory growth began after a deploy.
5. Add a memory-usage alert at 80% of the limit.
