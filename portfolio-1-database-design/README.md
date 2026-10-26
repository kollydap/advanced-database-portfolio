# Portfolio 1: Database design and modelling

**Case study:** Squadfire, a real-time multiplayer game with a leaderboard, extended with an in-game store where players buy and gift ammunition in USD. It runs in three regions (`us-east`, `eu-west`, `af-west`). The relational core is CockroachDB, a distributed SQL (NewSQL) database.

## Contents

| File | What it is |
|---|---|
| [`erd.md`](erd.md) | Entity–relationship diagram of the 12 tables ([image](../docs/screenshots/p1-erd.png)) |
| [`normalisation.md`](normalisation.md) | From a purchases spreadsheet (0NF) to BCNF, and the deliberate denormalisations |
| [`sql/00_database.sql`](sql/00_database.sql) | Multi-region database: 3 regions, survives a region failure |
| [`sql/01_schema.sql`](sql/01_schema.sql) | 12 tables with keys, foreign keys, `CHECK`s, partial unique indexes and regional placement |
| [`sql/02_rules.sql`](sql/02_rules.sql) | Triggers, sign-up/rename procedures, roles and privileges |
| [`sql/03_views.sql`](sql/03_views.sql) | Current-state views and the regional fragment views |
| [`sql/04_rule_tests.sql`](sql/04_rule_tests.sql) | 23 tests: each rule tried the right way and the wrong way → [`evidence/rule-tests.txt`](evidence/rule-tests.txt) |
| [`sql/05_integrity.sql`](sql/05_integrity.sql) | 11 checks over the generated data → [`evidence/integrity.txt`](evidence/integrity.txt) |
| [`../data-gen/generate.py`](../data-gen/generate.py) | Generator: a consistent 30-day history |

## Key design choices

**1. History is never edited (chains).** A change to a player, friendship, purchase or ammo balance inserts a **new row** pointing to the row it replaces (`previous_id`). Nothing is overwritten, so the full history is always available.
- `UNIQUE (previous_id)`: a row can be replaced only once. Two concurrent changes to the same state cannot both succeed, so lost updates are impossible.
- `root_id`: the first row's id, which is the entity's permanent identity. Other tables reference it.
- "Current" means the row nothing points to (views `current_player`, `current_inventory`, …).
- The rule is enforced twice: a trigger rejects `UPDATE`/`DELETE` for every user, and the application role is granted only `SELECT`/`INSERT`.
- The single deliberate exception is `limited_drop.remaining`, a stock counter updated in place. It is the concurrency case in portfolio 3.

**2. Rules in the database, not only in the application.**

| Rule | How it is enforced |
|---|---|
| Money is exact | Integer cents (`BIGINT`), never floating point |
| A purchase has 1–10 packs per item, and line total = packs × price | `CHECK` |
| Ammo never goes below zero, and each change adds up | `CHECK (units_after >= 0)` + trigger `inventory_adds_up` |
| A purchase can be refunded once, and only if it succeeded | partial unique index + trigger |
| Payment references and inventory credits are recorded once | partial unique indexes |
| Player names are unique across all regions, case-insensitively | `player_handle` (a GLOBAL table) + composite foreign key |
| A name is never reserved without a player | procedures `sign_up` / `rename_player`; the app role cannot insert names directly |

**3. Horizontal fragmentation by region.** Player-owned tables are `REGIONAL BY ROW`: each row lives in its owner's region, so af-west players are served from the af-west node.
- Child rows follow their parent (derived fragmentation): a purchase's lines go with the purchase.
- Small reference tables (`region`, `item`, `player_handle`) are `GLOBAL`: copied to every region for fast local reads.
- The textbook fragments exist as views (`player_af_west`, …) and are proved in portfolio 5.

## Evidence

- **Rule tests** ([`evidence/rule-tests.txt`](evidence/rule-tests.txt)): 23 tests; all 15 invalid operations are rejected with the expected error. Examples: an `UPDATE` on history, a fork in a chain, an inventory that does not add up, going below zero, refunding a failed purchase, a taken name, an orphan name claim.
- **Integrity checks** ([`evidence/integrity.txt`](evidence/integrity.txt)): over the generated month (5,100 player versions, 47,430 purchases, 94,880 inventory changes, 2,500 matches), **all 11 checks report 0 violations**. They cover rules a constraint cannot express: the $50 daily limit, gifts only to friends, squads of at most 4, drop stock never oversold, balances equal to the sum of their chains.

## Run it

```bash
make up-core      # start the cluster
make p1-tests     # rebuild the database and run the rule tests
make seed         # rebuild, generate the month of data, run the integrity checks (~20 min)
make sql          # SQL shell
```
