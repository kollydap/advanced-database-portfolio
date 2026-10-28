"""Experiment 4.3: the player graph in Neo4j.

Builds (:Player) nodes, FRIENDS_WITH links (accepted friendships) and GIFTED
links (one per payer -> recipient pair, with count and total) from
CockroachDB, then asks two graph questions:

  1  "people you may know": friends of friends who are not friends yet,
     ranked by mutual friends; the same question in SQL for comparison
  2  gift farming: accounts that never play, have one friend, and only
     gift to that friend, feeding one receiver. Run on the real data, then
     with a planted, labelled test ring (:TestRing) that is removed after.
     Bonus: three-way gift loops.

D8 feeds the graph from the lake's gold layer (portfolio 5); until then it
is loaded from CockroachDB directly. Cypher goes through cypher-shell inside
the Neo4j container, so no extra Python dependency is needed.

    make p4 EXP=4.3_player_graph
"""
import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
BATCH = 1000
REPEAT = 10


def cypher(statements, fmt="plain"):
    out = subprocess.run(["docker", "compose", "exec", "-T", "neo4j", "cypher-shell", "--format", fmt],
                         input=statements, capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise SystemExit(out.stderr or out.stdout)
    return out.stdout


def show(title, query):
    print(f"\n   {title}")
    print("   " + "\n   ".join(query.strip().splitlines()))
    lines = cypher(query + ";").strip().splitlines()
    for line in lines or ["(no rows)"]:
        print("     " + line)


def load(conn):
    players = conn.execute("""
        SELECT cp.player_id::STRING, cp.handle, cp.home_region::STRING, count(mp.match_id)::INT
        FROM current_player cp LEFT JOIN match_player mp ON mp.player_id = cp.player_id
        GROUP BY 1, 2, 3""").fetchall()
    friends = conn.execute("""
        SELECT player_low::STRING, player_high::STRING, since::STRING
        FROM current_friendship WHERE status = 'ACCEPTED'""").fetchall()
    gifts = conn.execute("""
        SELECT payer_id::STRING, recipient_id::STRING, count(*)::INT, sum(total_cents)::INT
        FROM current_purchase WHERE payer_id <> recipient_id AND status = 'SUCCEEDED'
        GROUP BY 1, 2""").fetchall()

    t = time.monotonic()
    stmts = ["MATCH (n) DETACH DELETE n;",
             "CREATE CONSTRAINT player_id IF NOT EXISTS FOR (p:Player) REQUIRE p.id IS UNIQUE;",
             "CREATE INDEX player_handle IF NOT EXISTS FOR (p:Player) ON (p.handle);"]
    batches = [
        (players, "CREATE (:Player {id: r[0], handle: r[1], region: r[2], matches: r[3]})"),
        (friends, "MATCH (a:Player {id: r[0]}), (b:Player {id: r[1]}) "
                  "CREATE (a)-[:FRIENDS_WITH {since: datetime(replace(r[2], ' ', 'T'))}]->(b)"),
        (gifts, "MATCH (a:Player {id: r[0]}), (b:Player {id: r[1]}) "
                "CREATE (a)-[:GIFTED {count: r[2], total_cents: r[3]}]->(b)"),
    ]
    for rows, body in batches:
        for i in range(0, len(rows), BATCH):
            stmts.append(f"UNWIND {json.dumps([list(r) for r in rows[i:i + BATCH]])} AS r {body};")
    cypher("\n".join(stmts))
    print(f"== Load from CockroachDB ({time.monotonic() - t:.1f} s)")
    print(f"   {len(players):,} players, {len(friends):,} accepted friendships, "
          f"{len(gifts):,} gift pairs ({sum(g[2] for g in gifts):,} gifts)")
    show("check:", """
MATCH (p:Player) WITH count(p) AS players
MATCH ()-[f:FRIENDS_WITH]->() WITH players, count(f) AS friends_with
MATCH ()-[g:GIFTED]->() RETURN players, friends_with, count(g) AS gifted""")


def neo4j_ms(query):
    """Server-side time (planning + streaming) of REPEAT runs, from cypher-shell's verbose footer."""
    out = cypher(";\n".join([query] * REPEAT) + ";", fmt="verbose")
    times = [int(a) + int(b) for a, b in re.findall(r"after (\d+) ms, results consumed after another (\d+) ms", out)]
    return statistics.median(times[1:])           # the first run includes planning the query


def sql_ms(conn, query, args):
    ms = []
    for _ in range(REPEAT):
        t = time.perf_counter()
        conn.execute(query, args).fetchall()
        ms.append((time.perf_counter() - t) * 1000)
    return statistics.median(ms[1:])


SQL_PYMK = """
WITH f AS (SELECT player_low AS a, player_high AS b FROM current_friendship WHERE status = 'ACCEPTED'
           UNION ALL
           SELECT player_high, player_low FROM current_friendship WHERE status = 'ACCEPTED')
SELECT s.handle, count(DISTINCT f1.b) AS mutual
FROM f f1 JOIN f f2 ON f2.a = f1.b JOIN current_player s ON s.player_id = f2.b
JOIN current_player me ON me.player_id = f1.a
WHERE me.handle = %s AND f2.b <> f1.a
  AND NOT EXISTS (SELECT 1 FROM f f3 WHERE f3.a = f1.a AND f3.b = f2.b)
GROUP BY s.handle ORDER BY mutual DESC, s.handle LIMIT 5"""


def people_you_may_know(conn):
    print("\n== Question 1: people you may know")
    best = cypher("""
MATCH (me:Player)-[:FRIENDS_WITH]-(f)-[:FRIENDS_WITH]-(s)
WHERE s <> me AND NOT (me)-[:FRIENDS_WITH]-(s)
WITH me, s, count(DISTINCT f) AS mutual ORDER BY mutual DESC, me.handle LIMIT 1
RETURN me.handle;""").strip().splitlines()[-1].strip('"')
    print(f"   player: {best} (the player with the strongest suggestion in the graph)")
    pymk = f"""
MATCH (me:Player {{handle: "{best}"}})-[:FRIENDS_WITH]-(friend)-[:FRIENDS_WITH]-(suggestion)
WHERE suggestion <> me AND NOT (me)-[:FRIENDS_WITH]-(suggestion)
RETURN suggestion.handle AS suggestion, suggestion.region AS region,
       count(DISTINCT friend) AS mutual, collect(DISTINCT friend.handle) AS through
ORDER BY mutual DESC, suggestion LIMIT 5"""
    show("Cypher:", pymk)
    in_sql = conn.execute(SQL_PYMK, (best,)).fetchall()
    print("\n   the same question in SQL (friendship joined with itself, both directions):")
    print("   " + "\n   ".join(SQL_PYMK.strip().splitlines()))
    for handle, mutual in in_sql:
        print(f"     {handle:<20} {mutual}")
    print(f"\n   time, median of {REPEAT - 1} runs (server side for Neo4j, client side for SQL):")
    print(f"     Neo4j        {neo4j_ms(pymk):6.1f} ms")
    print(f"     CockroachDB  {sql_ms(conn, SQL_PYMK, (best,)):6.1f} ms")


FARMING = """
MATCH (feeder:Player)-[g:GIFTED]->(receiver:Player)
WHERE feeder.matches = 0
  AND COUNT { (feeder)-[:FRIENDS_WITH]-() } = 1
  AND COUNT { (feeder)-[:GIFTED]->() } = 1
WITH receiver, collect(feeder.handle) AS feeders, sum(g.count) AS gifts, sum(g.total_cents) AS cents
WHERE size(feeders) >= 3
RETURN receiver.handle AS receiver, size(feeders) AS feeders_n, gifts, cents, feeders"""

RING = """
CREATE (boss:Player:TestRing {id: "test-ring-boss", handle: "RingBoss_TEST", region: "af-west", matches: 12})
WITH boss
UNWIND range(1, 5) AS i
CREATE (f:Player:TestRing {id: "test-ring-" + i, handle: "RingFeeder" + i + "_TEST", region: "af-west", matches: 0})
CREATE (f)-[:FRIENDS_WITH {since: datetime()}]->(boss)
CREATE (f)-[:GIFTED {count: 3, total_cents: 897}]->(boss)"""


def gift_farming():
    print("\n== Question 2: gift farming")
    print("   rule: feeders who never play, have exactly one friend, and gift only to that friend;")
    print("         flag a receiver with 3 or more such feeders")
    show("on the real data:", FARMING)
    print("\n   plant a labelled test ring (Neo4j only): 5 feeders -> RingBoss_TEST")
    cypher(RING + ";")
    show("same rule, with the test ring:", FARMING)
    cypher("MATCH (n:TestRing) DETACH DELETE n;")
    print("\n   test ring removed")
    show("bonus: three-way gift loops (A -> B -> C -> A), each loop counted once", """
MATCH (a:Player)-[:GIFTED]->(b:Player)-[:GIFTED]->(c:Player)-[:GIFTED]->(a)
WHERE a.id < b.id AND a.id < c.id
RETURN a.handle AS a, b.handle AS b, c.handle AS c LIMIT 5""")
    show("loops in total, and pairs who gift each other:", """
MATCH (a:Player)-[:GIFTED]->(b:Player)-[:GIFTED]->(c:Player)-[:GIFTED]->(a)
WHERE a.id < b.id AND a.id < c.id
WITH count(*) AS three_way_loops
MATCH (x:Player)-[:GIFTED]->(y:Player)-[:GIFTED]->(x) WHERE x.id < y.id
RETURN three_way_loops, count(*) AS mutual_gift_pairs""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    args = ap.parse_args()
    conn = psycopg.connect(args.dsn, autocommit=True)
    load(conn)
    people_you_may_know(conn)
    gift_farming()


if __name__ == "__main__":
    main()
