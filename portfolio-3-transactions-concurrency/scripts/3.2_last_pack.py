"""Experiment 3.2: two players race for the last limited-drop pack.

limited_drop.remaining is the one value updated in place (P1, D7), so it is
where a lost update can happen. A test drop with 1 pack left is created in
us-east; two sessions (A and B, both as squadfire_app) try to buy it. The
steps are forced into the worst order (both read, then both write) so the
race happens on every run.

  run 1  read, then write remaining = 0           READ COMMITTED   -> oversold
  run 2  SELECT ... FOR UPDATE, then write        READ COMMITTED   -> one sale
  run 3  UPDATE remaining = remaining - 1
         WHERE remaining > 0 (no separate read)   READ COMMITTED   -> one sale
  run 4  as run 1                                 SERIALIZABLE     -> 40001, retry, one sale

Each sale is recorded in a scratch table (lab_drop_sale), so the database
itself counts how many packs were sold. The test drop and scratch table are
removed at the end, leaving the generated data as it was.

    make p3 EXP=3.2_last_pack
"""
import argparse
import threading
import time

import psycopg
from psycopg import errors

T0 = time.monotonic()


def log(who, msg):
    print(f"  {(time.monotonic() - T0) * 1000:7.1f} ms  [{who}] {msg}", flush=True)


def session(dsn, isolation):
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET ROLE squadfire_app")
    conn.isolation = isolation
    return conn


def begin(conn, who):
    conn.execute(f"BEGIN ISOLATION LEVEL {conn.isolation}")
    log(who, f"BEGIN ({conn.isolation})")


def sell(conn, who, drop_id):
    conn.execute("INSERT INTO lab_drop_sale (drop_id, buyer) VALUES (%s, %s)", (drop_id, who))
    conn.execute("COMMIT")
    log(who, "COMMIT -> bought the pack")


# ------------------------------------------------------------------ the runs

def run_read_then_write(dsn, drop_id, isolation):
    """Runs 1 and 4: the buggy code. Read the counter, decide in the app, write the result."""
    a, b = session(dsn, isolation), session(dsn, isolation)
    for conn, who in ((a, "A"), (b, "B")):
        begin(conn, who)
    seen = {}
    for conn, who in ((a, "A"), (b, "B")):
        seen[who] = conn.execute("SELECT remaining FROM limited_drop WHERE id = %s", (drop_id,)).fetchone()[0]
        log(who, f"SELECT remaining -> {seen[who]}   (\"one left, I can buy it\")")
    for conn, who in ((a, "A"), (b, "B")):
        try:
            conn.execute("UPDATE limited_drop SET remaining = %s WHERE id = %s", (seen[who] - 1, drop_id))
            log(who, f"UPDATE remaining = {seen[who] - 1}")
            sell(conn, who, drop_id)
        except errors.SerializationFailure as e:
            log(who, f"ERROR 40001 {e.diag.message_primary.split(':')[0]}")
            conn.execute("ROLLBACK")
            log(who, "ROLLBACK; retrying the whole purchase")
            retry_once(conn, who, drop_id)


def retry_once(conn, who, drop_id):
    begin(conn, who)
    left = conn.execute("SELECT remaining FROM limited_drop WHERE id = %s", (drop_id,)).fetchone()[0]
    log(who, f"SELECT remaining -> {left}")
    if left > 0:
        conn.execute("UPDATE limited_drop SET remaining = %s WHERE id = %s", (left - 1, drop_id))
        sell(conn, who, drop_id)
    else:
        conn.execute("ROLLBACK")
        log(who, "sold out -> ROLLBACK, nothing charged")


def run_for_update(dsn, drop_id):
    """Run 2: lock the row on read. B's read waits until A has finished."""
    a, b = session(dsn, "READ COMMITTED"), session(dsn, "READ COMMITTED")
    begin(a, "A")
    left = a.execute("SELECT remaining FROM limited_drop WHERE id = %s FOR UPDATE", (drop_id,)).fetchone()[0]
    log("A", f"SELECT remaining FOR UPDATE -> {left}   (row locked)")

    def buyer_b():
        begin(b, "B")
        log("B", "SELECT remaining FOR UPDATE ... waiting for A's lock")
        t = time.monotonic()
        left_b = b.execute("SELECT remaining FROM limited_drop WHERE id = %s FOR UPDATE", (drop_id,)).fetchone()[0]
        log("B", f"... got the lock after {(time.monotonic() - t) * 1000:.0f} ms -> {left_b}")
        if left_b > 0:
            b.execute("UPDATE limited_drop SET remaining = %s WHERE id = %s", (left_b - 1, drop_id))
            sell(b, "B", drop_id)
        else:
            b.execute("ROLLBACK")
            log("B", "sold out -> ROLLBACK, nothing charged")

    tb = threading.Thread(target=buyer_b)
    tb.start()
    time.sleep(0.5)                      # B is now blocked on the lock
    a.execute("UPDATE limited_drop SET remaining = %s WHERE id = %s", (left - 1, drop_id))
    log("A", f"UPDATE remaining = {left - 1}")
    sell(a, "A", drop_id)
    tb.join()


