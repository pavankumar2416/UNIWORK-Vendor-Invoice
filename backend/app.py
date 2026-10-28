from __future__ import annotations

import csv
import io
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("UNIWORK_DB", BASE_DIR / "instance" / "uniwork.sqlite3"))
app = Flask(__name__, template_folder="../frontend/templates", static_folder="../frontend/static", static_url_path="/static")
# Each server process gets a fresh identifier so sessions from a previous run are invalidated.
BOOT_ID = secrets.token_urlsafe(24)
app.secret_key = os.environ.get("SECRET_KEY", "uniwork-local-demo-secret-change-before-deployment")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1", PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
app.config["JSON_SORT_KEYS"] = False

GST_RATES = {"8536": 18, "8544": 18, "8504": 18, "8538": 18, "9403": 18, "3926": 18}
STATUSES = ["Draft", "Ordered", "Partially Received", "Received", "Cancelled"]


def money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tab_sessions (
        tab_token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        boot_id TEXT NOT NULL, created_at TEXT NOT NULL, last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','procurement','storekeeper','accounts','viewer')),
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vendors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, gstin TEXT NOT NULL,
        state TEXT NOT NULL, email TEXT, phone TEXT, payment_terms INTEGER DEFAULT 30,
        active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
        hsn TEXT NOT NULL, gst_rate REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'pcs',
        reorder_level INTEGER NOT NULL DEFAULT 10, unit_cost REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS warehouses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, city TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT UNIQUE NOT NULL,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id), order_date TEXT NOT NULL,
        expected_date TEXT, status TEXT NOT NULL DEFAULT 'Draft',
        notes TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_order_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0), unit_price REAL NOT NULL CHECK(unit_price >= 0)
    );
    CREATE TABLE IF NOT EXISTS goods_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, grn_number TEXT UNIQUE NOT NULL,
        po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        received_date TEXT NOT NULL, reference TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS goods_receipt_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grn_id INTEGER NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0)
    );
    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id),
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), movement_type TEXT NOT NULL,
        quantity INTEGER NOT NULL, reference TEXT NOT NULL, movement_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vendor_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id), po_id INTEGER REFERENCES purchase_orders(id),
        invoice_date TEXT NOT NULL, taxable_value REAL NOT NULL, gst_amount REAL NOT NULL,
        total_amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
        itc_status TEXT NOT NULL DEFAULT 'Review', notes TEXT, created_at TEXT NOT NULL,
        UNIQUE(vendor_id, invoice_number)
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
        entity_type TEXT NOT NULL, entity_id INTEGER, details TEXT, created_at TEXT NOT NULL
    );
    """)
    db.commit()
    # A predictable local-demo admin makes first launch straightforward. Change it immediately.
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        username = os.environ.get("ADMIN_USERNAME", "admin")
        password = os.environ.get("ADMIN_PASSWORD", "Forge@2026!")
        db.execute("INSERT INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                   (username, generate_password_hash(password, method="pbkdf2:sha256"), "UNIWORK Administrator", "admin", datetime.now().isoformat(timespec="seconds")))
        db.commit()


def audit(actor, action, entity_type, entity_id=None, details=""):
    get_db().execute(
        "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
        (actor, action, entity_type, entity_id, details, datetime.now().isoformat(timespec="seconds"))
    )
    get_db().commit()


def seed_db():
    db = get_db()
    disabled = db.execute("SELECT value FROM app_state WHERE key='demo_seed_disabled'").fetchone()
    if disabled and disabled["value"] == "1":
        return
    if db.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]:
        return
    now = datetime.now().isoformat(timespec="seconds")
    vendors = [
        ("VND-001", "Mysuru Copper & Conductors", "29AABCM1001A1Z5", "Karnataka", "orders@mysurucopper.example", "0821-4001001", 30),
        ("VND-002", "Bharat Switchgear Works", "27AABCB2002B1Z6", "Maharashtra", "sales@bharatswitch.example", "022-4002002", 45),
        ("VND-003", "Southline Polymers", "33AABCS3003C1Z7", "Tamil Nadu", "billing@southline.example", "044-4003003", 30),
        ("VND-004", "Deccan Electrical Metals", "36AABCD4004D1Z8", "Telangana", "ap@deccanmetals.example", "040-4004004", 15),
        ("VND-005", "Precision Enclosures India", "29AABCP5005E1Z9", "Karnataka", "accounts@precision.example", "080-4005005", 30),
        ("VND-006", "Western Relay & Controls", "24AABCW6006F1Z0", "Gujarat", "finance@westernrelay.example", "079-4006006", 60),
    ]
    db.executemany("INSERT INTO vendors(code,name,gstin,state,email,phone,payment_terms,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   [(*v, now) for v in vendors])
    products = [
        ("SKU-EL-001", "Copper winding wire 1.2mm", "Conductors", "8544", 18, "kg", 45, 620),
        ("SKU-EL-002", "Miniature circuit breaker 16A", "Switchgear", "8536", 18, "pcs", 80, 145),
        ("SKU-EL-003", "Terminal block 12-way", "Connectors", "8536", 18, "pcs", 55, 78),
        ("SKU-EL-004", "ABS junction enclosure", "Enclosures", "3926", 18, "pcs", 35, 92),
        ("SKU-EL-005", "Control transformer 230/24V", "Transformers", "8504", 18, "pcs", 12, 890),
        ("SKU-EL-006", "DIN rail 1 metre", "Mounting", "8538", 18, "pcs", 40, 64),
        ("SKU-EL-007", "PVC insulated cable 2.5sqmm", "Conductors", "8544", 18, "m", 120, 28),
        ("SKU-EL-008", "Weatherproof enclosure IP65", "Enclosures", "3926", 18, "pcs", 18, 410),
        ("SKU-EL-009", "Industrial relay 24V DC", "Switchgear", "8536", 18, "pcs", 24, 235),
        ("SKU-EL-010", "Panel mounting plate", "Mounting", "8538", 18, "pcs", 20, 185),
        ("SKU-EL-011", "Copper busbar 20x3mm", "Conductors", "7407", 18, "kg", 15, 760),
        ("SKU-EL-012", "Cable gland M20", "Connectors", "8536", 18, "pcs", 60, 24),
    ]
    db.executemany("INSERT INTO products(sku,name,category,hsn,gst_rate,unit,reorder_level,unit_cost) VALUES(?,?,?,?,?,?,?,?)", products)
    db.executemany("INSERT INTO warehouses(code,name,city) VALUES(?,?,?)",
                   [("WH-MYS", "Mysuru Main Store", "Mysuru"), ("WH-BLR", "Bengaluru Dispatch Store", "Bengaluru")])

    # Opening stock and a six-month operational trail, intentionally including a few exceptions.
    rng = __import__("random").Random(31)
    product_rows = db.execute("SELECT id,sku,unit_cost,reorder_level FROM products").fetchall()
    warehouses = db.execute("SELECT id FROM warehouses").fetchall()
    for p in product_rows:
        for w in warehouses:
            qty = rng.randint(max(2, p["reorder_level"] // 2), p["reorder_level"] * 3)
            db.execute("""INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at)
                          VALUES(?,?,?,?,?,?,?)""",
                       (p["id"], w["id"], "OPENING", qty, "OPENING-BALANCE", (date.today()-timedelta(days=190)).isoformat(), now))
    vendors_db = db.execute("SELECT id FROM vendors").fetchall()
    for i in range(1, 13):
        v = vendors_db[(i-1) % len(vendors_db)]["id"]
        od = date.today() - timedelta(days=170 - i*11)
        status = ["Received", "Received", "Partially Received", "Ordered"][i % 4]
        po_num = f"PO-2026-{i:04d}"
        db.execute("""INSERT INTO purchase_orders(po_number,vendor_id,order_date,expected_date,status,notes,created_at)
                      VALUES(?,?,?,?,?,?,?)""",
                   (po_num, v, od.isoformat(), (od+timedelta(days=10)).isoformat(), status,
                    "Seeded demonstration transaction" if i in (3, 8) else "", now))
        po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        p = product_rows[(i*3) % len(product_rows)]
        qty = 30 + i*3
        price = p["unit_cost"] * (1.03 if i % 3 == 0 else 1.0)
        db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                   (po_id, p["id"], qty, price))
        if status in ("Received", "Partially Received"):
            wh = warehouses[i % 2]["id"]
            rec_qty = qty if status == "Received" else max(1, qty // 2)
            db.execute("INSERT INTO goods_receipts(grn_number,po_id,warehouse_id,received_date,reference,created_at) VALUES(?,?,?,?,?,?)",
                       (f"GRN-2026-{i:04d}", po_id, wh, (od+timedelta(days=7)).isoformat(), f"Challan-{i:04d}", now))
            grn_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("INSERT INTO goods_receipt_lines(grn_id,product_id,quantity) VALUES(?,?,?)",
                       (grn_id, p["id"], rec_qty))
            db.execute("""INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at)
                          VALUES(?,?,?,?,?,?,?)""",
                       (p["id"], wh, "IN", rec_qty, f"GRN-2026-{i:04d}", (od+timedelta(days=7)).isoformat(), now))
        if i in (2, 5, 8, 11):
            taxable = round(qty * price, 2)
            gst = round(taxable * .18, 2)
            # Two intentionally discrepant invoices create review work.
            if i == 8:
                gst = round(taxable * .12, 2)
            if i == 11:
                taxable += 500
                gst = round(taxable * .18, 2)
            db.execute("""INSERT INTO vendor_invoices(invoice_number,vendor_id,po_id,invoice_date,taxable_value,gst_amount,total_amount,status,itc_status,notes,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (f"INV-{i:04d}", v, po_id, (od+timedelta(days=8)).isoformat(), taxable, gst,
                        round(taxable+gst, 2), "Pending" if i in (8,11) else "Approved",
                        "Review" if i in (8,11) else "Eligible",
                        "Seeded exception: verify GST rate" if i == 8 else ("Seeded exception: taxable value differs from PO" if i == 11 else ""),
                        now))
    db.commit()
    audit("system", "DEMO_SEED", "database", None, "Seeded demo master data and transactions")


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def inventory_rows():
    return query_all("""
      SELECT p.id,p.sku,p.name,p.category,p.hsn,p.gst_rate,p.unit,p.reorder_level,p.unit_cost,
        COALESCE(SUM(CASE WHEN sm.movement_type IN ('OPENING','IN','ADJUST_IN') THEN sm.quantity
                          WHEN sm.movement_type IN ('OUT','ADJUST_OUT') THEN -sm.quantity ELSE 0 END),0) AS stock
      FROM products p LEFT JOIN stock_movements sm ON sm.product_id=p.id
      GROUP BY p.id ORDER BY p.name
    """)


