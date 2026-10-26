-- Portfolio 1 evidence: every rule is tried once the right way (accepted) and
-- once the wrong way (rejected). Run with errexit off so all tests execute:
--   cockroach sql --insecure --set=errexit=false < 04_rule_tests.sql
-- Fixed UUIDs make the output easy to read:
--   a1… = Tunde (af-west)   e1… = Ada (eu-west)
SET DATABASE = squadfire;

-- ---------------------------------------------------------------- setup
INSERT INTO region VALUES ('us-east', 'US East (Virginia)'), ('eu-west', 'EU West (Ireland)'), ('af-west', 'Africa West (Lagos)');
INSERT INTO item (sku, name, item_type, units_per_pack, consumable)
VALUES ('AMMO-556-120', '5.56 mm Rounds (120)', 'AMMO', 120, true),
       ('SKIN-TIGER-AK', 'Tiger Stripe AK skin', 'SKIN', 1, false);

-- T1  Sign up as the application: claim + first version in one call (root_id = id)
SET ROLE squadfire_app;
CALL sign_up('a1000000-0000-0000-0000-000000000001', 'IronHarmattan', 'af-west');
CALL sign_up('e1000000-0000-0000-0000-000000000001', 'Adaeze', 'eu-west');
RESET ROLE;

-- T2  REJECT: Ada tries to claim Tunde's name (any capitalisation)
INSERT INTO player_handle VALUES ('ironharmattan', 'e1000000-0000-0000-0000-000000000001');

-- T3  REJECT: a player version using a name the player does not own
INSERT INTO player (root_id, previous_id, crdb_region, handle, reason)
VALUES ('e1000000-0000-0000-0000-000000000001', 'e1000000-0000-0000-0000-000000000001', 'eu-west', 'IronHarmattan', 'renamed');

-- T4  Tunde moves to eu-west: a new version (same name, new fragment)
INSERT INTO player (id, root_id, previous_id, crdb_region, handle, reason)
VALUES ('a1000000-0000-0000-0000-000000000002', 'a1000000-0000-0000-0000-000000000001',
        'a1000000-0000-0000-0000-000000000001', 'eu-west', 'IronHarmattan', 'moved_region');

-- T5  REJECT: a second version replacing the same row (a fork)
INSERT INTO player (root_id, previous_id, crdb_region, handle, reason)
VALUES ('a1000000-0000-0000-0000-000000000001', 'a1000000-0000-0000-0000-000000000001', 'af-west', 'IronHarmattan', 'renamed');

-- T6  REJECT: UPDATE on a history table (trigger, even for an admin)
UPDATE player SET handle = 'Hacked' WHERE id = 'a1000000-0000-0000-0000-000000000001';

-- T7  Friendship: Tunde requests Ada (pair stored sorted), Ada accepts (new version)
INSERT INTO friendship (id, root_id, crdb_region, player_low, player_high, requested_by, status)
VALUES ('f1000000-0000-0000-0000-000000000001', 'f1000000-0000-0000-0000-000000000001', 'af-west',
        'a1000000-0000-0000-0000-000000000001', 'e1000000-0000-0000-0000-000000000001',
        'a1000000-0000-0000-0000-000000000001', 'REQUESTED');
INSERT INTO friendship (root_id, previous_id, crdb_region, player_low, player_high, requested_by, status)
VALUES ('f1000000-0000-0000-0000-000000000001', 'f1000000-0000-0000-0000-000000000001', 'af-west',
        'a1000000-0000-0000-0000-000000000001', 'e1000000-0000-0000-0000-000000000001',
        'a1000000-0000-0000-0000-000000000001', 'ACCEPTED');

-- T8  REJECT: the same pair written the other way round (not sorted)
INSERT INTO friendship (root_id, crdb_region, player_low, player_high, requested_by, status)
SELECT id, 'eu-west', 'e1000000-0000-0000-0000-000000000001', 'a1000000-0000-0000-0000-000000000001',
       'e1000000-0000-0000-0000-000000000001', 'REQUESTED' FROM (SELECT gen_random_uuid() AS id);

