"""MESA Adaptive Engine — isolated, deterministic, rule-based.

No UI, no HTTP, no database imports. The server passes state in and gets
decisions out, so this module is independently unit-testable.

LEVELS ARE DATA, NOT CODE. `config.json` holds an ordered ladder, easiest
first, and everything else is derived from it — the state machine, the
selection fallback, the database constraint, the admin dropdowns. Adding a
fourth level is a config edit:

    "adaptive": {
      "startingLevel": "easy",
      "levels": [
        {"name": "easy",   "promoteAfterCorrect": 3},
        {"name": "medium", "promoteAfterCorrect": 2, "demoteAfterWrong": 2},
        {"name": "hard",   "promoteAfterCorrect": 2, "demoteAfterWrong": 1},
        {"name": "expert",                           "demoteAfterWrong": 1}
      ]
    }

`promoteAfterCorrect` on the top level and `demoteAfterWrong` on the bottom
are ignored — there is nowhere to go. Omit either key (or set it to 0/null)
to pin a level: a level with no `promoteAfterCorrect` is never promoted out
of upward, however long the correct streak runs.

State machine, for every adjacent pair in the ladder:
    L --[consecutive_correct >= promoteAfterCorrect(L)]--> next level up
    L --[consecutive_wrong   >= demoteAfterWrong(L)]-->    next level down
Counters reset on every level change; a correct answer resets the wrong
streak and vice versa. Promotion is checked before demotion, but the two
cannot both be armed at once because one counter is always zero.

Selection fallback ladder (never repeats a question):
    1. unseen question at the target level, in a topic still under its
       blueprint quota (most-under-quota topic first)
    2. unseen question at the target level, any topic
    3. unseen question at the nearest other level, under-quota topic first,
       walking outwards through the ladder by distance
    4. any unseen question
    5. nothing unseen -> None (attempt ends early; reason recorded)

The old three-level config — `startingDifficulty` plus the four
`easyToMediumCorrectThreshold`-style keys — is still read, so existing
config files keep working unchanged.
"""

from __future__ import annotations
import random

# Only used to read a legacy config; the ladder itself comes from config.
LEGACY_LEVELS = ("easy", "medium", "hard")
LEGACY_KEYS = {
    ("easy", "up"): "easyToMediumCorrectThreshold",
    ("medium", "up"): "mediumToHardCorrectThreshold",
    ("medium", "down"): "mediumToEasyWrongThreshold",
    ("hard", "down"): "hardToMediumWrongThreshold",
}


def _levels_from_config(a: dict) -> list:
    """Normalise either config shape into [{name, up, down}, ...], easiest first."""
    if a.get("levels"):
        out = []
        for spec in a["levels"]:
            if isinstance(spec, str):           # bare name: never moves on its own
                spec = {"name": spec}
            name = str(spec["name"]).strip()
            if not name:
                raise ValueError("every level needs a non-empty name")
            out.append({
                "name": name,
                "up": int(spec.get("promoteAfterCorrect") or 0),
                "down": int(spec.get("demoteAfterWrong") or 0),
            })
    else:                                        # legacy three-level config
        out = [{"name": n,
                "up": int(a.get(LEGACY_KEYS.get((n, "up"), ""), 0) or 0),
                "down": int(a.get(LEGACY_KEYS.get((n, "down"), ""), 0) or 0)}
               for n in LEGACY_LEVELS]
    if not out:
        raise ValueError("config.adaptive.levels must list at least one level")
    names = [lv["name"] for lv in out]
    if len(set(names)) != len(names):
        raise ValueError("level names must be unique: %s" % (names,))
    return out