def po_totals(po_id):
    row = query_one("""SELECT COALESCE(SUM(quantity*unit_price),0) AS taxable,
                              COALESCE(SUM(quantity*unit_price*0.18),0) AS gst
                       FROM purchase_order_lines WHERE po_id=?""", (po_id,))
    return money(row["taxable"]), money(row["gst"])


def invoice_checks(inv):
    po = query_one("""SELECT po.id,po.po_number,po.status,v.gstin,v.state FROM purchase_orders po
                      JOIN vendors v ON v.id=po.vendor_id WHERE po.id=?""", (inv["po_id"],)) if inv["po_id"] else None
    issues = []
    if not po:
        issues.append("No linked purchase order")
    else:
        expected_taxable, _ = po_totals(po["id"])
        if abs(float(inv["taxable_value"]) - expected_taxable) > 1.0:
            issues.append(f"Taxable value variance ₹{abs(float(inv['taxable_value'])-expected_taxable):,.2f}")
    expected_gst = money(float(inv["taxable_value"]) * 0.18)
    if abs(float(inv["gst_amount"]) - expected_gst) > 1.0:
        issues.append(f"GST mismatch: expected ₹{expected_gst:,.2f} at 18%")
    return {"issues": issues, "match": not issues, "expected_gst": expected_gst}


