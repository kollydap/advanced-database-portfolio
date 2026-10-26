# Decision log — Squadfire

Each design decision, the options considered, and the reason for the choice.
Written as the design was brainstormed, before anything was built.

## D1. The game (case study)

| | Decision |
|---|---|
| **Game** | **Squadfire**, a tactical squad shooter: players form squads, play matches, and buy in-game ammunition and gear |
| **What players buy** | Ammo types (e.g. 5.56 mm, 7.62 mm, shotgun shells, armour-piercing), grenades, med kits, weapon skins. Paid **in US dollars**; prices set in US dollars |
| **Regions (game servers)** | `us-east`, `eu-west`, `af-west` (Lagos) |

**Options considered:**
- **Game type:** tactical shooter, battle royale, or sci-fi shooter.
- **Regions:** global regions, or three African regions.

**Why:**
- Ammo is a *consumable*: it is bought, then used up in matches. That gives a clear inventory ledger and a natural concurrency story (buying while playing).
- Squads and matches give player interactions, suitable for a graph model.
- Three continents make region-based fragmentation and the network latency between regions realistic and easy to explain.

## D2. Player journeys (scope)

| In scope | Out of scope |
|---|---|
| J1 Browse the store by region and item type | J9 Player-to-player trading (two-sided transactions and abuse rules: too much for this project) |
| J2 Buy for yourself, in USD | |
| J3 Gift to a friend (one player pays, another receives) | |
| J4 Play a match and use ammo | |
| J5 Social: friend requests, squads | |
| J6 Limited drops (scarce shared stock) | |
| J7 Daily spend limit per player | |
| J8 Refunds | |

**Ammo usage (J4):**
- Every shot goes to **telemetry (Kafka → lake)**.
- The database records **one usage row per ammo type at the end of each match**.

**Why:**
- J6 and J7 give two *different* concurrency problems for P3: a race on shared stock (lost update) and a race on a per-player rule (write skew).
- Writing every shot to the transactional database would overload it. Splitting high-volume events (lake) from business state (OLTP) is the reason the system has both.

## D3. Money and purchases

| | Decision |
|---|---|
| **Storage** | Integer **cents** (`BIGINT`): $1.99 is stored as `199`. Never floating point |
| **Prices** | **Regional**: each item has its own price in each region (e.g. cheaper in `af-west`). Prices change over time |
| **How ammo is sold** | In **packs** (e.g. 5.56 mm × 120 rounds = $1.99). The player buys 1–10 packs, and inventory receives packs × rounds |
| **Purchase shape** | A **basket**: one `purchase` (one payment) with several `purchase_line`s |
| **Payment flow** | Simulated card gateway: `PENDING → SUCCEEDED / FAILED`, and later `REFUNDED`. Inventory is granted **only** in the same transaction as a successful payment |
| **Daily limit** | $50 per player per UTC day, counting gifts the player pays for. Refunds give the amount back |

**Options considered:**
- One global price vs regional prices.
- One item per purchase vs a basket.

**Why:**
- Floating point cannot represent cents exactly, and money must reconcile to the cent.
- Regional prices fit horizontal fragmentation and give the warehouse price history (SCD2).
- A basket is a classic parent–child design, which is useful for the normalisation write-up.
- Granting inventory only in the payment's transaction makes "paid but no ammo" impossible.

## D4. Entities and relationships

```
REGION ─┬─< PLAYER >─┬─< FRIENDSHIP >─ PLAYER
        │            ├─< PURCHASE (payer) ──< PURCHASE_LINE >── ITEM
        │            │        └── recipient = PLAYER     (gift when payer ≠ recipient)
        │            ├─< INVENTORY >── ITEM               (append-only chain via previous_id)
        │            └─< MATCH_PLAYER >── MATCH >── REGION
        ├─< ITEM_PRICE >── ITEM                           (regional price, changes over time)
        └─< LIMITED_DROP >── ITEM                         (scarce stock, `remaining` counter)
```

**11 tables:** `region`, `player`, `friendship`, `item`, `item_price`, `limited_drop`, `purchase`, `purchase_line`, `inventory`, `match`, `match_player`.

