"""Experiment 3.5: a gift whose two writes live in two regions.

A gift writes the purchase in the PAYER's region and the ammo credit in the
RECIPIENT's region (P1: REGIONAL BY ROW, derived from the owner). Both must
commit or neither. CockroachDB's atomic commit across ranges is parallel
commits: provisional writes (intents) on every range, plus one transaction
record whose state (STAGING -> COMMITTED or ABORTED) decides them all.

  run 1  local gift          eu-west payer -> eu-west friend   1 region leads
  run 2  cross-region gift   eu-west payer -> af-west friend   2 regions lead
  run 3  cross-region gift, the af-west write fails            nothing kept anywhere
  timing 20 of each, through the eu-west node

The players are real (BoldCobra432 and two friends). The writes go to two
scratch tables with the same placement as purchase and inventory, because
those tables never allow deletes (P1). Both are removed at the end.

    make p3 EXP=3.5_cross_region_gift
"""
import argparse
import re
import statistics
import time

import psycopg
from psycopg import errors

PAYER = "BoldCobra432"
SKU, UNITS, CENTS = "AMMO-556-120", 120, 219


def friends(conn):
    """The payer and one ACCEPTED friend in eu-west and one in af-west."""
    rows = conn.execute("""
        WITH f AS (SELECT player_low AS a, player_high AS b FROM current_friendship WHERE status = 'ACCEPTED'
                   UNION ALL
                   SELECT player_high, player_low FROM current_friendship WHERE status = 'ACCEPTED')
        SELECT p.player_id, p.home_region::STRING, q.player_id, q.handle, q.home_region::STRING
        FROM current_player p JOIN f ON f.a = p.player_id JOIN current_player q ON q.player_id = f.b
        WHERE p.handle = %s ORDER BY q.handle""", (PAYER,)).fetchall()
    payer, payer_region = rows[0][0], rows[0][1]
    pick = {}
    for _, _, fid, fhandle, fregion in rows:
        pick.setdefault(fregion, (fid, fhandle))
    return payer, payer_region, pick["eu-west"], pick["af-west"]


def setup(owner):
    owner.execute("""CREATE TABLE lab_gift_purchase (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        crdb_region crdb_internal_region NOT NULL,          -- payer's region, as purchase
        payer_id UUID NOT NULL, recipient_id UUID NOT NULL, total_cents INT8 NOT NULL
    ) LOCALITY REGIONAL BY ROW""")
    owner.execute("""CREATE TABLE lab_gift_inventory (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        crdb_region crdb_internal_region NOT NULL,          -- owner's region, as inventory
        player_id UUID NOT NULL, sku STRING NOT NULL, change INT NOT NULL CHECK (change > 0),
        purchase_id UUID NOT NULL
    ) LOCALITY REGIONAL BY ROW""")


def leaseholder(conn, table, region):
    return conn.execute(f"""SELECT lease_holder, lease_holder_locality
        FROM [SHOW RANGE FROM TABLE {table} FOR ROW ('{region}', gen_random_uuid())]""").fetchone()


def wait_for_placement(conn, nodes, timeout=120):
    """Put each region's rows under a leaseholder in that region.

    A new REGIONAL BY ROW table is split by region within seconds, but the
    allocator applies the lease preference (lease_preferences = [[+region=…]])
    from a background scanner that visits each range about every 10 minutes.
    Instead of waiting, ask for the move the zone config already prescribes.
    """
    t = time.monotonic()
    want = [(tbl, r) for tbl in ("lab_gift_purchase", "lab_gift_inventory") for r in ("eu-west", "af-west")]
    node_in = {region: n for n, region in nodes.items()}
    while True:
        placed = {(tbl, r): leaseholder(conn, tbl, r) for tbl, r in want}
        if all(f"region={r}," in loc for (tbl, r), (_, loc) in placed.items()):
            print(f"   placement ready after {time.monotonic() - t:.0f} s:")
            for (tbl, r), (node, loc) in placed.items():
                print(f"     {tbl:<20} {r:<8} rows -> leaseholder n{node} ({loc.split(',')[0]})")
            return
        if time.monotonic() - t > timeout:
            raise SystemExit("leaseholders did not move to their regions in time")
        time.sleep(5)
        for (tbl, r), (_, loc) in placed.items():
            if f"region={r}," not in loc:
                conn.execute(f"""ALTER RANGE RELOCATE LEASE TO {node_in['region=' + r]} FOR
                    SELECT range_id FROM [SHOW RANGE FROM TABLE {tbl} FOR ROW ('{r}', gen_random_uuid())]""")


def gift(conn, payer, payer_region, friend, friend_region, change=UNITS):
    conn.execute("BEGIN")
    pid = conn.execute("""INSERT INTO lab_gift_purchase (crdb_region, payer_id, recipient_id, total_cents)
                          VALUES (%s, %s, %s, %s) RETURNING id""",
                       (payer_region, payer, friend, CENTS)).fetchone()[0]
    conn.execute("""INSERT INTO lab_gift_inventory (crdb_region, player_id, sku, change, purchase_id)
                    VALUES (%s, %s, %s, %s, %s)""", (friend_region, friend, SKU, change, pid))
    conn.execute("COMMIT")
    return pid


