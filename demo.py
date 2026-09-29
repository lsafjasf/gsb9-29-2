"""Demo: route a mixed log stream to three destinations, one of them slow,
and print the routing/drop/latency report. Run: python3 demo.py"""

import json
import random
import time

from logrouter import DropPolicy, LogRouter, Rule


def main():
    router = LogRouter()
    router.add_destination("console", lambda r: None, queue_size=5000)
    router.add_destination("error-file", lambda r: None, queue_size=5000)

    def slow_sink(record):
        time.sleep(0.02)  # 50 records/s max; the burst below overwhelms it

    router.add_destination(
        "slow-remote", slow_sink, queue_size=32, drop_policy=DropPolicy.DROP_NEWEST
    )
    router.add_rule(Rule("errors", "level", "eq", "ERROR",
                         ("error-file", "slow-remote", "console")))
    router.add_rule(Rule("nginx-access", "source", "prefix", "nginx", ("console",)))
    router.add_rule(Rule("http-5xx", "msg", "regex", r"status=5\d\d",
                         ("error-file", "slow-remote")))

    levels = ["INFO"] * 7 + ["WARN"] * 2 + ["ERROR"]
    sources = ["app", "nginx-1", "nginx-2", "cron"]
    for i in range(2000):
        router.route({
            "level": random.choice(levels),
            "source": random.choice(sources),
            "msg": f"request status={random.choice([200, 200, 200, 404, 500, 502])}",
            "seq": i,
        })
    router.close()

    report = router.stats().to_dict()
    print(json.dumps(report, indent=2, sort_keys=True))

    dests = report["destinations"]
    print("\nsummary:")
    print(f"  routed={report['routed']} unmatched={report['unmatched']}")
    for name, d in sorted(dests.items()):
        lat = d["latency"]
        print(f"  {name:12s} enqueued={d['enqueued']:5d} dropped={d['dropped']:5d} "
              f"delivered={d['delivered']:5d} failed={d['failed']} "
              f"avg={lat['avg']*1e3:7.2f}ms p95={lat['p95']*1e3:7.2f}ms "
              f"max={lat['max']*1e3:7.2f}ms")


if __name__ == "__main__":
    main()
