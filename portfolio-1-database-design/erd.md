# Squadfire ERD

12 tables in CockroachDB. `PK` primary key, `FK` foreign key, `UK` unique. A line from a table to itself is a **chain**: a new version points to the version it replaces (`previous_id`, unique, so each version is replaced at most once).

```mermaid
erDiagram
    region {
        crdb_internal_region code PK
        string name
    }
    player_handle {
        string handle_key PK "lowercase name, never released"
        uuid root_id "the owning player"
    }
    player {
        uuid id PK
        uuid root_id FK "first version = the player's permanent id"
        uuid previous_id FK,UK
        crdb_internal_region crdb_region "home region = fragment"
        string handle
        player_status status
        timestamptz created_at
    }
    friendship {
        uuid id PK
        uuid root_id FK
        uuid previous_id FK,UK
        uuid player_low FK
        uuid player_high FK
        uuid requested_by FK
        friendship_status status
    }
    item {
        string sku PK
        string name
        item_type item_type
        int units_per_pack
        jsonb change_log
    }
    item_price {
        crdb_internal_region crdb_region PK
        string sku PK,FK
        timestamptz effective_from PK
        bigint price_cents
    }
    limited_drop {
        uuid id PK
        crdb_internal_region crdb_region
        string sku FK
        bigint price_cents
        int total_packs
        int remaining "the one mutable counter"
    }
    purchase {
        uuid id PK
        uuid root_id FK
        uuid previous_id FK,UK "refund version"
        crdb_internal_region crdb_region "payer's region"
        uuid payer_id FK
        uuid recipient_id FK "payer, or a friend (gift)"
        purchase_status status
        bigint total_cents
        string gateway_ref
    }
    purchase_line {
        uuid purchase_id PK,FK
        string sku PK,FK
        int packs
        bigint unit_price_cents "price paid"
        bigint line_total_cents
        uuid drop_id FK
    }
    inventory {
        uuid id PK
        uuid previous_id FK,UK
        crdb_internal_region crdb_region "owner's region"
        uuid player_id FK
        string sku FK
        int change
        int units_after
        inventory_reason reason
        uuid purchase_id FK
        uuid match_id FK
    }
    match {
        uuid id PK
        crdb_internal_region crdb_region "server's region"
        match_mode mode
        timestamptz started_at
        timestamptz ended_at
        squad_callsign winning_squad
    }
    match_player {
        uuid match_id PK,FK
        uuid player_id PK,FK
        squad_callsign squad
        int kills
        int deaths
        int assists
    }

    region        ||--o{ player        : "is home of"
    player_handle ||--|{ player        : "names"
    player        |o--o| player        : "replaced by"
    player        ||--o{ friendship    : "is in"
    friendship    |o--o| friendship    : "replaced by"
    player        ||--o{ purchase      : "pays / receives"
    purchase      |o--o| purchase      : "refunded by"
    purchase      ||--|{ purchase_line : "contains"
    item          ||--o{ purchase_line : "sold as"
    limited_drop  |o--o{ purchase_line : "sold through"
    item          ||--o{ item_price    : "priced in each region"
    item          ||--o{ limited_drop  : "dropped as"
    player        ||--o{ inventory     : "owns"
    item          ||--o{ inventory     : "held as"
    inventory     |o--o| inventory     : "replaced by"
    purchase      |o--o{ inventory     : "credits / refunds"
    match         |o--o{ inventory     : "uses ammo in"
    match         ||--|{ match_player  : "has"
    player        ||--o{ match_player  : "plays in"
```

**Not drawn as lines:**
- `crdb_region` on the other tables takes its values from `region` too. Only player's home region is drawn, to keep the diagram readable.
- `purchase` has two separate links to `player` (payer and recipient), and `friendship` has three (low, high, requested_by). Each is drawn as one line.