def traced_gift(conn, table_ids, nodes, *args):
    """Run one gift with KV tracing; keep the writes to the lab tables and the commit."""
    conn.execute("SET tracing = kv")
    gift(conn, *args)
    conn.execute("SET tracing = off")
    lines, current = [], None
    for (msg,) in conn.execute("SELECT message FROM [SHOW KV TRACE FOR SESSION]"):
        m = re.match(r"key: /Table/(\d+)/", msg)
        if m:
            current = table_ids.get(int(m.group(1)))
        m = re.match(r"(r\d+): sending batch (.+) to \(n(\d+),", msg)
        if m and current and ("Put" in m.group(2) or "EndTxn" in m.group(2)):
            lines.append(f"     {current:<20} {m.group(1):<6} {m.group(2):<22} -> n{m.group(3)} ({nodes[int(m.group(3))]})")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26258/squadfire?sslmode=disable",
                    help="the eu-west node: the payer's region is the gateway")
    ap.add_argument("--repeat", type=int, default=20)
    args = ap.parse_args()

    conn = psycopg.connect(args.dsn, autocommit=True)
    payer, payer_region, (eu_id, eu_name), (af_id, af_name) = friends(conn)
    gateway = conn.execute("SELECT gateway_region()").fetchone()[0]
    nodes = {n: loc.split(",")[0] for n, loc in conn.execute(
        "SELECT node_id, locality FROM crdb_internal.gossip_nodes")}
    print(f"== Setup\n   payer {PAYER} ({payer_region}), friends {eu_name} (eu-west) and {af_name} (af-west)")
    print(f"   gateway node region: {gateway}")

    conn.execute("DROP TABLE IF EXISTS lab_gift_inventory")
    conn.execute("DROP TABLE IF EXISTS lab_gift_purchase")
    setup(conn)
    try:
        wait_for_placement(conn, nodes)
        table_ids = {i: n for n, i in conn.execute(
            "SELECT name, id FROM system.namespace WHERE name LIKE 'lab_gift_%'")}

        print(f"\n== Run 1: local gift, {PAYER} (eu-west) -> {eu_name} (eu-west)")
        print("   writes and commit, as the gateway sent them:")
        print("\n".join(traced_gift(conn, table_ids, nodes, payer, payer_region, eu_id, "eu-west")))

        print(f"\n== Run 2: cross-region gift, {PAYER} (eu-west) -> {af_name} (af-west)")
        print("   writes and commit, as the gateway sent them:")
        print("\n".join(traced_gift(conn, table_ids, nodes, payer, payer_region, af_id, "af-west")))

        print(f"\n== Run 3: cross-region gift, the af-west ammo write fails")
        conn.execute("BEGIN")
        pid = conn.execute("""INSERT INTO lab_gift_purchase (crdb_region, payer_id, recipient_id, total_cents)
                              VALUES (%s, %s, %s, %s) RETURNING id""",
                           (payer_region, payer, af_id, CENTS)).fetchone()[0]
        seen = conn.execute("SELECT count(*) FROM lab_gift_purchase WHERE id = %s", (pid,)).fetchone()[0]
        print(f"   eu-west purchase written (provisional); visible inside the transaction: {seen} row")
        try:
            conn.execute("""INSERT INTO lab_gift_inventory (crdb_region, player_id, sku, change, purchase_id)
                            VALUES ('af-west', %s, %s, -120, %s)""", (af_id, SKU, pid))
        except errors.CheckViolation as e:
            print(f"   af-west ammo write rejected: ERROR 23514 {e.diag.message_primary}")
        conn.execute("ROLLBACK")
        left_p = conn.execute("SELECT count(*) FROM lab_gift_purchase WHERE id = %s", (pid,)).fetchone()[0]
        left_i = conn.execute("SELECT count(*) FROM lab_gift_inventory WHERE purchase_id = %s", (pid,)).fetchone()[0]
        print(f"   after ROLLBACK: purchase rows in eu-west = {left_p}, ammo rows in af-west = {left_i}"
              f"  -> {'nothing kept, as required' if left_p == left_i == 0 else 'PARTIAL GIFT'}")

        print(f"\n== Timing: {args.repeat} gifts each, through the {gateway} node")
        timings = {}
        for label, friend, region in (("local (eu-west -> eu-west)", eu_id, "eu-west"),
                                      ("cross-region (eu-west -> af-west)", af_id, "af-west")):
            ms = []
            for _ in range(args.repeat):
                t = time.perf_counter()
                gift(conn, payer, payer_region, friend, region)
                ms.append((time.perf_counter() - t) * 1000)
            timings[label] = ms
            q = statistics.quantiles(ms, n=10)
            print(f"   {label:<34} median {statistics.median(ms):5.1f} ms   p90 {q[-1]:5.1f} ms")
        rows = conn.execute("SELECT count(*) FROM lab_gift_purchase").fetchone()[0]
        credits = conn.execute("SELECT count(*) FROM lab_gift_inventory").fetchone()[0]
        print(f"   every gift complete: {rows} purchases, {credits} ammo credits")
    finally:
        conn.execute("DROP TABLE IF EXISTS lab_gift_inventory")
        conn.execute("DROP TABLE IF EXISTS lab_gift_purchase")


if __name__ == "__main__":
    main()
