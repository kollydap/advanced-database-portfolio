# Portfolio 4: NoSQL

Squadfire uses three NoSQL stores next to CockroachDB, each for the job it does best (D6):

| Store | Model | Holds | Source of truth? |
|---|---|---|---|
| **MongoDB** | document | the storefront: item details that differ by item type, plus each region's price | details: yes; prices: a copy from CockroachDB |
| **Redis** | key-value, sorted sets | leaderboards and a page cache | no: everything can be rebuilt |
| **Neo4j** | graph | who is friends with whom, who gifts to whom | no: loaded from CockroachDB (from the lake in portfolio 5) |

Money, stock and history stay in CockroachDB, where they get ACID transactions and constraints (portfolios 1 and 3).

## Contents

| Experiment | Script | Evidence |
|---|---|---|
| 4.1 MongoDB: validator and aggregation | [`mongo/4.1_storefront_rules.js`](mongo/4.1_storefront_rules.js), rules in [`mongo/storefront.schema.json`](mongo/storefront.schema.json) | [`evidence/4.1_storefront_rules.txt`](evidence/4.1_storefront_rules.txt) |
| 4.2 Redis: leaderboards and cache-aside | [`redis/4.2_leaderboard_cache.py`](redis/4.2_leaderboard_cache.py) | [`evidence/4.2_leaderboard_cache.txt`](evidence/4.2_leaderboard_cache.txt) |
| 4.3 Neo4j: friend suggestions and gift farming | [`neo4j/4.3_player_graph.py`](neo4j/4.3_player_graph.py) | [`evidence/4.3_player_graph.txt`](evidence/4.3_player_graph.txt) |
| 4.4 Which store for which job | this page, [below](#44-which-store-for-which-job) | |

Each script undoes its own changes (test documents, prices, test ring), so every store is left matching CockroachDB.

## 4.1 MongoDB: flexible, but not anything goes

The `storefront` collection holds one document per (region, item), 33 in total, zone-sharded by region (portfolio 2.6). Item types need different fields:

```js
// ammo
details: { calibre: '7.62mm', damage: 41, penetration: 3 }
// skin
details: { weapon: 'AK-47', rarity: 'RARE', tradable: false }
```

In a relational table these would be columns that are mostly empty, or an entity–attribute–value table.

**The validator** (`$jsonSchema`, `validationLevel: strict`, `validationAction: error`):
- Every document needs a known region, a sku, a name, `units_per_pack ≥ 1` and `price.cents ≥ 1`.
- A `oneOf` then requires each type's own fields: AMMO a calibre, damage and penetration; GRENADE a damage or a cloud radius; MEDKIT a heal; SKIN a weapon and a rarity from RARE/EPIC/LEGENDARY.
- Extra fields are still allowed.
- The rules live in one file, used by both the loader ([`data-gen/storefront.py`](../data-gen/storefront.py)) and the test script.

| Test | Expected | Result (MongoDB's reason, code 121) |
|---|---|---|
| T1 valid new skin, with an extra field | accept | ✅ accepted |
| T2 ammo without a calibre | reject | ✅ `missing calibre` |
| T3 skin with rarity "SUPER" | reject | ✅ `rarity = "SUPER" fails enum` |
| T4 negative price | reject | ✅ `cents = -500 fails minimum` |
| T5 unknown region | reject | ✅ `region = "ng-lagos" fails enum` |
| T6 update: remove a calibre | reject | ✅ `missing calibre` |
| T7 update: turn ammo into a SKIN | reject | ✅ `missing rarity, weapon` |
| T8 update: change a price | accept | ✅ accepted |

All 33 existing documents pass.
- **Updates are checked as well as inserts** (T6, T7).
- **A `oneOf` failure lists why the document fails every type's rules.** The script keeps only the branch for the document's own type, so the reason is readable.

**The aggregation** (`$group` → `$project` with `$arrayToObject` → `$sort`) asks: *what does each item cost in each region, and how much cheaper is af-west than us-east?*
- Most items show the planned regional pricing (D3): af-west 40% cheaper, eu-west 10% dearer.
- The outliers come from the generator's random price changes, and the aggregation finds them at once:
  - Arctic M4 skin: af-west only 6% cheaper
  - Gold Desert Eagle: $13.74 in eu-west

## 4.2 Redis: leaderboards and a cache

**Leaderboards (sorted sets).** One sorted set per region holds the players ordered by kills: `sf:{af-west}:kills`, with each player a member and their kills the score. They are built from CockroachDB's `match_player`: us-east 1,787 players, eu-west 1,647, af-west 1,203. The encoding is `skiplist`, at about 100–145 KiB per board. Redis's top 10 is identical to CockroachDB's.

| Question (af-west, 50 runs) | CockroachDB | Redis |
|---|---|---|
| Top 10 | 11.1 ms (sum + sort) | **0.56 ms** (`ZREVRANGE 0 9`) |
| "My rank" | 18.1 ms (count players above me) | **0.30 ms** (`ZCOUNT`) |

- **Live update.** One `ZINCRBY` after a match moved NightFox371 from #11 to #1, with no re-sorting (O(log N)).
- **Worldwide board.** `ZUNIONSTORE` combines the three regional boards (4,637 players). This works on a single Redis. In Redis Cluster the `{region}` hash tags put the boards in different slots, so the application would merge them instead.
- **Ranks with ties.** BoldFox551 has 23 kills, tied with 7 others.
  - `ZREVRANK` gives **#42**: the position in the list, with equal scores ordered by name.
  - `ZCOUNT (23 +inf` + 1 gives **#35**: the standing, which is what players expect and what the SQL computes.

**Cache-aside for the shop page** (`sf:{af-west}:storefront`, TTL 60 s). The application checks Redis first. On a miss it reads MongoDB and stores a copy.

| 200 loads of the af-west page | Median | p95 |
|---|---|---|
| Straight from MongoDB | 0.98 ms | 2.24 ms |
| Through the Redis cache | **0.30 ms** | **0.66 ms** |

- **Hit ratio 99.5%** (199 hits, 1 miss). On a laptop MongoDB is close and the page is small, so the time saved is modest. The bigger saving is that MongoDB is not asked at all for 199 of the 200 loads.
- **Freshness, the trade-off.**
  1. The af-west 7.62 price is changed in MongoDB, $1.49 → $1.39.
  2. The cached page still shows **$1.49**.
  3. After `DEL` (invalidation), it shows **$1.39**.

  Staleness is limited by the TTL, and by deleting the key when a price changes. In portfolio 5 the change stream does that automatically.

## 4.3 Neo4j: the player graph

The graph has 5,000 `(:Player)` nodes (region, matches played), 9,867 `FRIENDS_WITH` links (accepted friendships) and 4,877 `GIFTED {count, total_cents}` links (one per giver → receiver pair, covering 8,262 gifts). It loads from CockroachDB in a few seconds. Cypher is sent through `cypher-shell` in the container, so no extra Python dependency is needed.

**People you may know.** Friends of friends who are not friends yet, ranked by mutual friends:

```cypher
MATCH (me:Player {handle: "BoldFalcon857"})-[:FRIENDS_WITH]-(friend)-[:FRIENDS_WITH]-(suggestion)
WHERE suggestion <> me AND NOT (me)-[:FRIENDS_WITH]-(suggestion)
RETURN suggestion.handle, count(DISTINCT friend) AS mutual ORDER BY mutual DESC LIMIT 5
```

- **Top suggestion:** RogueBison907, through 2 mutual friends (GrimSniper548, GrimBison936).
- **The SQL version gives the same five suggestions.** It needs the friendship table joined with itself three times, in both directions.
- **Time:** Neo4j ~1–3 ms, CockroachDB ~460 ms.
  - Neo4j starts at one player and follows only that player's links.
  - The SQL builds the whole two-direction friendship list through the `current_friendship` view first.
  - A hand-tuned SQL query would close some of the gap, but every extra hop is another join, while a graph traversal costs only the links it visits.

**Gift farming.** The rule: flag a receiver with 3 or more *feeders*, i.e. accounts that never play, have exactly one friend, and gift only to that friend.

| Data | Flagged |
|---|---|
| Real data | **none**: no false alarms |
| With a planted, labelled test ring (`:TestRing`, 5 feeders → `RingBoss_TEST`) | **exactly the ring**: 5 feeders, 15 gifts, $44.85 |

Planting a known case and checking that only it is caught is the usual way to test a fraud rule. The ring is deleted afterwards.

**Bonus:**
- **Three-way gift loops** (A → B → C → A): **0**.
- **749 pairs gift each other**, which is ordinary friendship. That is why mutual gifting alone should not count as fraud.

## 4.4 Which store for which job

| Need in Squadfire | Best fit | Why | Why not the others |
|---|---|---|---|
| Purchases, balances, daily limit, drop stock | **CockroachDB** | ACID across regions, constraints, SERIALIZABLE (P1, P3) | Redis and Neo4j cannot share a transaction with the money. MongoDB is weaker at cross-document rules such as "balance = sum of changes" |
| Item details that differ by type | **MongoDB** | each document carries its own fields, and a validator still enforces the required ones per type (4.1) | relational would need sparse columns or entity–attribute–value tables |
| Leaderboards, "my rank" | **Redis** sorted set | kept in order on every update; top-N and rank in O(log N), sub-millisecond (4.2) | SQL recomputes the sum and sort on every request |
| Repeated page reads | **Redis** cache | 99.5% hit ratio; the source is barely asked (4.2) | — (a cache is an add-on; it is never the source) |
| Friend suggestions, gift rings | **Neo4j** | traversal follows only the links it needs; queries read like the pattern (4.3) | SQL needs one self-join per hop and gets heavier with each one |

**The costs of using several stores:**
- **Copies can be stale.** Prices in MongoDB, boards and pages in Redis, and the graph in Neo4j are all copies. Each needs a way to stay in step: a TTL, invalidation, rebuilds, and (portfolio 5) a change stream from CockroachDB.
- **No transaction spans the stores.** That is why everything that must be exactly right stays in CockroachDB (D7, limited-drop stock).
- **More to run.** Every store adds operations, backups and failure modes. Portfolio 5 tests two of those failures: losing a CockroachDB node, and a Redis failover.

## Run it

```bash
make up-core && make seed                     # CockroachDB data + MongoDB storefront
make p4-mongo EXP=4.1_storefront_rules
make p4 EXP=4.2_leaderboard_cache
make p4 EXP=4.3_player_graph
```
