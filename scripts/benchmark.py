"""Burst benchmark: fire N distinct events concurrently at a running instance and
report end-to-end latency percentiles plus the per-stage breakdown.

To measure an optimisation, run it twice against differently configured servers, e.g.

    # "naive": big context, many chunks, no trace trimming
    TOP_K=6 MAX_CONTEXT_CHARS=12000 MAX_TRACE_LINES=500 MAX_TRACE_CHARS=20000 uvicorn triage.main:app
    # "optimised" (defaults)
    uvicorn triage.main:app

    python scripts/benchmark.py --n 50 --lines 200
"""
import argparse
import statistics
import time

import httpx


def event(i: int, trace_lines: int) -> dict:
    frames = "\n".join(f"  File \"svc/module_{j}.py\", line {j}, in handler_{j}" for j in range(trace_lines))
    return {
        "service": f"svc-{i % 7}",
        "level": "error",
        "message": f"request {i} failed: connection pool exhausted (too many connections) shard {i}",
        "stack_trace": f"psycopg2.OperationalError: too many connections\n{frames}",
    }


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--lines", type=int, default=150, help="stack frames per event")
    args = ap.parse_args()

    with httpx.Client(timeout=300) as c:
        start = time.perf_counter()
        ids = []
        for i in range(args.n):
            r = c.post(f"{args.url}/webhook/telemetry", json=event(i, args.lines))
            if r.status_code == 202:
                ids.append(r.json()["event_id"])
        results = []
        for event_id in ids:
            while True:
                data = c.get(f"{args.url}/events/{event_id}").json()
                if data["status"] in ("done", "failed"):
                    results.append(data)
                    break
                time.sleep(0.2)
        wall = time.perf_counter() - start

    done = [r for r in results if r["status"] == "done"]
    totals = [r["timings_ms"]["total"] for r in done]
    print(f"events accepted={len(ids)} done={len(done)} wall={wall:.1f}s throughput={len(done) / wall:.2f}/s")
    if totals:
        print(f"end-to-end ms: p50={pct(totals, .5):.0f} p95={pct(totals, .95):.0f} mean={statistics.mean(totals):.0f}")
        for stage in ("queue_wait", "retrieval", "inference", "dispatch"):
            print(f"  {stage:<11} mean={statistics.mean(r['timings_ms'][stage] for r in done):.0f} ms")
        print(f"  llm={sum(r['source'] == 'llm' for r in done)} fallback={sum(r['source'] == 'fallback' for r in done)}"
              f"  avg context chars={statistics.mean(r['context_chars'] for r in done):.0f}")


if __name__ == "__main__":
    main()
