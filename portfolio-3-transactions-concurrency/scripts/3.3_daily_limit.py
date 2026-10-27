"""Experiment 3.3: the $50 daily limit under two simultaneous purchases (write skew).

The player has spent $40 today and makes two $8 purchases at the same moment.
Each one checks "today's spend + $8 <= $50?" and then inserts a NEW purchase
row. The two writes never touch the same row, so nobody overwrites anybody:
each decision is right on its own, and together they break the rule.

  run 1  sum, then insert                          READ COMMITTED  -> $56
  run 2  sum with FOR UPDATE, then insert          READ COMMITTED  -> $56 (the new rows are phantoms)
  run 3  lock the player's row first, in a
         SECURITY DEFINER procedure                READ COMMITTED  -> second refused
  run 4  as run 1                                  SERIALIZABLE    -> 40001, retry, second refused

The player is a real one (SteelSniper642); the purchases go into a scratch
table (lab_purchase) because purchase rows can never be deleted (P1). In
run 3 the player row is only locked, never changed. The scratch table and
procedure are removed at the end.

    make p3 EXP=3.3_daily_limit
"""
import argparse
import threading
import time

import psycopg
from psycopg import errors

PLAYER = "56f78d43-a6e5-4870-9a87-1c5d42c8ed9f"   # SteelSniper642, us-east
LIMIT, SPENT, EACH = 5000, 4000, 800
T0 = time.monotonic()

SPEND_SQL = """SELECT coalesce(sum(total_cents), 0) FROM lab_purchase
               WHERE payer_id = %s AND created_at >= date_trunc('day', now())"""
INSERT_SQL = "INSERT INTO lab_purchase (payer_id, total_cents, buyer) VALUES (%s, %s, %s)"

# Run 3's guard. The app role may not lock player rows (FOR UPDATE needs the
# UPDATE privilege, and P1 grants the app only SELECT/INSERT on player), so
# the check runs as the owner, like sign_up (D15). Locking the player's
# current row makes every purchase by that player queue behind the previous one.
GUARDED_BUY = f"""
CREATE OR REPLACE PROCEDURE lab_buy_guarded(p_player UUID, p_cents INT8, p_buyer STRING)
LANGUAGE PLpgSQL SECURITY DEFINER AS $$
DECLARE
    v_version UUID;
    v_spent   INT8;
BEGIN
    SELECT p.id INTO v_version FROM player p
    WHERE p.root_id = p_player
      AND NOT EXISTS (SELECT 1 FROM player n WHERE n.previous_id = p.id)
    FOR UPDATE;
    SELECT coalesce(sum(total_cents), 0) INTO v_spent FROM lab_purchase
    WHERE payer_id = p_player AND created_at >= date_trunc('day', now());
    IF v_spent + p_cents > {LIMIT} THEN
        RAISE EXCEPTION 'daily limit: spent % + % > {LIMIT}', v_spent, p_cents USING ERRCODE = '23514';
    END IF;
    INSERT INTO lab_purchase (payer_id, total_cents, buyer) VALUES (p_player, p_cents, p_buyer);
END
$$"""


def log(who, msg):
    print(f"  {(time.monotonic() - T0) * 1000:7.1f} ms  [{who}] {msg}", flush=True)


def usd(cents):
    return f"${cents / 100:.2f}"


def session(dsn, isolation):
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET ROLE squadfire_app")
    conn.isolation = isolation
    return conn


def begin(conn, who):
    conn.execute(f"BEGIN ISOLATION LEVEL {conn.isolation}")
    log(who, f"BEGIN ({conn.isolation})")


def check(who, spent):
    ok = spent + EACH <= LIMIT
    log(who, f"spent today {usd(spent)} + {usd(EACH)} = {usd(spent + EACH)} -> {'OK' if ok else 'over the limit'}")
    return ok


def buy(conn, who):
    conn.execute(INSERT_SQL, (PLAYER, EACH, who))
    log(who, f"INSERT purchase {usd(EACH)}")
    conn.execute("COMMIT")
    log(who, "COMMIT -> purchase accepted")


def refuse(conn, who):
    conn.execute("ROLLBACK")
    log(who, "refused -> ROLLBACK, nothing charged")


# ------------------------------------------------------------------ the runs

