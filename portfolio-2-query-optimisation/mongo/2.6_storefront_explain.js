// Experiment 2.6: MongoDB explain() on the zone-sharded storefront.
// 33 documents (11 items x 3 regions), shard key { region: 1, sku: 1 }.
// Lesson 1: giving the region sends the query to one shard (as in 2.5).
// Lesson 2: an index on a nested detail field turns COLLSCAN into IXSCAN.
// Run through mongos:  make p2-mongo EXP=2.6_storefront_explain

// every stage name in a plan tree, top-down (e.g. FETCH > IXSCAN)
function stages(plan) {
  if (!plan) return [];
  const p = plan.queryPlan || plan;
  const children = p.inputStage ? [p.inputStage] : (p.inputStages || []);
  return [p.stage].concat(...children.map(stages));
}

function report(title, cursor) {
  const e = cursor.explain("executionStats");
  const win = e.queryPlanner.winningPlan;
  const shards = (win.shards || []).map(s => s.shardName + ": " + stages(s.winningPlan).join(" > "));
  print("\n## " + title);
  printjson({
    router_stage: win.stage,                    // SINGLE_SHARD or SHARD_MERGE
    shards_asked: shards.length,
    shard_plans: shards,
    returned: e.executionStats.nReturned,
    keys_examined: e.executionStats.totalKeysExamined,
    docs_examined: e.executionStats.totalDocsExamined,
    time_ms: e.executionStats.executionTimeMillis,
  });
}

// start without the experiment's indexes
for (const name of ["sku_1", "details.calibre_1"]) {
  if (db.storefront.getIndexes().some(i => i.name === name)) db.storefront.dropIndex(name);
}

// ------------------------------------------- lesson 1: region given or not
report("A. AP rounds in us-east (region given)",
  db.storefront.find({ region: "us-east", sku: "AMMO-AP-60" }));
report("B. AP rounds, region not given",
  db.storefront.find({ sku: "AMMO-AP-60" }));

// B used COLLSCAN because the shard-key index starts with region. An index
// on sku alone cuts the docs read, but the router still asks every shard:
// the region picks the shard, the index picks the documents.
db.storefront.createIndex({ sku: 1 });
report("B2. AP rounds, region not given, with index on sku",
  db.storefront.find({ sku: "AMMO-AP-60" }));
db.storefront.dropIndex("sku_1");

// --------------------------------------------- lesson 2: index or not
report("C. all 5.56mm ammo, no index on details.calibre",
  db.storefront.find({ "details.calibre": "5.56mm" }));

db.storefront.createIndex({ "details.calibre": 1 });

report("D. all 5.56mm ammo, with index on details.calibre",
  db.storefront.find({ "details.calibre": "5.56mm" }));

// --------------------------------------------- both together
report("E. 5.56mm ammo in af-west (region + index)",
  db.storefront.find({ region: "af-west", "details.calibre": "5.56mm" }));

// the answer itself
print("\n## E. answer");
db.storefront.find({ region: "af-west", "details.calibre": "5.56mm" },
                   { _id: 0, sku: 1, name: 1, "price.cents": 1, "details.penetration": 1 })
  .sort({ sku: 1 }).forEach(printjson);
