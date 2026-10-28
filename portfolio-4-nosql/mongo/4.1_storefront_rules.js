// Experiment 4.1: the storefront validator, tried the right way and the wrong
// way, then one aggregation across the documents.
// SCHEMA is storefront.schema.json, prepended by the Makefile:
//   make p4-mongo EXP=4.1_storefront_rules

// (Re)apply the rules, so this script does not depend on how the collection was built.
db.runCommand({ collMod: "storefront", validator: { $jsonSchema: SCHEMA },
                validationLevel: "strict", validationAction: "error" });
const v = db.getCollectionInfos({ name: "storefront" })[0].options;
print(`## Validator: level=${v.validationLevel}, action=${v.validationAction}, types=` +
      SCHEMA.oneOf.map(b => b.properties.type.enum[0]).join("/"));

const breaking = db.storefront.countDocuments({ $nor: [{ $jsonSchema: SCHEMA }] });
print(`   existing documents: ${db.storefront.countDocuments()}, breaking the rules: ${breaking}`);

// The reasons MongoDB gives (errInfo.details), reduced to the rule that failed
// for the document's own type: missing fields and refused values. A oneOf
// failure lists every type's branch; the branches for other types (which fail
// on "type" itself) are dropped.
const failsOnType = b => JSON.stringify(b).includes('"propertyName":"type"');
function reasons(node, out = []) {
  if (Array.isArray(node)) { node.forEach(n => reasons(n, out)); return out; }
  if (!node || typeof node !== "object") return out;
  if (node.operatorName === "oneOf") return reasons(node.schemasNotSatisfied.filter(b => !failsOnType(b)), out);
  if (node.missingProperties) out.push("missing " + node.missingProperties.join(", "));
  if (node.propertyName && node.details) {
    for (const d of node.details) {
      if (d.operatorName === "enum" || d.operatorName === "minimum")
        out.push(`${node.propertyName} = ${JSON.stringify(d.consideredValue)} fails ${d.operatorName}`);
    }
  }
  Object.values(node).forEach(n => reasons(n, out));
  return out;
}

const valid_skin = {
  _id: "us-east:SKIN-TEST-UZI", region: "us-east", sku: "SKIN-TEST-UZI", name: "Test Uzi skin",
  type: "SKIN", units_per_pack: 1, price: { cents: 499, currency: "USD" },
  details: { weapon: "Uzi", rarity: "RARE", tradable: false },
};
const tests = [
  ["valid new skin (extra field 'tradable' allowed)", "accept",
    () => db.storefront.insertOne(valid_skin)],
  ["ammo without a calibre", "reject",
    () => db.storefront.insertOne({ ...valid_skin, _id: "us-east:AMMO-TEST", sku: "AMMO-TEST", type: "AMMO",
      details: { damage: 30, penetration: 2 } })],
  ["skin with rarity 'SUPER'", "reject",
    () => db.storefront.insertOne({ ...valid_skin, _id: "us-east:SKIN-TEST-2", sku: "SKIN-TEST-2",
      details: { weapon: "Uzi", rarity: "SUPER" } })],
  ["negative price", "reject",
    () => db.storefront.insertOne({ ...valid_skin, _id: "us-east:SKIN-TEST-3", sku: "SKIN-TEST-3",
      price: { cents: -500, currency: "USD" } })],
  ["unknown region 'ng-lagos'", "reject",
    () => db.storefront.insertOne({ ...valid_skin, _id: "ng-lagos:SKIN-TEST-4", region: "ng-lagos" })],
  ["update: remove calibre from 7.62 rounds", "reject",
    () => db.storefront.updateOne({ region: "us-east", sku: "AMMO-762-90" }, { $unset: { "details.calibre": "" } })],
  ["update: turn 7.62 rounds into a SKIN", "reject",
    () => db.storefront.updateOne({ region: "us-east", sku: "AMMO-762-90" }, { $set: { type: "SKIN" } })],
  ["update: af-west 7.62 price to $1.29", "accept",
    () => db.storefront.updateOne({ region: "af-west", sku: "AMMO-762-90" }, { $set: { "price.cents": 129 } })],
];

print("\n## Rule tests");
const summary = [];
tests.forEach(([name, expect, op], i) => {
  let got, why = "";
  try { op(); got = "accept"; }
  catch (e) {
    if (e.code !== 121) throw e;                       // 121 = DocumentValidationFailure
    got = "reject";
    why = [...new Set(reasons(e.errInfo.details))].join("; ");
  }
  const ok = got === expect;
  summary.push(ok);
  print(`T${i + 1}  ${name}\n    expected ${expect}, got ${got}${why ? " (code 121: " + why + ")" : ""}  ${ok ? "OK" : "UNEXPECTED"}`);
});
print(`\n   ${summary.filter(Boolean).length} of ${summary.length} as expected`);

// undo the accepted changes: the test skin, and af-west's real price from CockroachDB
db.storefront.deleteOne({ region: "us-east", sku: "SKIN-TEST-UZI" });
db.storefront.updateOne({ region: "af-west", sku: "AMMO-762-90" }, { $set: { "price.cents": 149 } });

// ------------------------------------------------------------- aggregation
// "For each item, what does it cost in each region, and how much cheaper is
// af-west than us-east?"  (regional pricing, D3)
print("\n## Aggregation: price per region, af-west vs us-east");
db.storefront.aggregate([
  { $group: { _id: { type: "$type", sku: "$sku", name: "$name" },
              prices: { $push: { k: "$region", v: "$price.cents" } } } },
  { $project: { _id: 0, type: "$_id.type", name: "$_id.name", price: { $arrayToObject: "$prices" } } },
  { $project: { type: 1, name: 1,
                us_east: "$price.us-east", eu_west: "$price.eu-west", af_west: "$price.af-west",
                af_west_cheaper_pct: { $round: [{ $multiply: [
                  { $subtract: [1, { $divide: ["$price.af-west", "$price.us-east"] }] }, 100] }, 0] } } },
  { $sort: { type: 1, name: 1 } },
]).forEach(r => print(
  `   ${r.type.padEnd(8)} ${r.name.padEnd(30)} us-east ${(r.us_east / 100).toFixed(2).padStart(5)}` +
  `  eu-west ${(r.eu_west / 100).toFixed(2).padStart(5)}  af-west ${(r.af_west / 100).toFixed(2).padStart(5)}` +
  `  af-west ${r.af_west_cheaper_pct}% cheaper`));
