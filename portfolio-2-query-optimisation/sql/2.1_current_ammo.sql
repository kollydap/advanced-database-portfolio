-- Experiment 2.1: "How much ammo does this player have right now?"
-- Asked before every match. Current = the last row of each (player, item)
-- chain: the row no other row names as its previous_id.
-- Player: SharpLion (us-east, a heavy buyer with 253 inventory rows).
SET DATABASE = squadfire;

-- ---------------------------------------------------------------- before
-- No index starts with player_id: the only way in is a full scan.
DROP INDEX IF EXISTS inventory@inventory_by_player;

EXPLAIN ANALYZE
SELECT i.sku, i.units_after AS units
FROM inventory i
WHERE i.player_id = '5e6e7b59-9e81-4967-8f0f-1d28281b9cae'
  AND NOT EXISTS (SELECT 1 FROM inventory n WHERE n.previous_id = i.id);

-- ---------------------------------------------------------------- the fix
-- One player's rows sit together, grouped by item. STORING copies the
-- balance into the index, so it answers without visiting the table
-- (a covering index).
CREATE INDEX inventory_by_player ON inventory (player_id, sku) STORING (units_after);

-- ---------------------------------------------------------------- after
EXPLAIN ANALYZE
SELECT i.sku, i.units_after AS units
FROM inventory i
WHERE i.player_id = '5e6e7b59-9e81-4967-8f0f-1d28281b9cae'
  AND NOT EXISTS (SELECT 1 FROM inventory n WHERE n.previous_id = i.id);

-- the answer itself
SELECT i.sku, i.units_after AS units
FROM inventory i
WHERE i.player_id = '5e6e7b59-9e81-4967-8f0f-1d28281b9cae'
  AND NOT EXISTS (SELECT 1 FROM inventory n WHERE n.previous_id = i.id)
ORDER BY i.sku;