@app.context_processor
def inject_shared_context():
    return {"now_date": date.today().isoformat()}


ROLE_LABELS = {"admin":"Administrator", "procurement":"Procurement", "storekeeper":"Storekeeper", "accounts":"Accounts & GST", "viewer":"Read-only viewer"}
ROLE_ACCESS = {
    "admin": {"*"},
    "procurement": {"dashboard","inventory","vendors","vendor_detail","create_vendor","purchase_orders","create_po","update_po_status","reports","export_csv","profile"},
    "storekeeper": {"dashboard","inventory","receipts","create_receipt","profile"},
    "accounts": {"dashboard","vendors","vendor_detail","invoices","create_invoice","review_invoice","approve_invoice_override","reports","export_csv","profile"},
    "viewer": {"dashboard","inventory","vendors","vendor_detail","purchase_orders","receipts","invoices","reports","api_summary","export_csv","change_password","profile"},
}

@app.url_defaults
def preserve_tab_token(_endpoint, values):
    """Keep the tab-specific authentication token on same-tab navigation."""
    token = getattr(g, "tab_token", None)
    if token and _endpoint != "static" and "__tab" not in values:
        values["__tab"] = token


@app.before_request
def bootstrap():
    init_db()
    seed_db()

    if request.endpoint in {"static", "setup_status"} or request.endpoint is None:
        return None

    token = request.args.get("__tab") or request.form.get("__tab") or request.headers.get("X-UNIWORK-Tab")
    g.tab_token = token

    if request.endpoint == "login":
        if not token:
            fresh_token = secrets.token_urlsafe(32)
            return redirect(url_for("login", __tab=fresh_token, next=request.args.get("next", "")))
        if request.method == "GET":
            row = query_one("SELECT user_id,boot_id FROM tab_sessions WHERE tab_token=?", (token,))
            if row and row["boot_id"] == BOOT_ID:
                user = query_one("SELECT id,username,full_name,role,active FROM users WHERE id=?", (row["user_id"],))
                if user and user["active"]:
                    return redirect(url_for("dashboard", __tab=token))
        return None

    if request.endpoint == "logout":
        return None
    if not token:
        return redirect(url_for("login", next=request.path))

    row = query_one("SELECT user_id,boot_id FROM tab_sessions WHERE tab_token=?", (token,))
    if not row or row["boot_id"] != BOOT_ID:
        return redirect(url_for("login", __tab=token, next=request.path))
    user = query_one("SELECT id,username,full_name,role,active FROM users WHERE id=?", (row["user_id"],))
    if not user or not user["active"]:
        get_db().execute("DELETE FROM tab_sessions WHERE tab_token=?", (token,))
        get_db().commit()
        return redirect(url_for("login", __tab=token))
    get_db().execute("UPDATE tab_sessions SET last_seen=? WHERE tab_token=?", (datetime.now().isoformat(timespec="seconds"), token))
    get_db().commit()
    g.user = user
    allowed = ROLE_ACCESS.get(user["role"], set())
    if "*" not in allowed and request.endpoint not in allowed and request.endpoint not in {"logout", "forbidden", "change_password"}:
        if request.method == "POST":
            flash("Your role does not have permission to perform that action.", "error")
        return render_template("error.html", title="Access denied", code=403,
                               message="Access denied — your role does not have permission for this page."), 403


@app.after_request
def prevent_private_page_caching(response):
    # Authentication pages and private business data should not be kept in the
    # browser's HTTP cache; authorization is still checked on every request.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.context_processor
def inject_user_context():
    user = getattr(g, "user", None)
    return {"current_user": user, "role_labels": ROLE_LABELS, "is_admin": bool(user and user["role"] == "admin")}

