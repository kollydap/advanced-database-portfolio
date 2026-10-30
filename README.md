# Squadfire: a multi-region game store on five databases

MIT 8103 Advanced Database Systems · 2026/2027 First Semester Continuous Assessment

Squadfire is a simulated squad shooter with an in-game store. Players in three regions (`us-east`, `eu-west`, `af-west`) buy ammo, grenades, med kits and skins in USD, for themselves or as gifts to friends. This repository builds its data platform on CockroachDB, MongoDB, Redis, Neo4j and a Kafka → Spark → HDFS → DuckDB pipeline. Every claim is backed by a script whose output is saved under `evidence/`.

> **Status:** portfolios 1–4 and the 5.1 pipeline are complete. Still to do: 5.2 (CockroachDB node failure), 5.3 (Redis Sentinel failover), the portfolio 5 README and the final write-up. Design decisions D1–D17 are recorded in [`DECISIONS.md`](DECISIONS.md).

## At a glance

| | |
|---|---|
| Data | 30 days, seeded and reproducible: 5,000 players, 25,386 friendship versions, 47,430 purchases, 66,563 purchase lines, 94,880 inventory changes, 2,500 matches, 433,978 telemetry events |
| Source of truth | CockroachDB v25.3, 3 nodes = 3 regions, `SURVIVE REGION FAILURE` |
| Other stores | MongoDB 8.0 (zone-sharded storefront), Redis 8 (leaderboards, cache), Neo4j 5.26 (player graph) |
| Analytics | changefeed → Kafka 4.0 → Spark 3.5.6 → HDFS 3.4.1 lake → DuckDB star schema |
| Runs on | one laptop with Docker; every component is in `docker-compose.yml` with a pinned image version |

## Architecture

```
                     ┌──────────────────────── CockroachDB (source of truth) ───────────────────────┐
  players / game ──▶ │  us-east node        eu-west node        af-west node                        │
  servers / store    │  REGIONAL BY ROW: each player's rows led from their home region              │
                     │  GLOBAL: region, item, player_handle (copied everywhere)                     │
                     └───────┬───────────────────────────┬───────────────────────────┬──────────────┘
                             │ prices copied             │ kills, friendships, gifts │ changefeed (CDC)
                             ▼                           ▼                           ▼
        MongoDB storefront (3 zone shards)    Redis (leaderboards, cache)       Kafka topics squadfire.*
        Neo4j player graph ◀────────────────────────────────────┘                    │   ▲ telemetry
                                                                                     ▼   │ (game servers)
                                                         Spark ──▶ HDFS lake: bronze → silver → gold
                                                                                     │
                                                                                     ▼
                                                                       DuckDB star schema (warehouse)
```

| Store | Holds | Why this store | Source of truth? |
|---|---|---|---|
| **CockroachDB** | players, friendships, prices, drops, purchases, inventory, matches | ACID across regions, constraints, SERIALIZABLE, row-level regional placement | yes |
| **MongoDB** | storefront: one document per (region, item), with type-specific details | each item type has different fields; a `$jsonSchema` validator still requires each type's own | details yes; prices are a copy |
| **Redis** | per-region kill leaderboards (sorted sets), cached shop pages | top-N and rank in O(log N), sub-millisecond reads | no: rebuilt from the others |
| **Neo4j** | `FRIENDS_WITH` and `GIFTED` links between players | friend-of-friend and ring queries follow links instead of self-joins | no: loaded from CockroachDB |
| **Kafka / HDFS / DuckDB** | every committed change plus game telemetry | full history for analytics, away from the live store | no: derived |

## 1. Relational core: CockroachDB

[`portfolio-1-database-design/`](portfolio-1-database-design/) has 12 tables, normalised to BCNF ([`normalisation.md`](portfolio-1-database-design/normalisation.md), [ERD](portfolio-1-database-design/erd.md)).