class AdaptiveEngine:
    def __init__(self, config: dict):
        a = config["adaptive"]
        self.levels = _levels_from_config(a)
        self.names = [lv["name"] for lv in self.levels]
        self._index = {n: i for i, n in enumerate(self.names)}
        self.starting = (a.get("startingLevel")
                         or a.get("startingDifficulty")
                         or self.names[0])
        if self.starting not in self._index:
            raise ValueError("startingLevel %r is not one of %s"
                             % (self.starting, self.names))
        # None / 0 / absent  ->  no cap: the exam runs until time or bank runs out
        self.max_questions = int(config["exam"].get("maxQuestions") or 0) or None
        self.blueprint = dict(config.get("blueprint", {}).get("topics", {}))

    # ---- ladder helpers ----
    def promote_threshold(self, name):
        """Correct answers needed to move up, or None if it can't move up."""
        i = self._index[name]
        if i >= len(self.levels) - 1:
            return None                      # already at the top
        return self.levels[i]["up"] or None

    def demote_threshold(self, name):
        """Wrong answers needed to move down, or None if it can't move down."""
        i = self._index[name]
        if i == 0:
            return None                      # already at the bottom
        return self.levels[i]["down"] or None

    def neighbours(self, name):
        """Other levels, nearest first; ties break towards the easier one."""
        i = self._index[name]
        others = [n for n in self.names if n != name]
        return sorted(others, key=lambda n: (abs(self._index[n] - i),
                                             self._index[n]))

    # ---- state ----
    def initial_state(self) -> dict:
        return {"difficulty": self.starting,
                "consecutive_correct": 0,
                "consecutive_wrong": 0}

    def record_answer(self, state: dict, is_correct: bool):
        """Return (new_state, decision_string). Pure function of inputs."""
        s = dict(state)
        if is_correct:
            s["consecutive_correct"] += 1
            s["consecutive_wrong"] = 0
        else:
            s["consecutive_wrong"] += 1
            s["consecutive_correct"] = 0

        d = s["difficulty"]
        if d not in self._index:                 # config changed mid-attempt
            d = self.starting
            s["difficulty"] = d
        i = self._index[d]
        new_d, decision = d, "stay %s" % d

        up = self.promote_threshold(d)
        down = self.demote_threshold(d)
        if up and s["consecutive_correct"] >= up:
            new_d = self.names[i + 1]
            decision = "promote %s->%s" % (d, new_d)
        elif down and s["consecutive_wrong"] >= down:
            new_d = self.names[i - 1]
            decision = "demote %s->%s" % (d, new_d)

        if new_d != d:
            s = {"difficulty": new_d, "consecutive_correct": 0,
                 "consecutive_wrong": 0}
        return s, decision

    # ---- blueprint ----
    def topic_targets(self, total: int | None = None) -> dict:
        """Blueprint weights as target counts for an exam of `total` questions.

        With no fixed length the caller passes the size of the pool, so the
        quotas stay proportional however long a student keeps going.
        """
        total = total or self.max_questions or 0
        weight_sum = sum(self.blueprint.values()) or 1
        return {t: round(w / weight_sum * total)
                for t, w in self.blueprint.items()}

    def _under_quota_topics(self, topic_served: dict, total=None) -> list:
        targets = self.topic_targets(total)
        under = [(targets[t] - topic_served.get(t, 0), t)
                 for t in targets if topic_served.get(t, 0) < targets[t]]
        under.sort(key=lambda x: (-x[0], x[1]))  # most under-served first, then name
        return [t for _, t in under]

    # ---- selection ----
    def select_next(self, state: dict, seen_ids: set, questions: list,
                    topic_served: dict, rng: random.Random | None = None):
        """Return (question_or_None, debug_dict). Never returns a seen question."""
        rng = rng or random.Random()
        unseen = [q for q in questions if q["id"] not in seen_ids]
        debug = {
            "target_difficulty": state["difficulty"],
            "excluded_seen": len(questions) - len(unseen),
            "unseen_total": len(unseen),
            "ladder_step": None,
            "candidate_pool": 0,
        }
        if not unseen:
            debug["ladder_step"] = "5:pool_exhausted"
            return None, debug

        target = state["difficulty"]
        if target not in self._index:
            target = self.starting
        # Without a fixed length, balance topics against the whole pool.
        under = self._under_quota_topics(topic_served,
                                         self.max_questions or len(questions))

        def pick(cands, step):
            debug["ladder_step"] = step
            debug["candidate_pool"] = len(cands)
            return rng.choice(cands), debug

        # step 1: target level + most under-quota topic
        at_target = [q for q in unseen if q["difficulty"] == target]
        for topic in under:
            cands = [q for q in at_target if topic in q["topics"]]
            if cands:
                return pick(cands, "1:target_diff+under_quota_topic(%s)" % topic)
        # step 2: target level, any topic
        if at_target:
            return pick(at_target, "2:target_diff_any_topic")
        # step 3: nearest other levels, under-quota topic first
        for adj in self.neighbours(target):
            at_adj = [q for q in unseen if q["difficulty"] == adj]
            for topic in under:
                cands = [q for q in at_adj if topic in q["topics"]]
                if cands:
                    return pick(cands, "3:adjacent(%s)+under_quota_topic(%s)"
                                % (adj, topic))
            if at_adj:
                return pick(at_adj, "3:adjacent(%s)_any_topic" % adj)
        # step 4: anything unseen
        return pick(unseen, "4:any_unseen")

    def is_exam_complete(self, answered_count: int) -> bool:
        """True only when a cap is configured and has been reached."""
        if not self.max_questions:
            return False           # unlimited: time or an empty bank ends it
        return answered_count >= self.max_questions
