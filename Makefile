PY      := .venv/bin/python
COMPOSE := docker compose
ALL     := --profile core --profile stream --profile bigdata

.PHONY: venv up-core up-stream up-bigdata down status sql mongo redis neo4j p1-build p1-tests seed p1-integrity p2

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
	@$(MAKE) --no-print-directory p1-integrity

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
