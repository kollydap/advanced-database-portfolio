# Portfolio 5: Distributed and cloud

Squadfire's data is spread over many machines and several stores. This portfolio covers two questions:
- **Moving the data.** How changes in CockroachDB reach an analytics lake and warehouse without slowing down the store (5.1).
- **Surviving failures.** What happens when machines fail: a CockroachDB node (5.2) and a Redis master (5.3).

## Contents

| Experiment | Script | Evidence |
|---|---|---|
| 5.1a Changefeed: CockroachDB → Kafka | [`sql/changefeed.sql`](sql/changefeed.sql), [`scripts/5.1a_changefeed.py`](scripts/5.1a_changefeed.py) | [`evidence/5.1a_changefeed.txt`](evidence/5.1a_changefeed.txt) |
| 5.1b Telemetry: game servers → Kafka | [`scripts/5.1b_replay_telemetry.py`](scripts/5.1b_replay_telemetry.py) | [`evidence/5.1b_replay_telemetry.txt`](evidence/5.1b_replay_telemetry.txt) |
| 5.1c Spark: Kafka → HDFS lake | [`spark/lake.py`](spark/lake.py) | [`evidence/5.1c_lake.txt`](evidence/5.1c_lake.txt) |
| 5.1d DuckDB warehouse + reconciliation | [`scripts/5.1d_warehouse.py`](scripts/5.1d_warehouse.py) | [`evidence/5.1d_warehouse.txt`](evidence/5.1d_warehouse.txt) |
| 5.2 CockroachDB node failure | [`scripts/5.2_node_failure.py`](scripts/5.2_node_failure.py) | [`evidence/5.2_node_failure.txt`](evidence/5.2_node_failure.txt) |
| 5.3 Redis Sentinel failover | [`scripts/5.3_redis_failover.py`](scripts/5.3_redis_failover.py) | [`evidence/5.3_redis_failover.txt`](evidence/5.3_redis_failover.txt) |
| 5.4 CAP, PACELC and cloud deployment | this page, [below](#54-cap-pacelc-and-cloud-deployment) | |

## 5.1 The pipeline: from the store to the warehouse

```
CockroachDB ──changefeed──▶ Kafka squadfire.{purchase, purchase_line, inventory, item, region}
game servers ─────────────▶ Kafka squadfire.telemetry
                                 │
                       Spark Structured Streaming
                                 ▼
          HDFS /lake/bronze ──▶ /lake/silver ──▶ /lake/gold ──▶ DuckDB star schema
          raw messages          typed, deduped    star schema     manager questions,
          (as received)         partitioned       + daily sums    reconciliation
```

**Why this shape:**
- **The store isn't touched by analytics.** Reports never query CockroachDB. The changefeed pushes each committed change once, and everything downstream reads Kafka or the lake.
- **Telemetry never enters CockroachDB.** Shots, kills and sessions (434k events a month) don't need transactions, so game servers publish them straight to Kafka (D8).
- **The lake keeps history.** Every message is kept raw in bronze, so silver and gold can be rebuilt with different rules at any time.

### 5.1a Changefeed

```sql
CREATE CHANGEFEED FOR TABLE purchase, purchase_line, inventory, item, region
  INTO 'kafka://kafka:9092?topic_prefix=squadfire.'
  WITH initial_scan = 'yes', format = json, resolved = '10s', updated;
```

**What each option does:**
- `initial_scan` sends the existing history first.
- `updated` stamps each message with its commit timestamp.
- `resolved` markers say "everything up to time T has been sent".

Since v24.3, changefeeds need no licence. The job was created with four tables, and `region` was added later (PAUSE → `ALTER CHANGEFEED ADD` → RESUME), so the evidence printout shows the original four.

| Topic | Row messages | Distinct keys | CockroachDB rows | Match |
|---|---|---|---|---|
| `squadfire.purchase` | 47,430 | 47,430 | 47,430 | ✅ |
| `squadfire.purchase_line` | 66,563 | 66,563 | 66,563 | ✅ |
| `squadfire.inventory` | 94,880 | 94,880 | 94,880 | ✅ |
| `squadfire.item` | 11 | 11 | 11 | ✅ |

**A live change:** setting the Gold Desert Eagle skin inactive and back to active showed up in Kafka **392 ms and 783 ms after commit**. Both versions stay in the topic, so the lake keeps the history. The commit itself took ~620 ms because `item` is a GLOBAL table (see Operational findings in the top-level README).

### 5.1b Telemetry

433,978 events were replayed into `squadfire.telemetry` in 2.1 s. The topic has 3 partitions, keyed by player, so each player's events stay in order.
- **Idempotent producer.** The producer has idempotence switched on, so a network retry can't write a duplicate.
- **No double replay.** The script refuses to run if the topic already exists, so a second run can't duplicate the month.

### 5.1c The lake (Spark → HDFS)

| Layer | What | How |
|---|---|---|
| **Bronze** | every Kafka message as received (topic, partition, offset, timestamp, key, value) | Structured Streaming with `trigger(availableNow)` and a checkpoint: each run appends only what is new, then stops |
| **Silver** | typed tables | the changefeed delivers **at least once**, so CDC rows are de-duplicated on their key, keeping the latest `updated`. Partitioned by `crdb_region, dt` (purchase, inventory), `crdb_region` (purchase_line) and `region, dt` (telemetry), so a region/day query reads only its own files |
| **Gold** | warehouse tables | `fact_sales` + `dim_date`, `dim_item`, `dim_region`; daily summaries `daily_sales`, `daily_fired`, `daily_events` |

**Why bronze has more messages than silver has rows:** for example, 47,526 bronze messages for `purchase` become 47,430 silver rows. The extra ~100 messages are resolved markers. On `item`, 13 changes collapse to 11 rows: the two extra changes are the live test in 5.1a.

### 5.1d Warehouse and reconciliation

Gold is copied out of HDFS (`hdfs dfs -get`) and loaded into DuckDB. The fact table has **64,536 sales lines and 0 rows without a matching dimension**.

**Reconciliation: does the warehouse agree with the store?**

| Region | Packs (DW = CRDB) | Net revenue (DW = CRDB) | Match |
|---|---|---|---|
| af-west | 40,022 | $60,070.68 | ✅ |
| eu-west | 48,532 | $130,436.68 | ✅ |
| us-east | 50,516 | $125,486.94 | ✅ |

**A second check, through two independent paths:**
- Rounds fired, counted from telemetry (game servers → Kafka), equal the ammo and grenades marked USED in CockroachDB's inventory chains (store → changefeed): 818,316 / 1,155,322 / 1,233,500.
- Revives equal med kits used: 1,545 / 2,220 / 2,420.

**Manager questions** (star joins):
- **Best seller** in every region is Armour-Piercing 5.56 mm, at 23% of net revenue.
- **Gifts** are 17.8–19.3% of revenue. **Limited drops** are 0.9–2.0%.
- **Weekdays vs weekends:** weekdays bring slightly more revenue per day in all three regions.
- **Bought vs fired:** only **28–44%** of the ammo bought is fired. Players stockpile. 9 mm has the lowest use rate (28%), and 12-gauge the highest (44%).

## 5.2 A CockroachDB node fails, then two do

Every range has 3 replicas, one per node. Raft commits a write once **a majority (2 of 3)** has it. The script writes to a scratch table placed like `purchase` (REGIONAL BY ROW) and removes it at the end. The af-west rows start out led by the af-west node.

**Part A: the af-west node is killed (SIGKILL, like pulling the plug)**

| Step | Result |
|---|---|
| First af-west order right after the crash | committed after **3.4 s**: the survivors waited out the dead node's lease, then n1 took over leadership of af-west's rows |
| n3 marked not live | after 6 s |
| 20 orders per region with af-west down | **all 60 committed**, median ~2.5 ms; af-west's 13,350 past purchases still readable |
| Copies | 173 ranges under-replicated (2 of 3 copies), **0 unavailable** |
| Node restarted | live in 2 s; all copies back 12 s later. Read through the af-west node itself, it has **all 61 orders written while it was dead** |

A 3-node cluster has nowhere to make a third copy. With 5 or more nodes, CockroachDB would re-replicate onto the survivors after `server.time_until_store_dead` (5 min).

**Part B: eu-west and af-west killed; 1 node of 3 left**

| Request through the us-east node | Result |
|---|---|
| write a us-east order | no answer, `57014 statement timeout` after 8 s |
| read us-east orders | no answer after 8 s |
| read the item catalogue (GLOBAL) | no answer after 8 s |

**What Part B shows:**
- **The node refuses to answer.** It is up and accepts connections, but without a majority it can't be sure its data is current. It **stops rather than give an answer that might be wrong**: consistency over availability (CP).
- **Recovery:** after both nodes restarted, writes worked within 2 s. Every order from before was still there, and none appeared twice.

**Two findings that change how the app must be written.** These came from the earlier runs of the same script. Both depend on timing, so they don't happen on every run, and the saved evidence is from a run where neither did.

1. **"Result is ambiguous" (SQLSTATE 40003).**
   - **What happened:** on one run, the first write after the crash failed with `result is ambiguous ... failed to connect to n3`.
   - **Why:** the gateway had sent the write to the leaseholder, which then died. It can't know whether the commit happened.
   - **What the app must do:** an ambiguous error is **not** a failure. The app must check before retrying, or it could charge a player twice. The script now does what a correct app does: the order carries its own id, and after an ambiguous error the app looks that id up before retrying.
2. **A timeout is not a "no".**
   - **What happened:** on another run, the Part B write that timed out **committed later**. Its proposal was already in the Raft log, and it was applied once a majority came back.
   - **What the app must do:** the same as for an ambiguous error. The real store already supports this: every purchase carries the payment provider's reference, and a unique index (`purchase_gateway_ref_once`, P1) lets a retried purchase fail with a duplicate error instead of charging twice.

## 5.3 Redis Sentinel failover

**The setup:**
- **Replication:** Redis has 1 master and 2 replicas, and copies to them **asynchronously**. The master says OK before any replica has the write.
- **Sentinels:** 3 sentinels watch the master. When 2 of them (the quorum) can't reach it for 5 s, they vote and promote a replica.
- **The failure:** a network partition (`docker network disconnect`), not a crash. It's the harder case, because the old master stays alive on the wrong side of the split.

| Step | Result |
|---|---|
| Before | `SET` answered OK, then `WAIT 2` confirmed both replicas had it (asynchronous) |
| Master cut off | the sentinels promoted `redis-replica-1` **6.7 s** after the split; all 3 agreed. The app asked a sentinel for the master, followed it and wrote 20 of 20 |
| …meanwhile, on the old master's side | a game server stuck there wrote for ~20 s: **100 of 100 writes answered OK**. The old master can't tell it was cut off and still reports `role: master` |
| Split healed | the sentinels turned the old master into a replica of the new one. It copied the new master and **threw away all 100 writes it had said OK to**; the app's 20 writes on the new master were kept |
| **Fix:** `min-replicas-to-write 1`, `min-replicas-max-lag 3` | split again: the cut-off master refused writes **4.1 s** after the split (`NOREPLICAS Not enough good replicas to write`). 23 of 100 were accepted and lost, 77 refused |
| Restore | `redis-master` made master again for 4.2 (other replicas set to `replica-priority 0`, then `SENTINEL FAILOVER`) |

**What this means for Squadfire:**
- **The fix limits the loss but can't remove it.** Writes are lost for at most the lag limit plus the detection time, however long the split lasts. Redis has no majority commit, so it can't do better than that.
- **That's fine for what Redis holds.** Losing a few seconds of leaderboard increments or cached pages is acceptable, because both can be rebuilt from CockroachDB. Losing money is not, which is why money never lives in Redis (D7). Compare 5.2, where CockroachDB refused straight away and lost nothing.

**Operational lessons from building the experiment:**
- **Announce hostnames, not IPs.** A node that rejoins after a partition gets a new IP address. The sentinels only knew the replicas by their old IPs, so on the first run they could never find `redis-replica-1` again, and promoted a stale entry. Fix: each node announces its hostname (`replica-announce-ip`, now in `docker-compose.yml`), and the script clears stale sentinel entries (`SENTINEL RESET`) before it starts.
- **A reconnected container's published port goes stale** (Docker Desktop). The script restarts those nodes while they are replicas, which is harmless, so `localhost:16379` works again afterwards.

## 5.4 CAP, PACELC and cloud deployment

**CAP** says: during a network partition (P), a system must choose between consistency (C) and availability (A). **PACELC** adds: else (E), when there is no partition, it chooses between latency (L) and consistency (C).

| Store | During a partition | Normally | Evidence |
|---|---|---|---|
| **CockroachDB** | **PC**: the minority side refuses to answer | **EC**: Raft majority on every write; GLOBAL tables pay ~620 ms writes for consistent local reads | 5.2 Part B; 5.1a commit times; P3 3.5 |
| **Redis + Sentinel** | **PA**: both sides accept writes; the losing side's are discarded | **EL**: the master answers before replicas have the write | 5.3 |
| **MongoDB** (zone-sharded, replica sets) | **PC** for majority writes (`w:"majority"`); a cut-off primary steps down. With `w:1` acknowledged writes can be rolled back, like Redis | **EL** by default for reads from secondaries; EC with majority read/write concerns | P2 2.6 (sharding); not failure-tested here |
| **Kafka** (one broker here) | a real cluster with `acks=all` + `min.insync.replicas=2` is CP for the log | | 5.1b (idempotent producer) |

**Squadfire's choice:** each kind of data goes to the store whose trade-off it can live with.
- **Money, stock and history** need CP: a buyer must never see a balance that isn't real.
- **Leaderboards, caches and the storefront copy** take AP, or a shorter wait, because a few stale or lost seconds are harmless and can be rebuilt.

**Cloud deployment (what this laptop simulates):**

| Here | In the cloud |
|---|---|
| 3 containers with `--locality=region=…` | CockroachDB in 3 cloud regions, **3 nodes per region** (9), across availability zones. `SURVIVE REGION FAILURE` then keeps 5 replicas and survives a whole region going down. Managed option: CockroachDB Cloud (Dedicated/Standard) |
| MongoDB 3 single-node zone shards | each shard a 3-member replica set in its region (MongoDB Atlas global clusters with zone sharding) |
| Redis master + 2 replicas + 3 sentinels | a managed Redis per region (e.g. ElastiCache / Memorystore) with Multi-AZ automatic failover; a separate cache per region, since it is rebuildable |
| 1 Kafka broker | 3+ brokers (MSK / Confluent Cloud), replication factor 3, `min.insync.replicas=2` |
| HDFS namenode + datanode | object storage (S3 / GCS / Azure Blob) as the lake, Spark on EMR / Dataproc / Databricks, DuckDB → a cloud warehouse (BigQuery, Snowflake, Redshift) |
| clock: laptop sleep stopped nodes | NTP / the cloud's time-sync service (Amazon Time Sync, Google's NTP); CockroachDB stops a node more than 400 ms off rather than risk wrong ordering |

The trade-off: managed services remove most of the operational failures found here (clock drift, stale leaseholders, failover wiring). In return they cost more, and data placement and residency must be set per provider.

## Run it

```bash
make up-core && make seed                 # CockroachDB, MongoDB, Redis + Sentinel, Neo4j
make up-stream up-bigdata                 # Kafka, HDFS, Spark
make p5-changefeed                        # once
make p5 EXP=5.1a_changefeed
make p5 EXP=5.1b_replay_telemetry         # once (refuses to replay twice)
make p5-lake                              # Spark: bronze/silver/gold
make p5 EXP=5.1d_warehouse                # DuckDB load + reconciliation
make p5 EXP=5.2_node_failure              # kills and restarts CockroachDB nodes (~1 min)
make p5 EXP=5.3_redis_failover            # partitions Redis twice, then restores redis-master (~2 min)
```

**How each experiment cleans up:**
- **5.2** restarts any node it killed and drops its scratch table, even if it stops early.
- **5.3** ends with `redis-master` as master again. If it is interrupted halfway, run it again: it resets the sentinels first.

If it says the master is not `redis-master`, fail back by hand:
1. Set `replica-priority 0` on the other replica.
2. Run `SENTINEL FAILOVER squadfire-master`.
