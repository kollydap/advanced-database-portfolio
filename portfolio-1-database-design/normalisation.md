# Normalisation: from a purchases spreadsheet to BCNF

The core store tables (`player`, `item`, `purchase`, `purchase_line`) are derived here step by step from one unnormalised sheet. The section at the end lists where the final schema departs from the normal forms on purpose, and how each departure is kept consistent.

## 0NF: the spreadsheet

Squadfire's imagined first version is one sheet with a row per purchase:

| purchase | date | payer | payer_region | recipient | items | total |
|---|---|---|---|---|---|---|
| P1 | 3 Oct | IronHarmattan | af-west | IronHarmattan | 5.56 Rounds ×2 @ $1.19, Med Kits ×1 @ $1.49 | $3.87 |
| P2 | 4 Oct | Adaeze | eu-west | IronHarmattan | 5.56 Rounds ×3 @ $2.19 | $6.57 |
| P3 | 4 Oct | IronHarmattan | af-west | IronHarmattan | Frag Grenades ×1 @ $1.79 | $1.79 |

Problems:
- **Repeating group:** `items` holds a list. "Med kits sold this month" needs text parsing.
- **Update anomaly:** "IronHarmattan is in af-west" is stored in P1 and P3. Changing one copy leaves the sheet contradicting itself.
- **Insert anomaly:** a player who has not bought anything cannot be recorded. Most players of a game never buy, and they still play, add friends and receive gifts.
- **Delete anomaly:** deleting P2 loses the only record of Adaeze's region.

## 1NF: one value per cell

Each item in a basket becomes its own row, with `item`, `packs` and `price` as separate columns.

| purchase | date | payer | payer_region | recipient | item | packs | price | total |
|---|---|---|---|---|---|---|---|---|
| P1 | 3 Oct | IronHarmattan | af-west | IronHarmattan | 5.56 Rounds | 2 | $1.19 | $3.87 |
| P1 | 3 Oct | IronHarmattan | af-west | IronHarmattan | Med Kits | 1 | $1.49 | $3.87 |
| P2 | 4 Oct | Adaeze | eu-west | IronHarmattan | 5.56 Rounds | 3 | $2.19 | $6.57 |
| P3 | 4 Oct | IronHarmattan | af-west | IronHarmattan | Frag Grenades | 1 | $1.79 | $1.79 |

**Key: (purchase, item).** The repeating group is gone, but the purchase details now repeat for every line.

## 2NF: no partial dependencies

With a composite key, every non-key column must depend on the whole key.

| Dependency | Kind | Moves to |
|---|---|---|
| purchase → date, payer, payer_region, recipient, total | partial (part of the key) | `purchase` |
| item → item_type, units_per_pack | partial | `item` |
| (purchase, item) → packs, unit_price | full | `purchase_line` |

**Why the price stays on the line, not on `item`:** 5.56 Rounds costs $1.19 in P1 and $2.19 in P2. The price depends on region and time, so it is not a fact about the item alone. The line records the price actually paid. The list price is kept separately, by region and date, in `item_price`.

```
purchase(purchase, date, payer, payer_region, recipient, total)
item(item, item_type, units_per_pack)
purchase_line(purchase, item, packs, unit_price)
```

## 3NF: no transitive dependencies

In `purchase`: purchase → payer and payer → payer_region, so purchase → payer_region holds only *through* the payer. The region is a fact about the player, and it is still repeated for every purchase the player makes.

```
player(player, region)
purchase(purchase, date, payer → player, recipient → player, total)
item(item, item_type, units_per_pack)
purchase_line(purchase, item, packs, unit_price)
```

The insert anomaly is gone too: a player exists from sign-up, with or without purchases.

## BCNF: every determinant is a key

BCNF removes 3NF's exception for dependencies whose right-hand side is part of a key: **every determinant must be a candidate key.**

- `player`, `item`, `purchase` and `purchase_line` above: the only determinants are their keys. ✅
- **The real case in the schema: player names.** Players are versioned (a rename or region move is a new row; see D5). In `player(id, root_id, handle, …)` the dependency **handle → root_id** holds, because a name belongs to one player forever. But `handle` is not a key: it repeats on every version of that player. That violates BCNF.
- **Decomposition:** `player_handle(handle_key PK, root_id)` stores "this name belongs to this player" once, with the determinant as its key.

```
player_handle(handle_key, root_id)
player(id, root_id, previous_id, region, handle, status, …)
  FOREIGN KEY (lower(handle), root_id) → player_handle
```

The name is kept on `player` for reads (see the next section). The composite foreign key means no copy can disagree with the single stored fact.

## Deliberate denormalisation

Each column below can be derived from other data. Each is kept for a stated reason and guarded so it cannot drift. Rows in history tables are never edited, so a value that was correct on insert stays correct.

| Column | Derivable from | Kept because | Guarded by |
|---|---|---|---|
| `purchase.total_cents` | sum of its lines | the $50 daily check runs on every purchase; it is also the amount charged | integrity check "purchase total <> sum of its lines" = 0 |
| `purchase_line.line_total_cents` | `packs × unit_price_cents` | simple reporting | `CHECK (line_total_cents = packs * unit_price_cents)` |
| `inventory.units_after` | sum of the chain's changes | current ammo is one row read instead of a sum over history | trigger `inventory_adds_up`; check "current balance <> sum of chain changes" = 0 |
| `player.handle` | `player_handle` | display without a join | composite foreign key to `player_handle` |
| `match_player.kills/deaths/assists` | kill telemetry in the lake | leaderboards and the warehouse need totals quickly | written once from the same events the lake receives |
| `crdb_region` on child rows (`purchase_line`, `match_player`) | the parent's region | derived fragmentation: a child row is stored in its parent's region, so a purchase and its lines are read from one node | check "child rows outside their parent's fragment" = 0 |

## Result

The derivation gives the core of the final schema ([`sql/01_schema.sql`](sql/01_schema.sql)). The remaining tables follow the same rules:
- `friendship`: one row per pair of players
- `inventory`: one chain per (player, item)
- `match` and `match_player`: the key (match, player), with the squad as a fact about that pair
- `item_price`: key (region, item, effective_from)
- `limited_drop`: one row per drop
