"""演示：路由命中分布、丢弃计数、各目的地延迟。"""
import json
import random
import time

from logrouter import Destination, FieldEquals, FieldPrefix, FieldRegex, Router


class StdoutDestination(Destination):
    def __init__(self, name, delay=0.0):
        self.name = name
        self.delay = delay

    def send(self, record):
        if self.delay:
            time.sleep(self.delay)


def main():
    router = Router(queue_size=64)
    router.add_destination(StdoutDestination("console"), queue_size=4096)
    router.add_destination(StdoutDestination("slow-http", delay=0.02),
                           queue_size=16)
    router.add_rule(FieldEquals("errors", "level", "ERROR",
                                ["console", "slow-http"]))
    router.add_rule(FieldPrefix("nginx", "source", "nginx/", ["console"]))
    router.add_rule(FieldRegex("latency-spike", "msg", r"took \d{4,}ms",
                               ["slow-http"]))

    levels = ["INFO", "INFO", "INFO", "WARN", "ERROR"]
    sources = ["nginx/access", "app", "nginx/error"]
    for i in range(2000):
        router.route({
            "level": random.choice(levels),
            "source": random.choice(sources),
            "msg": "request took %dms" % random.randint(1, 9999),
        })
    router.flush(timeout=10)
    router.close(timeout=1)
    print(json.dumps(router.stats(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
