"""Squadfire data generator (D16): 30 days of store and match history.

Simulates the timeline in order, so every row is consistent with the rules:
inventory chains add up and never go negative, nobody spends over $50 a UTC
day, gifts go only to accepted friends, drops never oversell, refunds only
of SUCCEEDED purchases. The game servers' telemetry for the same matches is
written to out/telemetry.jsonl (replayed into Kafka in portfolio 5); its
rounds fired equal the USED rows in the inventory chains.

This is a historic backfill, so it writes rows directly with past timestamps
as the database owner. The live application path is the sign_up /
rename_player procedures and the squadfire_app role (D15).

    python data-gen/generate.py            # expects a freshly built database
"""
import argparse
import bisect
import heapq
import itertools
import json
import random
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

SEED = 8103
REGIONS = ["us-east", "eu-west", "af-west"]
REGION_WEIGHTS = [0.40, 0.35, 0.25]
PRICE_FACTOR = {"us-east": 1.00, "eu-west": 1.10, "af-west": 0.60}   # regional pricing (D3)
DAILY_LIMIT_CENTS = 5000

# sku, name, type, units per pack, base price (cents, us-east)
ITEMS = [
    ("AMMO-556-120", "5.56 mm Rounds (120)", "AMMO", 120, 199),
    ("AMMO-762-90", "7.62 mm Rounds (90)", "AMMO", 90, 249),
    ("AMMO-9MM-150", "9 mm Rounds (150)", "AMMO", 150, 149),
    ("AMMO-12G-40", "12 Gauge Shells (40)", "AMMO", 40, 179),
    ("AMMO-AP-60", "Armour-Piercing 5.56 mm (60)", "AMMO", 60, 399),
    ("GREN-FRAG-3", "Frag Grenades (3)", "GRENADE", 3, 299),
    ("GREN-SMOKE-3", "Smoke Grenades (3)", "GRENADE", 3, 199),
    ("MED-KIT-5", "Med Kits (5)", "MEDKIT", 5, 249),
    ("SKIN-TIGER-AK", "Tiger Stripe AK skin", "SKIN", 1, 499),
    ("SKIN-ARCTIC-M4", "Arctic M4 skin", "SKIN", 1, 699),
    ("SKIN-GOLD-DEAGLE", "Gold Desert Eagle skin", "SKIN", 1, 999),
]
ITEM = {i[0]: i for i in ITEMS}
AMMO_SKUS = [i[0] for i in ITEMS if i[2] == "AMMO"]
STORE_WEIGHTS = {"AMMO": 6, "GRENADE": 3, "MEDKIT": 3, "SKIN": 1}
DROP_SKU, DROP_PRICE, DROP_PACKS = "AMMO-AP-60", 299, 400
MODES = ["TEAM_DEATHMATCH", "SEARCH_AND_DESTROY", "DOMINATION"]
MAPS = ["Lagos Docks", "Harmattan Ridge", "Dublin Yards", "Boston Rail", "Kano Market", "Frost Relay"]
SQUADS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
ADJ = ["Iron", "Silent", "Rapid", "Ghost", "Crimson", "Steel", "Night", "Storm", "Dusty", "Lone", "Wild",
       "Frost", "Sandy", "Swift", "Rogue", "Bold", "Quiet", "Grim", "Lucky", "Sharp"]
NOUN = ["Viper", "Harmattan", "Falcon", "Jackal", "Raven", "Rhino", "Cobra", "Lion", "Wolf", "Mamba",
        "Hawk", "Bison", "Fox", "Badger", "Kestrel", "Panther", "Hyena", "Eagle", "Sniper", "Ranger"]


def uid(rng):
    return uuid.UUID(int=rng.getrandbits(128), version=4)


