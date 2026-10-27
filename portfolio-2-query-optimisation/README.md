# Portfolio 2: Query optimisation

Four experiments on the Squadfire data from portfolio 1 (5,100 player versions, 47,430 purchases, 94,880 inventory changes) and the MongoDB storefront. Each one takes a query the game really runs, reads the database's own plan (`EXPLAIN ANALYZE` in CockroachDB, `explain("executionStats")` in MongoDB), changes one thing and reads the plan again.

## Contents

| Experiment | Script | Evidence |
|---|---|---|
| 2.1 Current ammo: a covering index | [`sql/2.1_current_ammo.sql`](sql/2.1_current_ammo.sql) | [`evidence/2.1_current_ammo.txt`](evidence/2.1_current_ammo.txt) |
| 2.3 Today's spend: column order and a partial index | [`sql/2.3_daily_spend.sql`](sql/2.3_daily_spend.sql) | [`evidence/2.3_daily_spend.txt`](evidence/2.3_daily_spend.txt) |
| 2.5 Region-local vs region unknown | [`sql/2.5_region_local.sql`](sql/2.5_region_local.sql) | [`evidence/2.5_region_local.txt`](evidence/2.5_region_local.txt) |
| 2.6 MongoDB: shard targeting and indexes | [`mongo/2.6_storefront_explain.js`](mongo/2.6_storefront_explain.js) | [`evidence/2.6_storefront_explain.txt`](evidence/2.6_storefront_explain.txt) |

**How to read the results.** Data volume is small and every "region" runs on one laptop, so the timings are small and vary between runs. The reliable evidence is **rows read** and **where they were read from** (`spans:` in CockroachDB, shards asked in MongoDB). Those numbers come from the plan and are the same on every run.

## 2.1 "How much ammo does this player have right now?"

Asked before every match. The current balance is the last row of each (player, item) chain: the row nothing points to (`NOT EXISTS … previous_id = i.id`). The player is SharpLion (us-east), who has 253 inventory rows.

| Plan | How the rows are found | Rows read | Time |
|---|---|---|---|
| Before | `FULL SCAN` of `inventory` | 95,122 | 204 ms |
| After: `inventory_by_player (player_id, sku) STORING (units_after)` | index range for this player | 495 | 13 ms |

**Why it works:**
- The index puts one player's rows together, grouped by item.
- `STORING (units_after)` copies the balance into the index (a **covering index**), so the table itself is never visited.
- The "is this the last row?" check uses the existing `UNIQUE (previous_id)` index. That is the index portfolio 1 added to stop chains forking, so it serves two purposes.

The 495 rows read are 253 index rows plus 242 lookups of that unique index.

## 2.3 "How much has this player spent today?"

Runs before **every purchase** to enforce the $50 daily limit, so it sits on the store's busiest path. Spend means the player's original purchases from today that succeeded and were not refunded. The player is SteelSniper642 on 26 Oct, the last day of the data: 3 purchases, $49.86.

| Plan | How the rows are found | Rows read | Time |
|---|---|---|---|
| Before | `FULL SCAN` of `purchase` | 47,430 | 62 ms |
| `(payer_id, created_at)`, partial, covering | this player → today | **3** | 3 ms |
| Reversed `(created_at, payer_id)` | everyone's purchases today → this player | 1,212 | 3 ms |

```sql
CREATE INDEX purchase_spend_by_day ON purchase (payer_id, created_at)
    STORING (status, total_cents) WHERE previous_id IS NULL;
```

**Lessons:**
- **The exact-match column goes before the range column.** With the payer first, one player's purchases sit together in time order, so "today" is one short span. With time first, "today" is everyone's purchases from today, and the player is filtered out afterwards. Both orders take 3 ms here, but the reversed one reads 400× more rows, and that gap grows with traffic.
- **Partial index.** Refund versions (`previous_id IS NOT NULL`) are never spend, so `WHERE previous_id IS NULL` keeps them out of the index. The plan names it `purchase_spend_by_day (partial index)`.
- The plan's `spans:` still list **af-west, eu-west and us-east**, which leads to 2.5.

