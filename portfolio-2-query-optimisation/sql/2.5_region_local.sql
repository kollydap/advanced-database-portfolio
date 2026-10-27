-- Experiment 2.5: "Tell the database which region."
-- purchase is REGIONAL BY ROW: each row lives in the payer's home region,
-- and every index is split by crdb_region. A query that does not name the
-- region has to look in all three; one that does reads a single region.
-- Same query as 2.3 (today's spend for SteelSniper642, home region us-east).
-- The session runs on the us-east node.
SET DATABASE = squadfire;

-- the index from 2.3 (kept, so this script also runs on its own)
CREATE INDEX IF NOT EXISTS purchase_spend_by_day ON purchase (payer_id, created_at)
    STORING (status, total_cents) WHERE previous_id IS NULL;

-- ------------------------------------------------- region unknown (3 spans)
EXPLAIN ANALYZE
SELECT coalesce(sum(p.total_cents), 0) AS spent_cents
FROM purchase p
WHERE p.payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
  AND p.previous_id IS NULL
  AND p.status = 'SUCCEEDED'
  AND p.created_at >= '2026-10-26' AND p.created_at < '2026-10-27'
  AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id);

-- ---------------------------------------------------- region given (1 span)
-- The app knows the player's home region from login and passes it.
EXPLAIN ANALYZE
SELECT coalesce(sum(p.total_cents), 0) AS spent_cents
FROM purchase p
WHERE p.crdb_region = 'us-east'
  AND p.payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
  AND p.previous_id IS NULL
  AND p.status = 'SUCCEEDED'
  AND p.created_at >= '2026-10-26' AND p.created_at < '2026-10-27'
  AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id);

-- ------------------------------------ bonus: locality-optimised search
-- A lookup that can match at most one row (id is unique) checks the local
-- region first and only asks the others if the row is not found. A sum
-- over many rows cannot stop early, which is why the region is given above.
EXPLAIN
SELECT id, status, total_cents
FROM purchase
WHERE id = '12e08b3b-6b46-4890-af76-b4c2f0b61703';

-- both answers agree
SELECT
  (SELECT sum(total_cents) FROM purchase p
    WHERE payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
      AND previous_id IS NULL AND status = 'SUCCEEDED'
      AND created_at >= '2026-10-26' AND created_at < '2026-10-27'
      AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id)) AS any_region,
  (SELECT sum(total_cents) FROM purchase p
    WHERE crdb_region = 'us-east'
      AND payer_id = '56f78d43-a6e5-4870-9a87-1c5d42c8ed9f'
      AND previous_id IS NULL AND status = 'SUCCEEDED'
      AND created_at >= '2026-10-26' AND created_at < '2026-10-27'
      AND NOT EXISTS (SELECT 1 FROM purchase r WHERE r.previous_id = p.id)) AS us_east_only;
