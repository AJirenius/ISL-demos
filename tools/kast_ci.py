#!/usr/bin/env python3
"""Delegating shim — the real work moved to stats_ci.py (CS2League issue #48).

The compute-kast workflow calls this file by name; since #48 the demo pass
computes KAST plus the whole stat family MatchZy never tracks (trades, bomb
work, entry duels, knife/AWP/pistol kills, flash assists, friendly flashes,
team kills, suicides, nades thrown) and submits through the submit_demo_stats
RPC. This shim exists because updating .github/workflows needs a token scope
the league's tooling doesn't hold — same args, same env, same behaviour.

KAST-only rollback: `git show <pre-#48>:tools/kast_ci.py`.
"""
import os, runpy, sys

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_ci.py"),
               run_name="__main__")
