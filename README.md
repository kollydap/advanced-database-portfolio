# Advanced Database Systems Portfolio — Squadfire

MIT 8103 Advanced Database Systems · 2026/2027 First Semester Continuous Assessment

> **Status:** design complete ([`DECISIONS.md`](DECISIONS.md)); build in progress.

## Case study: Squadfire

| | |
|---|---|
| **Organisation** | Squadfire, a (simulated) tactical squad shooter with game servers in three regions: `us-east`, `eu-west` and `af-west` (Lagos) |
| **Nature of the system** | Players form squads and play matches. They buy in-game ammunition and gear (5.56 mm, 7.62 mm, shotgun shells, armour-piercing rounds, grenades, med kits, skins) **in US dollars**, from regional prices, or **gift** them to friends. Limited-edition drops sell a fixed number of packs per region. Ammo is used up in matches. |
| **Users of the database** | Players (buyers, gift recipients), game servers (matches, ammo usage), store and live-ops staff (prices, drops), analysts (revenue, balance, fraud) |
| **Major data** | Players, friendships, items and regional prices, limited drops, purchases (with payment status), each player's ammo inventory, matches and squads, gameplay telemetry (shots, kills, sessions) |
| **Key operations** | Browse the store, buy for yourself, gift a friend, buy a limited drop, enforce a $50 daily spend limit, play a match and use ammo, add friends, leaderboards, revenue and gameplay analytics |

## Architecture

| Store | Role |
|---|---|
| CockroachDB (NewSQL, 3 nodes = 3 regions) | Source of truth: players, purchases, inventory, prices, drops, matches |
| MongoDB (zone-sharded; each shard primary + async secondary) | Item catalogue with type-specific attributes |
| Redis (master + 2 async replicas + Sentinel) | Leaderboards, store cache, drop stock display |
| Neo4j | Player interaction graph |
| Kafka + CDC (changefeeds, Debezium) | Committed changes and gameplay telemetry as event streams |
| HDFS + Spark | Data lake (bronze/silver/gold) |
| DuckDB | Star-schema data warehouse |

## Running it

Requirements: Docker Desktop with 8 GB RAM, and Python 3.12.

```bash
make venv
make up-core      # CockroachDB, MongoDB, Redis, Neo4j
make status
```

| Service | Address |
|---|---|
| CockroachDB SQL (us-east / eu-west / af-west) | `localhost:26257` / `:26258` / `:26259` |
| CockroachDB console | http://localhost:8090 |
| MongoDB (mongos) | `localhost:27017` |
| Redis master / replicas | `localhost:16379` / `:16380`, `:16381` |
| Neo4j browser / Bolt | http://localhost:7474 / `localhost:7687` |
| Kafka / Kafka Connect | `localhost:29092` / http://localhost:8083 |
| HDFS UI / Spark UI | http://localhost:9870 / http://localhost:8088 |

## Portfolio index

| # | Activity | Folder |
|---|---|---|
| 1 | Database design and modelling | `portfolio-1-database-design/` *(B2)* |
| 2 | Query processing and optimisation | `portfolio-2-query-optimisation/` *(B4)* |
| 3 | Transactions and concurrency | `portfolio-3-transactions-concurrency/` *(B5)* |
| 4 | NoSQL and advanced data models | `portfolio-4-nosql/` *(B6)* |
| 5 | Distributed and cloud databases | `portfolio-5-distributed-cloud/` *(B7)* |

All data is synthetic, with generated player handles and no personal data.