-- T9  REJECT: a second, separate friendship chain for the same pair
INSERT INTO friendship (id, root_id, crdb_region, player_low, player_high, requested_by, status)
VALUES ('f2000000-0000-0000-0000-000000000001', 'f2000000-0000-0000-0000-000000000001', 'eu-west',
        'a1000000-0000-0000-0000-000000000001', 'e1000000-0000-0000-0000-000000000001',
        'e1000000-0000-0000-0000-000000000001', 'REQUESTED');

-- T10 Purchase: Ada gifts Tunde 3 packs ($1.49 each) - a cross-region gift
INSERT INTO purchase (id, root_id, crdb_region, payer_id, recipient_id, status, total_cents, payment_method, gateway_ref, reason)
VALUES ('b1000000-0000-0000-0000-000000000001', 'b1000000-0000-0000-0000-000000000001', 'eu-west',
        'e1000000-0000-0000-0000-000000000001', 'a1000000-0000-0000-0000-000000000001',
        'SUCCEEDED', 447, 'CARD', 'GW-0001', 'created');
INSERT INTO purchase_line (crdb_region, purchase_id, sku, packs, unit_price_cents, line_total_cents)
VALUES ('eu-west', 'b1000000-0000-0000-0000-000000000001', 'AMMO-556-120', 3, 149, 447);

-- T11 REJECT: a line total that does not equal packs x price
INSERT INTO purchase_line (crdb_region, purchase_id, sku, packs, unit_price_cents, line_total_cents)
VALUES ('eu-west', 'b1000000-0000-0000-0000-000000000001', 'SKIN-TIGER-AK', 1, 499, 400);

-- T12 Inventory chain: +360 (gift in), then -214 (used in a match)
INSERT INTO match (id, crdb_region, mode, map, started_at, ended_at, winning_squad)
VALUES ('c1000000-0000-0000-0000-000000000001', 'eu-west', 'TEAM_DEATHMATCH', 'Lagos Docks',
        now() - INTERVAL '20 minutes', now(), 'Alpha');
INSERT INTO inventory (id, crdb_region, player_id, sku, change, units_after, reason, purchase_id)
VALUES ('d1000000-0000-0000-0000-000000000001', 'eu-west', 'a1000000-0000-0000-0000-000000000001',
        'AMMO-556-120', 360, 360, 'GIFT_IN', 'b1000000-0000-0000-0000-000000000001');
INSERT INTO inventory (id, previous_id, crdb_region, player_id, sku, change, units_after, reason, match_id)
VALUES ('d1000000-0000-0000-0000-000000000002', 'd1000000-0000-0000-0000-000000000001', 'eu-west',
        'a1000000-0000-0000-0000-000000000001', 'AMMO-556-120', -214, 146, 'USED', 'c1000000-0000-0000-0000-000000000001');

-- T13 REJECT: arithmetic that does not add up (146 + 100 is not 999) - trigger
INSERT INTO inventory (previous_id, crdb_region, player_id, sku, change, units_after, reason, purchase_id)
VALUES ('d1000000-0000-0000-0000-000000000002', 'eu-west', 'a1000000-0000-0000-0000-000000000001',
        'AMMO-556-120', 100, 999, 'PURCHASED', 'b1000000-0000-0000-0000-000000000001');

-- T14 REJECT: a second change built on the same previous row (lost update prevented)
INSERT INTO inventory (previous_id, crdb_region, player_id, sku, change, units_after, reason, match_id)
VALUES ('d1000000-0000-0000-0000-000000000001', 'eu-west', 'a1000000-0000-0000-0000-000000000001',
        'AMMO-556-120', -10, 350, 'USED', 'c1000000-0000-0000-0000-000000000001');

-- T15 REJECT: going below zero ammo
INSERT INTO inventory (previous_id, crdb_region, player_id, sku, change, units_after, reason, purchase_id)
VALUES ('d1000000-0000-0000-0000-000000000002', 'eu-west', 'a1000000-0000-0000-0000-000000000001',
        'AMMO-556-120', -360, -214, 'REFUNDED', 'b1000000-0000-0000-0000-000000000001');

