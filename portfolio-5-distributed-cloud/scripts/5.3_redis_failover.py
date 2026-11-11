"""Experiment 5.3: Redis Sentinel failover, and the writes asynchronous replication loses.

Redis replicates asynchronously: the master says OK before any replica has
the write. Three sentinels watch the master; when 2 of them (the quorum)
cannot reach it for 5 s, they vote and promote a replica.

  1 before     1 master + 2 replicas, 3 sentinels; WAIT shows a write reaching the replicas
  2 failover   the master is cut off from the network (a partition). A game
               server stuck on its side keeps writing and gets OK; the sentinels
               promote a replica and the main client follows them to it
  3 heal       the old master returns as a replica of the new one, copies it,
               and throws away every write it had said OK to
  4 the fix    min-replicas-to-write 1: a master that cannot see a replica
               stops accepting writes, so the loss window is bounded
  5 restore    redis-master is made master again (P4 4.2 expects it)

The partition is `docker network disconnect`. Clients run inside containers:
the main client in sentinel-1 (on the network, like an app server), the
stuck game server inside the cut-off master's own container.

    make p5 EXP=5.3_redis_failover
"""
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NETWORK = "squadfire"
MASTER_NAME = "squadfire-master"
NODES = ("redis-master", "redis-replica-1", "redis-replica-2")
SENTINELS = ("sentinel-1", "sentinel-2", "sentinel-3")


def container(service):
    return f"advanced-database-portfolio-{service}-1"


def sh(*args):
    return subprocess.run(args, check=True, cwd=ROOT, capture_output=True, text=True).stdout.strip()


def on(service, *args, host="localhost", port=6379):
    """redis-cli run inside `service`'s container."""
    return subprocess.run(["docker", "exec", container(service), "redis-cli", "-h", host, "-p", str(port), *args],
                          cwd=ROOT, capture_output=True, text=True).stdout.strip()


def app(host, *args):
    """The main client: on the network, inside sentinel-1."""
    return on("sentinel-1", *args, host=host)


def sentinel(*args, service="sentinel-1"):
    return on(service, "SENTINEL", *args, port=26379)


def name(addr):
    """A sentinel may report a node by IP; show (and use) its service name."""
    ips = {sh("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
              container(n)): n for n in NODES}
    return ips.get(addr, addr)


def master(service="sentinel-1"):
    return name(sentinel("get-master-addr-by-name", MASTER_NAME, service=service).splitlines()[0])


def sentinel_replicas(service):
    out = sentinel("replicas", MASTER_NAME, service=service).splitlines()
    return sorted(out[i + 1] for i, k in enumerate(out) if k == "ip")


def role(service):
    return on(service, "ROLE").splitlines()[0]


def nkeys(service, pattern):
    return int(on(service, "EVAL", "return #redis.call('KEYS', ARGV[1])", "0", pattern) or 0)


def replicas(service):
    info = dict(l.split(":", 1) for l in on(service, "INFO", "replication").splitlines() if ":" in l)
    return [dict(kv.split("=") for kv in info[f"slave{i}"].split(","))["ip"] for i in range(int(info["connected_slaves"]))]


def wait(cond, what, timeout=60):
    t = time.monotonic()
    while not cond():
        if time.monotonic() - t > timeout:
            raise SystemExit(f"timed out waiting for {what}")
        time.sleep(0.3)
    return time.monotonic() - t


def synced_replica(service, of):
    info = on(service, "INFO", "replication")
    return role(service) == "slave" and f"master_host:{of}" in info and "master_link_status:up" in info


def partition(service):
    sh("docker", "network", "disconnect", NETWORK, container(service))


def heal(service):
    sh("docker", "network", "connect", "--alias", service, NETWORK, container(service))


def stuck_writes(service, prefix, n, gap=0.1):
    """The game server on the master's side of the split: n writes, one every `gap` s."""
    ok, refused, first, t = 0, 0, None, time.monotonic()
    for i in range(n):
        out = on(service, "SET", f"{prefix}:{i}", "kills=1")
        if out == "OK":
            ok += 1
        else:
            refused += 1
            first = first or (time.monotonic() - t, out)
        time.sleep(gap)
    return ok, refused, first


def split_and_write(service, prefix, n=100):
    """Cut `service` off, keep the stuck game server writing, and time the sentinels' promotion meanwhile."""
    partition(service)
    t0 = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        writes = pool.submit(stuck_writes, service, prefix, n)
        wait(lambda: master() != service, "a new master")
        promoted = time.monotonic() - t0
        return (*writes.result(), master(), promoted)


def announce(service):
    # By hostname, not IP: a node reconnected after a split gets a new IP, and sentinels that only
    # knew the old IP never find it again (what happened on this script's first run)
    on(service, "CONFIG", "SET", "replica-announce-ip", service)


