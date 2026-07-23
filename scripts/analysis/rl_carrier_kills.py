#!/usr/bin/env python3
"""Timeline of kills of enemy RL carriers: join RL backpack drops (dropper =
victim who held RL) with the frag list (who killed them), from mvd-api."""
import json, urllib.request

DEMO = "sha:c928578ebf5f8b43eea96ef1cf8b03c0edd20b07df42ea6cf393e3b0438e77c9"
TEAM_018 = {"Xerial", "peppe", ". grisling", "Javve"}
TEAM_HX = {"squeeze", "realpit", "zamsha", "ok98"}
BASE = "http://127.0.0.1:8080/v1/demos/" + DEMO


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.load(r)


packs = get("/backpacks?weapon=rl")
packs = packs if isinstance(packs, list) else packs["backpacks"]
frags = get("/frags")
frags = frags if isinstance(frags, list) else frags.get("frags", [])
print("SAMPLE PACK:", json.dumps(packs[0]))

# join: backpack dropped by hx player == that player died holding RL;
# find the frag with victim == dropper closest in time
rows = []
for p in packs:
    if p["player"] not in TEAM_HX:
        continue
    pt = p["time"]
    best = None
    for f in frags:
        if f.get("victim") != p["player"]:
            continue
        dt = abs(f["time"] - pt)
        if best is None or dt < best[0]:
            best = (dt, f)
    if best is None:
        rows.append((pt, "???", p["player"], p.get("loc", "?"), "no frag match"))
        continue
    dt, f = best
    killer = f.get("killer")
    note = f.get("weapon", "")
    if dt > 1500:  # ms; sanity — drop and frag should coincide
        note += f" (dt={dt}ms)"
    rows.append((pt, killer, p["player"], p.get("loc", "?"), note))

rows.sort()
n018 = sum(1 for _, k, *_ in rows if k in TEAM_018)
print(f"\nRL-carrier deaths on [hx]: {len(rows)}; killed by 018: {n018}\n")
per, vic = {}, {}
for t, killer, victim, loc, note in rows:
    mark = "" if killer in TEAM_018 else "  [not 018: tk/suicide]"
    m, s = divmod(int(t / 1000), 60)
    print(f"{m:2d}:{s:02d}  {str(killer):12s} -> {victim:8s} ({note:8s}) @ {loc}{mark}")
    if killer in TEAM_018:
        per[killer] = per.get(killer, 0) + 1
        vic[victim] = vic.get(victim, 0) + 1
print("\nPER KILLER:", per)
print("PER VICTIM (RL lost):", vic)
