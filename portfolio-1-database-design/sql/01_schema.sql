-- Squadfire schema: 12 tables (D10–D14).
--
-- Conventions
--   * Money is BIGINT cents ($1.99 = 199).                                       (D3)
--   * Chains: a change is a NEW row whose previous_id points to the row it
--     replaces. UNIQUE(previous_id) means a row can be replaced only once.      (D4, D5)
--   * root_id = id of a chain's first row: the entity's permanent identity.
--     Other tables reference an entity through this first-row id.              (D5, D10)
--   * crdb_region = the row's fragment (REGIONAL BY ROW). GLOBAL tables are
--     copied to every region.                                                  (D7, D10)
SET DATABASE = squadfire;

-- ============================================================ enums
CREATE TYPE player_status     AS ENUM ('ACTIVE', 'SUSPENDED', 'BANNED');
CREATE TYPE friendship_status AS ENUM ('REQUESTED', 'ACCEPTED', 'DECLINED', 'REMOVED');
CREATE TYPE item_type         AS ENUM ('AMMO', 'GRENADE', 'MEDKIT', 'SKIN');
CREATE TYPE purchase_status   AS ENUM ('SUCCEEDED', 'FAILED', 'REFUNDED');
CREATE TYPE payment_method    AS ENUM ('CARD', 'WALLET');
CREATE TYPE inventory_reason  AS ENUM ('PURCHASED', 'GIFT_IN', 'USED', 'REFUNDED');
CREATE TYPE match_mode        AS ENUM ('TEAM_DEATHMATCH', 'SEARCH_AND_DESTROY', 'DOMINATION');
CREATE TYPE squad_callsign    AS ENUM ('Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot');

-- ============================================================ group 1 (D10)
CREATE TABLE region (
    code  crdb_internal_region PRIMARY KEY,
    name  STRING NOT NULL UNIQUE
) LOCALITY GLOBAL;

-- Name ownership: a handle belongs to one player forever (insert-only).
-- GLOBAL so uniqueness holds across all regions.
CREATE TABLE player_handle (
    handle_key  STRING PRIMARY KEY CHECK (handle_key = lower(handle_key) AND length(handle_key) BETWEEN 3 AND 20),
    root_id     UUID NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (handle_key, root_id)              -- target for player's composite foreign key
) LOCALITY GLOBAL;

CREATE TABLE player (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    root_id      UUID NOT NULL,
    previous_id  UUID UNIQUE,                  -- a version can be replaced only once
    crdb_region  crdb_internal_region NOT NULL, -- home region = fragment
    handle       STRING NOT NULL,
    handle_key   STRING AS (lower(handle)) STORED,
    status       player_status NOT NULL DEFAULT 'ACTIVE',
    reason       STRING NOT NULL,              -- created | renamed | moved_region | suspended | banned
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, root_id),
    -- the first version is its own root; later versions are not
    CHECK ((previous_id IS NULL) = (root_id = id)),
    -- every version uses a name its player owns
    FOREIGN KEY (handle_key, root_id) REFERENCES player_handle (handle_key, root_id),
    -- the root and the previous version belong to the same player
    FOREIGN KEY (root_id) REFERENCES player (id),
    FOREIGN KEY (previous_id, root_id) REFERENCES player (id, root_id)
) LOCALITY REGIONAL BY ROW;
CREATE UNIQUE INDEX player_one_first_version ON player (root_id) WHERE previous_id IS NULL;