| Question | Decision | Why |
|---|---|---|
| Q1. Payment | A **status on `purchase`**, not a separate table | A failed attempt is a failed purchase, and the player simply buys again. One table fewer. |
| Q2. Daily limit | **Sum today's purchases** on every check (no counter table) | Gives a natural **write-skew** case for P3 |
| Q3/Q4. Player ammo | **One `inventory` table, never edited.** Each row is the player's balance for one item *after* a change, with `previous_id` pointing to the row it replaced, plus `change`, `reason` (PURCHASED, GIFT_IN, USED, REFUNDED) and a reference | History and current balance in one place. `UNIQUE (previous_id)` lets a row be replaced only once, so two concurrent changes cannot both apply: lost updates become impossible. |
| Q5. Limited drop | **`remaining` counter** on `limited_drop` | A deliberately *hot row*: many buyers, one row. Gives the classic **lost update** demo and its fix (`FOR UPDATE` / SERIALIZABLE). |
| `purchase` history | **Same rule as `inventory`:** never edited. A status change (e.g. refund) is a new row with `previous_id` | One consistent rule: history is never overwritten |
| Squads | Exist **only inside a match**, as a **call-sign** (`Alpha`, `Bravo`, … as an enum) on `match_player` | No extra tables. Same match + same squad = teammates; same match + different squad = opponents. That is enough for interaction tracking. |

**Considered and left out:**
- A separate `payment` table.
- A daily-spend counter table.
- A separate ammo ledger table.
- Persistent parties / clans.
- Weapon ↔ ammo compatibility, achievements and seasons.

**Inventory rules:**
1. `UNIQUE (previous_id)`
2. Only one starting row per player × item: a partial unique index `WHERE previous_id IS NULL`
3. `CHECK (rounds >= 0)`

## D5. History: which tables are never edited

| Table | Rule | Form |
|---|---|---|
| `player` | Never edited | **Chain** (`previous_id`, `root_id`) |
| `friendship` | Never edited | **Chain** (requested → accepted → removed) |
| `purchase` | Never edited | **Chain** (e.g. succeeded → refunded) |
| `inventory` | Never edited | **Chain** (balance after each change) |
| `item_price` | Never edited | **Dated list** (`effective_from`) |
| `match`, `match_player`, `purchase_line` | Insert-only | Written once |
| `limited_drop` | **Mutable (deliberate)** | `remaining` is decremented: the hot row for P3 |
| `item` | Mutable | The detailed history lives in MongoDB (D6) |
| `region` | Fixed reference data | — |

**Terms:**
- **Append-only** is the rule: only `INSERT`, never `UPDATE` or `DELETE`.
- A **chain** is one way of organising append-only rows: each row points to the row it replaces (`previous_id`).
- A **dated list** orders rows by date instead.

Every chain is append-only, but not every append-only table is a chain.

**When to use which:**
- A **chain** where each new state is *calculated from* the previous one (balances, statuses). `UNIQUE (previous_id)` guarantees nobody changed it in between.
- A **dated list** where each row is an independent fact valid *from a date* (prices).

**`root_id`:** every row of a chain stores the id of the chain's first row. It is the entity's permanent identity:
- `purchase_line` references the purchase's root.
- `friendship` and `match_player` reference the player's root.
- Rules such as "one refund per purchase" are enforced on `root_id`.

**Enforcement:** two layers.
- A trigger rejects `UPDATE` and `DELETE` on append-only tables.
- The application role has only `SELECT` and `INSERT` on them.

`limited_drop` and `item` are the documented exceptions.

## D6. Which data goes in which store

| Store | Holds | Role |
|---|---|---|
| **CockroachDB** | All 11 tables | **Source of truth.** ACID, constraints, SERIALIZABLE |
| **MongoDB** | Item catalogue details: attributes that differ by item type (calibre, damage, penetration; blast radius; rarity), plus revision history | Flexible documents. CockroachDB keeps a slim `item` row (sku, type, rounds per pack) so purchases and inventory have real foreign keys |
| **Redis** | Leaderboards (sorted sets), store cache, purchase cooldowns, recently viewed, a **display copy** of drop stock | Speed. Everything is rebuildable |
| **Neo4j** (added) | The player interaction graph: friends, teammates, opponents, gifts | Relationship queries (friends of friends, gift-farming rings). Edges are loaded from the lake |
| **Kafka → HDFS lake → DuckDB** | CDC from CockroachDB and MongoDB, plus telemetry | Analytics, high volume |

**Limited-drop stock:**
- The truth stays in **CockroachDB**, so decrementing stock, recording the purchase and adding inventory are **one transaction**: all or nothing.
- **Redis is only a display copy**, updated through CDC.