@app.route("/login", methods=["GET", "POST"])
def login():
    token = request.args.get("__tab") or request.form.get("__tab")
    if not token:
        token = secrets.token_urlsafe(32)
        return redirect(url_for("login", __tab=token, next=request.args.get("next", "")))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query_one("SELECT * FROM users WHERE username=? AND active=1", (username,))
        if user and check_password_hash(user["password_hash"], password):
            now = datetime.now().isoformat(timespec="seconds")
            get_db().execute(
                "INSERT INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tab_token) DO UPDATE SET user_id=excluded.user_id,boot_id=excluded.boot_id,last_seen=excluded.last_seen",
                (token, user["id"], BOOT_ID, now, now)
            )
            get_db().commit()
            audit(user["username"], "LOGIN", "user", user["id"], "Successful login (tab-specific session)")
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("dashboard")
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            parts = urlsplit(target)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["__tab"] = token
            target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            return redirect(target)
        flash("Invalid username or password, or the account is disabled.", "error")
    return render_template("login.html", title="Sign in · UNIWORK", tab_token=token)

@app.route("/logout", methods=["POST"])
def logout():
    user = getattr(g, "user", None)
    token = request.args.get("__tab") or request.form.get("__tab") or request.headers.get("X-UNIWORK-Tab")
    if user:
        audit(user["username"], "LOGOUT", "user", user["id"], "Signed out of this tab")
    if token:
        get_db().execute("DELETE FROM tab_sessions WHERE tab_token=?", (token,))
        get_db().commit()
    flash("You have been signed out of this tab.", "success")
    return redirect(url_for("login", __tab=secrets.token_urlsafe(32)))

@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    if request.method == "POST":
        current = query_one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
        new_password = request.form.get("new_password", "")
        if not current or not check_password_hash(current["password_hash"], request.form.get("current_password", "")):
            flash("Current password is incorrect.", "error")
        elif len(new_password) < 10:
            flash("New password must be at least 10 characters.", "error")
        elif new_password != request.form.get("confirm_password", ""):
            flash("New password and confirmation do not match.", "error")
        else:
            get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password, method="pbkdf2:sha256"), g.user["id"]))
            get_db().commit()
            audit(g.user["username"], "CHANGE_PASSWORD", "user", g.user["id"], "Password changed")
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", title="Change password")

