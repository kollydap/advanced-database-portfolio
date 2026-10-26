-- Rules the database enforces beyond constraints (D5, D12).
SET DATABASE = squadfire;

-- ------------------------------------------------------------ never edited
-- Layer 1: a trigger rejects UPDATE/DELETE on append-only tables, for everyone.
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS TRIGGER LANGUAGE PLpgSQL AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only: % is not allowed (insert a new version instead)',
    TG_TABLE_NAME, TG_OP USING ERRCODE = '55000';
END
$$;

CREATE TRIGGER player_never_edited        BEFORE UPDATE OR DELETE ON player        FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER player_handle_never_edited BEFORE UPDATE OR DELETE ON player_handle FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER friendship_never_edited    BEFORE UPDATE OR DELETE ON friendship    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER item_price_never_edited    BEFORE UPDATE OR DELETE ON item_price    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER purchase_never_edited      BEFORE UPDATE OR DELETE ON purchase      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER purchase_line_never_edited BEFORE UPDATE OR DELETE ON purchase_line FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER inventory_never_edited     BEFORE UPDATE OR DELETE ON inventory     FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER match_never_edited         BEFORE UPDATE OR DELETE ON match         FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER match_player_never_edited  BEFORE UPDATE OR DELETE ON match_player  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ------------------------------------------------------------ inventory arithmetic (Q12)
-- A CHECK cannot see the previous row; this trigger can. It guarantees every
-- chain adds up: units_after = previous units_after + change.
CREATE OR REPLACE FUNCTION inventory_adds_up() RETURNS TRIGGER LANGUAGE PLpgSQL AS $$
DECLARE
  prev_units INT;
BEGIN
  IF (NEW).previous_id IS NULL THEN
    prev_units := 0;
  ELSE
    SELECT units_after INTO prev_units FROM inventory WHERE id = (NEW).previous_id;
  END IF;
  IF (NEW).units_after <> prev_units + (NEW).change THEN
    RAISE EXCEPTION 'inventory does not add up: % + % <> %', prev_units, (NEW).change, (NEW).units_after
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER inventory_adds_up BEFORE INSERT ON inventory FOR EACH ROW EXECUTE FUNCTION inventory_adds_up();

-- ------------------------------------------------------------ refunds only of successful purchases
CREATE OR REPLACE FUNCTION purchase_refund_valid() RETURNS TRIGGER LANGUAGE PLpgSQL AS $$
DECLARE
  prev_status purchase_status;
BEGIN
  IF (NEW).previous_id IS NOT NULL THEN
    SELECT status INTO prev_status FROM purchase WHERE id = (NEW).previous_id;
    IF prev_status <> 'SUCCEEDED' THEN
      RAISE EXCEPTION 'only a SUCCEEDED purchase can be refunded (previous status: %)', prev_status
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER purchase_refund_valid BEFORE INSERT ON purchase FOR EACH ROW EXECUTE FUNCTION purchase_refund_valid();

-- ------------------------------------------------------------ names: claim + player together (D15)
-- player_handle has no foreign key back to player (CockroachDB cannot defer
-- one), so a claim inserted alone would reserve a name for nobody. The app
-- cannot insert claims; these procedures always write the claim and the
-- player version in the same transaction. SECURITY DEFINER = runs with the
-- owner's rights. The app generates the player id.
CREATE OR REPLACE PROCEDURE sign_up(p_id UUID, p_handle STRING, p_region crdb_internal_region)
LANGUAGE PLpgSQL SECURITY DEFINER AS $$
BEGIN
  INSERT INTO player_handle (handle_key, root_id) VALUES (lower(p_handle), p_id);
  INSERT INTO player (id, root_id, crdb_region, handle, reason)
  VALUES (p_id, p_id, p_region, p_handle, 'created');
END
$$;

CREATE OR REPLACE PROCEDURE rename_player(p_player UUID, p_handle STRING)
LANGUAGE PLpgSQL SECURITY DEFINER AS $$
DECLARE
  v_id     UUID;
  v_region crdb_internal_region;
  v_status player_status;
BEGIN
  SELECT p.id, p.crdb_region, p.status INTO v_id, v_region, v_status
  FROM player p
  WHERE p.root_id = p_player
    AND NOT EXISTS (SELECT 1 FROM player n WHERE n.previous_id = p.id);
  IF v_id IS NULL THEN
    RAISE EXCEPTION 'player % does not exist', p_player USING ERRCODE = '23503';
  END IF;
  -- a name the player already owns (old name, or a change of capitals) needs no new claim
  IF NOT EXISTS (SELECT 1 FROM player_handle h WHERE h.handle_key = lower(p_handle) AND h.root_id = p_player) THEN
    INSERT INTO player_handle (handle_key, root_id) VALUES (lower(p_handle), p_player);
  END IF;
  INSERT INTO player (root_id, previous_id, crdb_region, handle, status, reason)
  VALUES (p_player, v_id, v_region, p_handle, v_status, 'renamed');
END
$$;

-- ------------------------------------------------------------ privileges
-- Layer 2: the application role can only read and add rows to history tables.
CREATE ROLE IF NOT EXISTS squadfire_app;
CREATE ROLE IF NOT EXISTS squadfire_analyst;

GRANT SELECT ON region, item, player_handle TO squadfire_app;   -- claims only through the procedures
GRANT SELECT, INSERT ON player, friendship, item_price, purchase, purchase_line,
                        inventory, match, match_player TO squadfire_app;
GRANT EXECUTE ON PROCEDURE sign_up, rename_player TO squadfire_app;
GRANT SELECT, UPDATE ON limited_drop TO squadfire_app;     -- the documented mutable hot row
GRANT SELECT ON TABLE * TO squadfire_analyst;