## 2.5 "Tell the database which region"

`purchase` is `REGIONAL BY ROW`: every row, and every index, is split by `crdb_region`. A query that does not name the region has to look in all three regions. This is the same query as 2.3, with and without `crdb_region = 'us-east'`.

| Query | Regions in `spans:` | Rows read | Answer |
|---|---|---|---|
| Region not given | af-west, eu-west, us-east | 3 | $49.86 |
| `crdb_region = 'us-east'` | **us-east only** | 3 | $49.86 |

**Lessons:**
- **Same answer, one region asked instead of three.** The application knows the player's home region from login and passes it.
- Locally the time difference is small (8 ms vs 3 ms). In a real deployment each extra region costs a round trip across an ocean (~100–150 ms), on every purchase.
- **Locality-optimised search.** For `WHERE id = …`, which matches at most one row, CockroachDB plans a `union all … limit 1`: it scans us-east first and asks the other regions only if the row is not found. A **sum** cannot stop early, which is why the spend query must give the region.

## 2.6 MongoDB: shard targeting and indexes

The storefront collection (from [`data-gen/storefront.py`](../data-gen/storefront.py)) holds one document per (region, item), 33 in total. It is zone-sharded on `{ region: 1, sku: 1 }`, so each region's 11 documents live on that region's shard.

| Query | Shards asked | Plan on each shard | Docs read | Returned |
|---|---|---|---|---|
| A. AP rounds in us-east | **1** (`SINGLE_SHARD`) | `IXSCAN` (shard key) | 1 | 1 |
| B. AP rounds, region not given | 3 (`SHARD_MERGE`) | `COLLSCAN` | 33 | 3 |
| B2. as B, with an index on `sku` | 3 | `IXSCAN` | 3 | 3 |
| C. all 5.56mm ammo, no index | 3 | `COLLSCAN` | 33 | 6 |
| D. as C, index on `details.calibre` | 3 | `IXSCAN` | 6 | 6 |
| E. 5.56mm ammo in af-west | **1** | `IXSCAN` | 2 | 2 |

**Lessons:**
- **The region picks the shard; the index picks the documents.** B2 reads only 3 documents but still asks every shard. Only a query that gives the region (A, E) reaches a single shard.
- **An index helps only if the query gives its first field.** The shard-key index starts with `region`, so B, which gives only the sku, cannot use it. The same rule produced the 400× gap in 2.3.
- **Indexing a field only some documents have.** `details.calibre` exists only on ammo documents, the flexible-schema case MongoDB was chosen for.
- **Zones must cover the whole key space.** The first version defined each zone as exactly `(region, MinKey) → (region, MaxKey)`. That left gap chunks on the primary shard, and E then asked **2** shards, because a region-only query's bounds reach `(region, MaxKey)`, the edge of the next gap chunk. Zones now run back to back from `MinKey` to `MaxKey`, and E asks one.

## Summary

| | Before | After | What changed |
|---|---|---|---|
| 2.1 current ammo | 95,122 rows | 495 rows | covering index |
| 2.3 today's spend | 47,430 rows | 3 rows | index column order + partial index |
| 2.5 region | 3 regions | 1 region | region in the query |
| 2.6 storefront | 3 shards, 33 docs | 1 shard, 2 docs | region in the query + index on a nested field |

The same two rules run through all four experiments, in both databases:
- **Read only the rows you need:** an index whose leading columns match the query.
- **Read them only where they live:** give the region, so the query stays in one region or on one shard.

## Run it

```bash
make up-core && make seed                    # data from portfolio 1 (+ MongoDB storefront)
make p2 EXP=2.1_current_ammo                 # also 2.3_daily_spend, 2.5_region_local
make p2-mongo EXP=2.6_storefront_explain
```
