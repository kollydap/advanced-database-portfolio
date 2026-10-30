"""Experiment 5.1b: replay the game servers' telemetry into Kafka.

The generator wrote 30 days of telemetry (shots, hits, kills, sessions,
store views) to data-gen/out/telemetry.jsonl. Telemetry never goes into
CockroachDB (D8): game servers publish it straight to Kafka. This replays
the file into topic squadfire.telemetry, keyed by player, so one player's
events stay in order in one partition.

    make p5 EXP=5.1b_replay_telemetry
"""
import argparse
import json
import time
from collections import Counter
from pathlib import Path

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic

TOPIC = "squadfire.telemetry"
FILE = Path(__file__).resolve().parents[2] / "data-gen" / "out" / "telemetry.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kafka", default="localhost:29092")
    args = ap.parse_args()

    admin = AdminClient({"bootstrap.servers": args.kafka})
    if TOPIC in admin.list_topics(timeout=10).topics:
        raise SystemExit(f"{TOPIC} already exists; replaying again would duplicate every event")
    admin.create_topics([NewTopic(TOPIC, num_partitions=3, replication_factor=1)])[TOPIC].result()

    p = Producer({"bootstrap.servers": args.kafka, "linger.ms": 50, "compression.type": "lz4",
                  "enable.idempotence": True})
    kinds, t = Counter(), time.monotonic()
    with open(FILE) as f:
        for line in f:
            e = json.loads(line)
            kinds[e["event"]] += 1
            while True:
                try:
                    p.produce(TOPIC, key=e["player"] if "player" in e else e.get("match"), value=line.rstrip("\n"))
                    break
                except BufferError:
                    p.poll(0.1)
            p.poll(0)
    p.flush()
    total = sum(kinds.values())
    print(f"== Replayed {total:,} events into {TOPIC} in {time.monotonic() - t:.1f} s")
    for kind, n in kinds.most_common():
        print(f"   {kind:<15} {n:>8,}")


if __name__ == "__main__":
    main()
