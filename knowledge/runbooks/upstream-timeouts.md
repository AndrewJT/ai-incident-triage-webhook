# Upstream dependency timeouts

## Symptoms
`ReadTimeout`, `ConnectTimeout`, "context deadline exceeded", "upstream request timeout", 504 Gateway Timeout,
or "connection refused" when calling another internal service or third-party API. Retries amplify load.

## Diagnosis
- Check the health and latency dashboards of the upstream service.
- Verify DNS, network policy and recent infrastructure changes between the services.
- Look for retry storms: many identical requests in a short window.

## Remediation
1. Confirm whether the upstream is down; if so, page its owning team.
2. Enable the circuit breaker or degrade gracefully (cached or default responses).
3. Reduce client retries and add jittered exponential backoff.
4. Scale out the upstream service if it is saturated.
5. If a third-party API is down, switch to the fallback provider or queue requests.
