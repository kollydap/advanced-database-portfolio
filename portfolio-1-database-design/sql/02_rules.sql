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

-- ------------------------------------------------------------ privileges
-- Layer 2: the application role can only read and add rows to history tables.
CREATE ROLE IF NOT EXISTS squadfire_app;
CREATE ROLE IF NOT EXISTS squadfire_analyst;

GRANT SELECT ON region, item TO squadfire_app;
GRANT SELECT, INSERT ON player, player_handle, friendship, item_price, purchase, purchase_line,
                        inventory, match, match_player TO squadfire_app;
GRANT SELECT, UPDATE ON limited_drop TO squadfire_app;     -- the documented mutable hot row
GRANT SELECT ON TABLE * TO squadfire_analyst;
