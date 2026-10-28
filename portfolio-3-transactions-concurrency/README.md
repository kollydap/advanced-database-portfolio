# Portfolio 3: Transactions and concurrency

Three experiments on the store's riskiest moments: two players racing for the last pack, two purchases racing past the daily limit, and one gift that has to commit in two regions at once. Each is a Python script that opens real database sessions and forces the steps into the worst order, so the race happens on every run and the log is the same each time.

## Contents

| Experiment | Script | Evidence |
|---|---|---|
| 3.2 The last limited-drop pack (lost update) | [`scripts/3.2_last_pack.py`](scripts/3.2_last_pack.py) | [`evidence/3.2_last_pack.txt`](evidence/3.2_last_pack.txt) |
| 3.3 The $50 daily limit (write skew) | [`scripts/3.3_daily_limit.py`](scripts/3.3_daily_limit.py) | [`evidence/3.3_daily_limit.txt`](evidence/3.3_daily_limit.txt) |
| 3.5 A cross-region gift (atomic commit) | [`scripts/3.5_cross_region_gift.py`](scripts/3.5_cross_region_gift.py) | [`evidence/3.5_cross_region_gift.txt`](evidence/3.5_cross_region_gift.txt) |

Basic commit/rollback and the chain rule `UNIQUE (previous_id)`, which stops two concurrent changes to the same balance, are already shown by the portfolio 1 rule tests ([`../portfolio-1-database-design/evidence/rule-tests.txt`](../portfolio-1-database-design/evidence/rule-tests.txt)).

**Test data.** The buyers run as the application role `squadfire_app`, with its real privileges. The players are real players from the generated data. The scripts write into scratch tables (`lab_…`) with the same columns and regional placement as the real ones, because history tables never allow deletes (P1). Each script removes its scratch objects at the end, so the portfolio 1 integrity checks stay at 0.

## 3.2 The last limited-drop pack: lost update

`limited_drop.remaining` is the one value in the schema that is updated in place (P1, D7). A test drop has **1 pack left**, and players A and B try to buy it at the same moment: both read, then both write.

| Run | How the app buys | Isolation | Packs sold | Verdict |
|---|---|---|---|---|
| 1 | read `remaining`, then write the app's value (`SET remaining = 0`) | READ COMMITTED | **2** | ❌ oversold |
| 2 | `SELECT … FOR UPDATE`, then write | READ COMMITTED | 1 | ✅ |
| 3 | `UPDATE … SET remaining = remaining - 1 WHERE remaining > 0` | READ COMMITTED | 1 | ✅ |
| 4 | as run 1 | SERIALIZABLE | 1 | ✅ (40001, retry) |

- **Run 1.** Both read 1. A writes 0 and commits, then B writes 0 and commits. The counter looks right, but **two sales** are recorded: B's write silently replaced A's.
- **Run 2.** A locks the row when it reads. B's read waits (~0.5 s, the time A holds the lock), then sees 0 and stops without charging.
- **Run 3.** The check and the decrement are one statement, so there is no separate read that can go stale. B waits for A's lock. Under READ COMMITTED the database then **re-evaluates `remaining > 0` on A's committed value**, and B updates 0 rows.
- **Run 4.** The same buggy code is caught by the database: B gets `40001 restart transaction`, retries, sees 0 and stops.

**Run 2 or run 3?** Run 2 holds the lock from the read until the commit, so it suits a purchase that needs further checks while the item is reserved. Run 3 holds it only for one statement, which suits a hot counter that hundreds of players hit at once.

## 3.3 The $50 daily limit: write skew

The player (SteelSniper642) has spent **$40** today and makes **two $8 purchases** at the same moment. Each one checks "today's spend + $8 ≤ $50?" and then inserts a **new** purchase row.

| Run | How the app checks | Isolation | Accepted | Spent today | Verdict |
|---|---|---|---|---|---|
| 1 | sum today's purchases, then insert | READ COMMITTED | 2 | **$56** | ❌ limit broken |
| 2 | sum with `FOR UPDATE` on the summed rows, then insert | READ COMMITTED | 2 | **$56** | ❌ limit broken |
| 3 | lock the player's row first, then sum and insert (procedure) | READ COMMITTED | 1 | $48 | ✅ |
| 4 | as run 1 | SERIALIZABLE | 1 | $48 | ✅ (40001, retry) |