@app.route("/admin/users", methods=["GET", "POST"])
def user_management():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        if len(username) < 3 or len(full_name) < 2 or len(password) < 10 or role not in ROLE_LABELS:
            flash("Use a username of 3+ characters, full name, password of 10+ characters, and valid role.", "error")
        else:
            try:
                get_db().execute("INSERT INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                    (username, generate_password_hash(password, method="pbkdf2:sha256"), full_name, role, datetime.now().isoformat(timespec="seconds")))
                get_db().commit()
                audit(g.user["username"], "CREATE_USER", "user", None, f"Created {username} with role {role}")
                flash(f"Account {username} created.", "success")
            except sqlite3.IntegrityError:
                flash("That username already exists.", "error")
        return redirect(url_for("user_management"))
    return render_template("users.html", title="User & role management", users=query_all("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY id"), roles=ROLE_LABELS)

@app.post("/admin/users/<int:user_id>/toggle")
def toggle_user(user_id):
    if user_id == g.user["id"]:
        flash("You cannot disable your own account.", "error")
    else:
        user = query_one("SELECT username,active FROM users WHERE id=?", (user_id,))
        if user:
            get_db().execute("UPDATE users SET active=? WHERE id=?", (0 if user["active"] else 1, user_id))
            get_db().commit()
            audit(g.user["username"], "TOGGLE_USER", "user", user_id, f"{user['username']} active={not user['active']}")
            flash("User access updated.", "success")
    return redirect(url_for("user_management"))

@app.route("/admin/data", methods=["GET", "POST"])
def data_management():
    if request.method == "POST":
        action = request.form.get("action")
        confirm = request.form.get("confirm", "").strip()
        db = get_db()
        if action == "clear_transactions" and confirm == "CLEAR TRANSACTIONS":
            for table in ("goods_receipt_lines", "goods_receipts", "vendor_invoices", "purchase_order_lines", "purchase_orders", "stock_movements", "audit_log"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            audit(g.user["username"], "CLEAR_TRANSACTIONS", "database", None, "Cleared transaction records; master data and accounts preserved")
            flash("Transaction data cleared. Vendors, products, warehouses and user accounts were preserved.", "success")
        elif action == "reset_demo" and confirm == "RESET DEMO DATA":
            db.execute("INSERT INTO app_state(key,value) VALUES('demo_seed_disabled','0') ON CONFLICT(key) DO UPDATE SET value='0'")
            for table in ("goods_receipt_lines", "goods_receipts", "vendor_invoices", "purchase_order_lines", "purchase_orders", "stock_movements", "audit_log", "vendors", "products", "warehouses"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            seed_db()
            audit(g.user["username"], "RESET_DEMO_DATA", "database", None, "Re-seeded demonstration data")
            flash("Demo data has been reset to the original scenario. User accounts were preserved.", "success")
        elif action == "delete_all_business_data" and confirm == "DELETE ALL BUSINESS DATA":
            db.execute("INSERT INTO app_state(key,value) VALUES('demo_seed_disabled','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            for table in ("goods_receipt_lines", "goods_receipts", "vendor_invoices", "purchase_order_lines", "purchase_orders", "stock_movements", "audit_log", "vendors", "products", "warehouses"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            audit(g.user["username"], "DELETE_ALL_BUSINESS_DATA", "database", None, "Deleted all business/master/transaction data; accounts preserved")
            flash("All business data deleted. User accounts remain so you can continue signing in.", "success")
        else:
            flash("Confirmation text did not match. No data was deleted.", "error")
        return redirect(url_for("data_management"))
    counts = {table: query_one(f"SELECT COUNT(*) n FROM {table}")["n"] for table in ("users","vendors","products","warehouses","purchase_orders","goods_receipts","vendor_invoices","stock_movements","audit_log")}
    return render_template("data_management.html", title="Data management", counts=counts)


@app.route("/")
def dashboard():
    inv = inventory_rows()
    low = [p for p in inv if p["stock"] <= p["reorder_level"]]
    invoice_rows = query_all("""SELECT vi.*,v.name AS vendor_name,po.po_number FROM vendor_invoices vi
                                JOIN vendors v ON v.id=vi.vendor_id LEFT JOIN purchase_orders po ON po.id=vi.po_id
                                ORDER BY vi.invoice_date DESC,vi.id DESC""")
    exception_count = sum(1 for i in invoice_rows if not invoice_checks(i)["match"])
    outstanding = query_one("SELECT COALESCE(SUM(total_amount),0) amount FROM vendor_invoices WHERE status='Pending'")["amount"]
    po_open = query_one("SELECT COUNT(*) n FROM purchase_orders WHERE status IN ('Draft','Ordered','Partially Received')")["n"]
    stock_value = sum(float(p["stock"]) * float(p["unit_cost"]) for p in inv)
    monthly = query_all("""SELECT substr(order_date,1,7) month, COUNT(*) orders,
                            COALESCE(SUM(pol.quantity*pol.unit_price),0) value
                           FROM purchase_orders po JOIN purchase_order_lines pol ON pol.po_id=po.id
                           WHERE order_date >= date('now','-5 months') GROUP BY substr(order_date,1,7) ORDER BY month""")
    activity = query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 6")
    return render_template("dashboard.html", title="Operations overview", products=inv, low=low,
                           invoices=invoice_rows[:6], exception_count=exception_count, outstanding=outstanding,
                           po_open=po_open, stock_value=stock_value, monthly=monthly, activity=activity)


@app.route("/inventory")
def inventory():
    rows = inventory_rows()
    return render_template("inventory.html", title="Inventory control", products=rows,
                           low_count=sum(1 for p in rows if p["stock"] <= p["reorder_level"]))


@app.route("/vendors")
def vendors():
    rows = query_all("SELECT v.*,COUNT(DISTINCT po.id) AS po_count FROM vendors v LEFT JOIN purchase_orders po ON po.vendor_id=v.id GROUP BY v.id ORDER BY v.name")
    return render_template("vendors.html", title="Vendor directory", vendors=rows)


@app.route("/vendors/<int:vendor_id>")
def vendor_detail(vendor_id):
    vendor = query_one("SELECT * FROM vendors WHERE id=?", (vendor_id,))
    if not vendor:
        return render_template("error.html", title="Vendor not found", code=404,
                               message="The requested vendor record could not be found."), 404
    orders = query_all("""SELECT po.*, COALESCE(SUM(pol.quantity*pol.unit_price),0) AS taxable,
                         COALESCE(SUM(pol.quantity*pol.unit_price*0.18),0) AS gst,
                         COUNT(pol.id) AS line_count
                         FROM purchase_orders po LEFT JOIN purchase_order_lines pol ON pol.po_id=po.id
                         WHERE po.vendor_id=? GROUP BY po.id ORDER BY po.order_date DESC,po.id DESC""", (vendor_id,))
    invoices_rows = query_all("""SELECT vi.*,po.po_number FROM vendor_invoices vi
                                 LEFT JOIN purchase_orders po ON po.id=vi.po_id
                                 WHERE vi.vendor_id=? ORDER BY vi.invoice_date DESC,vi.id DESC""", (vendor_id,))
    po_lines = {}
    for order in orders:
        po_lines[order["id"]] = query_all("""SELECT pol.quantity,pol.unit_price,p.sku,p.name,p.unit
            FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id
            WHERE pol.po_id=? ORDER BY p.name""", (order["id"],))
    return render_template("vendor_detail.html", title="Vendor details", vendor=vendor,
                           orders=orders, po_lines=po_lines, invoices=invoices_rows)


@app.post("/vendors/create")
def create_vendor():
    if g.user["role"] not in {"admin", "procurement"}:
        return render_template("error.html", title="Access denied", code=403,
                               message="Only administrators and procurement users can add vendors."), 403
    db = get_db()
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    gstin = request.form.get("gstin", "").strip().upper()
    state = request.form.get("state", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    try:
        terms = int(request.form.get("payment_terms", "30"))
        if not all([code, name, gstin, state]):
            raise ValueError("Vendor code, name, GSTIN and state are required.")
        if terms < 0 or terms > 365:
            raise ValueError("Payment terms must be between 0 and 365 days.")
        now = datetime.now().isoformat(timespec="seconds")
        cur = db.execute("""INSERT INTO vendors(code,name,gstin,state,email,phone,payment_terms,active,created_at)
                            VALUES(?,?,?,?,?,?,?,1,?)""",
                         (code, name, gstin, state, email, phone, terms, now))
        db.commit()
        audit(g.user["username"], "CREATE", "vendor", cur.lastrowid, f"{code}; {name}")
        flash(f"Vendor {name} added successfully.", "success")
    except (ValueError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Could not add vendor: {exc if str(exc) else 'Vendor code already exists.'}", "error")
    return redirect(url_for("vendors"))


@app.route("/profile")
def profile():
    user = query_one("SELECT id,username,full_name,role,active,created_at FROM users WHERE id=?", (g.user["id"],))
    activity = query_all("SELECT action,entity_type,details,created_at FROM audit_log WHERE actor=? ORDER BY id DESC LIMIT 10", (g.user["username"],))
    return render_template("profile.html", title="My profile", profile=user, activity=activity)


@app.route("/purchase-orders")
def purchase_orders():
    rows = query_all("""SELECT po.*,v.name vendor_name,
                      COALESCE(SUM(pol.quantity*pol.unit_price),0) taxable,
                      COALESCE(SUM(pol.quantity*pol.unit_price*0.18),0) gst
                      FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id
                      LEFT JOIN purchase_order_lines pol ON pol.po_id=po.id
                      GROUP BY po.id ORDER BY po.order_date DESC,po.id DESC""")
    products = query_all("SELECT * FROM products ORDER BY name")
    vendors_list = query_all("SELECT * FROM vendors WHERE active=1 ORDER BY name")
    return render_template("purchase_orders.html", title="Procurement", orders=rows, products=products, vendors=vendors_list, statuses=STATUSES)


@app.post("/purchase-orders/create")
def create_po():
    db = get_db()
    try:
        vendor_id = int(request.form["vendor_id"])
        product_id = int(request.form["product_id"])
        quantity = int(request.form["quantity"])
        unit_price = float(request.form["unit_price"])
        order_date = request.form.get("order_date") or date.today().isoformat()
        expected_date = request.form.get("expected_date") or None
        if quantity < 1 or unit_price < 0:
            raise ValueError("Quantity must be positive and unit price cannot be negative.")
        vendor = query_one("SELECT id FROM vendors WHERE id=?", (vendor_id,))
        product = query_one("SELECT id FROM products WHERE id=?", (product_id,))
        if not vendor or not product:
            raise ValueError("Choose a valid vendor and product.")
        po_num = f"PO-{date.today().year}-{int(datetime.now().timestamp()) % 100000:05d}"
        db.execute("""INSERT INTO purchase_orders(po_number,vendor_id,order_date,expected_date,status,notes,created_at)
                      VALUES(?,?,?,?,'Ordered',?,?)""",
                   (po_num, vendor_id, order_date, expected_date, request.form.get("notes","").strip(),
                    datetime.now().isoformat(timespec="seconds")))
        po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                   (po_id, product_id, quantity, unit_price))
        db.commit()
        audit(g.user["username"], "CREATE", "purchase_order", po_id, f"{po_num}; qty={quantity}")
        flash(f"{po_num} created and added to the procurement queue.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as e:
        db.rollback()
        flash(f"Could not create purchase order: {e}", "error")
    return redirect(url_for("purchase_orders"))


@app.post("/purchase-orders/<int:po_id>/status")
def update_po_status(po_id):
    status = request.form.get("status")
    if status not in STATUSES:
        flash("Invalid purchase order status.", "error")
    else:
        db = get_db()
        row = query_one("SELECT po_number FROM purchase_orders WHERE id=?", (po_id,))
        if row:
            db.execute("UPDATE purchase_orders SET status=? WHERE id=?", (status, po_id))
            db.commit()
            audit(g.user["username"], "STATUS_CHANGE", "purchase_order", po_id, f"{row['po_number']} → {status}")
            flash(f"{row['po_number']} status updated.", "success")
        else:
            flash("Purchase order not found.", "error")
    return redirect(url_for("purchase_orders"))


@app.route("/receipts")
def receipts():
    rows = query_all("""SELECT gr.*,po.po_number,v.name vendor_name,w.name warehouse_name,
                       GROUP_CONCAT(p.name || ' × ' || grl.quantity, ', ') items
                       FROM goods_receipts gr JOIN purchase_orders po ON po.id=gr.po_id
                       JOIN vendors v ON v.id=po.vendor_id JOIN warehouses w ON w.id=gr.warehouse_id
                       LEFT JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                       LEFT JOIN products p ON p.id=grl.product_id
                       GROUP BY gr.id ORDER BY gr.received_date DESC""")
    orders = query_all("""SELECT po.id,po.po_number,v.name vendor_name FROM purchase_orders po
                          JOIN vendors v ON v.id=po.vendor_id
                          WHERE po.status IN ('Ordered','Partially Received') ORDER BY po.order_date DESC""")
    warehouses = query_all("SELECT * FROM warehouses ORDER BY name")
    products = query_all("SELECT * FROM products ORDER BY name")
    return render_template("receipts.html", title="Goods receipts", receipts=rows, orders=orders,
                           warehouses=warehouses, products=products)


@app.post("/receipts/create")
def create_receipt():
    db = get_db()
    try:
        po_id = int(request.form["po_id"])
        warehouse_id = int(request.form["warehouse_id"])
        product_id = int(request.form["product_id"])
        qty = int(request.form["quantity"])
        if qty <= 0:
            raise ValueError("Receipt quantity must be greater than zero.")
        po = query_one("SELECT * FROM purchase_orders WHERE id=?", (po_id,))
        line = query_one("SELECT * FROM purchase_order_lines WHERE po_id=? AND product_id=?", (po_id, product_id))
        if not po or not line:
            raise ValueError("The selected product must exist on the selected purchase order.")
        already = query_one("""SELECT COALESCE(SUM(grl.quantity),0) qty FROM goods_receipts gr
                               JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                               WHERE gr.po_id=? AND grl.product_id=?""", (po_id, product_id))["qty"]
        if qty + already > line["quantity"]:
            raise ValueError(f"Receipt exceeds ordered quantity. Ordered {line['quantity']}; already received {already}.")
        wh = query_one("SELECT id FROM warehouses WHERE id=?", (warehouse_id,))
        if not wh:
            raise ValueError("Choose a valid warehouse.")
        grn = f"GRN-{date.today().year}-{int(datetime.now().timestamp()) % 100000:05d}"
        received_date = request.form.get("received_date") or date.today().isoformat()
        db.execute("""INSERT INTO goods_receipts(grn_number,po_id,warehouse_id,received_date,reference,created_at)
                      VALUES(?,?,?,?,?,?)""", (grn, po_id, warehouse_id, received_date,
                      request.form.get("reference","").strip(), datetime.now().isoformat(timespec="seconds")))
        grn_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO goods_receipt_lines(grn_id,product_id,quantity) VALUES(?,?,?)", (grn_id, product_id, qty))
        db.execute("""INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at)
                      VALUES(?,?, 'IN',?,?,?,?)""",
                   (product_id, warehouse_id, qty, grn, received_date, datetime.now().isoformat(timespec="seconds")))
        new_received = already + qty
        new_status = "Received" if new_received == line["quantity"] else "Partially Received"
        db.execute("UPDATE purchase_orders SET status=? WHERE id=?", (new_status, po_id))
        db.commit()
        audit(g.user["username"], "CREATE", "goods_receipt", grn_id, f"{grn}; qty={qty}; warehouse={warehouse_id}")
        flash(f"{grn} posted. Stock ledger updated atomically.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as e:
        db.rollback()
        flash(f"Receipt not posted: {e}", "error")
    return redirect(url_for("receipts"))


@app.route("/invoices")
def invoices():
    rows = query_all("""SELECT vi.*,v.name vendor_name,v.gstin,po.po_number FROM vendor_invoices vi
                        JOIN vendors v ON v.id=vi.vendor_id LEFT JOIN purchase_orders po ON po.id=vi.po_id
                        ORDER BY vi.invoice_date DESC,vi.id DESC""")
    checked = []
    for row in rows:
        checked.append({"invoice": row, **invoice_checks(row)})
    vendors_list = query_all("SELECT * FROM vendors ORDER BY name")
    orders = query_all("SELECT * FROM purchase_orders ORDER BY order_date DESC")
    return render_template("invoices.html", title="Invoice & GST desk", invoices=checked, vendors=vendors_list, orders=orders)


@app.post("/invoices/create")
def create_invoice():
    db = get_db()
    try:
        vendor_id = int(request.form["vendor_id"])
        po_id = int(request.form["po_id"])
        invoice_number = request.form["invoice_number"].strip()
        invoice_date = request.form.get("invoice_date") or date.today().isoformat()
        taxable = money(float(request.form["taxable_value"]))
        gst = money(float(request.form["gst_amount"]))
        if not invoice_number or taxable < 0 or gst < 0:
            raise ValueError("Enter an invoice number and non-negative values.")
        po = query_one("SELECT vendor_id FROM purchase_orders WHERE id=?", (po_id,))
        if not po or po["vendor_id"] != vendor_id:
            raise ValueError("The purchase order must belong to the selected vendor.")
        total = money(taxable + gst)
        expected = money(taxable * .18)
        expected_taxable, _ = po_totals(po_id)
        has_mismatch = abs(gst - expected) > 1 or abs(taxable - expected_taxable) > 1
        status = "Pending" if has_mismatch else "Approved"
        itc = "Review" if has_mismatch else "Eligible"
        db.execute("""INSERT INTO vendor_invoices(invoice_number,vendor_id,po_id,invoice_date,taxable_value,gst_amount,total_amount,status,itc_status,notes,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (invoice_number, vendor_id, po_id, invoice_date, taxable, gst, total, status, itc,
                    request.form.get("notes","").strip(), datetime.now().isoformat(timespec="seconds")))
        inv_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
        audit(g.user["username"], "CREATE", "vendor_invoice", inv_id, f"{invoice_number}; total={total}")
        flash(f"Invoice {invoice_number} captured. GST validation: {'review required' if status == 'Pending' else 'passed basic 18% check'}.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as e:
        db.rollback()
        flash(f"Invoice not saved: {e}", "error")
    return redirect(url_for("invoices"))


@app.post("/invoices/<int:invoice_id>/review")
def review_invoice(invoice_id):
    row = query_one("SELECT * FROM vendor_invoices WHERE id=?", (invoice_id,))
    if not row:
        flash("Invoice not found.", "error")
    else:
        checks = invoice_checks(row)
        db = get_db()
        if checks["match"]:
            db.execute("UPDATE vendor_invoices SET status='Approved',itc_status='Eligible' WHERE id=?", (invoice_id,))
            flash("Invoice passed the current rule checks and is marked approved.", "success")
            audit(g.user["username"], "APPROVE", "vendor_invoice", invoice_id, row["invoice_number"])
        else:
            db.execute("UPDATE vendor_invoices SET status='Pending',itc_status='Review' WHERE id=?", (invoice_id,))
            flash("Invoice still has exceptions. Review the PO value and GST rate before approval.", "error")
            audit(g.user["username"], "FLAG", "vendor_invoice", invoice_id, "; ".join(checks["issues"]))
        db.commit()
    return redirect(url_for("invoices"))


@app.post("/invoices/<int:invoice_id>/approve-override")
def approve_invoice_override(invoice_id):
    # Manual override is deliberately separate from automated matching. It requires
    # a reason and never marks ITC as eligible while an exception remains.
    reason = request.form.get("override_reason", "").strip()
    if not reason:
        flash("A reason is required to approve an invoice with exceptions.", "error")
        return redirect(url_for("invoices"))
    row = query_one("SELECT * FROM vendor_invoices WHERE id=?", (invoice_id,))
    if not row:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices"))
    checks = invoice_checks(row)
    db = get_db()
    db.execute("UPDATE vendor_invoices SET status='Approved',itc_status=? WHERE id=?",
               ("Eligible" if checks["match"] else "Review", invoice_id))
    db.commit()
    detail = f"{row['invoice_number']}; manual override reason: {reason}"
    if checks["issues"]:
        detail += "; unresolved checks: " + "; ".join(checks["issues"])
    audit(g.user["username"], "APPROVE_OVERRIDE", "vendor_invoice", invoice_id, detail)
    flash("Invoice manually approved. ITC remains under review because exceptions are unresolved." if checks["issues"] else "Invoice approved; automated checks passed.", "success")
    return redirect(url_for("invoices"))


@app.route("/reports")
def reports():
    inv = inventory_rows()
    invoices_rows = query_all("""SELECT vi.*,v.name vendor_name,v.state FROM vendor_invoices vi
                                 JOIN vendors v ON v.id=vi.vendor_id ORDER BY vi.invoice_date DESC""")
    gst_total = sum(float(i["gst_amount"]) for i in invoices_rows)
    eligible = sum(float(i["gst_amount"]) for i in invoices_rows if i["itc_status"] == "Eligible")
    pending = sum(float(i["total_amount"]) for i in invoices_rows if i["status"] == "Pending")
    interstate = query_all("""SELECT v.state,COUNT(*) invoices,SUM(vi.taxable_value) taxable,SUM(vi.gst_amount) gst
                               FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id
                               GROUP BY v.state ORDER BY taxable DESC""")
    return render_template("reports.html", title="GST & management reports", products=inv, invoices=invoices_rows,
                           gst_total=gst_total, eligible=eligible, pending=pending, interstate=interstate)


@app.route("/audit")
def audit_page():
    rows = query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
    return render_template("audit.html", title="Audit trail", events=rows)


@app.route("/api/summary")
def api_summary():
    inv = inventory_rows()
    invoice_rows = query_all("SELECT * FROM vendor_invoices")
    return jsonify({
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "inventory_skus": len(inv),
        "stock_units": sum(int(p["stock"]) for p in inv),
        "low_stock_skus": sum(1 for p in inv if p["stock"] <= p["reorder_level"]),
        "stock_value": money(sum(float(p["stock"]) * float(p["unit_cost"]) for p in inv)),
        "open_purchase_orders": query_one("SELECT COUNT(*) n FROM purchase_orders WHERE status IN ('Draft','Ordered','Partially Received')")["n"],
        "invoice_exceptions": sum(1 for i in invoice_rows if not invoice_checks(i)["match"]),
        "pending_payables": money(sum(float(i["total_amount"]) for i in invoice_rows if i["status"] == "Pending")),
    })


@app.route("/export/<kind>.csv")
def export_csv(kind):
    if kind == "inventory":
        rows = inventory_rows()
        headers = ["sku", "name", "category", "hsn", "gst_rate", "unit", "stock", "reorder_level", "unit_cost"]
        data = [[r[h] for h in headers] for r in rows]
    elif kind == "invoices":
        rows = query_all("""SELECT vi.invoice_number,v.name vendor, v.gstin,vi.invoice_date,vi.taxable_value,vi.gst_amount,vi.total_amount,vi.status,vi.itc_status
                            FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id ORDER BY vi.invoice_date DESC""")
        headers = list(rows[0].keys()) if rows else ["invoice_number","vendor","gstin","invoice_date","taxable_value","gst_amount","total_amount","status","itc_status"]
        data = [list(r) for r in rows]
    elif kind == "purchase-orders":
        rows = query_all("""SELECT po.po_number,v.name vendor,po.order_date,po.expected_date,po.status,
                            SUM(pol.quantity*pol.unit_price) taxable
                            FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id
                            JOIN purchase_order_lines pol ON pol.po_id=po.id GROUP BY po.id ORDER BY po.order_date DESC""")
        headers = list(rows[0].keys()) if rows else ["po_number","vendor","order_date","expected_date","status","taxable"]
        data = [list(r) for r in rows]
    else:
        return jsonify({"error": "Unknown export type"}), 404
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(data)
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=uniwork-{kind}.csv"})


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", title="Not found", code=404, message="This page does not exist."), 404


@app.errorhandler(500)
def server_error(_error):
    return render_template("error.html", title="Something went wrong", code=500,
                           message="The request could not be completed. Check the terminal logs."), 500


if __name__ == "__main__":
    with app.app_context():
        init_db()
        seed_db()
    print("\\nUNIWORK ERP is starting. Open http://127.0.0.1:5000\\n")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
