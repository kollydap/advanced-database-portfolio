"""Portfolio 5.1c: Kafka -> HDFS lake (bronze, silver, gold) with Spark.

bronze  every Kafka message exactly as it arrived (topic, partition, offset,
        timestamp, key, value), appended incrementally: a Structured Streaming
        query with trigger(availableNow) reads only what is new since its
        checkpoint, then stops.
silver  typed, cleaned tables, rebuilt from bronze. The changefeed delivers
        at least once, so CDC rows are de-duplicated on their key, keeping
        the latest commit timestamp (`updated`). Partitioned by region and
        date, so a region/day query reads only its own files.
gold    the warehouse star schema (fact_sales + dim_date, dim_item,
        dim_region) and daily summaries, including rounds fired and events
        from telemetry.

    make p5-lake
"""
from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import (BooleanType, LongType, StringType, StructField, StructType)

LAKE = "hdfs://namenode:8020/lake"
TOPICS = ["purchase", "purchase_line", "inventory", "item", "region", "telemetry"]

spark = SparkSession.builder.appName("squadfire-lake").getOrCreate()
spark.sparkContext.setLogLevel("WARN")


def schema(*cols):
    return StructType([StructField(n, t) for n, t in cols])


S, L, B = StringType(), LongType(), BooleanType()
AFTER = {
    "purchase": schema(("id", S), ("root_id", S), ("previous_id", S), ("crdb_region", S), ("payer_id", S),
                       ("recipient_id", S), ("status", S), ("total_cents", L), ("payment_method", S),
                       ("gateway_ref", S), ("reason", S), ("created_at", S)),
    "purchase_line": schema(("crdb_region", S), ("purchase_id", S), ("sku", S), ("packs", L),
                            ("unit_price_cents", L), ("line_total_cents", L), ("drop_id", S)),
    "inventory": schema(("id", S), ("previous_id", S), ("crdb_region", S), ("player_id", S), ("sku", S),
                        ("change", L), ("units_after", L), ("reason", S), ("purchase_id", S),
                        ("match_id", S), ("created_at", S)),
    "item": schema(("sku", S), ("name", S), ("item_type", S), ("units_per_pack", L), ("consumable", B),
                   ("active", B)),
    "region": schema(("code", S), ("name", S)),
}
TELEMETRY = schema(("event", S), ("ts", S), ("region", S), ("player", S), ("match", S), ("sku", S),
                   ("rounds", L), ("victim", S), ("mode", S))


def count(path):
    return spark.read.parquet(f"{LAKE}/{path}").count()


# ------------------------------------------------------------------ bronze
raw = (spark.readStream.format("kafka")
       .option("kafka.bootstrap.servers", "kafka:9092")
       .option("subscribe", ",".join(f"squadfire.{t}" for t in TOPICS))
       .option("startingOffsets", "earliest")
       .load())
bronze = raw.select(F.regexp_replace("topic", "^squadfire\\.", "").alias("topic"), "partition", "offset",
                    F.col("timestamp").alias("kafka_ts"),
                    F.col("key").cast("string").alias("key"), F.col("value").cast("string").alias("value"))
(bronze.writeStream.format("parquet")
 .option("path", f"{LAKE}/bronze").option("checkpointLocation", f"{LAKE}/_checkpoints/bronze")
 .partitionBy("topic").trigger(availableNow=True).start().awaitTermination())

b = spark.read.parquet(f"{LAKE}/bronze")
print("== bronze: Kafka messages as they arrived")
for row in b.groupBy("topic").count().orderBy("topic").collect():
    print(f"   {row['topic']:<15} {row['count']:>9,}")

