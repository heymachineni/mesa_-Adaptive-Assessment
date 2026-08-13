"""Simulate 5 student archetypes through the real adaptive engine.

Usage: python3 simulate.py
Runs entirely in-memory against questions.json + config.json (no server needed)
and prints each student's difficulty path, per spec §22.

Archetypes:
  A mostly correct (90%)      B mostly wrong (15% correct)   C random (50%)
  D scripted: easy ✓✓✓, medium ✓✓, then wrong on every hard  E strong (100%)
"""
import json
import os
import random

from adaptive_engine import AdaptiveEngine

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(BASE, "config.json")))
QUESTIONS = json.load(open(os.path.join(BASE, "questions.json")))
BANK = [{"id": q["id"], "difficulty": q["difficulty"], "topics": q["topics"]}
        for q in QUESTIONS]
# The real exam has no fixed length, so a simulation needs its own stopping
# point — long enough to show the ladder settle, short enough to read.
SIM_LENGTH = CONFIG["exam"].get("maxQuestions") or 30


def behavior(name, rng):
    if name == "A":
        return lambda q, i: rng.random() < 0.90
    if name == "B":
        return lambda q, i: rng.random() < 0.15
    if name == "C":
        return lambda q, i: rng.random() < 0.50
    if name == "D":
        return lambda q, i: q["difficulty"] != "hard"   # correct until hard, then wrong
    if name == "E":
        return lambda q, i: True
    raise ValueError(name)


def run(name, seed=7):
    eng = AdaptiveEngine(CONFIG)
    rng = random.Random(seed + ord(name))
    answer = behavior(name, rng)
    state = eng.initial_state()
    seen, served = set(), {}
    path = []
    while len(path) < SIM_LENGTH:
        q, debug = eng.select_next(state, seen, BANK, served, rng)
        if q is None:
            path.append(("<pool exhausted>", None, None))
            break
        assert q["id"] not in seen, "REPEAT DETECTED — non-repetition violated"
        seen.add(q["id"])
        for t in q["topics"]:
            served[t] = served.get(t, 0) + 1
        ok = answer(q, len(path))
        state, decision = eng.record_answer(state, ok)
        path.append((q["difficulty"], ok, decision))
    return path, served


def main():
    for name, label in [("A", "mostly correct"), ("B", "mostly wrong"),
                        ("C", "random"), ("D", "easy✓ medium✓ hard✗"),
                        ("E", "strong performance")]:
        path, served = run(name)
        print(f"\nStudent {name} — {label}")
        for i, (diff, ok, decision) in enumerate(path, 1):
            if ok is None:
                print(f"  {i:>2} {diff}")
                continue
            mark = "✓" if ok else "✗"
            note = "" if decision.startswith("stay") else f"   -> {decision}"
            print(f"  {i:>2} {diff.capitalize():<7}{mark}{note}")
        diffs = [p[0] for p in path if p[1] is not None]
        summary = {d: diffs.count(d) for d in ("easy", "medium", "hard")}
        print(f"  served: {summary} | topics: {dict(sorted(served.items()))}")


if __name__ == "__main__":
    main()
