"""
Tests for recommend().  Run it from the project folder:   python test_recommend.py

It works on a TEMPORARY COPY of database.sqlite, so your real data is not changed.
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
for name in ("database.sqlite", "censorship.dat"):
    shutil.copy(os.path.join(PROJECT, name), TMP)
os.chdir(TMP)
sys.path.insert(0, PROJECT)

import app  # noqa: E402  (imported after the chdir so it uses the temporary database)

DIY_POSTS = [1810, 1875, 1880, 2113]
DIY_USERS = ["starboy99", "DancingDolphin", "blogger_bob"]
HIKING_POSTS = [1778, 1826, 1857]

passed, failed = 0, 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def db():
    conn = sqlite3.connect("database.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def new_user(name):
    conn = db()
    uid = conn.execute("INSERT INTO users (username, password) VALUES (?, 'x')", (name,)).lastrowid
    conn.commit()
    conn.close()
    return uid


def user_id(name):
    conn = db()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()["id"]
    conn.close()
    return uid


def react(uid, post_ids):
    conn = db()
    for pid in post_ids:
        conn.execute("INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, 'like')", (pid, uid))
    conn.commit()
    conn.close()


def comment(uid, post_ids):
    conn = db()
    for pid in post_ids:
        conn.execute("INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, 'nice one')", (pid, uid))
    conn.commit()
    conn.close()


def follow(uid, usernames):
    conn = db()
    for n in usernames:
        conn.execute("INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)", (uid, user_id(n)))
    conn.commit()
    conn.close()


def newest_five():
    conn = db()
    ids = [r["id"] for r in conn.execute("SELECT id FROM posts ORDER BY created_at DESC LIMIT 5")]
    conn.close()
    return ids


def recommend(uid, following=False):
    with app.app.app_context():
        return [dict(r) for r in app.recommend(uid, following)]


def show(title, posts):
    print(f"\n  {title}")
    for p in posts:
        print(f"    #{p['id']:<5} @{p['username']:<16} {p['content'][:70]}")
    print()


def count_matching(posts, pattern):
    return sum(1 for p in posts if re.search(pattern, p["content"], re.I))


print("\n1) Cold start: a user who did nothing yet")
uid = new_user("t_cold")
recs = recommend(uid)
check("returns the 5 newest posts", [p["id"] for p in recs] == newest_five())
check("logged-out visitor (user_id None) also gets the 5 newest posts",
      [p["id"] for p in recommend(None)] == newest_five())

print("\n2) DIY interest (reactions on posts 1810, 1875, 1880, 2113)")
uid = new_user("t_diy")
conn = db()  # give the user an own DIY post: it must never be recommended back
own_id = conn.execute("INSERT INTO posts (user_id, content) VALUES (?, 'my own DIY shelf project')", (uid,)).lastrowid
conn.commit()
conn.close()
react(uid, DIY_POSTS)
recs = recommend(uid)
show("recommended:", recs)
ids = [p["id"] for p in recs]
dates = [p["created_at"] for p in recs]
check("returns exactly 5 posts", len(recs) == 5)
check("posts are in reverse-chronological order", dates == sorted(dates, reverse=True))
check("no post the user already reacted to", not set(ids) & set(DIY_POSTS))
check("no post written by the user", own_id not in ids and all(p["user_id"] != uid for p in recs))
check("no duplicates", len(set(ids)) == 5)
check("at least 4 of 5 are about DIY", count_matching(recs, r"\bdiy") >= 4)

print("\n3) DIY interest + 'Following' filter (follows the 3 DIY users)")
follow(uid, DIY_USERS)
recs = recommend(uid, following=True)
show("recommended (following only):", recs)
check("returns 5 posts", len(recs) == 5)
check("every post is from a followed user", all(p["username"] in DIY_USERS for p in recs))
check("at least 4 of 5 are about DIY", count_matching(recs, r"\bdiy") >= 4)

print("\n4) Comments count as interest too (comments instead of reactions)")
uid = new_user("t_diy_comments")
comment(uid, DIY_POSTS)
recs = recommend(uid)
check("at least 4 of 5 are about DIY", count_matching(recs, r"\bdiy") >= 4)
check("none of the commented posts come back", not {p["id"] for p in recs} & set(DIY_POSTS))

print("\n5) Control: a different interest must give different results (hiking)")
uid = new_user("t_hiking")
react(uid, HIKING_POSTS)
recs = recommend(uid)
show("recommended:", recs)
check("at least 3 of 5 are about hiking", count_matching(recs, r"\bhik") >= 3)
check("hardly any DIY posts (max 1)", count_matching(recs, r"\bdiy") <= 1)

print("\n6) 'Following' filter for a user without any engagement")
uid = new_user("t_cold_following")
follow(uid, ["starboy99"])
recs = recommend(uid, following=True)
check("only posts of followed users are shown", len(recs) > 0 and all(p["username"] == "starboy99" for p in recs))

print("\n7) Posts returned have everything the feed template needs")
recs = recommend(user_id("t_diy"))
needed = {"id", "content", "created_at", "username", "user_id"}
check("keys id, content, created_at, username, user_id exist", all(needed <= set(p) for p in recs))

print("\n8) The real feed page")
client = app.app.test_client()
with client.session_transaction() as s:
    s["user_id"], s["username"] = user_id("t_diy"), "t_diy"
check("/?sort=recommended renders (200)", client.get("/?sort=recommended").status_code == 200)
check("/?sort=recommended&show=following renders (200)",
      client.get("/?sort=recommended&show=following").status_code == 200)

print(f"\n{passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
