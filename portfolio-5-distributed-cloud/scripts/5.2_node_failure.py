"""Experiment 5.2: one CockroachDB node crashes, then two do.

Every range keeps 3 replicas, one per node (one per region), and Raft
commits a write once a majority (2 of 3) has it. The database survives
region failure (P1), so losing one region must not stop the store.

  part A  kill the af-west node (SIGKILL, like pulling the plug)
          1 before: af-west rows are led by the af-west node
          2 the first af-west write after the crash: how long it waits
          3 while down: writes and reads in all three regions still work
          4 the node comes back and catches up on what it missed
  part B  kill eu-west AND af-west: one node of three left, no majority
          writes and reads stop (time out) rather than answer wrongly;
          both nodes return and nothing written before is lost

Writes go to a scratch table placed like purchase (REGIONAL BY ROW),
because the real tables never allow deletes (P1). It is removed at the end.

    make p5 EXP=5.2_node_failure
"""
import argparse
import statistics
import subprocess
import time
from pathlib import Path

import psycopg
from psycopg import errors

ROOT = Path(__file__).resolve().parents[2]
REGIONS = ("us-east", "eu-west", "af-west")
SERVICE = {"us-east": "crdb-useast", "eu-west": "crdb-euwest", "af-west": "crdb-afwest"}
PORT = {"us-east": 26257, "eu-west": 26258, "af-west": 26259}


def dsn(region):
    return f"postgresql://root@localhost:{PORT[region]}/squadfire?sslmode=disable&connect_timeout=5"


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True, cwd=ROOT, capture_output=True)


def live(conn):
    return {n: (loc.split(",")[0].split("=")[1], is_live) for n, loc, is_live in conn.execute(
        "SELECT node_id, locality, is_live FROM crdb_internal.gossip_nodes ORDER BY 1")}


def show_live(conn):
    print("   " + "   ".join(f"n{n} {r}: {'live' if ok else 'DOWN'}" for n, (r, ok) in live(conn).items()))


def wait_live(conn, want, timeout=120):
    t = time.monotonic()
    while sum(ok for _, ok in live(conn).values()) != want:
        if time.monotonic() - t > timeout:
            raise SystemExit(f"cluster did not reach {want} live nodes")
        time.sleep(1)
    return time.monotonic() - t


def underreplicated(conn):
    return conn.execute("""SELECT sum((metrics->>'ranges.underreplicated')::INT8)::INT8,
                                  sum((metrics->>'ranges.unavailable')::INT8)::INT8
                           FROM crdb_internal.kv_store_status""").fetchone()


def range_of(conn, region):
    return conn.execute(f"""SELECT range_id, lease_holder, replicas
        FROM [SHOW RANGE FROM TABLE lab_order FOR ROW ('{region}', gen_random_uuid())]""").fetchone()


def place_leases(conn, nodes, timeout=120):
    """Each region's rows led from that region (see 3.5: ask for the move instead of waiting)."""
    node_in = {r: n for n, (r, _) in nodes.items()}
    t = time.monotonic()
    while True:
        wrong = [r for r in REGIONS if range_of(conn, r)[1] != node_in[r]]
        if not wrong:
            return
        if time.monotonic() - t > timeout:
            raise SystemExit("leaseholders did not move to their regions in time")
        for r in wrong:
            try:
                conn.execute(f"""ALTER RANGE RELOCATE LEASE TO {node_in[r]} FOR
                    SELECT range_id FROM [SHOW RANGE FROM TABLE lab_order FOR ROW ('{r}', gen_random_uuid())]""")
            except psycopg.Error:
                pass
        time.sleep(3)


def order(conn, region, note):
    t = time.perf_counter()
    conn.execute("INSERT INTO lab_order (crdb_region, note) VALUES (%s, %s)", (region, note))
    return (time.perf_counter() - t) * 1000


