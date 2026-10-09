# Database connection pool exhaustion

## Symptoms
Errors such as "too many connections", "remaining connection slots are reserved", "connection pool exhausted",
"QueuePool limit of size reached", or `OperationalError` / `PoolTimeout` from the database driver.
Request latency climbs, then requests fail with 500/503. Often follows a traffic spike or a leaked connection.

## Diagnosis
- Compare active connections with the configured `max_connections` on the database.
- Look for long-running or idle-in-transaction sessions holding connections.
- Check whether a recent deploy removed a `close()` / context manager around DB sessions.

## Remediation
1. Terminate idle-in-transaction sessions older than 5 minutes (`pg_terminate_backend`).
2. Restart the leaking service instances one at a time to release held connections.
3. Temporarily raise the pool size or add a pooler (PgBouncer) if the database has headroom.
4. Roll back the most recent deploy if the leak started right after it.
5. Open a follow-up to add connection-leak detection and pool-usage alerts.