**History is never edited.** Player, friendship, purchase and inventory are **append-only chains**: a change inserts a new row whose `previous_id` names the row it replaces.
- `UNIQUE (previous_id)`: a row can be replaced only once, so two concurrent changes to the same state cannot both commit.
- `root_id`: the entity's permanent identity, which other tables reference.
- "Current" = the row nothing points to, exposed as views (`current_player`, `current_inventory`, …).
- Enforced twice:
  - a `BEFORE UPDATE OR DELETE` trigger rejects edits for every user
  - the application role `squadfire_app` holds only SELECT/INSERT
- One deliberate exception: `limited_drop.remaining`, a stock counter updated in place (the concurrency case in §3).

| Rule | Enforcement |
|---|---|
| Money is exact | integer cents (`BIGINT`) |
| 1–10 packs per line; line total = packs × price | `CHECK` |
| Balance never negative and adds up along its chain | `CHECK (units_after >= 0)` + trigger `inventory_adds_up` |
| Refund once, and only a SUCCEEDED purchase | partial unique index + trigger |
| Gateway reference and inventory credit recorded once | partial unique indexes |
| Handles unique across regions, case-insensitive | `player_handle` (GLOBAL) + composite foreign key |
| No name claim without a player | `SECURITY DEFINER` procedures `sign_up` / `rename_player`; the app role cannot insert claims |

**Regional placement:**
- Player-owned tables are `REGIONAL BY ROW`: each row lives in its owner's region, and its leaseholder sits on that region's node.
- Child rows inherit their parent's region (derived fragmentation).
- Small reference tables are `GLOBAL`: replicated to every region for local reads, at the cost of ~600 ms writes (measured in §5).

**Evidence:**
- 23 rule tests, in which 15 invalid operations are rejected with the expected SQLSTATE.
- 11 integrity checks over the generated month, all at 0 violations.
- [`evidence/rule-tests.txt`](portfolio-1-database-design/evidence/rule-tests.txt), [`evidence/integrity.txt`](portfolio-1-database-design/evidence/integrity.txt).

The generator ([`data-gen/generate.py`](data-gen/generate.py)) simulates the month in time order, so the data obeys every rule:
- nobody spends over $50 a day
- gifts go only to accepted friends
- drops never oversell
- telemetry rounds fired equal the inventory's USED rows

## 2. Query optimisation

[`portfolio-2-query-optimisation/`](portfolio-2-query-optimisation/) uses `EXPLAIN ANALYZE` (CockroachDB) and `explain("executionStats")` (MongoDB), each before and after one change.

| Experiment | Before | After | Change |
|---|---|---|---|
| 2.1 Current ammo for a player | full scan, 95,122 rows, 204 ms | 495 rows, 13 ms | covering index `(player_id, sku) STORING (units_after)` |
| 2.3 Today's spend (runs on every purchase) | full scan, 47,430 rows, 62 ms | 3 rows, 3 ms | partial covering index `(payer_id, created_at) WHERE previous_id IS NULL`; the reversed column order reads 1,212 rows |
| 2.5 Region in the query | spans in 3 regions | spans in 1 region | `crdb_region = 'us-east'`; point lookups already use locality-optimised search |
| 2.6 MongoDB storefront | `SHARD_MERGE`, 3 shards, `COLLSCAN`, 33 docs | `SINGLE_SHARD`, `IXSCAN`, 2 docs | region in the query + index on `details.calibre` |

**Findings:**
- An index helps only when the query gives its leading column.
- The region picks the shard; the index picks the rows.
- Zone ranges in MongoDB must leave no gaps. A gap left the router asking an extra shard, until the zones were rebuilt back to back from `MinKey` to `MaxKey`.

## 3. Transactions and concurrency

[`portfolio-3-transactions-concurrency/`](portfolio-3-transactions-concurrency/): each experiment is a Python script that opens two real sessions as `squadfire_app` and forces the worst interleaving, so the race happens on every run.