def main():
    for n in NODES:
        announce(n)
    m = master()
    if m != "redis-master":
        raise SystemExit(f"expected redis-master as master, sentinels say {m}")
    # forget stale entries (a node once known by an IP it no longer has) and rediscover by hostname
    for s in SENTINELS:
        sentinel("reset", MASTER_NAME, service=s)
    want = sorted(n for n in NODES if n != m)
    wait(lambda: all(sentinel_replicas(s) == want for s in SENTINELS), "sentinels to rediscover the replicas")
    on(m, "EVAL", "for _, k in ipairs(redis.call('KEYS', ARGV[1])) do redis.call('DEL', k) end", "0", "lab:*")

    print("== 1. Before")
    print(f"   master {m}, replicas {', '.join(replicas(m))}")
    for s in SENTINELS:
        info = dict(zip(*[iter(sentinel("master", MASTER_NAME, service=s).splitlines())] * 2))
        print(f"   {s}: {MASTER_NAME} -> {info['ip']}, quorum {info['quorum']}, "
              f"down-after {int(info['down-after-milliseconds']) // 1000} s, {info['num-other-sentinels']} other sentinels")
    print(f"   app: SET lab:before -> {app(m, 'SET', 'lab:before', '1')} (the master answers before the replicas"
          " have it: asynchronous)")
    print(f"   WAIT 2 1000 -> {on(m, 'WAIT', '2', '1000')} replicas confirm they have everything written so far")

    # ------------------------------------------------------------------ 2
    print("\n== 2. Partition: redis-master is cut off from the replicas, the sentinels and the app")
    old = m
    ok, refused, _, new, promoted = split_and_write(old, "lab:split")
    print(f"   sentinels promoted {new} {promoted:.1f} s after the split (5 s down-after, then vote and promotion)")
    print(f"   meanwhile the game server on the old master's side wrote for ~20 s:"
          f" {ok} of 100 writes answered OK, {refused} refused")
    print(f"   (redis-master cannot tell it was cut off; it still says role: {role(old)})")
    for s in SENTINELS:
        print(f"     {s} says master is {master(s)}")
    main_ok = sum(app(master(), "SET", f"lab:main:{i}", "kills=1") == "OK" for i in range(20))
    print(f"   the app asks a sentinel for the master and writes there: {main_ok} of 20 OK on {master()}")

    # ------------------------------------------------------------------ 3
    print("\n== 3. Heal the partition")
    heal(old)
    took = wait(lambda: synced_replica(old, new), f"{old} to rejoin as a replica")
    print(f"   sentinels turned {old} into a replica of {new} after {took:.1f} s; it resynced from {new}")
    kept = nkeys(new, "lab:split:*")
    print(f"   of the {ok} writes the old master said OK to: {kept} on {new}, {nkeys(old, 'lab:split:*')} on {old}")
    print(f"   -> {ok - kept} acknowledged writes LOST (the old master threw its copy away to match {new})")
    print(f"   the app's 20 writes after the failover: {nkeys(new, 'lab:main:*')} kept")

    # ------------------------------------------------------------------ 4
    print("\n== 4. The fix: min-replicas-to-write 1, min-replicas-max-lag 3 (on every node), split again")
    wait(lambda: len(replicas(new)) == 2, f"{new} to have 2 replicas")
    for n in NODES:
        on(n, "CONFIG", "SET", "min-replicas-to-write", "1")
        on(n, "CONFIG", "SET", "min-replicas-max-lag", "3")
    ok2, refused2, first, third, promoted = split_and_write(new, "lab:fixed")
    print(f"   sentinels promoted {third} {promoted:.1f} s after the split")
    print(f"   game server on {new}'s side: {ok2} of 100 writes OK, {refused2} refused")
    if first:
        print(f"   first refusal {first[0]:.1f} s after the split: {first[1]}")
    heal(new)
    wait(lambda: synced_replica(new, third), f"{new} to rejoin as a replica")
    kept2 = nkeys(third, "lab:fixed:*")
    print(f"   after healing: {ok2 - kept2} acknowledged writes lost (before the fix: {ok - kept},"
          " and it would have kept growing with a longer split)")
    print("   the loss is now bounded by the max lag (~3 s of writes), not by how long the split lasts;")
    print("   it cannot be zero: Redis has no majority commit, so money stays in CockroachDB")

    # ------------------------------------------------------------------ 5
    print("\n== 5. Restore redis-master as master")
    for n in NODES:
        on(n, "CONFIG", "SET", "min-replicas-to-write", "0")
    # a reconnected container's host port forwarding is stale; a restart (as a replica) renews it
    for n in {old, new} - {third}:
        sh("docker", "restart", container(n))
        wait(lambda: on(n, "PING") == "PONG", f"{n} to start")
        announce(n)
        on(n, "REPLICAOF", third, "6379")
        wait(lambda: synced_replica(n, third), f"{n} to resync after restart")
    if third != "redis-master":
        others = [n for n in NODES if n not in (third, "redis-master")]
        for n in others:
            on(n, "CONFIG", "SET", "replica-priority", "0")       # so the failover can only pick redis-master
        try:
            sentinel("failover", MASTER_NAME)
            wait(lambda: master() == "redis-master", "the failover back")
            wait(lambda: len(replicas("redis-master")) == 2, "redis-master to have 2 replicas")
        finally:
            for n in others:
                on(n, "CONFIG", "SET", "replica-priority", "100")
    print(f"   master {master()}, replicas {', '.join(replicas('redis-master'))}")
    on("redis-master", "EVAL", "for _, k in ipairs(redis.call('KEYS', ARGV[1])) do redis.call('DEL', k) end",
       "0", "lab:*")
    print("   lab keys removed")


if __name__ == "__main__":
    main()
