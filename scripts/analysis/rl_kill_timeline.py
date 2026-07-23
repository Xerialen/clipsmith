#!/usr/bin/env python3
"""Timeline of RL kills by one team on the other, from an mvd-api frags stream."""
import json, sys, urllib.request

DEMO = "sha:c928578ebf5f8b43eea96ef1cf8b03c0edd20b07df42ea6cf393e3b0438e77c9"
TEAM = "018"

url = f"http://127.0.0.1:8080/v1/demos/{DEMO}/frags"
with urllib.request.urlopen(url) as r:
    data = json.load(r)

frags = data if isinstance(data, list) else data.get("frags") or data.get("events") or []
if not frags:
    print("UNEXPECTED SHAPE:", json.dumps(data)[:800])
    sys.exit(1)

TEAM_018 = {"Xerial", "peppe", ". grisling", "Javve"}
TEAM_HX = {"squeeze", "realpit", "zamsha", "ok98"}

weapons = {}
for f in frags:
    weapons[f.get("weapon")] = weapons.get(f.get("weapon"), 0) + 1
print("WEAPONS SEEN:", weapons)

rl = [f for f in frags
      if f.get("killer") in TEAM_018 and f.get("victim") in TEAM_HX
      and f.get("weapon") == "rl"]

print(f"RL kills by {TEAM} on [hx]: {len(rl)}")
per = {}
for f in rl:
    per[f["killer"]] = per.get(f["killer"], 0) + 1
    m, s = divmod(int(f["time"] / 1000), 60)  # time field is in ms
    print(f"{m:2d}:{s:02d}  {f['killer']} -> {f['victim']}")
print("PER KILLER:", per)
vic = {}
for f in rl:
    vic[f["victim"]] = vic.get(f["victim"], 0) + 1
print("PER VICTIM:", vic)
