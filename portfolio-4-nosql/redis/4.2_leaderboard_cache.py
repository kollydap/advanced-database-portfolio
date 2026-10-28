"""Experiment 4.2: Redis as a leaderboard (sorted sets) and a cache (cache-aside).

Redis is never the source of truth here (D6): the leaderboards are built
from CockroachDB's match_player kills, and the cached storefront is a copy
of MongoDB's documents. Both can be rebuilt at any time.

Part 1  leaderboard: build the three regional boards, check them against
        CockroachDB, time top-10 and "my rank" in both, play a pretend match
        (ZINCRBY), and combine the regions into a worldwide board.
Part 2  cache-aside: 200 loads of the af-west shop page straight from MongoDB
        vs through Redis; then the price-change freshness test.

Keys: sf:{region}:...  The {region} hash tag keeps a region's keys in one
Redis Cluster slot, like the regional fragments in P1.

    make p4 EXP=4.2_leaderboard_cache
"""
import argparse
import json
import statistics
import time

import psycopg
import redis
from pymongo import MongoClient

REGIONS = ["us-east", "eu-west", "af-west"]
TTL_S = 60

KILLS_SQL = """SELECT cp.handle, sum(mp.kills)::INT AS kills
               FROM match_player mp JOIN current_player cp ON cp.player_id = mp.player_id
               WHERE mp.crdb_region = %s
               GROUP BY cp.handle"""
TOP10_SQL = KILLS_SQL + " ORDER BY kills DESC, cp.handle DESC LIMIT 10"   # Redis breaks ties by member, descending
RANK_SQL = f"""WITH t AS ({KILLS_SQL})
               SELECT count(*) FROM t WHERE kills > (SELECT kills FROM t WHERE handle = %s)"""


def board(region):
    return f"sf:{{{region}}}:kills"


def timed(fn, n):
    ms = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ms.append((time.perf_counter() - t) * 1000)
    return statistics.median(ms), statistics.quantiles(ms, n=20)[-1]


# ------------------------------------------------------------- leaderboard

def part1(sql, r, repeat):
    print("== Part 1: leaderboards (sorted sets)")
    for region in REGIONS:
        rows = sql.execute(KILLS_SQL, (region,)).fetchall()
        r.delete(board(region))
        r.zadd(board(region), {handle: kills for handle, kills in rows})
        print(f"   {board(region):<20} {r.zcard(board(region)):>5} players  "
              f"(encoding {r.object('encoding', board(region))}, {r.memory_usage(board(region)) // 1024} KiB)")

    region = "af-west"
    key = board(region)
    top = [(m, int(s)) for m, s in r.zrevrange(key, 0, 9, withscores=True)]
    from_db = [(h, k) for h, k in sql.execute(TOP10_SQL, (region,)).fetchall()]
    print(f"\n   top 10 in {region}: ZREVRANGE {key} 0 9 WITHSCORES")
    for i, (m, s) in enumerate(top, 1):
        print(f"     {i:>2}. {m:<20} {s:>4} kills")
    print(f"   same as CockroachDB's top 10: {top == from_db}")

    # "My rank" with ties: ZREVRANK is the position in the list (equal scores
    # ordered by member); the standing "players with more kills + 1" is ZCOUNT
    # over the scores above mine, the same question as the SQL.
    me = r.zrevrange(key, 41, 41)[0]                 # a mid-table player: position 42
    mine = int(r.zscore(key, me))
    above = lambda: r.zcount(key, f"({mine}", "+inf")
    position, standing = r.zrevrank(key, me) + 1, above() + 1
    db_standing = sql.execute(RANK_SQL, (region, me)).fetchone()[0] + 1
    tied = r.zcount(key, mine, mine)
    print(f"\n   \"what's my rank?\" for {me} ({mine} kills, tied with {tied - 1} others):")
    print(f"     ZREVRANK (position in the list, ties alphabetical)  -> #{position}")
    print(f"     ZCOUNT ({mine} +inf, players with more kills, + 1)   -> #{standing}")
    print(f"     CockroachDB (count players with more kills, + 1)   -> #{db_standing}   same: {standing == db_standing}")

    print(f"\n   timing, {repeat} runs each (median / p95):")
    for label, fn in (("top 10, CockroachDB (sum + sort)", lambda: sql.execute(TOP10_SQL, (region,)).fetchall()),
                      ("top 10, Redis ZREVRANGE", lambda: r.zrevrange(key, 0, 9, withscores=True)),
                      ("my rank, CockroachDB (count above me)", lambda: sql.execute(RANK_SQL, (region, me)).fetchone()),
                      ("my rank, Redis ZCOUNT", above)):
        p50, p95 = timed(fn, repeat)
        print(f"     {label:<40} {p50:7.2f} ms / {p95:7.2f} ms")

    # a pretend match: the rank-11 player has a great game
    climber = r.zrevrange(key, 10, 10)[0]
    before, score = r.zrevrank(key, climber) + 1, int(r.zscore(key, climber))
    gain = int(r.zscore(key, r.zrevrange(key, 0, 0)[0])) - score + 1
    r.zincrby(key, gain, climber)
    after = r.zrevrank(key, climber) + 1
    print(f"\n   live update: ZINCRBY {key} {gain} {climber}")
    print(f"     {climber}: #{before} with {score} kills -> #{after} with {score + gain} kills (no re-sorting needed)")
    r.zincrby(key, -gain, climber)                   # undo: the board matches CockroachDB again

    world = "sf:global:kills"
    r.zunionstore(world, [board(x) for x in REGIONS])
    print(f"\n   worldwide board: ZUNIONSTORE {world} 3 <three regional boards>  ({r.zcard(world)} players)")
    for i, (m, s) in enumerate(r.zrevrange(world, 0, 4, withscores=True), 1):
        print(f"     {i}. {m:<20} {int(s):>4} kills")