**Alternative considered:** the drop counter in Redis (`DECR`). It is faster, but Redis and CockroachDB cannot share a transaction. A failed payment after a decrement needs a hand-written *compensating action* (`INCR` back), which can be missed, e.g. on a crash. This is the speed vs consistency trade-off for the write-up.

**Graph options considered:**
- (A) Recursive SQL / `$graphLookup` without a new database.
- (B) Neo4j. **Chosen:** a real graph model and query language (Cypher), and it completes the set the brief names (relational, document, key-value, graph).

## D7. Horizontal fragmentation by region

**Horizontal fragmentation:** a table's *rows* are split by region (`us-east`, `eu-west`, `af-west`), and each fragment is stored on that region's servers, so players read and write locally.

| Table | Fragment key |
|---|---|
| `player` | Home region (the anchor) |
| `inventory` | Owner's region |
| `purchase`, `purchase_line` | **Payer's** region |
| `item_price` | The price's region |
| `match`, `match_player` | The match server's region |
| `friendship` | Region of the player who sent the request |
| `region`, `item` | **Not fragmented (GLOBAL):** small reference data, replicated to every region |

| Case | Decision | Consequence |
|---|---|---|
| Cross-region gift | **Allowed** | One transaction writes the purchase (payer's region) and the inventory (recipient's region): a **distributed transaction**, measured local vs cross-region in P3/P5 |
| Limited drop | **One drop per region** | Each drop row is local; no cross-region contention. A worldwide drop may be run as a P5 experiment to show why it is worse (decided in D9) |
| Where players play | **Home region only** | A match and its players share one fragment. Moving region = a new `player` version |

**Other stores:**
- MongoDB storefront documents are zone-sharded by region.
- Redis keys are tagged by region.
- Neo4j is a single global instance (a limitation to discuss).

## D8. Telemetry and interaction events

**Telemetry** flows game server → Kafka → HDFS lake. It never goes into CockroachDB.

| Event group | Used for |
|---|---|
| `match_started` / `match_ended` | Match length, mode popularity |
| `shot_fired` (highest volume) | Ammo used vs bought; justifies Hadoop/Spark |
| `hit` | Accuracy, item balance |
| `kill` / `assist` / `revive` | Leaderboards, interaction edges |
| `session_start` / `session_end` | Play time vs spending, retention |
| `store_view` / `store_click` | Funnel: viewed → clicked → bought |

**Neo4j edges** (aggregated in the gold layer: one edge per pair, with a count):

```
(:Player)-[:FRIENDS_WITH {since}]->(:Player)         from friendship (CDC)
(:Player)-[:GIFTED {count, total_cents}]->(:Player)  from purchase where payer ≠ recipient
(:Player)-[:TEAMMATE {matches}]->(:Player)           from match_player (same match + same squad)
(:Player)-[:KILLED {count}]->(:Player)               from kill telemetry
```

**Simulator:**
- A match simulator writes matches and end-of-match `inventory` USED rows to CockroachDB, and sends the matching events to Kafka.
- The two sides agree: the rounds fired in telemetry equal the USED rounds in the inventory chain.
- Target volume: **~3–5 million events** (reduced to ~200k by D16).

**Privacy:** generated handles only. No real names, no chat text, nothing personal in the lake.

## D9. What each portfolio proves

| Portfolio | Experiments |
|---|---|
| **P1 Design** | 1.1 ERD and case statement · 1.2 schema with keys and constraints (`UNIQUE(previous_id)`, chain-start partial unique, `CHECK(rounds >= 0)`, cents) · 1.3 "never edited" enforced (trigger + permissions) · 1.4 normalisation 0NF → BCNF |
| **P2 Queries** | 2.1 current ammo (latest chain row: `NOT EXISTS` vs `LIMIT 1`) · 2.2 purchase and gift history (composite/covering index) · 2.3 today's spend (hot path) · 2.4 full chain history (recursive CTE) · 2.5 region-local vs unknown region in the plan · 2.6 MongoDB `explain()` |
| **P3 Transactions** | 3.1 commit/rollback on a declined card · 3.2 last limited-drop pack (lost update → `FOR UPDATE`) · 3.3 daily limit (write skew: READ COMMITTED vs SERIALIZABLE 40001) · 3.4 concurrent inventory changes stopped by `UNIQUE(previous_id)` · 3.5 cross-region vs local gift |
| **P4 NoSQL** | 4.1 MongoDB type-specific validation · 4.2 Redis leaderboards and drop display copy · 4.3 Neo4j: friend suggestions, gift-farming, rivalries · 4.4 suitability write-up |
| **P5 Distributed** | 5.1 fragmentation proofs · 5.2 node failure: sync vs async replication · 5.3 injected latency · 5.4 CDC + telemetry → Kafka → Spark → HDFS → DuckDB OLAP · 5.5 *(optional, if time allows)* worldwide vs per-region drop |

