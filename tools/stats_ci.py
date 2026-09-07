#!/usr/bin/env python3
"""Compute the MatchZy-dead stat family from a GOTV demo and submit it to the
ISL database (CS2League issue #48). Supersedes kast_ci.py as the CI writer —
KAST is computed in the same pass, same HLTV semantics as before.

WHY. MatchZy never populates trade_kills, bomb_plants, bomb_defuses,
knife_kills, first_kills/deaths_ct/t, flash_assists, friendlies_flashed,
team_kills or suicides (same documented gap as KAST), and never sends
utility_thrown / kills_with_sniper / kills_with_pistol at all. On top of the
dead family this pass mines the demo for flavour stats no server plugin has:
team_damage, last_alive (rounds as the team's last one standing), blind /
smoke / wallbang / noscope / airborne kills, longest_kill_m (max kill
distance, metres), cash_spent (total_cash_spent prop at the last death). The demo has
all of it. Keys MatchZy DOES track live (kills, deaths, damage,
utility_damage, enemies_flashed, headshot_kills, mvp, multikills, 1vX) are
deliberately not computed here — the demo pass must never fight the live
writer, and the submit_demo_stats RPC whitelist enforces that server-side.

Round boundaries are tick-based from round_start events (trusting
total_rounds_played bleeds warmup kills into round 1 — learned on real data).
Self-validates kill/death counts against the DB (tolerance ±1 — MatchZy
snapshots stats at the round_end trigger, so post-round frags differ) and
refuses to write anything on a bigger mismatch. Sides (CT/T) are read
per-event from the demo, so halftime and OT swaps are always right.

Auth: the same dedicated narrow secret as KAST (KAST_SECRET), accepted only
by the get_kast_inputs / submit_demo_stats RPCs — deliberately NOT a
database key.

Usage: stats_ci.py <match_id> <map_num> <demo_path> [--dry-run]
Env:   SUPABASE_URL, SUPABASE_ANON_KEY, KAST_SECRET
"""
import json, os, sys, urllib.request

TRADE_TICKS = 5 * 64          # 5 s at 64 tick — HLTV trade window
BLIND_MIN   = 1.0             # seconds; a shorter blind isn't "flashed" (calibrated
                              # against MatchZy's live enemies_flashed on real maps)

SNIPERS  = {"awp", "ssg08", "scar20", "g3sg1"}
PISTOLS  = {"glock", "usp_silencer", "hkp2000", "p250", "fiveseven",
            "tec9", "cz75a", "deagle", "elite", "revolver"}
GRENADES = {"hegrenade", "flashbang", "smokegrenade", "molotov", "incgrenade", "decoy"}

