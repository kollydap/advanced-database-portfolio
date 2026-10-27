-- Experiment 2.3: "How much has this player spent today?"
-- Runs before every purchase to enforce the $50 daily limit, so it sits on
-- the store's busiest path. Spend = the player's original purchases from
-- today that succeeded and were not refunded since.
-- "Today" is 26 Oct (UTC), the last day of the generated month.
-- Player: SteelSniper642 (us-east), 3 purchases that day for $49.86.
SET DATABASE = squadfire;

-- ---------------------------------------------------------------- before
-- No index starts with payer_id: the only way in is a full scan.
DROP INDEX IF EXISTS purchase@purchase_spend_by_day;

EXPLAIN ANALYZE
SELECT coalesce(sum(p.total_cents), 0) AS spent_cents
FROM purchase p
WHERE p.payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
  AND p.previous_id IS NULL
  AND p.status = 'SUCCEEDED'
  AND p.created_at >= '2026-10-26' AND p.created_at < '2026-10-27'
  AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id);

-- ---------------------------------------------------------------- the fix
-- Column order: the exact match (payer) first, then the range (time), so
-- one player's purchases sit together in time order and "today" is one
-- short span. Partial (WHERE previous_id IS NULL): refund versions are
-- never spend, so they are left out of the index. STORING makes it covering.
CREATE INDEX purchase_spend_by_day ON purchase (payer_id, created_at)
    STORING (status, total_cents) WHERE previous_id IS NULL;

-- ---------------------------------------------------------------- after
EXPLAIN ANALYZE
SELECT coalesce(sum(p.total_cents), 0) AS spent_cents
FROM purchase p
WHERE p.payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
  AND p.previous_id IS NULL
  AND p.status = 'SUCCEEDED'
  AND p.created_at >= '2026-10-26' AND p.created_at < '2026-10-27'
  AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id);

-- ------------------------------------------- the other column order (worse)
-- (created_at, payer_id): time first, so "today" is everyone's purchases
-- from today, and the player is filtered out of those afterwards.
CREATE INDEX purchase_by_day_then_payer ON purchase (created_at, payer_id)
    STORING (status, total_cents) WHERE previous_id IS NULL;

EXPLAIN ANALYZE
SELECT coalesce(sum(p.total_cents), 0) AS spent_cents
FROM purchase@purchase_by_day_then_payer p
WHERE p.payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
  AND p.previous_id IS NULL
  AND p.status = 'SUCCEEDED'
  AND p.created_at >= '2026-10-26' AND p.created_at < '2026-10-27'
  AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id);

DROP INDEX purchase@purchase_by_day_then_payer;

-- the answer itself
SELECT coalesce(sum(p.total_cents), 0) AS spent_cents, count(*) AS purchases
FROM purchase p
WHERE p.payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
  AND p.previous_id IS NULL
  AND p.status = 'SUCCEEDED'
  AND p.created_at >= '2026-10-26' AND p.created_at < '2026-10-27'
  AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id);
