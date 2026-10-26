-- Integrity checks over the whole database (run after seeding).
-- Rules that constraints cannot express are kept by the application or the
-- procedures; these queries prove they hold. Every count must be 0.
SET DATABASE = squadfire;

SELECT 'orphan name claims (D15)' AS rule, count(*) AS violations
FROM player_handle h
WHERE NOT EXISTS (SELECT 1 FROM player p WHERE p.root_id = h.root_id AND p.handle_key = h.handle_key)

UNION ALL
-- references must use a player's permanent id, the first version's id (D10)
SELECT 'references to a non-root player version', count(*)
FROM (SELECT payer_id AS ref FROM purchase UNION ALL SELECT recipient_id FROM purchase
      UNION ALL SELECT player_low FROM friendship UNION ALL SELECT player_high FROM friendship
      UNION ALL SELECT player_id FROM inventory UNION ALL SELECT player_id FROM match_player) r
JOIN player p ON p.id = r.ref
WHERE p.previous_id IS NOT NULL

UNION ALL
SELECT 'squads with more than 4 players (D14)', count(*)
FROM (SELECT match_id, squad FROM match_player GROUP BY match_id, squad HAVING count(*) > 4)

UNION ALL
SELECT 'purchase total <> sum of its lines', count(*)
FROM purchase p
WHERE p.previous_id IS NULL
  AND p.total_cents <> (SELECT coalesce(sum(l.line_total_cents), 0) FROM purchase_line l WHERE l.purchase_id = p.id)

UNION ALL
SELECT 'gifts to someone who was not a friend (D7)', count(*)
FROM purchase p
WHERE p.previous_id IS NULL AND p.payer_id <> p.recipient_id
  AND NOT EXISTS (SELECT 1 FROM friendship f
                  WHERE f.status = 'ACCEPTED' AND f.created_at <= p.created_at
                    AND f.player_low = least(p.payer_id, p.recipient_id)
                    AND f.player_high = greatest(p.payer_id, p.recipient_id))

UNION ALL
SELECT 'payer-days over the $50 limit (D3)', count(*)
FROM (SELECT payer_id, purchased_at::DATE FROM current_purchase
      WHERE status = 'SUCCEEDED'
      GROUP BY payer_id, purchased_at::DATE HAVING sum(total_cents) > 5000)

UNION ALL
SELECT 'paid lines not credited to the recipient (D3)', count(*)
FROM purchase p JOIN purchase_line l ON l.purchase_id = p.id
WHERE p.previous_id IS NULL AND p.status = 'SUCCEEDED'
  AND NOT EXISTS (SELECT 1 FROM inventory i
                  WHERE i.purchase_id = p.id AND i.player_id = p.recipient_id AND i.sku = l.sku
                    AND i.reason IN ('PURCHASED', 'GIFT_IN'))

UNION ALL
SELECT 'inventory credited from a FAILED purchase', count(*)
FROM inventory i JOIN purchase p ON p.id = i.purchase_id
WHERE p.status = 'FAILED'

UNION ALL
-- the chain is its own audit: replaying the changes gives the current balance
SELECT 'current balance <> sum of chain changes', count(*)
FROM current_inventory c
WHERE c.units <> (SELECT sum(i.change) FROM inventory i WHERE i.player_id = c.player_id AND i.sku = c.sku)

UNION ALL
SELECT 'drop counter <> packs sold (D6)', count(*)
FROM limited_drop d
WHERE d.remaining <> d.total_packs - (SELECT coalesce(sum(l.packs), 0)
                                      FROM purchase_line l JOIN purchase p ON p.id = l.purchase_id
                                      WHERE l.drop_id = d.id AND p.status = 'SUCCEEDED')

UNION ALL
-- derived fragmentation: a child row lives in its parent's fragment (D7)
SELECT 'child rows outside their parent''s fragment', count(*)
FROM (SELECT 1 FROM match_player mp JOIN match m ON m.id = mp.match_id WHERE mp.crdb_region <> m.crdb_region
      UNION ALL
      SELECT 1 FROM purchase_line l JOIN purchase p ON p.id = l.purchase_id WHERE l.crdb_region <> p.crdb_region);

-- volumes
SELECT 'player' AS t, count(*) AS rows FROM player
UNION ALL SELECT 'friendship', count(*) FROM friendship
UNION ALL SELECT 'purchase', count(*) FROM purchase
UNION ALL SELECT 'purchase_line', count(*) FROM purchase_line
UNION ALL SELECT 'inventory', count(*) FROM inventory
UNION ALL SELECT 'match', count(*) FROM match
UNION ALL SELECT 'match_player', count(*) FROM match_player;
