"""Experiment 5.1a: CockroachDB changefeed -> Kafka.

The changefeed (created once, see the README) streams every row of
purchase, purchase_line, inventory and item into Kafka topics
squadfire.<table>, starting with an initial scan of the existing history.

  1  the changefeed job and its state
  2  completeness: Kafka row messages per topic == CockroachDB rows
     (resolved markers counted separately)
  3  a live change: deactivate an item, time commit -> message in Kafka,
     then reactivate it (item is the one mutable table of the four; the
     history tables never allow UPDATE, P1)

    make p5 EXP=5.1a_changefeed
"""
import argparse
import json
import time
import uuid

import psycopg
from confluent_kafka import Consumer, TopicPartition

TABLES = ["purchase", "purchase_line", "inventory", "item"]
SKU = "SKIN-GOLD-DEAGLE"


def consumer(bootstrap):
    return Consumer({"bootstrap.servers": bootstrap, "group.id": f"p5-check-{uuid.uuid4()}",
                     "auto.offset.reset": "earliest", "enable.auto.commit": False})


def count_topic(bootstrap, topic):
    """Read a topic from the start to its current end: row messages vs resolved markers."""
    c = consumer(bootstrap)
    parts = c.list_topics(topic).topics[topic].partitions
    ends = {p: c.get_watermark_offsets(TopicPartition(topic, p))[1] for p in parts}
    c.assign([TopicPartition(topic, p, 0) for p in parts])
    rows = resolved = 0
    keys = set()
    done = {p for p, end in ends.items() if end == 0}
    while len(done) < len(parts):
        for m in c.consume(5000, timeout=5):
            if m.error():
                continue
            body = json.loads(m.value())
            if "resolved" in body:
                resolved += 1
            else:
                rows += 1
                keys.add(m.key())
            if m.offset() + 1 >= ends[m.partition()]:
                done.add(m.partition())
    c.close()
    return rows, len(keys), resolved


def live_change(conn, bootstrap, active):
    """Change one item and wait for its message; return (latency ms, message)."""
    topic = "squadfire.item"
    c = consumer(bootstrap)
    parts = c.list_topics(topic).topics[topic].partitions
    c.assign([TopicPartition(topic, p, c.get_watermark_offsets(TopicPartition(topic, p))[1]) for p in parts])
    c.poll(0.5)                                      # finish joining before the change
    t = time.perf_counter()
    conn.execute("UPDATE item SET active = %s WHERE sku = %s", (active, SKU))
    committed = time.perf_counter()
    while time.perf_counter() - committed < 60:
        m = c.poll(1)
        if m is None or m.error():
            continue
        body = json.loads(m.value())
        if body.get("after", {}).get("sku") == SKU:
            arrived = time.perf_counter()
            c.close()
            return (committed - t) * 1000, (arrived - committed) * 1000, m.partition(), m.offset(), body
    raise SystemExit("change did not arrive in Kafka within 60 s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    ap.add_argument("--kafka", default="localhost:29092")
    args = ap.parse_args()
    conn = psycopg.connect(args.dsn, autocommit=True)

    print("== 1. The changefeed job")
    job = conn.execute("""SELECT job_id, status, description, high_water_timestamp
                          FROM [SHOW CHANGEFEED JOBS] WHERE status = 'running'""").fetchone()
    print(f"   job {job[0]}: {job[1]}")
    print(f"   {job[2]}")
    hw = conn.execute("SELECT now() - %s::DECIMAL::INT8::TIMESTAMPTZ", (str(job[3]).split(".")[0][:10],)).fetchone()[0]
    print(f"   high-water mark (everything up to here is in Kafka): {hw} behind now")

    print("\n== 2. Completeness: Kafka vs CockroachDB")
    print(f"   {'topic':<26} {'row msgs':>9} {'distinct keys':>14} {'resolved':>9} {'table rows':>11}  match")
    for t in TABLES:
        rows, keys, resolved = count_topic(args.kafka, f"squadfire.{t}")
        in_db = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"   squadfire.{t:<16} {rows:>9,} {keys:>14,} {resolved:>9} {in_db:>11,}  {keys == in_db}")
    print("   (row messages can exceed distinct keys when a row changed after the initial scan;")
    print("    for the history tables every row is written once, so the two are equal)")

    print(f"\n== 3. A live change: {SKU}")
    for active in (False, True):
        commit_ms, lag_ms, part, off, body = live_change(conn, args.kafka, active)
        print(f"   UPDATE item SET active = {str(active).lower()}  (commit {commit_ms:.0f} ms)")
        print(f"     -> squadfire.item partition {part} offset {off}, {lag_ms:.0f} ms after commit: "
              f"active={str(body['after']['active']).lower()}, updated={body['updated'][:19]}")
    print("   the item is back to active; both versions stay in Kafka, so the lake keeps the history")


if __name__ == "__main__":
    main()