- **Why it differs from 3.2.** The two purchases write **different, new rows**, so neither overwrites the other. Each decision is correct on its own, and together they break the rule. This is **write skew**.
- **Run 2: locking the rows you read is not enough.** B really does wait ~0.5 s for A's locks, and still sees $40. It locked the two earlier purchases, but A's new $8 row did not exist when B looked. A row that appears after the read is a **phantom**, and a row that does not exist yet cannot be locked.
- **Run 3: lock one shared row.** Every purchase by the player first locks the player's current row, so purchases by the same player queue up. B waits, then sums **$48** and is refused (`23514 daily limit: spent 4800 + 800 > 5000`).
  - The app role is not allowed to lock `player` rows: `FOR UPDATE` needs the UPDATE privilege, and P1 grants the app only SELECT/INSERT. So the check runs in a `SECURITY DEFINER` procedure, the same pattern as `sign_up` (D15).
  - The player row is locked, never changed, so the never-edited rule still holds.
- **Run 4.** CockroachDB notices that B's sum was based on data A then changed: `40001`, retry, $48, refused.

## 3.5 A cross-region gift: atomic commit

A gift writes the purchase in the **payer's** region and the ammo credit in the **recipient's** region (P1: REGIONAL BY ROW). The payer is BoldCobra432 (eu-west), and the session runs on the eu-west node. Writes as sent by the gateway, from CockroachDB's KV trace (`SHOW KV TRACE FOR SESSION`):

| | Purchase write | Ammo write | Commit (`EndTxn`) |
|---|---|---|---|
| Run 1: local gift → WildRanger615 (eu-west) | n2 eu-west | n2 eu-west | n2 eu-west |
| Run 2: cross-region gift → FrostRanger658 (af-west) | n2 eu-west | **n3 af-west** | n2 eu-west |

- **One transaction, two regions, one decision.** In run 2 the ammo is written as a provisional value (an *intent*) on the af-west node. The commit is still **one** `EndTxn`, sent to the transaction record stored with the first write (the eu-west purchase). When that record becomes COMMITTED, every intent of the transaction is committed with it. This is CockroachDB's atomic commit across ranges, its counterpart to two-phase commit. The single transaction record removes 2PC's separate "prepare" round.
- **Run 3: all or nothing.**
  1. The eu-west purchase is written and is visible inside the transaction.
  2. The af-west ammo write is then rejected (`CHECK (change > 0)`).
  3. After ROLLBACK, **0 purchase rows in eu-west and 0 ammo rows in af-west**. No gift is ever "paid but not delivered".
- **Timing (20 gifts each).** Local median 3.8 ms, cross-region median 4.2 ms. All nodes run on one laptop, so the difference is small. In a real deployment the ammo write is a round trip to another continent.
- **Every write already crosses regions.** `SURVIVE REGION FAILURE` (P1) means even a local write must be acknowledged by replicas in another region. That cost is the price of surviving the loss of a whole region, and portfolio 5 discusses it.

**Lease placement.** A new REGIONAL BY ROW table is split by region within seconds. The leaseholders, however, move to their preferred region only on the allocator's background scan, which visits each range about every 10 minutes. The script therefore asks for the move its zone configuration already prescribes (`ALTER RANGE … RELOCATE LEASE`) and prints the placement before running.

## Summary

| | 3.2 last pack | 3.3 daily limit |
|---|---|---|
| Anomaly | lost update (same row) | write skew (new, different rows) |
| Lock the rows you read | ✅ fixes it | ❌ phantoms slip through |
| Lock one shared guard row | — | ✅ fixes it |
| SERIALIZABLE, unchanged code | ✅ 40001 + retry | ✅ 40001 + retry |

- **Under READ COMMITTED, every rule needs hand-made protection, and the obvious protection can be wrong** (3.3 run 2).
- **SERIALIZABLE**, CockroachDB's default, catches both anomalies in unchanged code. The price is that the application must retry on `40001`. Squadfire keeps SERIALIZABLE and adds the explicit fixes (3.2 run 3, 3.3 run 3) on the hottest paths, where retries would be most frequent.
- **3.5:** a transaction spanning regions is still all-or-nothing, decided by one transaction record.

## An unplanned failure

During this portfolio the af-west CockroachDB node stopped twice by itself (exit code 7). Its log says:

```
clock synchronization error: this node is more than 400ms away from at least half of the known nodes
```

- **What this check is.** CockroachDB orders transactions by timestamp. A node whose clock seems out of bounds shuts down rather than risk wrong answers.
- **The cause here.** Both stops were overnight, which points to the laptop sleeping and the containers waking unevenly.
- **What happened to the cluster.** It kept serving reads and writes on the two remaining nodes, because 2 of 3 replicas is still a majority. The node caught up by itself after a restart. Portfolio 5 repeats this failure on purpose.

## Run it

```bash
make up-core && make seed                 # data from portfolio 1
make p3 EXP=3.2_last_pack
make p3 EXP=3.3_daily_limit
make p3 EXP=3.5_cross_region_gift
```
