"""MongoDB storefront (D6, D7): one document per (region, item).

CockroachDB keeps the slim item row and the prices (the source of truth).
MongoDB holds what a shop page shows: the type-specific details, which differ
by item type, plus the region's current price copied from CockroachDB.

The collection is zone-sharded by region: each region's documents live on
that region's shard (the shards are tagged with zones by infra/mongo).

    python data-gen/storefront.py          # after make seed; replaces the collection
"""
import argparse

import psycopg
from pymongo import MongoClient
from bson.min_key import MinKey
from bson.max_key import MaxKey

# Details that only make sense for some item types: no empty columns needed.
DETAILS = {
    "AMMO-556-120": {"calibre": "5.56mm", "damage": 32, "penetration": 2},
    "AMMO-762-90": {"calibre": "7.62mm", "damage": 41, "penetration": 3},
    "AMMO-9MM-150": {"calibre": "9mm", "damage": 24, "penetration": 1},
    "AMMO-12G-40": {"calibre": "12g", "damage": 18, "pellets": 8, "penetration": 1},
    "AMMO-AP-60": {"calibre": "5.56mm", "damage": 30, "penetration": 5, "limited": True},
    "GREN-FRAG-3": {"blast_radius_m": 6, "fuse_s": 3.5, "damage": 120},
    "GREN-SMOKE-3": {"cloud_radius_m": 8, "duration_s": 20},
    "MED-KIT-5": {"heal": 50, "use_time_s": 4},
    "SKIN-TIGER-AK": {"weapon": "AK-47", "rarity": "RARE", "tradable": False},
    "SKIN-ARCTIC-M4": {"weapon": "M4A1", "rarity": "EPIC", "tradable": False},
    "SKIN-GOLD-DEAGLE": {"weapon": "Desert Eagle", "rarity": "LEGENDARY", "tradable": False},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    ap.add_argument("--mongo", default="mongodb://localhost:27017")
    args = ap.parse_args()

    with psycopg.connect(args.dsn) as conn:
        rows = conn.execute("""
            SELECT p.crdb_region::STRING, i.sku, i.name, i.item_type::STRING, i.units_per_pack, p.price_cents
            FROM current_price p JOIN item i ON i.sku = p.sku
            ORDER BY 1, 2""").fetchall()

    docs = [{
        "_id": f"{region}:{sku}",
        "region": region,
        "sku": sku,
        "name": name,
        "type": item_type,
        "units_per_pack": units,
        "price": {"cents": price, "currency": "USD"},
        "details": DETAILS[sku],
    } for region, sku, name, item_type, units, price in rows]

    client = MongoClient(args.mongo)
    db = client.squadfire
    db.storefront.drop()
    # Zones first, then shard the empty collection: MongoDB creates one chunk
    # per region directly on that region's shard, so nothing has to migrate.
    # The zones cover the whole key space with no gaps (af-west from MinKey,
    # us-east to MaxKey). With gaps, the gap chunks sit on the primary shard
    # and a region-only query, whose bounds reach (region, MaxKey), asks it too.
    regions = sorted({d["region"] for d in docs})
    bounds = [{"region": MinKey(), "sku": MinKey()}] + \
             [{"region": r, "sku": MinKey()} for r in regions[1:]] + \
             [{"region": MaxKey(), "sku": MaxKey()}]
    for region, lo, hi in zip(regions, bounds, bounds[1:]):
        client.admin.command("updateZoneKeyRange", "squadfire.storefront", min=lo, max=hi, zone=region)
    # shard key: region first, so one region's documents form one range
    client.admin.command("shardCollection", "squadfire.storefront", key={"region": 1, "sku": 1})
    db.storefront.insert_many(docs)
    print(f"  storefront     {db.storefront.count_documents({}):>8,} documents")


if __name__ == "__main__":
    main()
