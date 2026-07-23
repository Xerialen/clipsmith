#!/usr/bin/env python3
"""clipsmith Turso selection layer (POC).

Queries Xerial's private QuakeWorld stats database over Turso/libSQL's HTTP
/v2/pipeline API. This is the current discovery adapter, not a database bundled
with clipsmith; another source can replace the watcher query boundary.
Reads TURSO_DB_URL + TURSO_AUTH_TOKEN_QWSTATS straight from
~/.config/api-keys.env so the token never lives in a shell variable.

Usage:
    turso_query.py "<SQL>"            # run arbitrary read SQL, print JSON rows
    turso_query.py --schema <table>   # print CREATE TABLE for a table
    turso_query.py --tables           # list tables
"""
import json
import os
import sys
import urllib.request

ENV_PATH = os.path.expanduser("~/.config/api-keys.env")


def load_creds():
    url = token = None
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "TURSO_DB_URL":
                url = v
            elif k == "TURSO_AUTH_TOKEN_QWSTATS":
                token = v
    if not url or not token:
        sys.exit("missing TURSO_DB_URL or TURSO_AUTH_TOKEN_QWSTATS in env")
    # libsql:// -> https:// for the HTTP API
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/"), token


def run_sql(sql):
    url, token = load_creds()
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": sql}},
            {"type": "close"},
        ]
    }).encode()
    req = urllib.request.Request(
        url + "/v2/pipeline",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    # unwrap the libSQL pipeline result into list-of-dict rows
    res = data["results"][0]
    if res.get("type") != "ok":
        sys.exit("query error: " + json.dumps(data)[:500])
    r = res["response"]["result"]
    cols = [c["name"] for c in r["cols"]]
    rows = []
    for row in r["rows"]:
        rows.append({cols[i]: cell.get("value") for i, cell in enumerate(row)})
    return rows


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--tables":
        sql = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    elif args[0] == "--schema":
        sql = f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{args[1]}'"
    else:
        sql = args[0]
    rows = run_sql(sql)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
