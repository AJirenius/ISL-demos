#!/usr/bin/env python3
"""Compute real KAST from a GOTV demo and submit it to the ISL database.

Runs on this repo's Actions runners (workflow: compute-kast.yml), triggered by
the league's dathost-webhook the moment a demo is secured on a release.
HLTV semantics per round: Kill, enemy Assist, Survival, or death Traded within
5 s. Round boundaries are tick-based from round_start events (trusting
total_rounds_played bleeds warmup kills into round 1 — learned on real data).
Self-validates its kill/death counts against the DB (tolerance ±1 — MatchZy
snapshots stats at the round_end trigger, so post-round frags differ) and
refuses to write anything on a bigger mismatch.

Auth: a dedicated narrow secret (KAST_SECRET) accepted only by the
get_kast_inputs / submit_kast RPCs — deliberately NOT a database key.

Usage: kast_ci.py <match_id> <map_num> <demo_path>
Env:   SUPABASE_URL, SUPABASE_ANON_KEY, KAST_SECRET
"""
import json, os, sys, urllib.request

TRADE_TICKS = 5 * 64

def rpc(name, body):
    req = urllib.request.Request(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/rpc/{name}",
        data=json.dumps(body).encode(),
        headers={"apikey": os.environ["SUPABASE_ANON_KEY"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def main():
    match_id, map_num, demo_path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    secret = os.environ["KAST_SECRET"]
    inp = rpc("get_kast_inputs", {"p_secret": secret, "p_match_id": match_id, "p_map_num": map_num})
    n_rounds, players = inp["rounds"], inp["players"]
    if not players or not n_rounds:
        sys.exit(f"nothing to do: rounds={n_rounds} players={len(players)}")
    by_steam = {p["steamid"]: p for p in players}
    team_of = {p["steamid"]: p["team_id"] for p in players}

    from demoparser2 import DemoParser
    p = DemoParser(demo_path)
    starts = p.parse_event("round_start", other=["total_rounds_played"])
    start_tick = {}
    for _, r in starts.iterrows():
        start_tick[int(r["total_rounds_played"]) + 1] = int(r["tick"])  # last start per round wins (warmup restarts)
    demo_rounds = max(start_tick)
    if abs(demo_rounds - n_rounds) > 1:
        sys.exit(f"round-count mismatch: demo ~{demo_rounds}, DB {n_rounds} — wrong demo? refusing")
    n = min(demo_rounds, n_rounds)
    bounds = sorted((t, rnd) for rnd, t in start_tick.items() if rnd <= n)

    def round_of(tick):
        rnd = 0
        for t, r in bounds:
            if tick >= t: rnd = r
            else: break
        return rnd

    rows = []
    for _, d in p.parse_event("player_death").iterrows():
        rnd = round_of(int(d["tick"]))
        if 1 <= rnd <= n:
            rows.append({"round": rnd, "tick": int(d["tick"]),
                         "victim": str(d.get("user_steamid") or ""),
                         "attacker": str(d.get("attacker_steamid") or ""),
                         "assister": str(d.get("assister_steamid") or "")})

    for sid, pl in by_steam.items():
        dk = sum(1 for r in rows if r["attacker"] == sid and team_of.get(r["victim"]) != pl["team_id"])
        dd = sum(1 for r in rows if r["victim"] == sid)
        if abs(dk - pl["kills"]) > 1 or abs(dd - pl["deaths"]) > 1:
            sys.exit(f"VALIDATION FAILED for {pl['alias']}: demo K/D {dk}/{dd} vs DB {pl['kills']}/{pl['deaths']} — refusing to write")

    kast = {sid: 0 for sid in by_steam}
    for rnd in range(1, n + 1):
        rr = [r for r in rows if r["round"] == rnd]
        died = {r["victim"] for r in rr}
        for sid, pl in by_steam.items():
            k = any(r["attacker"] == sid and team_of.get(r["victim"]) != pl["team_id"] for r in rr)
            a = any(r["assister"] == sid and team_of.get(r["victim"]) != pl["team_id"] for r in rr)
            s = sid not in died
            t = False
            if not s:
                my = next(r for r in rr if r["victim"] == sid)
                t = any(r2["victim"] == my["attacker"] and team_of.get(r2["attacker"]) == pl["team_id"]
                        and my["tick"] < r2["tick"] <= my["tick"] + TRADE_TICKS for r2 in rr)
            if k or a or s or t: kast[sid] += 1

    print(f"match {match_id} map {map_num} — {n} rounds")
    for sid, pl in sorted(by_steam.items(), key=lambda x: -kast[x[0]]):
        print(f"  {pl['alias']:<16} kast {kast[sid]:>2} ({kast[sid]/n*100:.0f}%)")
    out = rpc("submit_kast", {"p_secret": secret, "p_match_id": match_id, "p_map_num": map_num,
                              "p_kast": [{"stat_id": by_steam[s]["stat_id"], "kast": kast[s]} for s in by_steam]})
    print("submitted:", out)
    if out.get("written") != len(by_steam):
        sys.exit(f"partial write: {out.get('written')}/{len(by_steam)}")

if __name__ == "__main__":
    main()
