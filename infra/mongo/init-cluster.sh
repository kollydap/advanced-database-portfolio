#!/usr/bin/env bash
# Builds the Squadfire MongoDB cluster:
#   - config server replica set
#   - 3 regional shard replica sets (primary + async secondary + arbiter)
#   - one zone per region, so regional documents can be pinned to their region's
#     shard (horizontal fragmentation, D7). Collections are sharded in portfolio 4.
set -euo pipefail

# shard slug : region (zone) name
SHARDS="useast:us-east euwest:eu-west afwest:af-west"

wait_for() {
  until mongosh --quiet --host "$1" --eval 'db.adminCommand({ping:1}).ok' >/dev/null 2>&1; do
    echo "waiting for $1"; sleep 2
  done
}

wait_for mongo-cfg
for pair in $SHARDS; do
  s=${pair%%:*}
  for m in a b arb; do wait_for "mongo-$s-$m"; done
done

init_rs() {  # $1=host to run on, $2=js config
  mongosh --quiet --host "$1" --eval "
    try { rs.status(); print('$1 already initiated'); }
    catch (e) { rs.initiate($2); print('$1 initiated'); }"
}

init_rs mongo-cfg "{_id:'cfgrs', configsvr:true, members:[{_id:0, host:'mongo-cfg:27017'}]}"

for pair in $SHARDS; do
  s=${pair%%:*}
  # priority 2 on "a" makes it the preferred primary (master); "b" is the slave.
  init_rs "mongo-$s-a" "{_id:'rs-$s', members:[
    {_id:0, host:'mongo-$s-a:27017', priority:2},
    {_id:1, host:'mongo-$s-b:27017', priority:1},
    {_id:2, host:'mongo-$s-arb:27017', arbiterOnly:true}]}"
done

# Each shard must elect its primary before it can join the cluster.
for pair in $SHARDS; do
  s=${pair%%:*}
  until mongosh --quiet --host "mongo-$s-a" --eval 'db.hello().isWritablePrimary' | grep -q true; do
    echo "waiting for rs-$s primary"; sleep 2
  done
done
wait_for mongos

mongosh --quiet --host mongos --eval "
// Primary-secondary-arbiter sets need an explicit default write concern before mongos adds them.
db.adminCommand({setDefaultRWConcern: 1, defaultWriteConcern: {w: 1}});
const existing = db.adminCommand({listShards: 1}).shards.map(s => s._id);
for (const pair of '$SHARDS'.split(' ')) {
  const [s, region] = pair.split(':');
  if (!existing.includes('rs-' + s)) {
    sh.addShard('rs-' + s + '/mongo-' + s + '-a:27017,mongo-' + s + '-b:27017');
  }
  sh.addShardToZone('rs-' + s, region);
}
sh.enableSharding('squadfire');
print('mongo cluster ready');
"