# ------------------------------------------------------------------ silver
print("\n== silver: typed, de-duplicated, partitioned")
rows = b.where(~F.col("value").startswith('{"resolved"'))
latest = Window.partitionBy("key").orderBy(F.col("updated").cast("decimal(38,10)").desc(), F.col("offset").desc())
for table, struct in AFTER.items():
    cdc = (rows.where(F.col("topic") == table)
           .select("key", "offset", F.from_json("value", schema(("after", struct), ("updated", S))).alias("m"))
           .select("key", "offset", "m.updated", "m.after.*"))
    received = cdc.count()
    t = (cdc.withColumn("n", F.row_number().over(latest)).where("n = 1")
         .where(F.col(struct.fieldNames()[0]).isNotNull())             # a delete has no "after"
         .drop("key", "offset", "n"))
    parts = []
    if "created_at" in struct.fieldNames():
        t = t.withColumn("created_at", F.to_timestamp("created_at")).withColumn("dt", F.to_date("created_at"))
        parts = ["crdb_region", "dt"]
    elif "crdb_region" in struct.fieldNames():
        parts = ["crdb_region"]
    t.write.mode("overwrite").partitionBy(*parts).parquet(f"{LAKE}/silver/{table}")
    kept = count(f"silver/{table}")
    print(f"   {table:<15} {received:>9,} received -> {kept:>9,} rows   partitioned by {', '.join(parts) or '-'}")

tel = (rows.where(F.col("topic") == "telemetry")
       .select(F.from_json("value", TELEMETRY).alias("e")).select("e.*")
       .withColumn("ts", F.to_timestamp("ts")).withColumn("dt", F.to_date("ts")))
tel.write.mode("overwrite").partitionBy("region", "dt").parquet(f"{LAKE}/silver/telemetry")
print(f"   {'telemetry':<15} {count('silver/telemetry'):>9,} events                    partitioned by region, dt")

# ------------------------------------------------------------------ gold
print("\n== gold: star schema and daily summaries")
silver = {t: spark.read.parquet(f"{LAKE}/silver/{t}") for t in list(AFTER) + ["telemetry"]}
p = silver["purchase"]
original = p.where(F.col("previous_id").isNull())
refunded = p.where(F.col("status") == "REFUNDED").select(F.col("previous_id").alias("id")).withColumn("refunded", F.lit(True))
sales = (silver["purchase_line"].drop("crdb_region")
         .join(original.where(F.col("status") == "SUCCEEDED"), F.col("purchase_id") == F.col("id"))
         .join(refunded, "id", "left")
         .select(F.date_format("dt", "yyyyMMdd").cast("int").alias("date_key"),
                 F.col("crdb_region").alias("region"), "sku", "purchase_id", "payer_id", "recipient_id",
                 (F.col("payer_id") != F.col("recipient_id")).alias("is_gift"),
                 F.col("drop_id").isNotNull().alias("is_limited_drop"),
                 F.coalesce("refunded", F.lit(False)).alias("refunded"),
                 "packs", "line_total_cents",
                 F.when(F.col("refunded"), 0).otherwise(F.col("line_total_cents")).alias("net_cents")))
bounds = sales.agg(F.min("date_key"), F.max("date_key")).first()
dates = (spark.sql(f"""SELECT explode(sequence(to_date('{bounds[0]}', 'yyyyMMdd'),
                                                to_date('{bounds[1]}', 'yyyyMMdd'))) AS date""")
         .select(F.date_format("date", "yyyyMMdd").cast("int").alias("date_key"), "date",
                 F.year("date").alias("year"), F.month("date").alias("month"),
                 F.date_format("date", "EEEE").alias("weekday"),
                 F.dayofweek("date").isin(1, 7).alias("is_weekend")))
gold = {
    "fact_sales": sales,
    "dim_date": dates,
    "dim_item": silver["item"].drop("updated"),
    "dim_region": silver["region"].drop("updated").withColumnRenamed("code", "region"),
    "daily_sales": (sales.groupBy("date_key", "region", "sku")
                    .agg(F.sum("packs").alias("packs"), F.sum("line_total_cents").alias("gross_cents"),
                         F.sum("net_cents").alias("net_cents"))),
    # shot_fired covers ammo rounds and grenade throws; med kits show up as revive events
    "daily_fired": (silver["telemetry"].where(F.col("event") == "shot_fired")
                    .groupBy(F.date_format("dt", "yyyyMMdd").cast("int").alias("date_key"), "region", "sku")
                    .agg(F.count("*").alias("shots"), F.sum("rounds").alias("rounds"))),
    "daily_events": (silver["telemetry"]
                     .groupBy(F.date_format("dt", "yyyyMMdd").cast("int").alias("date_key"), "region", "event")
                     .agg(F.count("*").alias("events"))),
}
for name, df in gold.items():
    df.coalesce(1).write.mode("overwrite").parquet(f"{LAKE}/gold/{name}")
    print(f"   {name:<17} {count('gold/' + name):>8,} rows")

spark.stop()