| Experiment | READ COMMITTED, naive code | Fixes that work | SERIALIZABLE, same naive code |
|---|---|---|---|
| 3.2 Two buyers, last drop pack (lost update) | 2 packs sold from 1 | `SELECT … FOR UPDATE`; one-step `UPDATE … SET remaining = remaining - 1 WHERE remaining > 0` | 40001, retry, 1 sold |
| 3.3 Two $8 buys at $40 spent, $50 limit (write skew) | $56 spent; `FOR UPDATE` on the summed rows also fails (phantoms) | lock the player's row first, inside a `SECURITY DEFINER` procedure | 40001, retry, second refused |
| 3.5 Gift from eu-west to af-west | — | — | one `EndTxn` on the eu-west transaction record commits both regions' writes; a failing af-west write rolls back the eu-west one too |

**Conclusion:** keep SERIALIZABLE, CockroachDB's default, with a retry on 40001. Add the explicit fixes on hot paths, where retries would be frequent.

## 4. NoSQL

[`portfolio-4-nosql/`](portfolio-4-nosql/):
- **MongoDB.** A `$jsonSchema` validator with a `oneOf` per item type: 8 of 8 rule tests as expected, and updates are checked too. An aggregation compares prices across regions and finds the generator's random price changes.
- **Redis.**
  - Top 10: 11.1 ms in CockroachDB vs 0.56 ms in Redis. "My rank": 18.1 ms vs 0.30 ms.
  - Tied scores need `ZCOUNT` for a standing (#35), not `ZREVRANK`, which gives the list position (#42).
  - Cache-aside: 99.5% hit ratio, with staleness bounded by a 60 s TTL and invalidation.
- **Neo4j.**
  - "People you may know": ~1–3 ms vs ~460 ms for the self-joining SQL, with identical results.
  - A gift-farming rule finds no false positives in the real data and catches exactly one planted test ring.

§4.4 of that README compares which store fits which job.

## 5. Distributed processing and the lake

[`portfolio-5-distributed-cloud/`](portfolio-5-distributed-cloud/):

1. **Changefeed.** [`sql/changefeed.sql`](portfolio-5-distributed-cloud/sql/changefeed.sql) streams `purchase`, `purchase_line`, `inventory`, `item` and `region` into Kafka topics `squadfire.<table>`, starting with an initial scan.
   - Kafka holds every row exactly once: 47,430 / 66,563 / 94,880.
   - A live change arrives 0.4–0.8 s after commit.
2. **Telemetry.** 433,978 game-server events are replayed into `squadfire.telemetry`. Telemetry never touches CockroachDB.
3. **Spark** ([`spark/lake.py`](portfolio-5-distributed-cloud/spark/lake.py)):
   - **Bronze:** raw messages, appended incrementally with Structured Streaming `availableNow` and a checkpoint.
   - **Silver:** typed rows, de-duplicated on key and latest `updated` (the changefeed is at-least-once), partitioned by region and date.
   - **Gold:** the star schema and daily summaries.
4. **DuckDB warehouse** ([`scripts/5.1d_warehouse.py`](portfolio-5-distributed-cloud/scripts/5.1d_warehouse.py)): `fact_sales` (64,536 lines) with `dim_date`, `dim_item` and `dim_region`, and 0 orphan facts.

**Reconciliation:**
- Net revenue and packs per region match CockroachDB to the cent.
- Rounds fired and revives from telemetry equal the ammo, grenades and med kits marked USED in the inventory chains. These are two independent paths into the lake.

## Operational findings

- **Clock offset kills a node.** The af-west node stopped twice overnight with `clock synchronization error: this node is more than 400ms away from at least half of the known nodes`, most likely from the laptop sleeping. The cluster kept serving on 2 of 3 replicas, and the node caught up after a restart.
- **Leaseholders move slowly.** Lease preferences are applied by a background scanner that visits each range about every 10 minutes. After an outage, or on a new table, leaders can sit in the wrong region. The experiments use `ALTER RANGE … RELOCATE LEASE` to apply the configured placement immediately.
- **GLOBAL tables trade write latency for local reads.** An `UPDATE item` took ~620 ms to commit, compared with a few ms on regional tables.

## Limitations

- **3 nodes, not 5.** `SURVIVE REGION FAILURE` would place 5 replicas (2 in the home region). With one node per region the cluster runs 3 replicas: it survives one node, not a full region with spare capacity.
- **Timing only shows the trend.** All "regions" are containers on one machine, so cross-region latency is not representative. The evidence relies on plans, spans, row counts and traces rather than wall-clock gaps.
- **Small data on purpose.** The volumes are enough to change query plans, not to benchmark (D16).
- **Neo4j is loaded straight from CockroachDB**, not yet from the lake's gold layer as D8 intends.
- **No authentication, insecure mode.** CockroachDB runs `--insecure` and Neo4j without auth. This is a local lab with synthetic data only.

## Repository layout

```
docker-compose.yml        profiles: core | stream | bigdata (pinned images)
Makefile                  a target for every experiment (below)
DECISIONS.md              design decisions D1–D17
data-gen/                 generate.py (CockroachDB history + telemetry), storefront.py (MongoDB)
infra/                    MongoDB cluster init, Redis Sentinel, Hadoop config
portfolio-1-database-design/          sql/ 00–05, erd.md, normalisation.md, evidence/
portfolio-2-query-optimisation/       sql/ 2.1 2.3 2.5, mongo/ 2.6, evidence/
portfolio-3-transactions-concurrency/ scripts/ 3.2 3.3 3.5, evidence/
portfolio-4-nosql/                    mongo/ 4.1, redis/ 4.2, neo4j/ 4.3, evidence/
portfolio-5-distributed-cloud/        sql/ changefeed, scripts/ 5.1a–d, spark/ lake.py, evidence/
docs/screenshots/                     ERD image
```

## Running it

Requirements: Docker Desktop and Python 3.12. The full stack needs about 9 GB of Docker memory; with 8 GB, stop MongoDB and Neo4j while the pipeline runs.

```bash
make venv                                 # Python dependencies (pinned in requirements.txt)
make up-core                              # CockroachDB x3, MongoDB, Redis + Sentinel, Neo4j
make seed                                 # rebuild schema, generate the month, load MongoDB, integrity checks (~20 min)

make p1-tests                             # rule tests
make p2 EXP=2.1_current_ammo              # also 2.3_daily_spend, 2.5_region_local
make p2-mongo EXP=2.6_storefront_explain
make p3 EXP=3.2_last_pack                 # also 3.3_daily_limit, 3.5_cross_region_gift
make p4-mongo EXP=4.1_storefront_rules
make p4 EXP=4.2_leaderboard_cache         # also 4.3_player_graph

make up-stream up-bigdata                 # Kafka, HDFS, Spark
make p5-changefeed                        # once
make p5 EXP=5.1a_changefeed
make p5 EXP=5.1b_replay_telemetry         # once
make p5-lake                              # Spark: bronze/silver/gold
make p5 EXP=5.1d_warehouse                # DuckDB load + reconciliation
```

| Service | Address |
|---|---|
| CockroachDB SQL (us-east / eu-west / af-west) | `localhost:26257` / `:26258` / `:26259` |
| CockroachDB console | http://localhost:8090 |
| MongoDB (mongos) | `localhost:27017` |
| Redis master / replicas | `localhost:16379` / `:16380`, `:16381` |
| Neo4j browser / Bolt | http://localhost:7474 / `localhost:7687` |
| Kafka | `localhost:29092` |
| HDFS UI / Spark UI | http://localhost:9870 / http://localhost:8088 |

All data is synthetic, with generated player handles and no personal data.
