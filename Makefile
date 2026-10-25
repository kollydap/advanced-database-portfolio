PY      := .venv/bin/python
COMPOSE := docker compose
ALL     := --profile core --profile stream --profile bigdata

.PHONY: venv up-core up-stream up-bigdata down status sql mongo redis neo4j

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

# Interactive shells
sql:
	$(COMPOSE) exec crdb-useast ./cockroach sql --insecure -d squadfire

mongo:
	$(COMPOSE) exec mongos mongosh squadfire

redis:
	$(COMPOSE) exec redis-master redis-cli

neo4j:
	$(COMPOSE) exec neo4j cypher-shell
