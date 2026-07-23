#!/usr/bin/env python3
"""Blow-by-blow for a time window: frags, powerup pickups, RL-pack drops, chat."""
import json, sys, urllib.request

DEMO = "sha:c928578ebf5f8b43eea96ef1cf8b03c0edd20b07df42ea6cf393e3b0438e77c9"
BASE = "http://127.0.0.1:8080/v1/demos/" + DEMO
T0, T1 = 295_000, 345_000  # ms window (5:02 push +- margin)
HX = {"squeeze", "realpit", "zamsha", "ok98"}


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.load(r)


def mmss(ms):
    m, s = divmod(int(ms / 1000), 60)
    return f"{m}:{s:02d}"


rows = []

frags = get("/frags")
frags = frags if isinstance(frags, list) else frags.get("frags", [])
for f in frags:
    if T0 <= f["time"] <= T1:
        side = "HX " if f.get("killer") in HX else "018"
        rows.append((f["time"], f"FRAG  {side} {f['killer']} -> {f['victim']} ({f['weapon']})"))

ev = get("/events?types=powerup")
for e in ev.get("events", []):
    t = e["t"] * 1000
    if T0 - 10000 <= t <= T1:
        d = e["detail"]
        rows.append((t, f"PWUP  {e['player']} takes {d['powerup']} ({d.get('frags')} frags, ends {mmss(d['endTime']*1000)})"))

packs = get("/backpacks?weapon=rl")
packs = packs if isinstance(packs, list) else packs["backpacks"]
for p in packs:
    if T0 <= p["time"] <= T1:
        rows.append((p["time"], f"PACK  {p['player']} drops RL @ {p.get('loc')}"))

try:
    chat = get("/chat")
    msgs = chat if isinstance(chat, list) else chat.get("messages") or chat.get("chat") or []
    for c in msgs:
        t = c.get("time", c.get("t", 0))
        if isinstance(t, (int, float)) and t < 10000:  # maybe seconds
            t = t * 1000 if t < 2000 else t
        if T0 <= t <= T1:
            rows.append((t, f"CHAT  {c.get('player', c.get('name'))}: {c.get('text', c.get('message'))}"))
except Exception as e:
    print("chat skipped:", e, file=sys.stderr)

rows.sort()
for t, line in rows:
    print(f"{mmss(t)}  {line}")