def counts(conn):
    return dict(conn.execute("SELECT crdb_region::STRING, count(*) FROM lab_order GROUP BY 1 ORDER BY 1").fetchall())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=20)
    args = ap.parse_args()

    conn = psycopg.connect(dsn("us-east"), autocommit=True)
    print("== Setup: the cluster, through the us-east node")
    show_live(conn)
    nodes = live(conn)
    if not all(ok for _, ok in nodes.values()):
        raise SystemExit("start all three nodes first: docker compose start crdb-useast crdb-euwest crdb-afwest")
    conn.execute("DROP TABLE IF EXISTS lab_order")
    conn.execute("""CREATE TABLE lab_order (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        crdb_region crdb_internal_region NOT NULL,
        note STRING NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    ) LOCALITY REGIONAL BY ROW""")
    stopped = []
    try:
        place_leases(conn, nodes)
        print("   lab_order: one range per region, each with a replica on every node")
        for r in REGIONS:
            rid, lh, reps = range_of(conn, r)
            print(f"     {r:<8} rows  range r{rid:<4} replicas {sorted(reps)}  leaseholder n{lh} ({nodes[lh][0]})")
        for r in REGIONS:
            for _ in range(5):
                order(conn, r, "before")
        print(f"   5 orders written per region before the crash: {counts(conn)}")
        u, unavail = underreplicated(conn)
        print(f"   under-replicated ranges: {u}, unavailable ranges: {unavail}")

        # ---------------------------------------------------------------- part A
        print("\n== Part A, step 2: kill the af-west node (SIGKILL) and write an af-west order at once")
        compose("kill", SERVICE["af-west"])
        stopped.append("af-west")
        t0 = time.monotonic()
        oid, tries = conn.execute("SELECT gen_random_uuid()").fetchone()[0], 0
        while True:
            tries += 1
            try:
                conn.execute("INSERT INTO lab_order (id, crdb_region, note) VALUES (%s, 'af-west', 'first after crash')",
                             (oid,))
                print(f"   attempt {tries}: committed")
                break
            except errors.StatementCompletionUnknown as e:
                print(f"   attempt {tries}: ERROR 40003 result is ambiguous ({e.diag.message_primary.split(':')[0]}),"
                      f" after {time.monotonic() - t0:.1f} s")
                print("     the gateway cannot tell whether the dead leaseholder committed it, so the app")
                print("     checks by the order's own id before retrying (never blindly insert again)")
                if conn.execute("SELECT count(*) FROM lab_order WHERE id = %s", (oid,)).fetchone()[0]:
                    print("     it had committed: done, no duplicate")
                    break
                print("     not there: safe to retry the same id")
            except errors.UniqueViolation:
                print(f"   attempt {tries}: already there (the ambiguous attempt had committed): no duplicate")
                break
        print(f"   the first af-west order was safely in after {time.monotonic() - t0:.1f} s")
        print("   (its leaseholder was gone: the survivors waited out its lease, then n1/n2 took over)")
        print(f"   af-west rows now led by n{range_of(conn, 'af-west')[1]}"
              f" ({nodes[range_of(conn, 'af-west')[1]][0]})")
        wait_live(conn, 2)
        print(f"   n3 marked not live after {time.monotonic() - t0:.0f} s:")
        show_live(conn)

        print(f"\n== Part A, step 3: the store keeps working with af-west down ({args.repeat} orders per region)")
        for r in REGIONS:
            lat = [order(conn, r, "during outage") for _ in range(args.repeat)]
            print(f"   {r:<8} orders: all {args.repeat} committed, median {statistics.median(lat):5.1f} ms")
        real = conn.execute("""SELECT count(*) FROM purchase WHERE crdb_region = 'af-west'""").fetchone()[0]
        print(f"   real af-west history still readable: {real:,} purchase rows")
        t = time.monotonic()                                          # the metric refreshes every ~10 s
        while not underreplicated(conn)[0] and time.monotonic() - t < 60:
            time.sleep(2)
        u, unavail = underreplicated(conn)
        print(f"   under-replicated ranges: {u:,} (each has 2 of 3 copies left), unavailable ranges: {unavail}")
        print("   with 3 nodes there is nowhere to make a third copy; with 5+ the cluster would")
        print("   re-replicate after server.time_until_store_dead (5 min)")
        during = counts(conn)
        print(f"   orders so far: {during}")

        print("\n== Part A, step 4: restart af-west and let it catch up")
        compose("start", SERVICE["af-west"])
        print(f"   n3 live again after {wait_live(conn, 3):.0f} s")
        stopped.remove("af-west")
        t = time.monotonic()
        while underreplicated(conn)[0] and time.monotonic() - t < 300:
            time.sleep(2)
        u, unavail = underreplicated(conn)
        print(f"   under-replicated ranges back to {u} after {time.monotonic() - t:.0f} s more (Raft log catch-up)")
        af = psycopg.connect(dsn("af-west"), autocommit=True)
        missed = af.execute("SELECT count(*) FROM lab_order WHERE note <> 'before'").fetchone()[0]
        print(f"   read through the af-west node itself: {counts(af)}")
        print(f"   it has all {missed} orders written while it was dead -> {'caught up' if counts(af) == during else 'MISSING'}")
        af.close()

        # ---------------------------------------------------------------- part B
        print("\n== Part B: kill eu-west AND af-west; only us-east is left (1 of 3, no majority)")
        place_leases(conn, live(conn))
        compose("kill", SERVICE["eu-west"], SERVICE["af-west"])
        stopped += ["eu-west", "af-west"]
        stuck = conn.execute("SELECT gen_random_uuid()").fetchone()[0]
        conn.execute("SET statement_timeout = '8s'")
        for label, sql in (("write a us-east order",
                            f"INSERT INTO lab_order (id, crdb_region, note) VALUES ('{stuck}', 'us-east', 'no quorum')"),
                           ("read us-east orders", "SELECT count(*) FROM lab_order WHERE crdb_region = 'us-east'"),
                           ("read the item catalogue (GLOBAL)", "SELECT count(*) FROM item")):
            t = time.monotonic()
            try:
                conn.execute(sql)
                print(f"   {label:<34} answered in {time.monotonic() - t:.1f} s")
            except errors.QueryCanceled:
                print(f"   {label:<34} no answer after {time.monotonic() - t:.1f} s: ERROR 57014 statement timeout")
        print("   the us-east node is up and accepts the connection, but cannot reach a majority,")
        print("   so it refuses to guess: consistency over availability (CP)")

        print("\n== Part B: restart both nodes")
        compose("start", SERVICE["eu-west"], SERVICE["af-west"])
        conn.execute("SET statement_timeout = '0'")
        t = time.monotonic()
        while True:
            try:
                order(conn, "us-east", "after quorum back")
                break
            except psycopg.OperationalError:
                time.sleep(1)
        print(f"   first write succeeded {time.monotonic() - t:.0f} s after restart")
        wait_live(conn, 3)
        stopped.clear()
        show_live(conn)
        final = counts(conn)
        late = conn.execute("SELECT count(*) FROM lab_order WHERE id = %s", (stuck,)).fetchone()[0]
        print(f"   orders: {final}")
        if late:
            print("   the order that timed out IS there: its write was already in the us-east Raft log and")
            print("   committed as soon as a majority returned. A timeout means 'unknown', not 'failed';")
            print("   like the ambiguous error in part A, the app must check by id before retrying")
        else:
            print("   the order that timed out never committed")
        ok = final == {**during, "us-east": during["us-east"] + 1 + late}
        print(f"   every order written before the outage is still there, none twice: {ok}")
    finally:
        if stopped:
            compose("start", *(SERVICE[r] for r in set(stopped)))
            conn = psycopg.connect(dsn("us-east"), autocommit=True)
            wait_live(conn, 3)
        conn.execute("SET statement_timeout = '0'")
        conn.execute("DROP TABLE IF EXISTS lab_order")


if __name__ == "__main__":
    main()