def run_sum_then_insert(dsn, isolation, lock_rows=False):
    """Runs 1, 2 and 4: both read, both decide, both write."""
    a, b = session(dsn, isolation), session(dsn, isolation)
    for conn, who in ((a, "A"), (b, "B")):
        begin(conn, who)
    spent = {}
    if lock_rows:
        # FOR UPDATE cannot lock a sum, so lock the rows being summed.
        sql = """SELECT total_cents FROM lab_purchase
                 WHERE payer_id = %s AND created_at >= date_trunc('day', now()) FOR UPDATE"""
        spent["A"] = sum(r[0] for r in a.execute(sql, (PLAYER,)))
        log("A", f"SELECT today's purchases FOR UPDATE -> {usd(spent['A'])} (those rows locked)")
        a.execute(INSERT_SQL, (PLAYER, EACH, "A")) if check("A", spent["A"]) else None
        log("A", f"INSERT purchase {usd(EACH)} (a new row: not one of the locked ones)")

        def buyer_b():
            log("B", "SELECT today's purchases FOR UPDATE ... waiting for A's locks")
            t = time.monotonic()
            spent["B"] = sum(r[0] for r in b.execute(sql, (PLAYER,)))
            log("B", f"... after {(time.monotonic() - t) * 1000:.0f} ms -> {usd(spent['B'])}")
            if check("B", spent["B"]):
                buy(b, "B")
            else:
                refuse(b, "B")

        tb = threading.Thread(target=buyer_b)
        tb.start()
        time.sleep(0.5)
        a.execute("COMMIT")
        log("A", "COMMIT -> purchase accepted")
        tb.join()
        return

    for conn, who in ((a, "A"), (b, "B")):
        spent[who] = conn.execute(SPEND_SQL, (PLAYER,)).fetchone()[0]
        log(who, f"SELECT sum(today) -> {usd(spent[who])}")
    for conn, who in ((a, "A"), (b, "B")):
        if not check(who, spent[who]):
            refuse(conn, who)
            continue
        try:
            buy(conn, who)
        except errors.SerializationFailure as e:
            log(who, f"ERROR 40001 {e.diag.message_primary.split(':')[0]}")
            conn.execute("ROLLBACK")
            log(who, "ROLLBACK; retrying the whole purchase")
            begin(conn, who)
            again = conn.execute(SPEND_SQL, (PLAYER,)).fetchone()[0]
            log(who, f"SELECT sum(today) -> {usd(again)}")
            buy(conn, who) if check(who, again) else refuse(conn, who)


def run_guarded(dsn):
    """Run 3: lock the player's row, then check and insert, all inside one procedure."""
    a, b = session(dsn, "READ COMMITTED"), session(dsn, "READ COMMITTED")
    call = "CALL lab_buy_guarded(%s, %s, %s)"
    begin(a, "A")
    a.execute(call, (PLAYER, EACH, "A"))
    log("A", "CALL lab_buy_guarded: player row locked, spent $40.00 + $8.00 OK, purchase inserted")

    def buyer_b():
        begin(b, "B")
        log("B", "CALL lab_buy_guarded ... waiting for the player row")
        t = time.monotonic()
        try:
            b.execute(call, (PLAYER, EACH, "B"))
            log("B", "CALL lab_buy_guarded: purchase inserted")
            b.execute("COMMIT")
            log("B", "COMMIT -> purchase accepted")
        except errors.CheckViolation as e:
            log("B", f"... after {(time.monotonic() - t) * 1000:.0f} ms -> ERROR 23514 {e.diag.message_primary}")
            refuse(b, "B")

    tb = threading.Thread(target=buyer_b)
    tb.start()
    time.sleep(0.5)
    a.execute("COMMIT")
    log("A", "COMMIT -> purchase accepted")
    tb.join()


# ------------------------------------------------------------------ harness

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    args = ap.parse_args()

    owner = psycopg.connect(args.dsn, autocommit=True)
    owner.execute("""CREATE TABLE IF NOT EXISTS lab_purchase (
                         id UUID PRIMARY KEY DEFAULT gen_random_uuid(), payer_id UUID NOT NULL,
                         total_cents INT8 NOT NULL, buyer STRING, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    # UPDATE is only so run 2 may take FOR UPDATE locks on the scratch rows
    owner.execute("GRANT SELECT, INSERT, UPDATE ON lab_purchase TO squadfire_app")
    owner.execute(GUARDED_BUY)
    owner.execute("GRANT EXECUTE ON PROCEDURE lab_buy_guarded TO squadfire_app")

    runs = [
        (1, "sum, then insert                     (READ COMMITTED)",
         lambda: run_sum_then_insert(args.dsn, "READ COMMITTED")),
        (2, "sum with FOR UPDATE, then insert     (READ COMMITTED)",
         lambda: run_sum_then_insert(args.dsn, "READ COMMITTED", lock_rows=True)),
        (3, "lock the player row first (procedure) (READ COMMITTED)", lambda: run_guarded(args.dsn)),
        (4, "sum, then insert                     (SERIALIZABLE)",
         lambda: run_sum_then_insert(args.dsn, "SERIALIZABLE")),
    ]
    results = []
    try:
        for n, title, fn in runs:
            owner.execute("DELETE FROM lab_purchase WHERE true")
            owner.execute(INSERT_SQL + ", (%s, %s, %s)", (PLAYER, SPENT // 2, "earlier", PLAYER, SPENT // 2, "earlier"))
            print(f"\n== Run {n}: {title}\n   spent today {usd(SPENT)} (2 earlier purchases), limit {usd(LIMIT)}, "
                  f"two {usd(EACH)} purchases at once")
            fn()
            total, accepted = owner.execute(
                "SELECT sum(total_cents), count(*) FILTER (WHERE buyer <> 'earlier') FROM lab_purchase "
                "WHERE payer_id = %s", (PLAYER,)).fetchone()
            verdict = "LIMIT BROKEN" if total > LIMIT else "correct"
            print(f"   result: {accepted} of 2 accepted, spent today = {usd(total)}  -> {verdict}")
            results.append((n, title, accepted, total, verdict))
    finally:
        owner.execute("DROP PROCEDURE IF EXISTS lab_buy_guarded")
        owner.execute("DROP TABLE IF EXISTS lab_purchase")

    print(f"\n== Summary (spent {usd(SPENT)}, limit {usd(LIMIT)}, two {usd(EACH)} purchases at once)")
    print(f"   {'run':<4} {'how':<56} {'accepted':>8} {'spent':>7}  verdict")
    for n, title, accepted, total, verdict in results:
        print(f"   {n:<4} {title:<56} {accepted:>8} {usd(total):>7}  {verdict}")


if __name__ == "__main__":
    main()