CREATE TABLE friendship (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    root_id       UUID NOT NULL,
    previous_id   UUID UNIQUE,
    crdb_region   crdb_internal_region NOT NULL, -- requester's region
    player_low    UUID NOT NULL REFERENCES player (id),
    player_high   UUID NOT NULL REFERENCES player (id),
    requested_by  UUID NOT NULL REFERENCES player (id),
    status        friendship_status NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, root_id),
    CHECK (player_low < player_high),          -- sorted pair: A-B and B-A are the same friendship
    CHECK (requested_by IN (player_low, player_high)),
    CHECK ((previous_id IS NULL) = (root_id = id)),
    CHECK (previous_id IS NOT NULL OR status = 'REQUESTED'),   -- every friendship starts as a request
    FOREIGN KEY (root_id) REFERENCES friendship (id),
    FOREIGN KEY (previous_id, root_id) REFERENCES friendship (id, root_id)
) LOCALITY REGIONAL BY ROW;
-- one chain per pair (re-friending continues it)
CREATE UNIQUE INDEX friendship_one_chain_per_pair ON friendship (player_low, player_high) WHERE previous_id IS NULL;

-- ============================================================ group 2 (D11)
-- Mutable catalogue row (rarely edited by staff); edits are logged in a JSON
-- change_log (D13). The rich, type-specific document lives in MongoDB.
CREATE TABLE item (
    sku             STRING PRIMARY KEY,
    name            STRING NOT NULL,
    item_type       item_type NOT NULL,
    units_per_pack  INT NOT NULL CHECK (units_per_pack > 0),
    consumable      BOOL NOT NULL,
    active          BOOL NOT NULL DEFAULT true,
    change_log      JSONB NOT NULL DEFAULT '[]',
    CHECK (consumable = (item_type <> 'SKIN')),
    INVERTED INDEX item_change_log_idx (change_log)
) LOCALITY GLOBAL;

-- Dated price list per region. Current price = latest effective_from <= now().
CREATE TABLE item_price (
    crdb_region     crdb_internal_region NOT NULL,
    sku             STRING NOT NULL REFERENCES item (sku),
    effective_from  TIMESTAMPTZ NOT NULL,
    price_cents     BIGINT NOT NULL CHECK (price_cents > 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (crdb_region, sku, effective_from)
) LOCALITY REGIONAL BY ROW;

-- One drop per region. `remaining` is deliberately mutable: the hot row (P3).
CREATE TABLE limited_drop (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    crdb_region  crdb_internal_region NOT NULL,
    sku          STRING NOT NULL REFERENCES item (sku),
    price_cents  BIGINT NOT NULL CHECK (price_cents > 0),
    total_packs  INT NOT NULL CHECK (total_packs > 0),
    remaining    INT NOT NULL,
    starts_at    TIMESTAMPTZ NOT NULL,
    ends_at      TIMESTAMPTZ NOT NULL,
    CHECK (remaining BETWEEN 0 AND total_packs),
    CHECK (ends_at > starts_at)
) LOCALITY REGIONAL BY ROW;

-- ============================================================ group 4 (D14)  (before inventory, which references match)
CREATE TABLE match (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    crdb_region    crdb_internal_region NOT NULL,  -- server's region
    mode           match_mode NOT NULL,
    map            STRING NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ NOT NULL,
    winning_squad  squad_callsign,                 -- NULL = draw
    CHECK (ended_at > started_at)
) LOCALITY REGIONAL BY ROW;

CREATE TABLE match_player (
    crdb_region  crdb_internal_region NOT NULL,    -- same as the match (derived fragmentation)
    match_id     UUID NOT NULL REFERENCES match (id),
    player_id    UUID NOT NULL REFERENCES player (id),
    squad        squad_callsign NOT NULL,
    kills        INT NOT NULL CHECK (kills >= 0),
    deaths       INT NOT NULL CHECK (deaths >= 0),
    assists      INT NOT NULL CHECK (assists >= 0),
    PRIMARY KEY (match_id, player_id)
) LOCALITY REGIONAL BY ROW;

-- ============================================================ group 3 (D12)
CREATE TABLE purchase (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    root_id         UUID NOT NULL,
    previous_id     UUID UNIQUE,
    crdb_region     crdb_internal_region NOT NULL,  -- payer's region
    payer_id        UUID NOT NULL REFERENCES player (id),
    recipient_id    UUID NOT NULL REFERENCES player (id),   -- = payer, or a friend for a gift
    status          purchase_status NOT NULL,
    total_cents     BIGINT NOT NULL CHECK (total_cents >= 0),
    payment_method  payment_method NOT NULL,
    gateway_ref     STRING NOT NULL,
    reason          STRING NOT NULL,                -- created | refunded
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, root_id),
    CHECK ((previous_id IS NULL) = (root_id = id)),
    -- a purchase starts SUCCEEDED or FAILED; a later version can only be a refund
    CHECK ((previous_id IS NULL AND status IN ('SUCCEEDED', 'FAILED'))
        OR (previous_id IS NOT NULL AND status = 'REFUNDED')),
    FOREIGN KEY (root_id) REFERENCES purchase (id),
    FOREIGN KEY (previous_id, root_id) REFERENCES purchase (id, root_id)
) LOCALITY REGIONAL BY ROW;
CREATE UNIQUE INDEX purchase_one_first_version ON purchase (root_id) WHERE previous_id IS NULL;
CREATE UNIQUE INDEX purchase_refunded_once     ON purchase (root_id) WHERE status = 'REFUNDED';
-- the gateway's payment reference is recorded once (no double recording)
CREATE UNIQUE INDEX purchase_gateway_ref_once  ON purchase (gateway_ref) WHERE previous_id IS NULL;