-- T16 Refund: a new purchase version (allowed once)
INSERT INTO purchase (root_id, previous_id, crdb_region, payer_id, recipient_id, status, total_cents, payment_method, gateway_ref, reason)
VALUES ('b1000000-0000-0000-0000-000000000001', 'b1000000-0000-0000-0000-000000000001', 'eu-west',
        'e1000000-0000-0000-0000-000000000001', 'a1000000-0000-0000-0000-000000000001',
        'REFUNDED', 447, 'CARD', 'GW-0001', 'refunded');

-- T17 REJECT: refunding a FAILED purchase (trigger)
INSERT INTO purchase (id, root_id, crdb_region, payer_id, recipient_id, status, total_cents, payment_method, gateway_ref, reason)
VALUES ('b2000000-0000-0000-0000-000000000001', 'b2000000-0000-0000-0000-000000000001', 'eu-west',
        'e1000000-0000-0000-0000-000000000001', 'e1000000-0000-0000-0000-000000000001',
        'FAILED', 149, 'CARD', 'GW-0002', 'created');
INSERT INTO purchase (root_id, previous_id, crdb_region, payer_id, recipient_id, status, total_cents, payment_method, gateway_ref, reason)
VALUES ('b2000000-0000-0000-0000-000000000001', 'b2000000-0000-0000-0000-000000000001', 'eu-west',
        'e1000000-0000-0000-0000-000000000001', 'e1000000-0000-0000-0000-000000000001',
        'REFUNDED', 149, 'CARD', 'GW-0002', 'refunded');

-- T18 Limited drop: selling a pack is an UPDATE (the documented mutable row)
INSERT INTO limited_drop (id, crdb_region, sku, price_cents, total_packs, remaining, starts_at, ends_at)
VALUES ('9d000000-0000-0000-0000-000000000001', 'af-west', 'AMMO-556-120', 399, 500, 1, now(), now() + INTERVAL '2 days');
UPDATE limited_drop SET remaining = remaining - 1 WHERE id = '9d000000-0000-0000-0000-000000000001';

-- T19 REJECT: selling below zero
UPDATE limited_drop SET remaining = remaining - 1 WHERE id = '9d000000-0000-0000-0000-000000000001';

-- T20 REJECT (privilege layer): the application role cannot UPDATE history
SET ROLE squadfire_app;
UPDATE purchase SET total_cents = 1 WHERE root_id = 'b1000000-0000-0000-0000-000000000001';
RESET ROLE;

-- T21 REJECT (privilege layer): the application inserts a claim on its own (an orphan)
SET ROLE squadfire_app;
INSERT INTO player_handle VALUES ('coolname', '00000000-0000-0000-0000-00000000dead');

-- T22 Ada renames herself: new claim + new version, same region and status
CALL rename_player('e1000000-0000-0000-0000-000000000001', 'AdaWarlord');

-- T23 REJECT: renaming to a name another player owns (nothing is written)
CALL rename_player('e1000000-0000-0000-0000-000000000001', 'ironHARMATTAN');
RESET ROLE;

-- ---------------------------------------------------------------- results
SELECT player_id, handle, home_region, status FROM current_player ORDER BY handle;
SELECT root_id, previous_id, crdb_region, handle, reason FROM player ORDER BY created_at;
SELECT status FROM current_friendship;
SELECT purchase_id, status, total_cents FROM current_purchase;
SELECT player_id, sku, units FROM current_inventory;
SELECT 'player_af_west' AS fragment, count(*) FROM player_af_west
UNION ALL SELECT 'player_eu_west', count(*) FROM player_eu_west
UNION ALL SELECT 'player_us_east', count(*) FROM player_us_east;
-- orphan claims (a name no player version uses): must be 0
SELECT count(*) AS orphan_claims FROM player_handle h
WHERE NOT EXISTS (SELECT 1 FROM player p WHERE p.root_id = h.root_id AND p.handle_key = h.handle_key);
