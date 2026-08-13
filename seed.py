"""Create the MESA POC database from scratch and seed it.

Usage:  python3 seed.py [--fresh]
  --fresh deletes any existing mesa.db first.

Seeds: 90 questions from questions.json, 5 demo students (fake credentials).
Non-repetition is enforced BY THE DATABASE: UNIQUE(attempt_id, question_id).
"""
import hashlib
import json
import os
import secrets
import sqlite3
import sys

DB_PATH = os.path.join(os.environ.get("DB_DIR", os.path.dirname(__file__)),
                       "mesa.db")


def level_names():
    """The difficulty ladder, straight from config.json.

    The questions table constrains `difficulty` to these values, so the
    constraint has to be built from the same source the engine reads. Adding
    a level to config and re-seeding is all it takes; a stale database keeps
    its old constraint until you re-run with --fresh.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config.json")
    with open(path) as f:
        adaptive = json.load(f)["adaptive"]
    levels = adaptive.get("levels")
    if not levels:
        return ["easy", "medium", "hard"]            # legacy config
    return [(lv if isinstance(lv, str) else lv["name"]) for lv in levels]


def _difficulty_check():
    values = ",".join("'%s'" % n.replace("'", "''") for n in level_names())
    return "CHECK(difficulty IN (%s))" % values


_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS questions(
  id TEXT PRIMARY KEY,
  difficulty TEXT NOT NULL {difficulty_check},
  qtype TEXT NOT NULL,
  prompt TEXT NOT NULL,
  options_json TEXT NOT NULL,
  answer_index INTEGER NOT NULL,
  explanation TEXT NOT NULL,
  topics_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  asset TEXT,
  dataset_json TEXT,
  active INTEGER NOT NULL DEFAULT 1        -- retired questions stay for history
);
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  started_at REAL NOT NULL,
  deadline REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','completed','expired','exhausted')),
  adaptive_state_json TEXT NOT NULL,
  current_question_id TEXT,
  answered_count INTEGER NOT NULL DEFAULT 0,
  score INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempt_questions(
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  question_id TEXT NOT NULL REFERENCES questions(id),
  seq INTEGER NOT NULL,
  shown_at REAL NOT NULL,
  answered_at REAL,
  answer_index INTEGER,
  is_correct INTEGER,
  time_taken REAL,
  difficulty TEXT NOT NULL,
  state_before_json TEXT NOT NULL,
  decision TEXT,
  next_difficulty TEXT,
  selection_debug_json TEXT,
  PRIMARY KEY(attempt_id, seq),
  UNIQUE(attempt_id, question_id)          -- HARD non-repetition guarantee
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                      -- blur | return | fullscreen_exit | resume
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON events(attempt_id);
"""

# Resolved once at import so `seed.SCHEMA` stays a ready-to-run script.
SCHEMA = _SCHEMA_TEMPLATE.format(difficulty_check=_difficulty_check())

DEMO_STUDENTS = [
    ("chandu", "Chandu Demo"),
    ("student001", "Student 001"),
    ("student002", "Student 002"),
    ("student003", "Student 003"),
    ("student004", "Student 004"),
]


def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def random_password(length=12):
    """Generate a readable random password: alternating consonants and vowels."""
    consonants = "bcdfghjkmnpqrstvwxyz"
    vowels = "aeiou"
    pw = ""
    for i in range(length):
        if i % 2 == 0:
            pw += secrets.choice(consonants)
        else:
            pw += secrets.choice(vowels)
    return pw


def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")      # concurrent readers + one writer
    con.execute("PRAGMA busy_timeout=10000")    # wait instead of failing under load
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def main():
    if "--fresh" in sys.argv and os.path.exists(DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        print("removed existing mesa.db")
    con = connect()
    con.executescript(SCHEMA)

    default_pw = os.environ.get("DEFAULT_STUDENT_PASSWORD", "mesa-demo-2026")
    roster = list(DEMO_STUDENTS)
    # --students N  adds N extra accounts (student005...) for a real cohort
    if "--students" in sys.argv:
        n = int(sys.argv[sys.argv.index("--students") + 1])
        for i in range(5, 5 + n):
            roster.append((f"student{i:03d}", f"Student {i:03d}"))
    for username, name in roster:
        salt = secrets.token_hex(8)
        con.execute(
            "INSERT OR IGNORE INTO students(username,name,salt,pw_hash) VALUES(?,?,?,?)",
            (username, name, salt, hash_pw(default_pw, salt)),
        )

    with open(os.path.join(os.path.dirname(__file__), "questions.json")) as f:
        questions = json.load(f)
    for q in questions:
        con.execute(
            """INSERT OR REPLACE INTO questions
               (id,difficulty,qtype,prompt,options_json,answer_index,explanation,
                topics_json,tags_json,asset,dataset_json,active)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (q["id"], q["difficulty"], q["type"], q["prompt"],
             json.dumps(q["options"]), q["answer_index"], q["explanation"],
             json.dumps(q["topics"]), json.dumps(q.get("tags", [])),
             q.get("asset"), json.dumps(q.get("dataset")) if q.get("dataset") else None),
        )
    con.commit()

    n_q = con.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    n_s = con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
    print(f"seeded: {n_q} questions, {n_s} students")
    print(f"demo logins: {', '.join(u for u, _ in DEMO_STUDENTS)}")
    print(f"demo password: {default_pw}  (override with DEFAULT_STUDENT_PASSWORD env var)")
    con.close()


if __name__ == "__main__":
    main()
