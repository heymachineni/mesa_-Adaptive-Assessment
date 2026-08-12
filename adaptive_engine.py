"""MESA Adaptive Engine — isolated, deterministic, rule-based.

No UI, no HTTP, no database imports. The server passes state in and gets
decisions out, so this module is independently unit-testable.

Difficulty state machine (thresholds come from config, never hardcoded):
    easy   --[consecutive_correct >= easy_to_medium]-->   medium
    medium --[consecutive_correct >= medium_to_hard]-->   hard
    medium --[consecutive_wrong  >= medium_to_easy]-->    easy
    hard   --[consecutive_wrong  >= hard_to_medium]-->    medium
Counters reset on every level change; a correct answer resets the wrong
streak and vice versa.

Selection fallback ladder (documented per spec; never repeats a question):
    1. unseen question at target difficulty, in a topic still under its
       blueprint quota (most-under-quota topic first)
    2. unseen question at target difficulty, any topic
    3. unseen question at an adjacent difficulty, under-quota topic first
       (adjacency: easy->[medium,hard], medium->[easy,hard], hard->[medium,easy])
    4. any unseen question
    5. nothing unseen -> None (attempt ends early; reason recorded)
"""

from __future__ import annotations
import random

DIFFICULTIES = ("easy", "medium", "hard")
ADJACENCY = {
    "easy": ("medium", "hard"),
    "medium": ("easy", "hard"),
    "hard": ("medium", "easy"),
}


class AdaptiveEngine:
    def __init__(self, config: dict):
        a = config["adaptive"]
        self.starting = a["startingDifficulty"]
        self.easy_to_medium = int(a["easyToMediumCorrectThreshold"])
        self.medium_to_hard = int(a["mediumToHardCorrectThreshold"])
        self.hard_to_medium = int(a["hardToMediumWrongThreshold"])
        self.medium_to_easy = int(a["mediumToEasyWrongThreshold"])
        self.max_questions = int(config["exam"]["maxQuestions"])
        self.blueprint = dict(config.get("blueprint", {}).get("topics", {}))
        if self.starting not in DIFFICULTIES:
            raise ValueError("startingDifficulty must be one of %s" % (DIFFICULTIES,))

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
        new_d, decision = d, "stay %s" % d
        if d == "easy" and s["consecutive_correct"] >= self.easy_to_medium:
            new_d, decision = "medium", "promote easy->medium"
        elif d == "medium" and s["consecutive_correct"] >= self.medium_to_hard:
            new_d, decision = "hard", "promote medium->hard"
        elif d == "medium" and s["consecutive_wrong"] >= self.medium_to_easy:
            new_d, decision = "easy", "demote medium->easy"
        elif d == "hard" and s["consecutive_wrong"] >= self.hard_to_medium:
            new_d, decision = "medium", "demote hard->medium"

        if new_d != d:
            s = {"difficulty": new_d, "consecutive_correct": 0, "consecutive_wrong": 0}
        return s, decision

    # ---- blueprint ----
    def topic_targets(self) -> dict:
        """Convert blueprint weights into target question counts for one exam."""
        total = sum(self.blueprint.values()) or 1
        return {t: round(w / total * self.max_questions)
                for t, w in self.blueprint.items()}

    def _under_quota_topics(self, topic_served: dict) -> list:
        targets = self.topic_targets()
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
        under = self._under_quota_topics(topic_served)

        def pick(cands, step):
            debug["ladder_step"] = step
            debug["candidate_pool"] = len(cands)
            return rng.choice(cands), debug

        # step 1: target difficulty + most under-quota topic
        at_target = [q for q in unseen if q["difficulty"] == target]
        for topic in under:
            cands = [q for q in at_target if topic in q["topics"]]
            if cands:
                return pick(cands, "1:target_diff+under_quota_topic(%s)" % topic)
        # step 2: target difficulty, any topic
        if at_target:
            return pick(at_target, "2:target_diff_any_topic")
        # step 3: adjacent difficulty, under-quota topic first
        for adj in ADJACENCY[target]:
            at_adj = [q for q in unseen if q["difficulty"] == adj]
            for topic in under:
                cands = [q for q in at_adj if topic in q["topics"]]
                if cands:
                    return pick(cands, "3:adjacent(%s)+under_quota_topic(%s)" % (adj, topic))
            if at_adj:
                return pick(at_adj, "3:adjacent(%s)_any_topic" % adj)
        # step 4: anything unseen
        return pick(unseen, "4:any_unseen")

    def is_exam_complete(self, answered_count: int) -> bool:
        return answered_count >= self.max_questions