# ------------------------------------------------------------- cache-aside

def part2(mongo, r, repeat):
    print("\n== Part 2: cache-aside for the af-west shop page")
    region, key = "af-west", "sf:{af-west}:storefront"
    stats = {"hit": 0, "miss": 0}

    def from_mongo():
        return list(mongo.storefront.find({"region": region}, {"_id": 0}).sort("sku", 1))

    def through_cache():
        cached = r.get(key)
        if cached is not None:
            stats["hit"] += 1
            return json.loads(cached)
        stats["miss"] += 1
        page = from_mongo()
        r.set(key, json.dumps(page), ex=TTL_S)
        return page

    r.delete(key)
    p50_m, p95_m = timed(from_mongo, repeat)
    p50_c, p95_c = timed(through_cache, repeat)
    print(f"   {repeat} page loads each (median / p95):")
    print(f"     straight from MongoDB            {p50_m:6.2f} ms / {p95_m:6.2f} ms")
    print(f"     through the Redis cache          {p50_c:6.2f} ms / {p95_c:6.2f} ms")
    print(f"     hits {stats['hit']}, misses {stats['miss']} -> hit ratio {stats['hit'] / repeat:.1%}"
          f"; key expires in {r.ttl(key)} s (TTL {TTL_S} s)")

    def price(page):
        return next(d["price"]["cents"] for d in page if d["sku"] == "AMMO-762-90")

    print("\n   freshness: change the af-west 7.62 price in MongoDB from $1.49 to $1.39")
    mongo.storefront.update_one({"region": region, "sku": "AMMO-762-90"}, {"$set": {"price.cents": 139}})
    print(f"     page through the cache  -> ${price(through_cache()) / 100:.2f}   (stale: the cached copy)")
    r.delete(key)
    print(f"     DEL {key}   (invalidate)")
    print(f"     page through the cache  -> ${price(through_cache()) / 100:.2f}   (miss, fresh from MongoDB)")
    mongo.storefront.update_one({"region": region, "sku": "AMMO-762-90"}, {"$set": {"price.cents": 149}})
    r.delete(key)                                    # undo: back to CockroachDB's price


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26259/squadfire?sslmode=disable",
                    help="the af-west node, the region the questions are about")
    ap.add_argument("--mongo", default="mongodb://localhost:27017")
    ap.add_argument("--redis", default="redis://localhost:16379/0", help="the Redis master")
    ap.add_argument("--repeat", type=int, default=200)
    args = ap.parse_args()

    sql = psycopg.connect(args.dsn, autocommit=True)
    r = redis.Redis.from_url(args.redis, decode_responses=True)
    mongo = MongoClient(args.mongo).squadfire
    part1(sql, r, args.repeat // 4)
    part2(mongo, r, args.repeat)


if __name__ == "__main__":
    main()
