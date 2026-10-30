"""Experiment 5.1d: HDFS gold layer -> DuckDB warehouse, checked against CockroachDB.

1  copy /lake/gold out of HDFS (into the namenode's /staging mount)
2  load the star schema into DuckDB: fact_sales + dim_date, dim_item, dim_region
3  reconcile: the warehouse's totals == CockroachDB's own totals
   (net revenue and packs per region; rounds fired and revives from
   telemetry == USED rows in the inventory chains, two independent paths
   into the lake)
4  manager questions, answered from the star schema

    make p5 EXP=5.1d_warehouse
"""
import argparse
import subprocess
from pathlib import Path

import duckdb
import psycopg

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE = ROOT / "portfolio-5-distributed-cloud" / "warehouse"
GOLD = WAREHOUSE / "staging" / "gold"
TABLES = ["dim_date", "dim_item", "dim_region", "fact_sales", "daily_sales", "daily_fired", "daily_events"]

NET_SQL = """
SELECT p.crdb_region::STRING, sum(l.packs)::INT8, sum(l.line_total_cents)::INT8,
       sum(l.line_total_cents) FILTER (WHERE r.id IS NULL)::INT8
FROM purchase p
JOIN purchase_line l ON l.purchase_id = p.id
LEFT JOIN purchase r ON r.previous_id = p.id AND r.status = 'REFUNDED'
WHERE p.previous_id IS NULL AND p.status = 'SUCCEEDED'
GROUP BY 1 ORDER BY 1"""
# USED rows per region: ammo + grenades (telemetry: shot_fired rounds) and med kits (telemetry: revives)
USED_SQL = """SELECT v.crdb_region::STRING,
                     (-sum(v.change) FILTER (WHERE i.item_type IN ('AMMO', 'GRENADE')))::INT8,
                     (-sum(v.change) FILTER (WHERE i.item_type = 'MEDKIT'))::INT8
              FROM inventory v JOIN item i USING (sku) WHERE v.reason = 'USED'
              GROUP BY 1 ORDER BY 1"""


def usd(cents):
    return f"${cents / 100:,.2f}"