class World:
    def __init__(self, args):
        self.rng = random.Random(SEED)
        self.args = args
        self.end = datetime.now(timezone.utc).replace(microsecond=0)
        self.start = self.end - timedelta(days=args.days)
        self.rows = defaultdict(list)          # table -> rows to insert, in order
        self.telemetry = []
        self.players = []                      # dicts: id, region, created, whale
        self.by_region = defaultdict(list)
        self.friends = defaultdict(list)       # player id -> [(friend id, accepted_at)]
        self.inv = {}                          # (player, sku) -> (row id, units)
        self.spend = defaultdict(int)          # (payer, utc date) -> cents
        self.prices = {}                       # (region, sku) -> [(effective_from, cents)]
        self.drops = {}                        # region -> dict
        self.gateway_no = 0

    # ------------------------------------------------------------ reference data
    def reference(self):
        names = {"us-east": "US East (Virginia)", "eu-west": "EU West (Ireland)", "af-west": "Africa West (Lagos)"}
        self.rows["region"] = [(r, names[r]) for r in REGIONS]
        self.rows["item"] = [(s, n, t, u, t != "SKIN") for s, n, t, u, _ in ITEMS]
        first = self.start - timedelta(days=1)
        for r in REGIONS:
            for s, *_, base in ITEMS:
                cents = round(base * PRICE_FACTOR[r])
                self.prices[(r, s)] = [(first, cents)]
                self.rows["item_price"].append((r, s, first, cents))
        # mid-month price changes: a few items get cheaper or dearer in one region
        for _ in range(8):
            r, s = self.rng.choice(REGIONS), self.rng.choice(ITEMS)[0]
            at = self.start + timedelta(days=self.rng.randint(10, 20), hours=self.rng.randint(0, 23))
            if any(e == at for e, _ in self.prices[(r, s)]):
                continue
            cents = max(49, round(self.prices[(r, s)][-1][1] * self.rng.choice([0.8, 0.9, 1.1, 1.25])))
            self.prices[(r, s)].append((at, cents))
            self.prices[(r, s)].sort()
            self.rows["item_price"].append((r, s, at, cents))
        for r in REGIONS:   # one limited drop per region, days 24-26
            starts = self.start + timedelta(days=24, hours=REGIONS.index(r) * 6)
            self.drops[r] = {"id": uid(self.rng), "starts": starts, "ends": starts + timedelta(days=2),
                             "remaining": DROP_PACKS}

    def price(self, region, sku, at):
        current = None
        for eff, cents in self.prices[(region, sku)]:
            if eff <= at:
                current = cents
        return current

    # ------------------------------------------------------------ players and friends
    def people(self):
        used = set()
        for _ in range(self.args.players):
            while True:
                handle = self.rng.choice(ADJ) + self.rng.choice(NOUN) + (str(self.rng.randint(1, 999)) if self.rng.random() < 0.8 else "")
                if handle.lower() not in used:
                    used.add(handle.lower())
                    break
            region = self.rng.choices(REGIONS, REGION_WEIGHTS)[0]
            # most players exist before the month starts; the rest join during it
            created = self.start - timedelta(days=self.rng.randint(1, 90)) if self.rng.random() < 0.7 \
                else self.start + timedelta(seconds=self.rng.randint(0, self.args.days * 86400 - 3600))
            p = {"id": uid(self.rng), "handle": handle, "region": region, "created": created,
                 "whale": self.rng.random() < 0.02}
            self.players.append(p)
            self.by_region[region].append(p)
            self.rows["player_handle"].append((handle.lower(), p["id"], created))
            self.rows["player"].append((p["id"], p["id"], None, region, handle, "ACTIVE", "created", created))
        # a few renames and suspensions on the last day (new versions in the chain)
        for p in self.rng.sample(self.players, len(self.players) // 50):
            at = self.end - timedelta(minutes=self.rng.randint(5, 1400))
            if self.rng.random() < 0.6:
                new = p["handle"] + "X" if len(p["handle"]) < 20 else p["handle"][:-1] + "X"
                if new.lower() in used:
                    continue
                used.add(new.lower())
                self.rows["player_handle"].append((new.lower(), p["id"], at))
                self.rows["player"].append((uid(self.rng), p["id"], p["id"], p["region"], new, "ACTIVE", "renamed", at))
            else:
                self.rows["player"].append((uid(self.rng), p["id"], p["id"], p["region"], p["handle"], "SUSPENDED", "suspended", at))

        self.players.sort(key=lambda p: p["created"])
        self.joined = [p["created"] for p in self.players]
        self.cum_weight = list(itertools.accumulate(30 if p["whale"] else 1 for p in self.players))

        pairs = set()
        for p in self.players:
            for _ in range(self.rng.randint(1, 5)):
                pool = self.by_region[p["region"]] if self.rng.random() < 0.8 else self.players
                q = self.rng.choice(pool)
                key = tuple(sorted((p["id"], q["id"])))
                if q is p or key in pairs:
                    continue
                pairs.add(key)
                at = max(p["created"], q["created"]) + timedelta(hours=self.rng.randint(1, 240))
                if at > self.end:
                    continue
                root = uid(self.rng)
                low, high = key
                self.rows["friendship"].append((root, root, None, p["region"], low, high, p["id"], "REQUESTED", at))
                roll = self.rng.random()
                if roll < 0.10:
                    continue                                        # still pending
                answered = at + timedelta(minutes=self.rng.randint(5, 2880))
                if answered > self.end:
                    continue
                status = "ACCEPTED" if roll < 0.85 else "DECLINED"
                self.rows["friendship"].append((uid(self.rng), root, root, p["region"], low, high, p["id"], status, answered))
                if status == "ACCEPTED":
                    self.friends[p["id"]].append((q, answered))
                    self.friends[q["id"]].append((p, answered))

    # ------------------------------------------------------------ inventory chain
    def credit(self, player, sku, change, reason, at, purchase_id=None, match_id=None):
        prev_id, units = self.inv.get((player["id"], sku), (None, 0))
        row_id = uid(self.rng)
        units += change
        assert units >= 0
        self.rows["inventory"].append((row_id, prev_id, player["region"], player["id"], sku, change, units,
                                       reason, purchase_id, match_id, at))
        self.inv[(player["id"], sku)] = (row_id, units)

    # ------------------------------------------------------------ purchases
    def purchase(self, at, events):
        # weighted pick among players who exist at `at` (whales buy 30x as often)
        k = bisect.bisect_left(self.joined, at)
        payer = self.players[bisect.bisect_right(self.cum_weight, self.rng.random() * self.cum_weight[k - 1])]
        recipient = payer
        friends = [f for f, since in self.friends[payer["id"]] if since < at]
        if friends and self.rng.random() < 0.2:
            recipient = self.rng.choice(friends)
        region = payer["region"]                      # purchase lives in the payer's region (D7)

        lines, drop = [], self.drops[region]
        if drop["starts"] <= at < drop["ends"] and drop["remaining"] > 0 and self.rng.random() < 0.35:
            packs = min(self.rng.randint(1, 2), drop["remaining"])
            lines.append((DROP_SKU, packs, DROP_PRICE, drop["id"]))
        for _ in range(self.rng.choices([1, 2, 3], [6, 3, 1])[0]):
            sku = self.rng.choices(ITEMS, [STORE_WEIGHTS[i[2]] for i in ITEMS])[0][0]
            if any(l[0] == sku for l in lines):
                continue
            packs = 1 if ITEM[sku][2] == "SKIN" else self.rng.choices([1, 2, 3, 5, 10], [10, 6, 3, 2, 1])[0]
            lines.append((sku, packs, self.price(region, sku, at), None))
        total = sum(p * c for _, p, c, _ in lines)

        day = (payer["id"], at.date())
        if self.spend[day] + total > DAILY_LIMIT_CENTS:
            return                                    # rejected by the $50 rule: nothing is written
        self.gateway_no += 1
        pid = uid(self.rng)
        status = "FAILED" if self.rng.random() < 0.03 else "SUCCEEDED"
        method = self.rng.choices(["CARD", "WALLET"], [7, 3])[0]
        ref = f"GW-{self.gateway_no:07d}"
        self.rows["purchase"].append((pid, pid, None, region, payer["id"], recipient["id"], status, total,
                                      method, ref, "created", at))
        for sku, packs, cents, drop_id in lines:
            self.rows["purchase_line"].append((region, pid, sku, packs, cents, packs * cents, drop_id))
        self.store_events(payer, at, [l[0] for l in lines])
        if status == "FAILED":
            return
        self.spend[day] += total
        for sku, packs, _, drop_id in lines:
            if drop_id:
                drop["remaining"] -= packs
            self.credit(recipient, sku, packs * ITEM[sku][3], "PURCHASED" if recipient is payer else "GIFT_IN",
                        at, purchase_id=pid)
        if self.rng.random() < 0.02:                  # refund requested 1-48 h later
            later = at + timedelta(hours=self.rng.randint(1, 48))
            if later < self.end:
                heapq.heappush(events, (later, self.rng.random(), "refund",
                                        (pid, payer, recipient, total, method, ref, lines, day)))

    def refund(self, at, data):
        pid, payer, recipient, total, method, ref, lines, day = data
        units = [(sku, packs * ITEM[sku][3]) for sku, packs, _, _ in lines]
        if any(self.inv[(recipient["id"], sku)][1] < n for sku, n in units):
            return                                    # already used: not refundable
        self.rows["purchase"].append((uid(self.rng), pid, pid, payer["region"], payer["id"], recipient["id"],
                                      "REFUNDED", total, method, ref, "refunded", at))
        self.spend[day] -= total                      # refunds give the limit back (D3)
        for sku, n in units:
            self.credit(recipient, sku, -n, "REFUNDED", at, purchase_id=pid)

    def store_events(self, player, at, skus):
        for sku in skus:
            for kind, back in (("store_view", 90), ("store_click", 30)):
                self.event(kind, at - timedelta(seconds=back), player["region"], player=player["id"], sku=sku)
        if self.rng.random() < 0.5:                   # a look that did not end in a purchase
            self.event("store_view", at - timedelta(hours=2), player["region"], player=player["id"],
                       sku=self.rng.choice(ITEMS)[0])

    # ------------------------------------------------------------ matches
    def match(self, at):
        region = self.rng.choices(REGIONS, REGION_WEIGHTS)[0]
        mode = self.rng.choice(MODES)
        n_squads = 3 if mode == "DOMINATION" else 2
        per_squad = self.rng.choice([3, 4, 4])
        pool = [p for p in self.by_region[region] if p["created"] < at]   # play in home region only (D7)
        if len(pool) < n_squads * per_squad:
            return
        chosen = self.rng.sample(pool, n_squads * per_squad)
        squads = {p["id"]: SQUADS[i // per_squad] for i, p in enumerate(chosen)}
        mid = uid(self.rng)
        ended = at + timedelta(minutes=self.rng.randint(8, 20))
        if ended > self.end:
            return
        winner = None if self.rng.random() < 0.05 else self.rng.choice(SQUADS[:n_squads])
        stats = {p["id"]: [0, 0, 0] for p in chosen}

        self.event("match_started", at, region, match=mid, mode=mode)
        for p in chosen:
            self.event("session_start", at - timedelta(seconds=40), region, player=p["id"], match=mid)
        for _ in range(self.rng.randint(per_squad * 3, per_squad * 8)):          # kills
            killer = self.rng.choice(chosen)
            victim = self.rng.choice([q for q in chosen if squads[q["id"]] != squads[killer["id"]]])
            t = at + (ended - at) * self.rng.random()
            stats[killer["id"]][0] += 1
            stats[victim["id"]][1] += 1
            self.event("kill", t, region, match=mid, player=killer["id"], victim=victim["id"])
            mates = [q for q in chosen if squads[q["id"]] == squads[killer["id"]] and q is not killer]
            if mates and self.rng.random() < 0.4:
                helper = self.rng.choice(mates)
                stats[helper["id"]][2] += 1
                self.event("assist", t, region, match=mid, player=helper["id"], victim=victim["id"])

        for p in chosen:
            for sku in self.consumables(p):
                held = self.inv[(p["id"], sku)][1]
                kind = ITEM[sku][2]
                used = min(held, self.rng.randint(60, 420) if kind == "AMMO" else self.rng.randint(1, 3))
                self.usage_events(p, sku, kind, used, at, ended, mid, region, squads, chosen)
                self.credit(p, sku, -used, "USED", ended, match_id=mid)
            self.rows["match_player"].append((region, mid, p["id"], squads[p["id"]], *stats[p["id"]]))
            self.event("session_end", ended + timedelta(seconds=30), region, player=p["id"], match=mid)
        self.rows["match"].append((mid, region, mode, self.rng.choice(MAPS), at, ended, winner))
        self.event("match_ended", ended, region, match=mid, mode=mode, winner=winner)

    def consumables(self, p):
        held = [s for s in ITEM if ITEM[s][2] != "SKIN" and self.inv.get((p["id"], s), (None, 0))[1] > 0]
        ammo = [s for s in held if s in AMMO_SKUS]
        other = [s for s in held if s not in AMMO_SKUS]
        picked = self.rng.sample(ammo, min(len(ammo), self.rng.choice([1, 2])))
        return picked + [s for s in other if self.rng.random() < 0.5]

    def usage_events(self, p, sku, kind, used, at, ended, mid, region, squads, chosen):
        if kind == "MEDKIT":                          # one revive per med kit used
            mates = [q for q in chosen if squads[q["id"]] == squads[p["id"]] and q is not p] or [p]
            for _ in range(used):
                self.event("revive", at + (ended - at) * self.rng.random(), region, match=mid,
                           player=p["id"], target=self.rng.choice(mates)["id"], sku=sku)
            return
        left = used
        while left > 0:                               # bursts of fire / grenade throws
            n = min(left, self.rng.randint(20, 90) if kind == "AMMO" else 1)
            left -= n
            t = at + (ended - at) * self.rng.random()
            self.event("shot_fired", t, region, match=mid, player=p["id"], sku=sku, rounds=n)
            hits = sum(self.rng.random() < 0.28 for _ in range(n))
            if hits:
                self.event("hit", t, region, match=mid, player=p["id"], sku=sku, hits=hits)

    def event(self, kind, at, region, **fields):
        fields = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in fields.items() if v is not None}
        self.telemetry.append({"event": kind, "ts": at.isoformat(), "region": region, **fields})

    # ------------------------------------------------------------ the timeline
    def simulate(self):
        events, span = [], self.args.days * 86400
        for _ in range(self.args.purchases):
            heapq.heappush(events, (self.start + timedelta(seconds=self.rng.randint(0, span)), self.rng.random(), "purchase", None))
        for _ in range(self.args.matches):
            heapq.heappush(events, (self.start + timedelta(seconds=self.rng.randint(0, span)), self.rng.random(), "match", None))
        while events:
            at, _, kind, data = heapq.heappop(events)
            if kind == "purchase":
                self.purchase(at, events)
            elif kind == "match":
                self.match(at)
            else:
                self.refund(at, data)
        for r, d in self.drops.items():
            self.rows["limited_drop"].append((d["id"], r, DROP_SKU, DROP_PRICE, DROP_PACKS, d["remaining"], d["starts"], d["ends"]))


# parents before children; chains are already in time order
INSERTS = [
    ("region", "code, name"),
    ("item", "sku, name, item_type, units_per_pack, consumable"),
    ("item_price", "crdb_region, sku, effective_from, price_cents"),
    ("limited_drop", "id, crdb_region, sku, price_cents, total_packs, remaining, starts_at, ends_at"),
    ("player_handle", "handle_key, root_id, claimed_at"),
    ("player", "id, root_id, previous_id, crdb_region, handle, status, reason, created_at"),
    ("friendship", "id, root_id, previous_id, crdb_region, player_low, player_high, requested_by, status, created_at"),
    ("match", "id, crdb_region, mode, map, started_at, ended_at, winning_squad"),
    ("match_player", "crdb_region, match_id, player_id, squad, kills, deaths, assists"),
    ("purchase", "id, root_id, previous_id, crdb_region, payer_id, recipient_id, status, total_cents, payment_method, gateway_ref, reason, created_at"),
    ("purchase_line", "crdb_region, purchase_id, sku, packs, unit_price_cents, line_total_cents, drop_id"),
    ("inventory", "id, previous_id, crdb_region, player_id, sku, change, units_after, reason, purchase_id, match_id, created_at"),
]


PREVIOUS_COL = {"player": 2, "friendship": 2, "purchase": 2, "inventory": 1}   # position of previous_id


def generations(rows, prev_col):
    """Split chain rows into generations: 1st rows, 2nd rows, ... A row's
    previous row is always in an earlier generation, so it is already saved
    when the row arrives (the inventory trigger reads it)."""
    gen, groups = {}, defaultdict(list)
    for r in rows:                       # rows are in time order: previous comes first
        g = gen[r[prev_col]] + 1 if r[prev_col] else 0
        gen[r[0]] = g
        groups[g].append(r)
    return [groups[g] for g in sorted(groups)]


def load(world, dsn, batch):
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT count(*) FROM player").fetchone()[0]:
            raise SystemExit("squadfire already has players: rebuild it first (make p1-build)")
        for table, cols in INSERTS:
            rows = world.rows[table]
            groups = generations(rows, PREVIOUS_COL[table]) if table in PREVIOUS_COL else [rows]
            one = "(" + ", ".join(["%s"] * len(cols.split(","))) + ")"
            t0 = time.time()
            for group in groups:
                # one multi-row INSERT per batch: constraints are checked for the whole batch at once
                for i in range(0, len(group), batch):
                    chunk = group[i:i + batch]
                    sql = f"INSERT INTO {table} ({cols}) VALUES " + ", ".join([one] * len(chunk))
                    conn.execute(sql, [v for r in chunk for v in r])
            print(f"  {table:<14} {len(rows):>8,} rows  {time.time() - t0:6.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://root@localhost:26257/squadfire?sslmode=disable")
    ap.add_argument("--players", type=int, default=5000)
    ap.add_argument("--purchases", type=int, default=50000)
    ap.add_argument("--matches", type=int, default=2500)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()

    t0 = time.time()
    world = World(args)
    world.reference()
    world.people()
    world.simulate()
    print(f"simulated in {time.time() - t0:.1f}s; loading CockroachDB")
    load(world, args.dsn, args.batch)

    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    world.telemetry.sort(key=lambda e: e["ts"])
    with open(out / "telemetry.jsonl", "w") as f:
        for e in world.telemetry:
            f.write(json.dumps(e) + "\n")
    print(f"  telemetry      {len(world.telemetry):>8,} events -> {out / 'telemetry.jsonl'}")


if __name__ == "__main__":
    main()