def run_one_step(dsn, drop_id):
    """Run 3: check and decrement in one statement; no separate read to go stale."""
    a, b = session(dsn, "READ COMMITTED"), session(dsn, "READ COMMITTED")
    sql = "UPDATE limited_drop SET remaining = remaining - 1 WHERE id = %s AND remaining > 0 RETURNING remaining"
    begin(a, "A")
    row = a.execute(sql, (drop_id,)).fetchone()
    log("A", f"UPDATE ... WHERE remaining > 0 -> 1 row, remaining now {row[0]}")

    def buyer_b():
        begin(b, "B")
        log("B", "UPDATE ... WHERE remaining > 0 ... waiting for A")
        t = time.monotonic()
        rows = b.execute(sql, (drop_id,)).fetchall()
        log("B", f"... after {(time.monotonic() - t) * 1000:.0f} ms -> {len(rows)} rows (re-checked remaining > 0 on A's result)")
        if rows:
            sell(b, "B", drop_id)
        else:
            b.execute("ROLLBACK")
            log("B", "sold out -> ROLLBACK, nothing charged")

    tb = threading.Thread(target=buyer_b)
    tb.start()
    time.sleep(0.5)
    sell(a, "A", drop_id)
    tb.join()


# ------------------------------------------------------------------ harness

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    args = ap.parse_args()

    owner = psycopg.connect(args.dsn, autocommit=True)
    owner.execute("CREATE TABLE IF NOT EXISTS lab_drop_sale (drop_id UUID, buyer STRING, run INT)")
    owner.execute("GRANT SELECT, INSERT ON lab_drop_sale TO squadfire_app")
    drop_id = owner.execute("""
        INSERT INTO limited_drop (crdb_region, sku, price_cents, total_packs, remaining, starts_at, ends_at)
        VALUES ('us-east', 'AMMO-AP-60', 299, 1, 1, now(), now() + INTERVAL '1 hour')
        RETURNING id""").fetchone()[0]

    runs = [
        (1, "read, then write the app's value  (READ COMMITTED)",
         lambda: run_read_then_write(args.dsn, drop_id, "READ COMMITTED")),
        (2, "SELECT ... FOR UPDATE              (READ COMMITTED)", lambda: run_for_update(args.dsn, drop_id)),
        (3, "one-step UPDATE ... WHERE > 0      (READ COMMITTED)", lambda: run_one_step(args.dsn, drop_id)),
        (4, "read, then write the app's value  (SERIALIZABLE)",
         lambda: run_read_then_write(args.dsn, drop_id, "SERIALIZABLE")),
    ]
    results = []
    try:
        for n, title, fn in runs:
            owner.execute("UPDATE limited_drop SET remaining = 1 WHERE id = %s", (drop_id,))
            owner.execute("DELETE FROM lab_drop_sale WHERE drop_id = %s", (drop_id,))
            print(f"\n== Run {n}: {title}\n   drop has 1 pack left")
            fn()
            sold = owner.execute("SELECT count(*), string_agg(buyer, ', ' ORDER BY buyer) FROM lab_drop_sale "
                                 "WHERE drop_id = %s", (drop_id,)).fetchone()
            left = owner.execute("SELECT remaining FROM limited_drop WHERE id = %s", (drop_id,)).fetchone()[0]
            verdict = "OVERSOLD" if sold[0] > 1 else "correct"
            print(f"   result: packs sold = {sold[0]} ({sold[1]}), remaining = {left}  -> {verdict}")
            results.append((n, title, sold[0], left, verdict))
    finally:
        owner.execute("DELETE FROM limited_drop WHERE id = %s", (drop_id,))
        owner.execute("DROP TABLE lab_drop_sale")

    print("\n== Summary (1 pack available each run)")
    print(f"   {'run':<4} {'how':<52} {'sold':>4} {'left':>4}  verdict")
    for n, title, sold, left, verdict in results:
        print(f"   {n:<4} {title:<52} {sold:>4} {left:>4}  {verdict}")


if __name__ == "__main__":
    main()