def rpc(name, body):
    req = urllib.request.Request(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/rpc/{name}",
        data=json.dumps(body).encode(),
        headers={"apikey": os.environ["SUPABASE_ANON_KEY"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def wname(w):  # "weapon_awp" and "awp" both appear in the wild — normalize
    w = str(w or "").lower()
    return w[7:] if w.startswith("weapon_") else w

def main():
    match_id, map_num, demo_path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    dry = "--dry-run" in sys.argv[4:]
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

    def col(row, name):
        v = row.get(name)
        return "" if v is None or (isinstance(v, float) and v != v) else str(v)

    def evt(name, **kw):
        # demoparser2 returns a DataFrame normally but a bare [] when the event
        # never fired in (or isn't known to) this demo — normalize to row dicts
        try: df = p.parse_event(name, **kw)
        except Exception: return []
        if df is None or isinstance(df, list): return df or []
        return [r for _, r in df.iterrows()]

    # ── deaths: the one event most stats hang off ─────────────────────────────
    deaths = []
    for d in evt("player_death", player=["team_name"]):
        rnd = round_of(int(d["tick"]))
        if not (1 <= rnd <= n): continue
        deaths.append({
            "round": rnd, "tick": int(d["tick"]),
            "victim":   col(d, "user_steamid"),
            "attacker": col(d, "attacker_steamid"),
            "assister": col(d, "assister_steamid"),
            "flash":    bool(d.get("assistedflash")),
            "weapon":   wname(d.get("weapon")),
            "blind":    bool(d.get("attackerblind")),
            "smoke":    bool(d.get("thrusmoke")),
            "wall":     int(d.get("penetrated") or 0) > 0,
            "noscope":  bool(d.get("noscope")),
            "air":      bool(d.get("attackerinair")),
            "dist":     float(d.get("distance") or 0),
            "att_side": col(d, "attacker_team_name"),   # "CT" / "TERRORIST" at that moment
            "vic_side": col(d, "user_team_name"),
        })

    # self-validate before writing anything
    for sid, pl in by_steam.items():
        dk = sum(1 for r in deaths if r["attacker"] == sid and team_of.get(r["victim"]) != pl["team_id"])
        dv = sum(1 for r in deaths if r["victim"] == sid)
        if abs(dk - pl["kills"]) > 1 or abs(dv - pl["deaths"]) > 1:
            sys.exit(f"VALIDATION FAILED for {pl['alias']}: demo K/D {dk}/{dv} vs DB {pl['kills']}/{pl['deaths']} — refusing to write")

    Z = lambda: {s: 0 for s in by_steam}
    kast = Z(); trades = Z(); knife = Z(); sniper = Z(); pistol = Z()
    fk_ct = Z(); fk_t = Z(); fd_ct = Z(); fd_t = Z()
    fassist = Z(); tk = Z(); suicides = Z()
    plants = Z(); defuses = Z(); util = Z(); ff_flash = Z(); ef_flash = Z()
    blindk = Z(); smokek = Z(); wallk = Z(); nsk = Z(); airk = Z()
    tdmg = Z(); last_alive = Z(); longest = Z(); cash = Z()

    def enemy_kill(r):
        a, v = r["attacker"], r["victim"]
        return a and v and a != v and a in team_of and v in team_of and team_of[a] != team_of[v]

    for r in deaths:
        a, v = r["attacker"], r["victim"]
        if enemy_kill(r):
            if r["weapon"].startswith("knife") or r["weapon"] == "bayonet": knife[a] += 1
            if r["weapon"] in SNIPERS: sniper[a] += 1
            if r["weapon"] in PISTOLS: pistol[a] += 1
            if r["flash"] and r["assister"] in team_of and team_of[r["assister"]] != team_of[v]:
                fassist[r["assister"]] += 1
            if r["blind"]: blindk[a] += 1
            if r["smoke"]: smokek[a] += 1
            if r["wall"]:  wallk[a] += 1
            if r["noscope"] and r["weapon"] in SNIPERS: nsk[a] += 1
            if r["air"]:   airk[a] += 1
            if r["dist"] > longest[a]: longest[a] = r["dist"]
            # trade: v had killed one of a's teammates within the window, same round
            if any(r2["round"] == r["round"] and r2["attacker"] == v
                   and r2["victim"] in team_of and team_of[r2["victim"]] == team_of[a]
                   and r2["victim"] != v
                   and r2["tick"] < r["tick"] <= r2["tick"] + TRADE_TICKS for r2 in deaths):
                trades[a] += 1
        elif v in team_of:
            if a == v or a not in team_of and not a:
                suicides[v] += 1                       # own hand or the world
            elif a in team_of and team_of[a] == team_of[v]:
                tk[a] += 1

    # opening duel of each round: the first ENEMY kill decides both sides' entry
    for rnd in range(1, n + 1):
        opener = next((r for r in sorted(deaths, key=lambda x: x["tick"])
                       if r["round"] == rnd and enemy_kill(r)), None)
        if not opener: continue
        (fk_ct if opener["att_side"] == "CT" else fk_t)[opener["attacker"]] += 1
        (fd_ct if opener["vic_side"] == "CT" else fd_t)[opener["victim"]] += 1

    # last one standing: rounds where all four teammates died first (whether
    # or not the player then survived the round)
    for rnd in range(1, n + 1):
        rr = [r for r in deaths if r["round"] == rnd and r["victim"] in team_of]
        for sid, pl in by_steam.items():
            mates_down = sum(1 for r in rr if r["victim"] != sid and team_of[r["victim"]] == pl["team_id"])
            if mates_down < 4: continue
            mine = next((r["tick"] for r in rr if r["victim"] == sid), None)
            mate_ticks = [r["tick"] for r in rr if r["victim"] != sid and team_of[r["victim"]] == pl["team_id"]]
            if mine is None or mine > max(mate_ticks):
                last_alive[sid] += 1

    # team damage (player_hurt: teammate on teammate, self excluded)
    for d in evt("player_hurt"):
        if not (1 <= round_of(int(d["tick"])) <= n): continue
        a, v = col(d, "attacker_steamid"), col(d, "user_steamid")
        if a in team_of and v in team_of and a != v and team_of[a] == team_of[v]:
            tdmg[a] += int(d.get("dmg_health") or 0)

    # money spent — the total_cash_spent player prop sampled at the last death
    if deaths:
        try:
            for _, row in p.parse_ticks(["total_cash_spent"], ticks=[max(r["tick"] for r in deaths)]).iterrows():
                sid = str(row.get("steamid") or "")
                if sid in cash: cash[sid] = int(row.get("total_cash_spent") or 0)
        except Exception as ex:
            print(f"  (cash_spent unavailable: {ex})")

    # KAST — unchanged HLTV semantics
    for rnd in range(1, n + 1):
        rr = [r for r in deaths if r["round"] == rnd]
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

    # bomb work
    for ev, acc in (("bomb_planted", plants), ("bomb_defused", defuses)):
        for d in evt(ev):
            if 1 <= round_of(int(d["tick"])) <= n:
                sid = col(d, "user_steamid")
                if sid in acc: acc[sid] += 1

    # grenades thrown
    for d in evt("weapon_fire"):
        if wname(d.get("weapon")) in GRENADES and 1 <= round_of(int(d["tick"])) <= n:
            sid = col(d, "user_steamid")
            if sid in util: util[sid] += 1

    # blinds — friendlies_flashed is submitted; enemies_flashed printed only
    # (MatchZy tracks it live; the print is a standing calibration check).
    # CS2 GOTV demos don't network player_blind, so blind attribution is
    # reconstructed: sample every player's flash_duration prop just before and
    # after each flashbang_detonate — whoever's duration JUMPED was blinded by
    # that grenade's thrower.
    dets = [(int(d["tick"]), col(d, "user_steamid")) for d in evt("flashbang_detonate")
            if 1 <= round_of(int(d["tick"])) <= n and col(d, "user_steamid") in team_of]
    if dets:
        PAD = 4
        wanted = sorted({t + s for t, _ in dets for s in (-PAD, PAD)})
        try:
            fd = p.parse_ticks(["flash_duration"], ticks=wanted)
            dur_at = {}
            for _, row in fd.iterrows():
                dur_at[(int(row["tick"]), str(row["steamid"]))] = float(row.get("flash_duration") or 0)
            for t, thrower in dets:
                for sid in team_of:
                    before = dur_at.get((t - PAD, sid), 0.0)
                    after  = dur_at.get((t + PAD, sid), 0.0)
                    if after - before >= BLIND_MIN and sid != thrower:
                        (ff_flash if team_of[thrower] == team_of[sid] else ef_flash)[thrower] += 1
        except Exception as ex:
            print(f"  (blind reconstruction unavailable: {ex})")

    print(f"match {match_id} map {map_num} — {n} rounds")
    print(f"  {'player':<16}{'kast':>5}{'tr':>4}{'fk':>4}{'fd':>4}{'pl':>4}{'df':>4}"
          f"{'kn':>4}{'awp':>4}{'pst':>4}{'fa':>4}{'ffl':>4}{'efl*':>5}{'tk':>4}{'sui':>4}{'utl':>5}"
          f"{'bld':>4}{'smk':>4}{'wb':>4}{'ns':>4}{'air':>4}{'la':>4}{'tdm':>5}{'lng':>5}{'cash':>7}")
    for sid, pl in sorted(by_steam.items(), key=lambda x: -kast[x[0]]):
        print(f"  {pl['alias']:<16}{kast[sid]:>5}{trades[sid]:>4}"
              f"{fk_ct[sid]+fk_t[sid]:>4}{fd_ct[sid]+fd_t[sid]:>4}"
              f"{plants[sid]:>4}{defuses[sid]:>4}{knife[sid]:>4}{sniper[sid]:>4}{pistol[sid]:>4}"
              f"{fassist[sid]:>4}{ff_flash[sid]:>4}{ef_flash[sid]:>5}{tk[sid]:>4}{suicides[sid]:>4}{util[sid]:>5}"
              f"{blindk[sid]:>4}{smokek[sid]:>4}{wallk[sid]:>4}{nsk[sid]:>4}{airk[sid]:>4}"
              f"{last_alive[sid]:>4}{tdmg[sid]:>5}{round(longest[sid]):>5}{cash[sid]:>7}")
    print("  (* efl = demo-computed enemies_flashed, print-only — MatchZy owns that key)")

    payload = [{
        "stat_id": by_steam[s]["stat_id"],
        "kast": kast[s],
        "trade_kills": trades[s],
        "bomb_plants": plants[s],
        "bomb_defuses": defuses[s],
        "knife_kills": knife[s],
        "first_kills_ct": fk_ct[s], "first_kills_t": fk_t[s],
        "first_deaths_ct": fd_ct[s], "first_deaths_t": fd_t[s],
        "flash_assists": fassist[s],
        "friendlies_flashed": ff_flash[s],
        "team_kills": tk[s],
        "suicides": suicides[s],
        "utility_thrown": util[s],
        "kills_with_sniper": sniper[s],
        "kills_with_pistol": pistol[s],
        "team_damage": tdmg[s],
        "last_alive": last_alive[s],
        "blind_kills": blindk[s],
        "smoke_kills": smokek[s],
        "wallbang_kills": wallk[s],
        "noscope_kills": nsk[s],
        "airborne_kills": airk[s],
        "longest_kill_m": round(longest[s]),
        "cash_spent": cash[s],
    } for s in by_steam]

    if dry:
        print("--dry-run: nothing written"); return
    out = rpc("submit_demo_stats", {"p_secret": secret, "p_match_id": match_id, "p_map_num": map_num,
                                    "p_stats": payload})
    print("submitted:", out)
    if out.get("written") != len(by_steam):
        sys.exit(f"partial write: {out.get('written')}/{len(by_steam)}")

if __name__ == "__main__":
    main()