CREATE TABLE purchase_line (
    crdb_region       crdb_internal_region NOT NULL,  -- same as its purchase
    purchase_id       UUID NOT NULL REFERENCES purchase (id),
    sku               STRING NOT NULL REFERENCES item (sku),
    packs             INT NOT NULL CHECK (packs BETWEEN 1 AND 10),
    unit_price_cents  BIGINT NOT NULL CHECK (unit_price_cents > 0),   -- price paid at that moment
    line_total_cents  BIGINT NOT NULL,
    drop_id           UUID REFERENCES limited_drop (id),
    PRIMARY KEY (purchase_id, sku),
    CHECK (line_total_cents = packs * unit_price_cents)
) LOCALITY REGIONAL BY ROW;

-- The player's ammo chain (identity = player_id + sku, so no root_id; D12 Q11).
CREATE TABLE inventory (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    previous_id  UUID UNIQUE,
    crdb_region  crdb_internal_region NOT NULL,   -- owner's region
    player_id    UUID NOT NULL REFERENCES player (id),
    sku          STRING NOT NULL REFERENCES item (sku),
    change       INT NOT NULL CHECK (change <> 0),
    units_after  INT NOT NULL CHECK (units_after >= 0),   -- never negative
    reason       inventory_reason NOT NULL,
    purchase_id  UUID REFERENCES purchase (id),
    match_id     UUID REFERENCES match (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, player_id, sku),
    -- the previous row is the same player's same item
    FOREIGN KEY (previous_id, player_id, sku) REFERENCES inventory (id, player_id, sku),
    -- every change says what caused it
    CHECK ((reason IN ('PURCHASED', 'GIFT_IN', 'REFUNDED') AND purchase_id IS NOT NULL AND match_id IS NULL)
        OR (reason = 'USED' AND match_id IS NOT NULL AND purchase_id IS NULL)),
    CHECK ((reason = 'USED' OR reason = 'REFUNDED') = (change < 0))
) LOCALITY REGIONAL BY ROW;
CREATE UNIQUE INDEX inventory_one_first_row ON inventory (player_id, sku) WHERE previous_id IS NULL;
-- no double crediting / double usage
CREATE UNIQUE INDEX inventory_once_per_purchase ON inventory (purchase_id, player_id, sku, reason) WHERE purchase_id IS NOT NULL;
CREATE UNIQUE INDEX inventory_once_per_match    ON inventory (match_id, player_id, sku) WHERE match_id IS NOT NULL;