**Why this shape:** 3–5 experiments per portfolio, each tied to a decision above, so each can be explained from its decision.

## D10. Schema, group 1: region, player, player_handle, friendship

**How fragmentation is implemented (CockroachDB):**
1. Nodes are started with `--locality=region=…`.
2. The database is made multi-region (`PRIMARY REGION us-east`, `ADD REGION eu-west, af-west`, `SURVIVE REGION FAILURE`).
3. Tables are declared `LOCALITY REGIONAL BY ROW` (fragmented: a hidden `crdb_region` column, one partition = one fragment per region, home copy on that region's node) or `GLOBAL` (not fragmented: full copy in every region).
4. Each row's `crdb_region` is set on insert. Child rows take their parent's region (derived fragmentation).

**Manual vs managed fragmentation.** Separate tables per region (`player_eu_west`, …) were considered. The choice is **one table partitioned by the database** (fragmentation transparency), because manual tables would break foreign keys and uniqueness across fragments, need application routing and `UNION ALL` everywhere, and triple every schema change. The textbook fragments are still created as **views** (`player_us_east`, `player_eu_west`, `player_af_west`) and used to prove completeness, reconstruction and disjointness.

**Replication alongside fragmentation.**
- Each fragment's home copy (leaseholder) is in its own region, with backup copies on the other nodes (`SURVIVE REGION FAILURE`), because there is only one node per region.
- Reads stay local. Writes pay a cross-region quorum round trip.
- In production: 3 nodes per region and `SURVIVE ZONE FAILURE`, which keeps writes local too.

| Table | Locality | Key columns | Rules |
|---|---|---|---|
| `region` | GLOBAL | `code` (region type), `name` | 3 fixed rows |
| `player` | REGIONAL BY ROW (home region) | `id`, `root_id`, `previous_id`, `handle`, `status` (ACTIVE/SUSPENDED/BANNED), `reason`, `created_at` | `UNIQUE(previous_id)`; one first version per `root_id`; first version has `root_id = id`; `(handle_key, root_id)` must be owned in `player_handle` |
| `player_handle` | **GLOBAL** | `handle_key` (lowercase name, PK), `root_id` | **Insert-only name ownership**: a name belongs to one player forever (never released) |
| `friendship` | REGIONAL BY ROW (requester's region) | `id`, `root_id`, `previous_id`, `player_low`, `player_high`, `requested_by`, `status` (REQUESTED/ACCEPTED/DECLINED/REMOVED), `created_at` | `player_low < player_high` (sorted pair); one chain per pair, re-friending continues it |

**Decisions:**
- **Unique handles.** A plain `UNIQUE(handle)` fails because old versions keep old names, and an `is_current` flag would break the never-edited rule. The ownership table enforces uniqueness append-only.
  - It is GLOBAL so uniqueness holds across regions. Its writes are slower, but sign-ups and renames are rare.
  - Trade-off: names are never freed.
- **Foreign keys to a player** always point at the first version's `id` (= `root_id`). That id is unique and permanent across renames and region moves.
- **No email or password stored.** Authentication belongs to a separate account system, so the database holds no personal data.
- **Player `status`** lets moderation (e.g. bans from P4's gift-farming detection) be recorded as new versions.
- **Friendship pairs are sorted**, so A→B and B→A are the same friendship and cannot be duplicated. `requested_by` records who asked.

## D11. Schema, group 2: item, item_price, limited_drop

| Table | Locality | Key columns | Rules |
|---|---|---|---|
| `item` | GLOBAL, mutable | `sku` (PK, e.g. `AMMO-556-120`), `name`, `item_type` (AMMO/GRENADE/MEDKIT/SKIN), `units_per_pack`, `consumable`, `active` | `units_per_pack > 0`. MongoDB holds the rich document under the same `sku` |
| `item_price` | REGIONAL BY ROW, dated list | `crdb_region`, `sku`, `effective_from`, `price_cents` | PK `(crdb_region, sku, effective_from)`; `price_cents > 0`. Current price = latest `effective_from ≤ now()`. **Future prices may be scheduled** |
| `limited_drop` | REGIONAL BY ROW, **`remaining` mutable** | `id`, `crdb_region`, `sku`, `price_cents`, `total_packs`, `remaining`, `starts_at`, `ends_at` | `0 ≤ remaining ≤ total_packs`; `ends_at > starts_at` |

**Decisions:**
- **Drops are per region** (confirming D7). A central worldwide drop homed in one region was reconsidered: it is simpler, but remote players pay latency and lose races, so it is unfair. It stays as optional experiment 5.5.
  - A central drop is not a GLOBAL table: GLOBAL suits rarely written data, and a drop is written constantly.
- **The drop price lives on the drop row**, not in `item_price`. A drop is a self-contained event: one locked row gives stock and price together, and repeat drops of the same item keep their own prices. Purchase lines record the price actually paid, so reporting still works.
- **No per-player cap on drops.** The daily spend limit already provides a per-player rule.
- **`CHECK (remaining >= 0)` is only a safety net.** Two buyers who both read 12 and both write 11 break no constraint (a lost update). Locking is still required (P3).

## D12. Schema, group 3: purchase, purchase_line, inventory

| Table | Locality | Key columns | Rules |
|---|---|---|---|
| `purchase` | REGIONAL BY ROW (**payer's** region), chain | `id`, `root_id`, `previous_id`, `payer_id`, `recipient_id` (= payer, or a friend for a gift), `status` (SUCCEEDED/FAILED/REFUNDED), `total_cents`, `payment_method` (CARD/WALLET), `gateway_ref` (unique), `reason`, `created_at` | `UNIQUE(previous_id)`; one first version per `root_id`; **at most one refund per purchase** (`UNIQUE(root_id) WHERE status = 'REFUNDED'`) |
| `purchase_line` | REGIONAL BY ROW (same as its purchase), insert-only | `purchase_id` (→ root), `sku`, `packs` (1–10), `unit_price_cents` (price paid), `line_total_cents`, `drop_id` (nullable) | PK `(purchase_id, sku)`; `line_total = packs × unit_price` |
| `inventory` | REGIONAL BY ROW (**owner's** region), chain without `root_id` | `id`, `previous_id`, `player_id`, `sku`, `change`, `units_after`, `reason` (PURCHASED/GIFT_IN/USED/REFUNDED), `purchase_id` / `match_id`, `created_at` | `UNIQUE(previous_id)`; one first row per `(player_id, sku)`; `units_after ≥ 0`; reason ↔ reference `CHECK`; no double crediting: `UNIQUE(purchase_id, player_id, sku, reason)` and `UNIQUE(match_id, player_id, sku)` |

| Question | Decision | Why |
|---|---|---|
| Q9 `PENDING` status? | **No** | The simulated gateway answers inside the transaction, so a purchase is saved directly as SUCCEEDED or FAILED |
| Q10 Who can receive gifts? | **Accepted friends only**, checked in the purchase transaction | Stops spam and makes gift-farming harder |
| Q11 `root_id` on inventory? | **No** (kept on `player`, `purchase`, `friendship`) | `root_id` has two jobs: grouping versions, and being a stable target for foreign keys. On inventory, `(player_id, sku)` never changes and already groups the chain, and no table references an inventory chain, so `root_id` would be redundant (the same fact stored twice). `friendship` keeps it for consistency with the other entity chains |
| Q12 Arithmetic guarantee | **`BEFORE INSERT` trigger** checks `units_after = previous.units_after + change` | A `CHECK` cannot see the previous row; the trigger lets the database itself guarantee the chain adds up |
| Q13 Refund after the ammo was used | **Allowed only if the player still holds all the units**; otherwise rejected by `units_after ≥ 0` | No negative balances, and a refund removes exactly what was bought |

**Daily $50 check:** the sum of the *current* versions of the payer's purchases created today with status SUCCEEDED. Refunded purchases drop out automatically.

**Cross-region gift:** the purchase is written in the payer's region and the recipient's inventory row in the recipient's region, in one distributed transaction.

## D14. Schema, group 4: match, match_player

| Table | Locality | Key columns | Rules |
|---|---|---|---|
| `match` | REGIONAL BY ROW (server's region), insert-only | `id`, `mode` (TEAM_DEATHMATCH/SEARCH_AND_DESTROY/DOMINATION), `map`, `started_at`, `ended_at`, `winning_squad` (call-sign, NULL = draw) | `ended_at > started_at`; **written once at match end** (live progress is only in telemetry) |
| `match_player` | REGIONAL BY ROW (match's region), insert-only | `match_id`, `player_id`, `squad` (Alpha…Foxtrot), `kills`, `deaths`, `assists` | PK `(match_id, player_id)`; counts ≥ 0 |

**Decisions:**
- **Ammo used** is recorded in `inventory` as `USED` rows pointing to the match, not in `match_player`. Every shot goes to telemetry.
- **Kills, deaths and assists are stored per match** even though telemetry holds every kill: a deliberate, documented denormalisation so leaderboards and the warehouse get totals quickly.
- **Squad rules** (max 4 per squad, up to 6 squads): followed by the simulator and verified by a P1 integrity query. A row-count rule cannot be a simple constraint.

**The schema is complete: 12 tables.**
- Groups 1–4 (D10–D12, D14): `region`, `player`, `player_handle`, `friendship`, `item`, `item_price`, `limited_drop`, `purchase`, `purchase_line`, `inventory`, `match`, `match_player`.
- D13 adds no table; it decides how history is kept.

## D13. JSON change log vs chains

**Option considered:** mutable rows with a JSON `change_log` column holding the history.

**Rejected for the core tables** (`inventory`, `purchase`, `player`, `friendship`):
- Lost updates return.
- The history becomes editable.
- The JSON grows without bound, and every update rewrites it.
- No foreign keys, `CHECK`s or unique rules on history entries.
- History is hard to query or index.
- It violates 1NF (a repeating group in one column).
- CDC would send the whole growing row on each change.

**Used where it fits:** a small JSON `change_log` on `item` (mutable, rarely edited by staff, no integrity rules needed). It also shows a JSON column with an inverted index.

## Background: isolation levels (notes)

- **READ COMMITTED:** each statement sees data committed at that moment. Two concurrent spend-limit checks can both pass (write skew), unless the code locks.
- **SERIALIZABLE:** the result must equal *some* one-after-another order. A transaction whose reads were invalidated gets error **40001** and must retry. This is CockroachDB's default.

## D15. Name claims only through procedures

**Problem:** `player → player_handle` is a foreign key, but the reverse direction can't be one: CockroachDB has no deferred constraints, so with both links no insert order works. A claim inserted on its own would reserve a name for nobody (an **orphan claim**).

**Decision:**
- Two `SECURITY DEFINER` procedures write the claim and the player version together, in one transaction:
  - `sign_up(id, handle, region)`
  - `rename_player(player, handle)`: no new claim if the player already owns the name
- `squadfire_app` loses INSERT on `player_handle` (keeps SELECT) and gets EXECUTE on both procedures.
- INSERT on `player` stays: moves and suspensions add versions directly, and the foreign key still forces a name the player owns.
- The P1 integrity query "orphan claims = 0" stays as the final proof.

**Evidence:** rule tests T1 (sign-up through `CALL`), T21 (direct claim rejected, 42501), T22 (rename), T23 (rename to a taken name rejected, nothing written).

## D16. Minimal scope (replaces the experiment list in D9)

**Decision:** keep the whole architecture and every data flow, and prove each **once, with small data**. The remaining build moves faster: Claude builds each step, explains it, and commits after approval.

| Portfolio | Kept |
|---|---|
| **P1** | ERD, schema and rules (done), normalisation 0NF → BCNF, integrity checks |
| **P2** | 2.1 current ammo (index before/after) · 2.3 today's spend · 2.5 region-local vs unknown region · one MongoDB `explain()` |
| **P3** | 3.2 last drop pack (lost update) · 3.3 daily limit (write skew, READ COMMITTED vs SERIALIZABLE) · 3.5 cross-region gift. Commit/rollback and `UNIQUE(previous_id)` are already shown by the rule tests (T12–T16) |
| **P4** | MongoDB catalogue with a validator + one aggregation · Redis leaderboard + cache-aside · Neo4j: friends-of-friends, gift graph |
| **P5** | One changefeed (`purchase`, `inventory`) → Kafka → one Spark job → HDFS bronze/silver/gold → DuckDB star (1 fact, 3 dimensions) · CockroachDB node failure · Redis Sentinel failover |

**Dropped:** 2.2, 2.4, 3.1/3.4 as separate experiments, Debezium CDC from MongoDB, the MongoDB `w:1` rollback demo, 5.5.

**Data volume:** ~5k players, ~50k purchases, ~2.5k matches, ~200k telemetry events, instead of 3–5M. That's enough for indexes to change plans, and loads in minutes on 8 GB.

**Why:** the marking criteria are about design, reasoning and evidence, not data volume. Every store and flow is still shown working.
