"""SafePlate: food delivery site that shows verified kitchen safety standards."""
import sqlite3, os
from datetime import date, timedelta
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
DB = os.path.join(os.path.dirname(__file__), "safeplate.db")
VALID_DAYS = 180  # an inspection older than this no longer counts as "verified"

CHECKS = {  # key: (label, weight)
    "temp": ("Food stored at safe temperatures", 25),
    "allergen": ("Allergen separation and labelling", 20),
    "hygiene": ("Staff hygiene and handwashing", 20),
    "pest": ("Pest control in place", 15),
    "license": ("Valid food licence on display", 20),
}

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def init_db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS restaurant(id INTEGER PRIMARY KEY, name TEXT, cuisine TEXT, area TEXT);
    CREATE TABLE IF NOT EXISTS dish(id INTEGER PRIMARY KEY, restaurant_id INT, name TEXT, price INT);
    CREATE TABLE IF NOT EXISTS inspection(id INTEGER PRIMARY KEY, restaurant_id INT, inspector TEXT,
        inspected_on TEXT, score INT, passed TEXT, notes TEXT);
    CREATE TABLE IF NOT EXISTS report(id INTEGER PRIMARY KEY, restaurant_id INT, message TEXT,
        created_on TEXT, status TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, restaurant_id INT, total INT,
        safety_snapshot TEXT, created_on TEXT);
    """)
    if not c.execute("SELECT 1 FROM restaurant").fetchone():
        rs = [("Spice Route", "North Indian", "Napier Town"), ("Green Bowl", "Healthy", "Wright Town"),
              ("Tandoor Nights", "Mughlai", "Civil Lines")]
        for r in rs: c.execute("INSERT INTO restaurant(name,cuisine,area) VALUES(?,?,?)", r)
        for rid, dishes in {1: [("Paneer Butter Masala", 240), ("Garlic Naan", 50)],
                            2: [("Quinoa Salad", 220), ("Fruit Bowl", 150)],
                            3: [("Chicken Biryani", 280), ("Seekh Kebab", 260)]}.items():
            for d in dishes: c.execute("INSERT INTO dish(restaurant_id,name,price) VALUES(?,?,?)", (rid, *d))
        today = date.today()
        seed = [(1, list(CHECKS), 20), (2, ["temp", "allergen", "hygiene", "license"], 400)]
        for rid, passed, ago in seed:
            c.execute("INSERT INTO inspection(restaurant_id,inspector,inspected_on,score,passed,notes) VALUES(?,?,?,?,?,?)",
                      (rid, "Dr. Rao", (today - timedelta(days=ago)).isoformat(), score_for(passed), ",".join(passed), "Seed data"))
    c.commit(); c.close()

def score_for(passed):
    return sum(w for k, (_, w) in CHECKS.items() if k in passed)

def grade(score):
    return "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "F"

def safety(rid):
    """Latest inspection plus whether it is still valid."""
    row = db().execute("SELECT * FROM inspection WHERE restaurant_id=? ORDER BY inspected_on DESC LIMIT 1", (rid,)).fetchone()
    if not row: return {"state": "none"}
    age = (date.today() - date.fromisoformat(row["inspected_on"])).days
    state = "verified" if age <= VALID_DAYS else "expired"
    return {"state": state, "score": row["score"], "grade": grade(row["score"]), "on": row["inspected_on"],
            "expires": (date.fromisoformat(row["inspected_on"]) + timedelta(days=VALID_DAYS)).isoformat(),
            "inspector": row["inspector"], "passed": row["passed"].split(","), "notes": row["notes"]}

def admin_only(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

@app.route("/")
def index():
    verified_only = request.args.get("verified") == "1"
    min_grade = request.args.get("grade", "")
    out = []
    for r in db().execute("SELECT * FROM restaurant").fetchall():
        s = safety(r["id"])
        if verified_only and s["state"] != "verified": continue
        if min_grade and (s["state"] != "verified" or s["grade"] > min_grade): continue
        out.append((r, s))
    out.sort(key=lambda x: -(x[1].get("score", -1) if x[1]["state"] == "verified" else -1))
    return render_template("index.html", rows=out, verified_only=verified_only, min_grade=min_grade)

@app.route("/restaurant/<int:rid>")
def restaurant(rid):
    r = db().execute("SELECT * FROM restaurant WHERE id=?", (rid,)).fetchone()
    if not r: return "Not found", 404
    dishes = db().execute("SELECT * FROM dish WHERE restaurant_id=?", (rid,)).fetchall()
    history = db().execute("SELECT * FROM inspection WHERE restaurant_id=? ORDER BY inspected_on DESC", (rid,)).fetchall()
    return render_template("restaurant.html", r=r, dishes=dishes, s=safety(rid), checks=CHECKS,
                           history=history, grade=grade)

@app.post("/restaurant/<int:rid>/report")
def report(rid):
    msg = request.form.get("message", "").strip()
    if len(msg) < 10:
        flash("Please describe the issue in at least 10 characters.")
    else:
        db().execute("INSERT INTO report(restaurant_id,message,created_on) VALUES(?,?,?)", (rid, msg, date.today().isoformat()))
        db().commit(); flash("Thanks. Our inspectors will review your report.")
    return redirect(url_for("restaurant", rid=rid))

@app.post("/cart/add/<int:did>")
def cart_add(did):
    cart = session.setdefault("cart", {})
    cart[str(did)] = cart.get(str(did), 0) + 1
    session.modified = True
    return redirect(request.referrer or url_for("cart"))

@app.route("/cart", methods=["GET", "POST"])
def cart():
    items, total, rid = [], 0, None
    for did, qty in session.get("cart", {}).items():
        d = db().execute("SELECT * FROM dish WHERE id=?", (did,)).fetchone()
        if d: items.append((d, qty)); total += d["price"] * qty; rid = d["restaurant_id"]
    if request.method == "POST" and items:
        s = safety(rid)
        snap = f"{s.get('grade','-')} / {s.get('score','-')} ({s['state']}) inspected {s.get('on','n/a')}"
        db().execute("INSERT INTO orders(restaurant_id,total,safety_snapshot,created_on) VALUES(?,?,?,?)",
                     (rid, total, snap, date.today().isoformat()))
        db().commit(); session["cart"] = {}
        flash(f"Order placed. Kitchen safety at order time: {snap}")
        return redirect(url_for("index"))
    return render_template("cart.html", items=items, total=total)

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == os.environ.get("ADMIN_PASSWORD", "admin123"):
            session["admin"] = True; return redirect(url_for("admin"))
        flash("Wrong password.")
    return render_template("login.html")

@app.route("/admin", methods=["GET", "POST"])
@admin_only
def admin():
    if request.method == "POST":
        passed = [k for k in CHECKS if request.form.get(k)]
        db().execute("INSERT INTO inspection(restaurant_id,inspector,inspected_on,score,passed,notes) VALUES(?,?,?,?,?,?)",
                     (request.form["rid"], request.form["inspector"], date.today().isoformat(),
                      score_for(passed), ",".join(passed), request.form.get("notes", "")))
        db().commit(); flash("Inspection recorded. The public badge is updated.")
        return redirect(url_for("admin"))
    rs = db().execute("SELECT * FROM restaurant").fetchall()
    reports = db().execute("SELECT report.*, restaurant.name FROM report JOIN restaurant ON restaurant.id=restaurant_id ORDER BY status, id DESC").fetchall()
    return render_template("admin.html", rs=rs, checks=CHECKS, reports=reports, safety=safety)

@app.post("/admin/report/<int:id>/close")
@admin_only
def close_report(id):
    db().execute("UPDATE report SET status='closed' WHERE id=?", (id,)); db().commit()
    return redirect(url_for("admin"))

@app.route("/admin/logout")
def logout():
    session.pop("admin", None); return redirect(url_for("index"))

init_db()  # runs on import, so "flask run" and "python app.py" both work

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\nOpen this in your browser: http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=True)