def table(rows, header):
    print("   " + "  ".join(f"{h:>{w}}" if i else f"{h:<{w}}" for i, (h, w) in enumerate(header)))
    for r in rows:
        print("   " + "  ".join(f"{v:>{w}}" if i else f"{v:<{w}}" for i, (v, (_, w)) in enumerate(zip(r, header))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    args = ap.parse_args()

    print("== 1. Copy the gold layer out of HDFS")
    subprocess.run(["docker", "compose", "exec", "-T", "namenode", "hdfs", "dfs", "-get", "-f",
                    "/lake/gold", "/staging/"], check=True, cwd=ROOT, capture_output=True)
    files = sorted(GOLD.rglob("*.parquet"))
    print(f"   hdfs dfs -get /lake/gold -> warehouse/staging/gold: {len(files)} Parquet files, "
          f"{sum(f.stat().st_size for f in files) / 1024:,.0f} KiB")

    print("\n== 2. Load the star schema into DuckDB (warehouse/squadfire.duckdb)")
    db = duckdb.connect(str(WAREHOUSE / "squadfire.duckdb"))
    for t in TABLES:
        db.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM read_parquet('{GOLD / t}/*.parquet')")
        n = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        cols = ", ".join(c[0] for c in db.execute(f"DESCRIBE {t}").fetchall())
        print(f"   {t:<17} {n:>7,} rows   ({cols})")
    orphans = db.execute("""
        SELECT count(*) FILTER (WHERE d.date_key IS NULL), count(*) FILTER (WHERE i.sku IS NULL),
               count(*) FILTER (WHERE r.region IS NULL)
        FROM fact_sales f LEFT JOIN dim_date d USING (date_key) LEFT JOIN dim_item i USING (sku)
        LEFT JOIN dim_region r USING (region)""").fetchone()
    print(f"   fact rows without a matching dimension (date, item, region): {orphans}")

    print("\n== 3. Reconcile with CockroachDB")
    pg = psycopg.connect(args.dsn, autocommit=True)
    crdb = {r[0]: r[1:] for r in pg.execute(NET_SQL).fetchall()}
    wh = {r[0]: r[1:] for r in db.execute("""SELECT region, sum(packs), sum(line_total_cents), sum(net_cents)
                                             FROM fact_sales GROUP BY 1 ORDER BY 1""").fetchall()}
    rows = [(reg, f"{wh[reg][0]:,}", f"{crdb[reg][0]:,}", usd(wh[reg][2]), usd(crdb[reg][2]),
             str(tuple(wh[reg]) == tuple(crdb[reg]))) for reg in sorted(crdb)]
    print("   sales (succeeded purchases; net = minus refunds):")
    table(rows, [("region", 8), ("packs (DW)", 10), ("(CRDB)", 8), ("net (DW)", 12), ("(CRDB)", 12), ("match", 5)])

    used = {r[0]: r[1:] for r in pg.execute(USED_SQL).fetchall()}
    tel = {r[0]: r[1:] for r in db.execute("""
        SELECT region, (SELECT sum(rounds) FROM daily_fired f WHERE f.region = e.region),
               sum(events) FILTER (WHERE event = 'revive')
        FROM daily_events e GROUP BY 1""").fetchall()}
    rows = [(reg, f"{tel[reg][0]:,}", f"{used[reg][0]:,}", f"{tel[reg][1]:,}", f"{used[reg][1]:,}",
             str(tuple(tel[reg]) == tuple(used[reg]))) for reg in sorted(used)]
    print("\n   items used in matches, two independent paths into the lake:")
    print("   telemetry (game servers -> Kafka) vs USED inventory rows (CockroachDB -> changefeed)")
    table(rows, [("region", 8), ("rounds fired", 12), ("ammo+gren.", 10), ("revives", 7), ("med kits", 8),
                 ("match", 5)])

    print("\n== 4. Manager questions (star joins)")
    print("\n   a. Best seller per region by net revenue")
    table([(r[0], r[1], usd(r[2]), f"{r[3]:.0%}") for r in db.execute("""
        WITH s AS (SELECT r.name AS region, i.name AS item, sum(f.net_cents) AS net
                   FROM fact_sales f JOIN dim_region r USING (region) JOIN dim_item i USING (sku)
                   GROUP BY 1, 2)
        SELECT region, item, net, net / sum(net) OVER (PARTITION BY region) AS share
        FROM s QUALIFY row_number() OVER (PARTITION BY region ORDER BY net DESC) = 1
        ORDER BY net DESC""").fetchall()],
          [("region", 22), ("best seller", 28), ("net", 10), ("share", 6)])

    print("\n   b. Gifts and limited drops as a share of net revenue")
    table([(r[0], usd(r[1]), f"{r[2]:.1%}", f"{r[3]:.1%}") for r in db.execute("""
        SELECT r.name, sum(net_cents),
               sum(net_cents) FILTER (WHERE is_gift) / sum(net_cents),
               sum(net_cents) FILTER (WHERE is_limited_drop) / sum(net_cents)
        FROM fact_sales f JOIN dim_region r USING (region) GROUP BY 1 ORDER BY 2 DESC""").fetchall()],
          [("region", 22), ("net", 11), ("gifts", 6), ("drops", 6)])

    print("\n   c. Average net revenue per day: weekdays vs weekends")
    table([(r[0], usd(r[1]), usd(r[2])) for r in db.execute("""
        WITH d AS (SELECT f.region, d.date, d.is_weekend, sum(net_cents) AS net
                   FROM fact_sales f JOIN dim_date d USING (date_key) GROUP BY 1, 2, 3)
        SELECT r.name, avg(net) FILTER (WHERE NOT is_weekend), avg(net) FILTER (WHERE is_weekend)
        FROM d JOIN dim_region r USING (region) GROUP BY 1 ORDER BY 1""").fetchall()],
          [("region", 22), ("weekday", 9), ("weekend", 9)])

    print("\n   d. Ammo bought vs fired per calibre (packs x rounds per pack, vs rounds fired)")
    table([(r[0], f"{r[1]:,}", f"{r[2]:,}", f"{r[3]:.0%}") for r in db.execute("""
        WITH bought AS (SELECT sku, sum(packs * i.units_per_pack) AS rounds
                        FROM fact_sales f JOIN dim_item i USING (sku)
                        WHERE i.item_type = 'AMMO' AND NOT refunded GROUP BY 1),
             fired AS (SELECT sku, sum(rounds) AS rounds FROM daily_fired GROUP BY 1)
        SELECT i.name, b.rounds, f.rounds, f.rounds / b.rounds
        FROM bought b JOIN fired f USING (sku) JOIN dim_item i USING (sku) ORDER BY 1""").fetchall()],
          [("ammo", 28), ("bought", 9), ("fired", 9), ("used", 5)])


if __name__ == "__main__":
    main()
