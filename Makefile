PY      := .venv/bin/python
COMPOSE := docker compose
ALL     := --profile core --profile stream --profile bigdata

.PHONY: venv up-core up-stream up-bigdata down status sql mongo redis neo4j p1-build p1-tests seed p1-integrity storefront p2 p2-mongo p3 p4-mongo p4 p5 p5-changefeed p5-lake

venv:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# CockroachDB x3, MongoDB sharded cluster, Redis + Sentinel, Neo4j  (~3.6 GB)
up-core:
	$(COMPOSE) --profile core up -d
	@echo "waiting for init jobs..."
	@$(COMPOSE) --profile core wait crdb-init mongo-init

# Kafka + Kafka Connect (Debezium)  (~1.5 GB)
up-stream:
	$(COMPOSE) --profile stream up -d

# HDFS + Spark  (~2.7 GB)
up-bigdata:
	$(COMPOSE) --profile bigdata up -d

# Stops and removes containers; data volumes are kept.
down:
	$(COMPOSE) $(ALL) down

status:
	$(COMPOSE) $(ALL) ps --format 'table {{.Name}}\t{{.Status}}'

# ---------------------------------------------------------------- portfolio 1
CRDB := $(COMPOSE) exec -T crdb-useast ./cockroach sql --insecure
P1   := portfolio-1-database-design

# Drops and recreates the squadfire database (all its rows are generated), then
# applies the schema, rules and views.
p1-build:
	$(CRDB) -e "DROP DATABASE IF EXISTS squadfire CASCADE"
	@for f in 00_database 01_schema 02_rules 03_views; do echo "applying $$f"; $(CRDB) < $(P1)/sql/$$f.sql > /dev/null || exit 1; done

# Every rule tried once right (accepted) and once wrong (rejected)
p1-tests: p1-build
	$(CRDB) --set=errexit=false --echo-sql < $(P1)/sql/04_rule_tests.sql > $(P1)/evidence/rule-tests.txt 2>&1
	@grep -c '^ERROR' $(P1)/evidence/rule-tests.txt | xargs echo "rejected statements:"

# 30 days of history (~5k players, ~50k purchases, ~2.5k matches) + telemetry file
seed: p1-build
	$(PY) data-gen/generate.py
	$(PY) data-gen/storefront.py
	@$(MAKE) --no-print-directory p1-integrity

# MongoDB storefront only: 33 documents (11 items x 3 regions), zone-sharded by region
storefront:
	$(PY) data-gen/storefront.py

p1-integrity:
	$(CRDB) < $(P1)/sql/05_integrity.sql | tee $(P1)/evidence/integrity.txt

# ---------------------------------------------------------------- portfolio 2
P2 := portfolio-2-query-optimisation

# Each experiment drops its index, plans the query (before), creates the index
# and plans it again (after). Usage: make p2 EXP=2.1_current_ammo
p2:
	$(CRDB) --format=table --echo-sql < $(P2)/sql/$(EXP).sql > $(P2)/evidence/$(EXP).txt 2>&1
	@grep -E "execution time|rows decoded|spans:" $(P2)/evidence/$(EXP).txt

# Interactive shells
sql:
	$(COMPOSE) exec crdb-useast ./cockroach sql --insecure -d squadfire

mongo:
	$(COMPOSE) exec mongos mongosh squadfire

redis:
	$(COMPOSE) exec redis-master redis-cli

neo4j:
	$(COMPOSE) exec neo4j cypher-shell

# usage: make p2-mongo EXP=2.6_storefront_explain   (mongosh script, run through mongos)
p2-mongo:
	$(COMPOSE) exec -T mongos mongosh --quiet squadfire --eval "$$(cat $(P2)/mongo/$(EXP).js)" > $(P2)/evidence/$(EXP).txt 2>&1
	@cat $(P2)/evidence/$(EXP).txt

# ---------------------------------------------------------------- portfolio 3
P3 := portfolio-3-transactions-concurrency
# usage: make p3 EXP=3.2_last_pack   (two-session race script; log saved as evidence)
p3:
	@mkdir -p $(P3)/evidence
	$(PY) $(P3)/scripts/$(EXP).py | tee $(P3)/evidence/$(EXP).txt

# ---------------------------------------------------------------- portfolio 4
P4 := portfolio-4-nosql
# usage: make p4-mongo EXP=4.1_storefront_rules   (the storefront schema is passed in as SCHEMA)
p4-mongo:
	@mkdir -p $(P4)/evidence
	$(COMPOSE) exec -T mongos mongosh --quiet squadfire --eval "const SCHEMA = $$(cat $(P4)/mongo/storefront.schema.json); $$(cat $(P4)/mongo/$(EXP).js)" > $(P4)/evidence/$(EXP).txt 2>&1
	@cat $(P4)/evidence/$(EXP).txt

# usage: make p4 EXP=4.2_leaderboard_cache   (Python scripts in redis/ or neo4j/)
p4:
	@mkdir -p $(P4)/evidence
	$(PY) $$(ls $(P4)/*/$(EXP).py) | tee $(P4)/evidence/$(EXP).txt

# ---------------------------------------------------------------- portfolio 5
P5 := portfolio-5-distributed-cloud
# usage: make p5 EXP=5.1a_changefeed
p5:
	@mkdir -p $(P5)/evidence
	$(PY) $(P5)/scripts/$(EXP).py | tee $(P5)/evidence/$(EXP).txt

# once, after make seed + make up-stream: CockroachDB -> Kafka
p5-changefeed:
	$(CRDB) < $(P5)/sql/changefeed.sql

# Kafka -> HDFS bronze/silver/gold; Spark downloads its official Kafka source on first run
SPARK_KAFKA := org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.6
p5-lake:
	@mkdir -p $(P5)/evidence
	$(COMPOSE) exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 --packages $(SPARK_KAFKA) \
	  --conf spark.jars.ivy=/tmp/.ivy2 --conf spark.driver.host=spark-master \
	  --conf spark.sql.session.timeZone=UTC --conf spark.executor.memory=1g \
	  /jobs/lake.py 2> $(P5)/evidence/5.1c_lake.spark.log | tee $(P5)/evidence/5.1c_lake.txt
