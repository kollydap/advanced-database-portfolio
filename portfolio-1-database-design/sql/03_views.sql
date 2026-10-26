-- Views: current state of each chain, and the textbook fragments (D10).
SET DATABASE = squadfire;

-- ------------------------------------------------------------ current state
-- "Current" = the row that no other row points to as its previous version.
CREATE VIEW current_player AS
SELECT p.root_id AS player_id, p.id AS version_id, p.handle, p.crdb_region AS home_region, p.status, p.created_at AS since
FROM player p
WHERE NOT EXISTS (SELECT 1 FROM player n WHERE n.previous_id = p.id);

CREATE VIEW current_friendship AS
SELECT f.player_low, f.player_high, f.requested_by, f.status, f.created_at AS since
FROM friendship f
WHERE NOT EXISTS (SELECT 1 FROM friendship n WHERE n.previous_id = f.id);

CREATE VIEW current_purchase AS
SELECT p.root_id AS purchase_id, p.payer_id, p.recipient_id, p.status, p.total_cents, p.crdb_region,
       r.created_at AS purchased_at
FROM purchase p
JOIN purchase r ON r.id = p.root_id
WHERE NOT EXISTS (SELECT 1 FROM purchase n WHERE n.previous_id = p.id);

CREATE VIEW current_inventory AS
SELECT i.player_id, i.sku, i.units_after AS units, i.crdb_region, i.created_at AS since
FROM inventory i
WHERE NOT EXISTS (SELECT 1 FROM inventory n WHERE n.previous_id = i.id);

CREATE VIEW current_price AS
SELECT DISTINCT ON (crdb_region, sku) crdb_region, sku, price_cents, effective_from
FROM item_price
WHERE effective_from <= now()
ORDER BY crdb_region, sku, effective_from DESC;

-- ------------------------------------------------------------ fragments (textbook names)
-- Each view is one horizontal fragment: rows of one region. Their UNION ALL
-- reconstructs the table (proved in portfolio 5).
CREATE VIEW player_us_east AS SELECT * FROM player WHERE crdb_region = 'us-east';
CREATE VIEW player_eu_west AS SELECT * FROM player WHERE crdb_region = 'eu-west';
CREATE VIEW player_af_west AS SELECT * FROM player WHERE crdb_region = 'af-west';

CREATE VIEW purchase_us_east AS SELECT * FROM purchase WHERE crdb_region = 'us-east';
CREATE VIEW purchase_eu_west AS SELECT * FROM purchase WHERE crdb_region = 'eu-west';
CREATE VIEW purchase_af_west AS SELECT * FROM purchase WHERE crdb_region = 'af-west';

CREATE VIEW inventory_us_east AS SELECT * FROM inventory WHERE crdb_region = 'us-east';
CREATE VIEW inventory_eu_west AS SELECT * FROM inventory WHERE crdb_region = 'eu-west';
CREATE VIEW inventory_af_west AS SELECT * FROM inventory WHERE crdb_region = 'af-west';
