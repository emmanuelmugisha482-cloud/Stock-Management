import base64
import calendar
import difflib
import hashlib
import io
import os
import re
import secrets
import smtplib
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import pandas as pd
import streamlit as st

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
except ImportError:  # only needed online (when DATABASE_URL is set)
    psycopg2 = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "stock.db")  # used only when no DATABASE_URL is set (local testing)
BG_IMAGE = os.path.join(BASE_DIR, "background", "cyarubare.jpg")
MANAGER, DISPENSER = "Store manager", "Dispensing"

try:
    st.set_page_config(page_title="CYARUBARE Health Center - Stock Management System", page_icon=":material/medication:", layout="wide")
except Exception:
    st.set_page_config(page_title="CYARUBARE Health Center - Stock Management System", layout="wide")


# ---------- database: PostgreSQL (Neon) online, SQLite locally ----------
def _secret(name):
    try:
        v = st.secrets[name]
    except Exception:
        return None
    return v if isinstance(v, str) else None


DATABASE_URL = _secret("DATABASE_URL") or os.getenv("DATABASE_URL")
IS_PG = bool(DATABASE_URL)
PK = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
IntegrityErrors = (sqlite3.IntegrityError,) + ((psycopg2.IntegrityError,) if psycopg2 else ())
_last_used = {}


@st.cache_resource
def _pool():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is missing. Add psycopg2-binary to requirements.txt.")
    return psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL, connect_timeout=20,
                                                 cursor_factory=psycopg2.extras.RealDictCursor)


def _checkout():
    """Get a working connection. Neon sleeps when idle, so test connections that have been unused for a while."""
    pool, last = _pool(), None
    for _ in range(3):
        raw = None
        try:
            raw = pool.getconn()
            if raw.closed or time.time() - _last_used.get(id(raw), 0) > 90:
                with raw.cursor() as cur:
                    cur.execute("SELECT 1")
                raw.rollback()
            return pool, raw
        except Exception as e:
            last = e
            if raw is not None:
                pool.putconn(raw, close=True)
            time.sleep(1.5)
    raise RuntimeError(f"Cannot reach the database right now. Please try again in a moment. ({last})")


class Conn:
    """Lets the same SQL (written with ? marks) run on SQLite and on PostgreSQL."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        params = tuple(p.item() if hasattr(p, "item") else p for p in params)  # numpy numbers -> python
        cur = self.raw.cursor()
        cur.execute(sql.replace("?", "%s") if IS_PG else sql, params)
        return cur


@contextmanager
def conn():
    if IS_PG:
        pool, raw = _checkout()
        try:
            yield Conn(raw)
            raw.commit()
        except BaseException:
            try:
                raw.rollback()
            except Exception:
                pass
            raise
        finally:
            _last_used[id(raw)] = time.time()
            pool.putconn(raw)
    else:
        raw = sqlite3.connect(DB)
        raw.row_factory = sqlite3.Row
        try:
            yield Conn(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS drugs(
        id {PK}, name TEXT UNIQUE NOT NULL, unit TEXT DEFAULT 'units', tracer INTEGER DEFAULT 0,
        main_qty INTEGER DEFAULT 0, disp_qty INTEGER DEFAULT 0, reorder_level INTEGER DEFAULT 0)""",
    f"""CREATE TABLE IF NOT EXISTS usage_log(
        id {PK}, drug_id INTEGER, qty INTEGER, used_on TEXT, by_user TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS requests(
        id {PK}, drug_id INTEGER, qty_requested INTEGER, qty_approved INTEGER, status TEXT DEFAULT 'Pending',
        requested_on TEXT, decided_on TEXT, note TEXT, batch INTEGER, requested_by TEXT, decided_by TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS stock_log(
        id {PK}, drug_id INTEGER, change_qty INTEGER, kind TEXT, note TEXT, by_user TEXT, on_date TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS users(
        id {PK}, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, email TEXT UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS app_settings(name TEXT PRIMARY KEY, val TEXT)""",
    """CREATE TABLE IF NOT EXISTS reset_codes(
        user_id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(used_on)",
    "CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)",
]


PG_MIGRATIONS = ["ALTER TABLE requests ADD COLUMN IF NOT EXISTS requested_by TEXT",
                 "ALTER TABLE requests ADD COLUMN IF NOT EXISTS decided_by TEXT"]


def first_manager():
    """The very first Store manager account comes from the app secrets (never from the code)."""
    try:
        s = dict(st.secrets["first_manager"])
    except Exception:
        s = {}
    return (s.get("username") or os.getenv("FIRST_MANAGER_USER"), s.get("password") or os.getenv("FIRST_MANAGER_PASSWORD"),
            s.get("email") or os.getenv("FIRST_MANAGER_EMAIL"))


def _legacy_sqlite(c):
    """Upgrade an old local stock.db made by earlier versions of this app."""
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "usage" in tables and "usage_log" not in tables:
        c.execute("ALTER TABLE usage RENAME TO usage_log")
    if "requests" in tables:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(requests)").fetchall()}
        for col in ("batch", "requested_by", "decided_by"):
            if col not in cols:
                c.execute(f"ALTER TABLE requests ADD COLUMN {col} {'INTEGER' if col == 'batch' else 'TEXT'}")
        c.execute("UPDATE requests SET batch=id WHERE batch IS NULL")


@st.cache_resource
def init_db():
    with conn() as c:
        if IS_PG:
            c.execute(";\n".join(SCHEMA + PG_MIGRATIONS))
        else:
            _legacy_sqlite(c)
            for s in SCHEMA:
                c.execute(s)
        c.execute("UPDATE users SET role=? WHERE role='Boss'", (MANAGER,))
        if c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
            u, p, e = first_manager()
            if u and p:
                c.execute("INSERT INTO users(username, password_hash, role, email) VALUES(?,?,?,?)",
                          (u.strip(), hash_pw(p), MANAGER, (e or "").strip().lower() or None))


def df(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        rows, cols = cur.fetchall(), [x[0] for x in cur.description]
    return pd.DataFrame([list(r.values()) if isinstance(r, dict) else list(r) for r in rows], columns=cols)


def run(sql, params=()):
    with conn() as c:
        c.execute(sql, params)


def users_exist():
    return int(df("SELECT COUNT(*) AS n FROM users")["n"][0]) > 0


# ---------- actions on stock and requests ----------
class Abort(Exception):
    pass


def decide_requests(decisions, user):
    """decisions = {request_id: (qty, reason)}. qty > 0 approves that quantity, qty <= 0 rejects that row.
    Rows that are not listed stay pending. All or nothing."""
    if not decisions:
        return None
    today, ids = str(date.today()), list(decisions)
    try:
        with conn() as c:
            rows = c.execute(f"""SELECT r.id, r.drug_id, d.name, d.main_qty FROM requests r
                                 JOIN drugs d ON d.id=r.drug_id
                                 WHERE r.status='Pending' AND r.id IN ({','.join('?' * len(ids))})""", ids).fetchall()
            if len(rows) != len(ids):
                raise Abort("Some of these requests were already handled or cancelled. Please refresh the page.")
            need = {}
            for r in rows:  # the same drug may be asked in several requests: check the total
                need[r["drug_id"]] = need.get(r["drug_id"], 0) + max(decisions[r["id"]][0], 0)
                if need[r["drug_id"]] > r["main_qty"]:
                    raise Abort(f"{r['name']}: main stock has only {r['main_qty']}.")
            for r in rows:
                q, why = decisions[r["id"]]
                if q > 0:
                    a = c.execute("UPDATE drugs SET main_qty=main_qty-?, disp_qty=disp_qty+? WHERE id=? AND main_qty>=?",
                                  (q, q, r["drug_id"], q)).rowcount
                    b = c.execute("UPDATE requests SET status='Approved', qty_approved=?, decided_on=?, decided_by=? "
                                  "WHERE id=? AND status='Pending'", (q, today, user, r["id"])).rowcount
                else:
                    a = 1
                    b = c.execute("UPDATE requests SET status='Rejected', qty_approved=0, decided_on=?, decided_by=?, note=? "
                                  "WHERE id=? AND status='Pending'", (today, user, why or "Not approved", r["id"])).rowcount
                if not (a and b):
                    raise Abort("Stock or requests changed while you were working. Please refresh and try again.")
    except Abort as e:
        return str(e)
    return None


def create_request(items, user):
    """items = {drug_id: qty}. All drugs go into one request; its number is the id of its first line."""
    first = True
    with conn() as c:
        for did, q in items.items():
            if first:
                b = c.execute("INSERT INTO requests(drug_id, qty_requested, requested_on, batch, requested_by) "
                              "VALUES(?,?,?,0,?) RETURNING id", (did, q, str(date.today()), user)).fetchone()["id"]
                c.execute("UPDATE requests SET batch=? WHERE id=?", (b, b))
                first = False
            else:
                c.execute("INSERT INTO requests(drug_id, qty_requested, requested_on, batch, requested_by) "
                          "VALUES(?,?,?,?,?)", (did, q, str(date.today()), b, user))
    return b


def update_pending(user, changes, removes):
    """Dispensing can change or delete their OWN requests while still pending. Returns how many lines could not
    be changed because the store manager already decided them."""
    skipped = 0
    with conn() as c:
        for rid in removes:
            skipped += c.execute("DELETE FROM requests WHERE id=? AND status='Pending' AND requested_by=?",
                                 (rid, user)).rowcount == 0
        for rid, q in changes.items():
            skipped += c.execute("UPDATE requests SET qty_requested=? WHERE id=? AND status='Pending' AND requested_by=?",
                                 (q, rid, user)).rowcount == 0
    return skipped


def cancel_request(batch, user):
    with conn() as c:
        return c.execute("DELETE FROM requests WHERE batch=? AND status='Pending' AND requested_by=?",
                         (batch, user)).rowcount


def record_usage(drug_id, qty, day, user):
    with conn() as c:
        if c.execute("UPDATE drugs SET disp_qty=disp_qty-? WHERE id=? AND disp_qty>=?", (qty, drug_id, qty)).rowcount == 0:
            have = c.execute("SELECT disp_qty FROM drugs WHERE id=?", (drug_id,)).fetchone()["disp_qty"]
            return f"Dispensing stock only has {have}."
        c.execute("INSERT INTO usage_log(drug_id, qty, used_on, by_user) VALUES(?,?,?,?)", (drug_id, qty, str(day), user))
    return None


# ---------- history: nothing is deleted automatically, the Store manager clears it ----------
def months_ago(n, today=None):
    t = today or date.today()
    y, m = divmod(t.year * 12 + t.month - 1 - n, 12)
    m += 1
    return date(y, m, min(t.day, calendar.monthrange(y, m)[1]))


def get_setting(name):
    r = df("SELECT val FROM app_settings WHERE name=?", (name,))
    return r["val"][0] if len(r) else None


def set_setting(name, val):
    run("INSERT INTO app_settings(name, val) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET val=excluded.val",
        (name, val))


def cleared_before():
    """The date before which history was cleared (reports older than this are incomplete), or None."""
    v = get_setting("history_cleared_before")
    return date.fromisoformat(v) if v else None


def usage_floor():
    """Earliest date allowed when recording usage."""
    f, c = date.today() - timedelta(days=366), cleared_before()
    return max(f, c) if c else f


def count_history(cutoff):
    """How many records a clear would delete. cutoff = None means all history."""
    if cutoff is None:
        r = df("""SELECT (SELECT COUNT(*) FROM usage_log) AS "u", (SELECT COUNT(*) FROM requests) AS "r",
                         (SELECT COUNT(*) FROM stock_log) AS "l\"""")
    else:
        c = str(cutoff)
        r = df("""SELECT (SELECT COUNT(*) FROM usage_log WHERE used_on < ?) AS "u",
                         (SELECT COUNT(*) FROM requests WHERE requested_on < ? AND status != 'Pending') AS "r",
                         (SELECT COUNT(*) FROM stock_log WHERE on_date < ?) AS "l\"""", (c, c, c))
    return {"usage records": int(r["u"][0]), "requests": int(r["r"][0]), "stock history lines": int(r["l"][0])}


def clear_history(cutoff):
    """Delete activity records older than `cutoff` (None = all of them). Stock quantities, the drug list and
    users are never touched. Requests still waiting for a decision are kept unless everything is cleared."""
    with conn() as c:
        if cutoff is None:
            out = {"usage records": c.execute("DELETE FROM usage_log").rowcount,
                   "requests": c.execute("DELETE FROM requests").rowcount,
                   "stock history lines": c.execute("DELETE FROM stock_log").rowcount}
        else:
            s = str(cutoff)
            out = {"usage records": c.execute("DELETE FROM usage_log WHERE used_on < ?", (s,)).rowcount,
                   "requests": c.execute("DELETE FROM requests WHERE requested_on < ? AND status != 'Pending'",
                                         (s,)).rowcount,
                   "stock history lines": c.execute("DELETE FROM stock_log WHERE on_date < ?", (s,)).rowcount}
    mark = cutoff or date.today()
    old = cleared_before()
    set_setting("history_cleared_before", str(max(mark, old) if old else mark))
    return out


def storage_info():
    r = df("""SELECT (SELECT COUNT(*) FROM usage_log) AS "u", (SELECT COUNT(*) FROM requests) AS "r",
                     (SELECT COUNT(*) FROM stock_log) AS "l", (SELECT MIN(used_on) FROM usage_log) AS "uo",
                     (SELECT MIN(requested_on) FROM requests) AS "ro", (SELECT MIN(on_date) FROM stock_log) AS "lo\"""")
    oldest = [x for x in (r["uo"][0], r["ro"][0], r["lo"][0]) if isinstance(x, str)]
    try:
        size = int(df("SELECT pg_database_size(current_database()) AS b")["b"][0]) if IS_PG else os.path.getsize(DB)
        mb = round(size / 1_048_576, 1)
    except Exception:
        mb = None
    return {"usage records": int(r["u"][0]), "requests": int(r["r"][0]), "stock history lines": int(r["l"][0]),
            "oldest": min(oldest) if oldest else None, "mb": mb}


# ---------- reports and Excel export ----------
def report_table(start, end, search="", person=None):
    """One row per drug that had activity in the period. `person` limits it to one dispenser."""
    s, e = str(start), str(end)
    rp, rpar = ("AND r.requested_by=?", (person,)) if person else ("", ())
    up, upar = ("AND u.by_user=?", (person,)) if person else ("", ())
    req = df(f"""SELECT d.name AS "Drug", SUM(r.qty_requested) AS "Requested" FROM requests r
                JOIN drugs d ON d.id=r.drug_id WHERE r.requested_on BETWEEN ? AND ? {rp} GROUP BY d.id, d.name""", (s, e) + rpar)
    rec = df(f"""SELECT d.name AS "Drug", r.qty_approved AS "Qty", r.decided_by AS "By" FROM requests r
                JOIN drugs d ON d.id=r.drug_id WHERE r.status='Approved' AND r.decided_on BETWEEN ? AND ? {rp}""",
             (s, e) + rpar)
    use = df(f"""SELECT d.name AS "Drug", u.qty AS "Qty", u.by_user AS "By" FROM usage_log u
                JOIN drugs d ON d.id=u.drug_id WHERE u.used_on BETWEEN ? AND ? {up}""", (s, e) + upar)
    # main stock at the END of the period = stock now, undoing every change made after that day
    main = df("""SELECT d.name AS "Drug", d.main_qty
                 - COALESCE((SELECT SUM(l.change_qty) FROM stock_log l WHERE l.drug_id=d.id AND l.on_date > ?), 0)
                 + COALESCE((SELECT SUM(r.qty_approved) FROM requests r
                             WHERE r.drug_id=d.id AND r.status='Approved' AND r.decided_on > ?), 0) AS "Main",
                 d.disp_qty AS "Disp"
                 FROM drugs d""", (e, e))
    names = sorted(n for n in set(req["Drug"]) | set(rec["Drug"]) | set(use["Drug"]) if search in n.lower())
    if not names:
        return pd.DataFrame()
    rq, mn, ds = req.set_index("Drug")["Requested"], main.set_index("Drug")["Main"], main.set_index("Drug")["Disp"]
    rc, us = rec.groupby("Drug")["Qty"].sum(), use.groupby("Drug")["Qty"].sum()

    def who(frame, n):
        return ", ".join(sorted({x for x in frame.loc[frame["Drug"] == n, "By"] if isinstance(x, str) and x}))

    return pd.DataFrame([{
        "Drug": n, "Requested": int(rq.get(n, 0)), "Main stock (end of period)": int(mn.get(n, 0)),
        "Received in dispensing": int(rc.get(n, 0)), "Dispensed": int(us.get(n, 0)),
        "Dispensing stock (now)": int(ds.get(n, 0)), "Dispensed by": who(use, n), "Approved by": who(rec, n)} for n in names])


def export_sheets():
    """Everything except user accounts (no passwords or emails leave the database)."""
    return {
        "Stock": df("""SELECT name AS "Drug", main_qty AS "Main stock", disp_qty AS "Dispensing stock",
                       CASE WHEN tracer=1 THEN 'Yes' ELSE 'No' END AS "Tracer", reorder_level AS "Low-stock level"
                       FROM drugs ORDER BY name"""),
        "Usage": df("""SELECT u.used_on AS "Date", d.name AS "Drug", u.qty AS "Quantity", u.by_user AS "Dispensed by"
                       FROM usage_log u JOIN drugs d ON d.id=u.drug_id ORDER BY u.used_on DESC, d.name"""),
        "Requests": df("""SELECT r.batch AS "Request #", r.requested_on AS "Requested on", r.requested_by AS "Requested by",
                          d.name AS "Drug", r.qty_requested AS "Qty requested", r.qty_approved AS "Qty approved",
                          r.status AS "Status", r.decided_on AS "Decided on", r.decided_by AS "Decided by",
                          r.note AS "Note" FROM requests r JOIN drugs d ON d.id=r.drug_id
                          ORDER BY r.batch DESC, d.name"""),
        "Stock movements": df("""SELECT l.on_date AS "Date", d.name AS "Drug", l.kind AS "Type",
                                 l.change_qty AS "Change", l.note AS "Note", l.by_user AS "By"
                                 FROM stock_log l JOIN drugs d ON d.id=l.drug_id ORDER BY l.id DESC""")}


def to_excel(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, frame in sheets.items():
            frame.to_excel(w, sheet_name=name[:31], index=False)
            ws = w.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for i, col in enumerate(frame.columns, 1):
                width = max([len(str(col))] + [len(str(v)) for v in frame[col].head(300)]) + 2
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width, 60)
    return buf.getvalue()


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------- user management (Store manager) ----------
def add_user(username, role, email, password):
    username, email = username.strip(), email.strip().lower()
    if not username or " " in username:
        return "Enter a user name without spaces."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if email and not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    try:
        with conn() as c:
            if c.execute("SELECT id FROM users WHERE lower(username)=lower(?)", (username,)).fetchone():
                return "That user name is already used."
            c.execute("INSERT INTO users(username, password_hash, role, email) VALUES(?,?,?,?)",
                      (username, hash_pw(password), role, email or None))
    except IntegrityErrors:
        return "That email is already used by another account."
    return None


def set_user_password(uid, password):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    run("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(password), uid))
    return None


def delete_user(uid, me):
    if uid == me:
        return "You cannot delete your own account."
    with conn() as c:
        row = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            return "User not found."
        if row["role"] == MANAGER and c.execute("SELECT COUNT(*) AS n FROM users WHERE role=?",
                                                (MANAGER,)).fetchone()["n"] <= 1:
            return "You cannot delete the last Store manager."
        c.execute("DELETE FROM reset_codes WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    return None


# ---------- helpers ----------
def drugs_df():
    return df("SELECT * FROM drugs ORDER BY name")


def status(qty, level):
    return "Out" if qty <= 0 else ("Low" if qty <= level else "OK")


def style_status(frame, col="Status"):
    """Colour the Status column (red = out, orange = low, green = ok) instead of using emoji."""
    colors = {"Out": "background-color:#fde2e2;color:#7a1f1f", "Low": "background-color:#fff0d6;color:#7a4a00",
              "OK": "background-color:#e3f5e8;color:#1b5e30"}
    try:
        sty = frame.style
        return (getattr(sty, "map", None) or sty.applymap)(lambda v: colors.get(v, ""), subset=[col])
    except Exception:
        return frame


def search_box(key, label="Search drug", placeholder="Type part of a name…"):
    """A visible search bar for a table. Returns the typed text in lower case (press Enter to apply)."""
    return st.text_input(label, placeholder=placeholder, key=key).strip().lower()


def match(frame, cols, text):
    """Keep the rows where any of the columns contains the text (capitals are ignored)."""
    if not text or frame.empty:
        return frame
    hit = pd.Series(False, index=frame.index)
    for c in cols:
        hit |= frame[c].astype(str).str.lower().str.contains(text, regex=False)
    return frame[hit]


def dispenser_select(key, label="Dispenser"):
    """Drop-down with all dispensers. Returns the user name, or None for everybody."""
    names = df("SELECT username FROM users WHERE role=? ORDER BY username", (DISPENSER,))["username"].tolist()
    pick = st.selectbox(label, ["All dispensers"] + names, key=key)
    return None if pick in (None, "All dispensers") else pick


def wide(fn, *a, **kw):
    """Full-width table/editor that works on old and new Streamlit versions."""
    try:
        return fn(*a, width="stretch", **kw)
    except Exception:
        return fn(*a, use_container_width=True, **kw)


def K(name):  # widget keys that change after each save, so the form clears itself
    return f"{name}{st.session_state.get('rk', 0)}"


def done(msg):
    st.session_state.flash = msg
    st.session_state.rk = st.session_state.get("rk", 0) + 1
    st.rerun()


def show_flash():
    m = st.session_state.pop("flash", None)
    if m:
        st.success(m)


def period_range(period, ref):
    """Daily = that day. Weekly = Monday to Sunday of that week. Monthly = the calendar month."""
    if period == "Daily":
        return ref, ref
    if period == "Weekly":
        start = ref - timedelta(days=ref.weekday())
        return start, start + timedelta(days=6)
    start = ref.replace(day=1)
    return start, (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def usage_by_drug(start, end, tracer_only=False, search="", person=None):
    extra, params = "", [str(start), str(end)]
    if tracer_only:
        extra += " AND d.tracer=1"
    if person:
        extra += " AND u.by_user=?"
        params.append(person)
    if search:
        extra += " AND LOWER(d.name) LIKE ?"
        params.append(f"%{search.lower()}%")
    return df(f"""SELECT d.name AS "Drug", SUM(u.qty) AS "Used" FROM usage_log u JOIN drugs d ON d.id=u.drug_id
                  WHERE u.used_on BETWEEN ? AND ?{extra} GROUP BY d.id, d.name ORDER BY "Used" DESC, "Drug" """,
              tuple(params))


def usage_chart(top):
    """Horizontal bar chart, biggest at the top."""
    try:
        import altair as alt
    except ImportError:
        st.bar_chart(top.set_index("Drug"))
        return
    chart = alt.Chart(top).mark_bar(color="#2b7bb9").encode(
        x=alt.X("Used:Q", title="Quantity used"),
        y=alt.Y("Drug:N", sort="-x", title=None, axis=alt.Axis(labelLimit=400)),
        tooltip=["Drug", "Used"]).properties(height=34 * len(top) + 50)
    wide(st.altair_chart, chart)


def log_stock(c, drug_id, change, kind, note, user):
    c.execute("INSERT INTO stock_log(drug_id, change_qty, kind, note, by_user, on_date) VALUES(?,?,?,?,?,?)",
              (drug_id, change, kind, note, user, str(date.today())))


def receive_stock(drug_id, qty, note, user):
    with conn() as c:
        c.execute("UPDATE drugs SET main_qty=main_qty+? WHERE id=?", (qty, drug_id))
        log_stock(c, drug_id, qty, "Received", note, user)


def reconcile(counts, reason, user):
    """counts = {drug_id: counted_qty}. Sets main stock to the counted value and logs the difference."""
    changed = 0
    with conn() as c:
        for did, counted in counts.items():
            cur = c.execute("SELECT main_qty FROM drugs WHERE id=?", (did,)).fetchone()["main_qty"]
            if counted != cur:
                c.execute("UPDATE drugs SET main_qty=? WHERE id=?", (counted, did))
                log_stock(c, did, counted - cur, "Reconciliation",
                          f"{reason} (system {cur}, counted {counted})", user)
                changed += 1
    return changed


NEW = "New drug (add to list)"


def key_of(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def nums_of(s):
    return re.findall(r"\d+(?:\.\d+)?", str(s))


def plan_receipt(raw, names):
    """Read an Excel table and decide, for each row, which existing drug it belongs to (or NEW).
    Same name (ignoring capitals, spaces, symbols) = exact match. A similar name is only suggested when the
    strength numbers are identical, so 250 MG is never matched to 500 MG."""
    cols = {str(c).strip().lower(): c for c in raw.columns}
    name_col = next((cols[k] for k in ("item name", "name", "drug name", "drug", "item") if k in cols), None)
    if name_col is None:
        return None, "I could not find a column called 'Item Name'."
    qty_col = next((cols[k] for k in ("quantity", "qty", "received", "stock") if k in cols), None)
    exact = {}
    for n in names:
        exact.setdefault(key_of(n), n)
    merged = {}
    for _, r in raw.iterrows():
        nm = str(r[name_col]).strip()
        if not nm or nm.lower() == "nan":
            continue
        q = pd.to_numeric(r[qty_col], errors="coerce") if qty_col is not None else 0
        q = 0 if pd.isna(q) else max(int(q), 0)
        k = key_of(nm)
        if k in merged:
            merged[k][1] += q
        else:
            merged[k] = [nm, q]
    if not merged:
        return None, "The file has no drug names."
    rows = []
    for k, (nm, q) in merged.items():
        if k in exact:
            target, chk, rank = exact[k], "Same name", 2
        else:
            cands = [n for n in names if nums_of(n) == nums_of(nm)]
            m = difflib.get_close_matches(k, [key_of(n) for n in cands], n=1, cutoff=0.85)
            if m:
                target, chk, rank = next(n for n in cands if key_of(n) == m[0]), "Similar name: please check", 0
            else:
                target, chk, rank = NEW, "New drug", 1
        rows.append({"Check": chk, "Name in Excel": nm, "Quantity": q, "Add to": target, "_r": rank})
    rows.sort(key=lambda r: r["_r"])
    plan = pd.DataFrame(rows).drop(columns="_r")
    info = None if qty_col is not None else "No Quantity column found: drugs will only be added to the list (quantity 0)."
    return plan, info


def apply_receipt(plan, note, user):
    """Add each quantity to its drug in main stock; create the drug first if it is new."""
    matched = new = total = 0
    with conn() as c:
        for _, r in plan.iterrows():
            q = 0 if pd.isna(r["Quantity"]) else int(r["Quantity"])
            row = None if r["Add to"] == NEW else c.execute("SELECT id FROM drugs WHERE name=?", (r["Add to"],)).fetchone()
            if row is None:
                row = c.execute("SELECT id FROM drugs WHERE name=?", (r["Name in Excel"],)).fetchone()
            if row is None:
                did = c.execute("INSERT INTO drugs(name, unit) VALUES(?, 'units') RETURNING id",
                                (r["Name in Excel"],)).fetchone()["id"]
                new += 1
            else:
                did = row["id"]
                matched += 1
            if q > 0:
                c.execute("UPDATE drugs SET main_qty=main_qty+? WHERE id=?", (q, did))
                log_stock(c, did, q, "Received", note or "Excel upload", user)
                total += q
    return matched, new, total


def template_bytes():
    b = io.BytesIO()
    pd.DataFrame({"Item Name": ["PARACETAMOL 500MG TABLET"], "Quantity": [100]}).to_excel(b, index=False)
    return b.getvalue()


# ---------- passwords & accounts ----------
def hash_pw(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return salt.hex() + "$" + dk.hex()


def check_pw(pw, stored):
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000)
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def authenticate(username, password):
    with conn() as c:
        u = c.execute("SELECT * FROM users WHERE lower(username)=lower(?)", (username.strip(),)).fetchone()
    return u if u and check_pw(password, u["password_hash"]) else None


def get_user(uid):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_email(uid, email):
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    try:
        run("UPDATE users SET email=? WHERE id=?", (email, uid))
    except IntegrityErrors:
        return "That email is already used by another account."
    return None


# ---------- email & password reset ----------
def smtp_conf():
    try:
        s = dict(st.secrets["smtp"])
    except Exception:
        s = {}
    s = {
        "host": s.get("host") or os.getenv("SMTP_HOST"),
        "port": int(s.get("port") or os.getenv("SMTP_PORT") or 465),
        "user": s.get("user") or os.getenv("SMTP_USER"),
        "password": s.get("password") or os.getenv("SMTP_PASSWORD"),
        "sender": s.get("sender") or os.getenv("SMTP_SENDER"),
    }
    return s if s["host"] and s["user"] and s["password"] else None


def send_email(to, subject, body):
    cfg = smtp_conf()
    if not cfg:
        raise RuntimeError("Email is not configured")
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = cfg["sender"] or cfg["user"]
    msg.set_content(body)
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20) as s:
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


def request_reset(email):
    """Create a 6-digit code (valid 15 min) and email it. Returns (found, code, error)."""
    with conn() as c:
        u = c.execute("SELECT id, username FROM users WHERE lower(email)=lower(?)", (email.strip(),)).fetchone()
        if not u:
            return False, None, None
        code = f"{secrets.randbelow(1_000_000):06d}"
        c.execute("INSERT INTO reset_codes(user_id, code_hash, expires_at, attempts) VALUES(?,?,?,0) "
                  "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash, "
                  "expires_at=excluded.expires_at, attempts=0",
                  (u["id"], hash_pw(code), (datetime.now() + timedelta(minutes=15)).isoformat()))
    try:
        send_email(
            email.strip(),
            "CYARUBARE Health Center - password reset code",
            f"Hello {u['username']},\n\nYour password reset code is: {code}\n"
            "It expires in 15 minutes. If you did not ask for this, ignore this email.\n",
        )
    except Exception as e:
        return True, code, str(e)
    return True, code, None


def reset_password(email, code, new_pw):
    with conn() as c:
        u = c.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email.strip(),)).fetchone()
        r = c.execute("SELECT * FROM reset_codes WHERE user_id=?", (u["id"],)).fetchone() if u else None
        if not r or datetime.fromisoformat(r["expires_at"]) < datetime.now() or r["attempts"] >= 5:
            return "The code is invalid or has expired. Please request a new one."
        if not check_pw(code.strip(), r["code_hash"]):
            c.execute("UPDATE reset_codes SET attempts=attempts+1 WHERE user_id=?", (u["id"],))
            return "Wrong code."
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(new_pw), u["id"]))
        c.execute("DELETE FROM reset_codes WHERE user_id=?", (u["id"],))
    return None


# ---------- protection against password guessing ----------
@st.cache_resource
def _fails():
    return {}


def is_locked(u):
    return _fails().get(u.strip().lower(), (0, 0))[1] > time.time()


def note_fail(u):
    k = u.strip().lower()
    n = _fails().get(k, (0, 0))[0] + 1
    _fails()[k] = (0, time.time() + 300) if n >= 5 else (n, 0)


def clear_fails(u):
    _fails().pop(u.strip().lower(), None)


# ---------- login page ----------
@st.cache_resource
def bg_uri():
    try:
        from PIL import Image
        img = Image.open(BG_IMAGE).convert("RGB")
        img.thumbnail((1400, 1400))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


LOGIN_CSS = """
<style>
header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stSidebar"] {display:none !important;}
[data-testid="stApp"], [data-testid="stAppViewContainer"] {background:#ffffff !important;}
.left-panel {position:fixed; top:0; left:0; width:50vw; height:100vh; z-index:1;
  background:#8ec9f0 url("__BG__") center/cover no-repeat;}
.left-panel .overlay {position:absolute; inset:0; display:flex; flex-direction:column; justify-content:flex-end;
  padding:3.5rem; background:linear-gradient(to top, rgba(168,213,245,.97) 0%, rgba(168,213,245,.65) 40%, rgba(168,213,245,0) 75%);}
.left-panel h1 {color:#0a3a66 !important; font-size:2.6rem; font-weight:800; line-height:1.15; margin:0; padding:0;}
.left-panel p {color:#1d5a8f !important; font-size:1.5rem; margin:.5rem 0 0 0;}
[data-testid="stMainBlockContainer"], .block-container {margin-left:50vw !important; width:50vw !important;
  max-width:50vw !important; padding:14vh 7vw 2rem 7vw !important;}
[data-testid="stApp"] h2, [data-testid="stApp"] label, [data-testid="stApp"] p, [data-testid="stApp"] span {color:#1f2937;}
.sub {color:#6b7280 !important; margin-bottom:1.2rem;}
[data-testid="stTextInput"] input {background:#f3f4f6 !important; color:#111827 !important;}
.stButton button, [data-testid="stFormSubmitButton"] button {width:100%; border-radius:8px;}
[data-testid="stFormSubmitButton"] button {background:#2b7bb9; border:0;}
[data-testid="stFormSubmitButton"] button p {color:#fff !important; font-weight:600;}
@media (max-width: 800px) {
  .left-panel {position:relative; width:100vw; height:30vh;}
  .left-panel .overlay {padding:1.2rem;} .left-panel h1 {font-size:1.5rem;} .left-panel p {font-size:1rem;}
  [data-testid="stMainBlockContainer"], .block-container {margin-left:0 !important; width:100% !important;
    max-width:100% !important; padding:1.5rem !important;}
}
</style>
"""

LEFT_HTML = ('<div class="left-panel"><div class="overlay"><h1>CYARUBARE Health Center</h1>'
             '<p>Stock Management System</p></div></div>')


def go(view):
    st.session_state.view = view
    st.rerun()


def login():
    st.markdown(LOGIN_CSS.replace("__BG__", bg_uri()), unsafe_allow_html=True)
    st.markdown(LEFT_HTML, unsafe_allow_html=True)
    view = st.session_state.get("view", "login")

    if view == "login":
        st.markdown("## Welcome back")
        st.markdown('<div class="sub">Sign in to manage the stock.</div>', unsafe_allow_html=True)
        if not users_exist():
            st.warning("No accounts exist yet. The owner must add a [first_manager] section to the app "
                       "secrets (see the README), then reload this page.")
        with st.form("login_form"):
            u = st.text_input("User name")
            p = st.text_input("Password", type="password")
            ok = st.form_submit_button("Log in")
        if ok:
            if is_locked(u):
                st.error("Too many wrong attempts. Please wait 5 minutes and try again.")
            else:
                user = authenticate(u, p)
                if user:
                    clear_fails(u)
                    st.session_state.update(user=user["username"], role=user["role"], uid=user["id"])
                    st.rerun()
                else:
                    note_fail(u)
                    st.error("Wrong user name or password.")
        if st.button("Forgot password?"):
            go("forgot")

    elif view == "forgot":
        st.markdown("## Forgot password")
        st.markdown('<div class="sub">Enter the email linked to your account and we will send you a code.</div>',
                    unsafe_allow_html=True)
        with st.form("forgot_form"):
            email = st.text_input("Email")
            send = st.form_submit_button("Send reset code")
        if send:
            if not EMAIL_RE.match(email.strip()):
                st.error("Please enter a valid email address.")
            else:
                found, code, err = request_reset(email)
                dev = os.getenv("STOCK_DEV_SHOW_CODE") == "1"
                if found and err and not dev:
                    st.error("The email could not be sent. Please contact the administrator.")
                else:
                    st.session_state.reset_email = email.strip()
                    st.session_state.dev_code = code if (found and err) else None
                    go("reset")
        if st.button("Back to log in"):
            go("login")

    elif view == "reset":
        st.markdown("## Reset password")
        st.markdown(f'<div class="sub">Enter the code sent to {st.session_state.get("reset_email", "your email")}.</div>',
                    unsafe_allow_html=True)
        st.success("If that email is registered, a 6-digit code has been sent.")
        if st.session_state.get("dev_code"):
            st.info(f"DEV MODE - email not sent. Your code is {st.session_state.dev_code}")
        with st.form("reset_form"):
            code = st.text_input("6-digit code")
            n1 = st.text_input("New password", type="password")
            n2 = st.text_input("Confirm new password", type="password")
            ok = st.form_submit_button("Change password")
        if ok:
            if len(n1) < 8:
                st.error("Password must be at least 8 characters.")
            elif n1 != n2:
                st.error("Passwords do not match.")
            else:
                err = reset_password(st.session_state.get("reset_email", ""), code, n1)
                if err:
                    st.error(err)
                else:
                    st.session_state.view = "login"
                    st.toast("Password changed. You can log in now.")
                    st.rerun()
        if st.button("Back to log in"):
            go("login")


def email_prompt():
    """Shown after login when the account has no recovery email yet."""
    st.title("Add your recovery email")
    st.write("This email is used to reset your password if you forget it.")
    with st.form("email_form"):
        email = st.text_input("Email")
        save = st.form_submit_button("Save email")
    if save:
        err = set_email(st.session_state.uid, email)
        if err:
            st.error(err)
        else:
            st.rerun()
    if st.button("Skip for now"):
        st.session_state.skip_email = True
        st.rerun()


# ---------- navigation: menu in the sidebar, user + log out at the bottom of the sidebar ----------
def side_menu(items, counts=None):
    """Sidebar menu. items = [(key, label, material_icon)]. Remembers the choice. Returns (key, label)."""
    skey = f"menu_{st.session_state.role}"
    if st.session_state.get(skey) not in [k for k, _, _ in items]:
        st.session_state[skey] = items[0][0]
    with st.sidebar:
        for k, label, icon in items:
            n = (counts or {}).get(k)
            text = f":material/{icon}: {label}" + (f" ({n})" if n else "")
            if st.button(text, key=f"nav_{k}", type="primary" if st.session_state[skey] == k else "secondary"):
                st.session_state[skey] = k
                st.rerun()
    return st.session_state[skey], next(label for k, label, _ in items if k == st.session_state[skey])


def header(title):
    st.title(title)


def user_box():
    """Small user name at the very bottom of the sidebar. Click it to see the role and the Log out button."""
    with st.container(key="userbox"):
        with st.popover(f":material/account_circle: {st.session_state.user}"):
            st.caption(st.session_state.role)
            if st.button(":material/logout: Log out", type="primary", key="logout"):
                st.session_state.clear()
                st.rerun()


# ---------- store manager ----------
def manager_page():
    user = st.session_state.user
    pend = int(df("SELECT COUNT(DISTINCT batch) AS n FROM requests WHERE status='Pending'")["n"][0])
    sec, title = side_menu([("dashboard", "Dashboard", "dashboard"), ("requests", "Requests", "inbox"),
                            ("receive", "Receive stock", "add_box"), ("reconcile", "Reconcile main stock", "fact_check"),
                            ("reports", "Reports", "assessment"), ("settings", "Settings", "settings")],
                           {"requests": pend})
    header(title)
    show_flash()
    d = drugs_df()

    if sec == "dashboard":
        d["Status"] = [status(q, l) for q, l in zip(d.disp_qty, d.reorder_level)]
        used = df("SELECT COUNT(DISTINCT drug_id) AS k FROM usage_log WHERE used_on=?", (str(date.today()),))["k"][0]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Pending requests", pend)
        m2.metric("Drugs used today", int(used))
        m3.metric("Low / out in dispensing", int((d.Status != "OK").sum()))
        m4.metric("Drugs in list", len(d))

        st.subheader("Stock levels")
        s1, s2 = st.columns([3, 2])
        show = s1.radio("Show", ["All", "Tracer drugs", "Low or out"], horizontal=True)
        with s2:
            q = search_box("q_stock")
        v = d[d.tracer == 1] if show == "Tracer drugs" else d[d.Status != "OK"] if show == "Low or out" else d
        v = match(v, ["name"], q)
        if q and v.empty:
            st.caption("No drug matches your search.")
        if d.empty:
            st.info("No drugs yet. Go to the Receive stock tab and upload your Excel list.")
        wide(st.dataframe, style_status(v[["name", "main_qty", "disp_qty", "Status"]].rename(
            columns={"name": "Drug", "main_qty": "Main stock", "disp_qty": "Dispensing stock"})), hide_index=True)

        st.subheader("Drugs used")
        p1, p2, p3 = st.columns([2, 1, 1])
        period = p1.radio("Period", ["Daily", "Weekly", "Monthly"], horizontal=True)
        ref = p2.date_input("Date", date.today())
        tracer_only = p3.toggle("Tracer drugs only")
        start, end = period_range(period, ref)
        st.caption(f"{start:%d %b %Y}" + ("" if start == end else f" to {end:%d %b %Y}"))
        u1, u2 = st.columns(2)
        with u1:
            q_used = search_box("q_used")
        with u2:
            who_used = dispenser_select("p_used")
        used_df = usage_by_drug(start, end, tracer_only, q_used, who_used)
        if used_df.empty:
            st.info("No drugs used in this period.")
        else:
            if len(used_df) > 10:
                st.caption(f"Showing the top 10 of {len(used_df)} drugs used.")
            usage_chart(used_df.head(10))
            with st.expander("See all drugs used in this period"):
                wide(st.dataframe, used_df, hide_index=True)

        with st.expander("Tracer drugs: trend in the last 30 days"):
            tr = df("""SELECT u.used_on AS "day", d.name AS "name", SUM(u.qty) AS "used" FROM usage_log u
                       JOIN drugs d ON d.id=u.drug_id WHERE d.tracer=1 AND u.used_on >= ?
                       GROUP BY u.used_on, d.name""", (str(date.today() - timedelta(days=30)),))
            if len(tr):
                st.line_chart(tr.pivot(index="day", columns="name", values="used").fillna(0))
            else:
                st.info("No tracer drug usage yet. Mark tracer drugs in the Receive stock tab.")

    if sec == "requests":
        pending = df("""SELECT r.id, r.batch, r.requested_by, d.name, d.main_qty, d.disp_qty, r.qty_requested
                        FROM requests r JOIN drugs d ON d.id=r.drug_id WHERE r.status='Pending'
                        ORDER BY r.batch, d.name""")
        if pending.empty:
            st.success("No pending requests.")
        else:
            f1, f2 = st.columns(2)
            with f1:
                q_req = search_box("q_req")
            with f2:
                who_req = dispenser_select("p_req", "Requested by")
            shown = match(pending, ["name"], q_req)
            if who_req:
                shown = shown[shown.requested_by == who_req]
        if not pending.empty and shown.empty:
            st.info("No pending request matches your search.")
        elif not pending.empty:
            st.write("Every row is one drug. Choose **Approve**, **Reject** or **Decide later** for each row, "
                     "change the quantity to give if needed, then press **Confirm decisions**.")
            tbl = pd.DataFrame({
                "Request #": shown.batch.astype(int).values, "By": shown.requested_by.fillna("").values,
                "Drug": shown.name.values,
                "Requested": shown.qty_requested.values, "In dispensing": shown.disp_qty.values,
                "In main stock": shown.main_qty.values,
                "Decision": ["Approve" if m > 0 else "Reject" for m in shown.main_qty],
                "Quantity to give": [min(a, m) for a, m in zip(shown.qty_requested, shown.main_qty)],
                "Reason if rejected": ""})
            ed = wide(st.data_editor, tbl, hide_index=True,
                      key=f"dec{K('d')}_{abs(hash(tuple(int(i) for i in shown.id)))}",
                      disabled=["Request #", "By", "Drug", "Requested", "In dispensing", "In main stock"],
                      column_config={
                          "Decision": st.column_config.SelectboxColumn(
                              options=["Approve", "Reject", "Decide later"], required=True),
                          "Quantity to give": st.column_config.NumberColumn(min_value=0, step=1)})
            if st.button(":material/check: Confirm decisions", type="primary"):
                decisions, problems = {}, []
                for rid, drug, dec, q, why in zip(shown.id, ed["Drug"], ed["Decision"],
                                                  ed["Quantity to give"], ed["Reason if rejected"]):
                    q = 0 if pd.isna(q) else int(q)
                    if dec == "Approve":
                        if q <= 0:
                            problems.append(drug)
                        else:
                            decisions[int(rid)] = (q, "")
                    elif dec == "Reject":
                        decisions[int(rid)] = (0, why.strip() if isinstance(why, str) and why.strip() else "Not approved")
                if problems:
                    st.error("Enter a quantity above 0, or choose Reject, for: " + ", ".join(problems))
                elif not decisions:
                    st.warning("Nothing to confirm: every row is set to Decide later.")
                else:
                    err = decide_requests(decisions, user)
                    if err:
                        st.error(err)
                    else:
                        ok = sum(q > 0 for q, _ in decisions.values())
                        done(f"{ok} approved, {len(decisions) - ok} rejected, "
                             f"{len(pending) - len(decisions)} left for later.")

        with st.expander("Past requests"):
            q_past = search_box("q_past", "Search drug or person")
            past = df("""SELECT r.batch AS "Request #", r.requested_by AS "By", d.name AS "Drug",
                r.qty_requested AS "Requested", r.qty_approved AS "Approved", r.status AS "Status",
                r.requested_on AS "Asked", r.decided_on AS "Decided", r.decided_by AS "Decided by",
                r.note AS "Note" FROM requests r JOIN drugs d ON d.id=r.drug_id WHERE r.status!='Pending'
                ORDER BY r.batch DESC, d.name LIMIT 1000""")
            wide(st.dataframe, match(past, ["Drug", "By", "Decided by"], q_past), hide_index=True)

    if sec == "receive":
        st.subheader("Receive stock: upload your Excel file")
        st.caption("The file needs two columns: **Item Name** and **Quantity**. A name already in the list gets the "
                   "quantity added. A name that is not found is added to the list as a new drug.")
        st.download_button(":material/download: Download Excel template", template_bytes(), "receive_stock_template.xlsx")
        up = st.file_uploader("Excel or CSV file", type=["xlsx", "csv"], key=K("up"))
        if up is not None:
            try:
                raw = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
                plan, info = plan_receipt(raw, d.name.tolist())
            except Exception as e:
                plan, info = None, f"Could not read the file: {e}"
            if plan is None:
                st.error(info)
            else:
                if info:
                    st.info(info)
                st.write(f"**{len(plan)} drugs found.** Check the table. If a match is wrong, change **Add to**.")
                ed = wide(st.data_editor, plan, hide_index=True, disabled=["Check", "Name in Excel"],
                          height=min(460, 60 + 36 * len(plan)), key=f"plan{K('x')}{up.name}{up.size}",
                          column_config={"Quantity": st.column_config.NumberColumn(min_value=0, step=1),
                                         "Add to": st.column_config.SelectboxColumn(
                                             options=[NEW] + d.name.tolist(), required=True)})
                note = st.text_input("Delivery note (optional)", key=K("note"))
                if st.button("Add to main stock", type="primary"):
                    mt, nw, tot = apply_receipt(ed, note, user)
                    done(f"Main stock updated: {mt} existing drug(s), {nw} new drug(s), {tot} units received.")
        with st.expander("Receive one drug by hand"):
            pick = st.selectbox("Drug name", d.name.tolist(), index=None,
                                placeholder="Start typing the drug name…", key=K("rc"))
            qty = st.number_input("Quantity received", min_value=1, step=1, value=None,
                                  placeholder="Enter quantity", key=K("rq"))
            if st.button("Add this drug to main stock"):
                if not pick or not qty:
                    st.warning("Choose a drug and enter the quantity.")
                else:
                    receive_stock(int(d.set_index("name").loc[pick, "id"]), int(qty), "Entered by hand", user)
                    done(f"Added {int(qty)} of {pick} to main stock.")
        st.subheader("Recent deliveries")
        q_del = search_box("q_del", "Search drug or note")
        deliveries = df("""SELECT l.on_date AS "Date", d.name AS "Drug", l.change_qty AS "Quantity", l.note AS "Note"
            FROM stock_log l JOIN drugs d ON d.id=l.drug_id WHERE l.kind='Received'
            ORDER BY l.id DESC LIMIT 500""")
        wide(st.dataframe, match(deliveries, ["Drug", "Note"], q_del).head(50 if not q_del else 500), hide_index=True)

        with st.expander("Mark tracer drugs and low-stock levels"):
            st.caption("Tick **Tracer** for drugs you want to monitor closely. The low-stock level is the quantity "
                       "at which dispensing shows a warning.")
            ed2 = wide(st.data_editor, pd.DataFrame({"Drug": d.name, "Tracer": d.tracer.astype(bool),
                                                     "Low-stock level": d.reorder_level}),
                       hide_index=True, height=420, disabled=["Drug"], key=K("dl"),
                       column_config={"Low-stock level": st.column_config.NumberColumn(min_value=0, step=1)})
            if st.button("Save tracer and low-stock settings", type="primary"):
                with conn() as c:
                    for i, t, l in zip(d.id, ed2["Tracer"], ed2["Low-stock level"]):
                        c.execute("UPDATE drugs SET tracer=?, reorder_level=? WHERE id=?",
                                  (int(bool(t)), int(0 if pd.isna(l) else l), int(i)))
                done("Settings saved.")

    if sec == "reconcile":
        st.subheader("Main stock reconciliation")
        st.write("Search for a drug, type the number you counted in the **Counted** column, then search for the next "
                 "drug. Your counts are kept while you search. Leave **Counted** empty for drugs you did not count.")
        counts = st.session_state.setdefault("recon", {})  # {drug_id: counted}
        q_rec = search_box("q_recon", "Search drug", "Type part of a drug name to find it…")
        shown = match(d, ["name"], q_rec)
        grid = pd.DataFrame({"Drug": shown.name.values, "In system": shown.main_qty.values,
                             "Counted": pd.array([counts.get(int(i)) for i in shown.id], dtype="Int64")})
        edited = wide(st.data_editor, grid, hide_index=True, height=420, disabled=["Drug", "In system"],
                      column_config={"Counted": st.column_config.NumberColumn("Counted", min_value=0, step=1)},
                      key=f"grid{K('g')}_{q_rec}")
        for i, c in zip(shown.id, edited["Counted"]):
            if pd.notna(c):
                counts[int(i)] = int(c)
            else:
                counts.pop(int(i), None)
        sysq = {int(i): int(m) for i, m in zip(d.id, d.main_qty)}
        names = {int(i): n for i, n in zip(d.id, d.name)}
        diff = pd.DataFrame([{"Drug": names[i], "In system": sysq[i], "Counted": c, "Difference": c - sysq[i]}
                             for i, c in counts.items() if i in sysq and c != sysq[i]])
        if len(diff):
            st.write(f"**{len(diff)} drug(s) will change:**")
            wide(st.dataframe, diff, hide_index=True)
        reason = st.text_input("Reason", "Physical count", key=K("rr"))
        b1, b2 = st.columns(2)
        if b1.button("Save reconciliation", type="primary", disabled=diff.empty):
            n = reconcile({i: c for i, c in counts.items() if i in sysq}, reason, user)
            counts.clear()
            done(f"Main stock updated for {n} drug(s).")
        if b2.button("Clear my counts", disabled=not counts):
            counts.clear()
            done("Counts cleared.")
        with st.expander("Reconciliation history"):
            q_hist = search_box("q_hist", "Search drug or person")
            hist = df("""SELECT l.on_date AS "Date", d.name AS "Drug", l.change_qty AS "Difference",
                l.note AS "Note", l.by_user AS "By" FROM stock_log l JOIN drugs d ON d.id=l.drug_id
                WHERE l.kind='Reconciliation' ORDER BY l.id DESC LIMIT 1000""")
            wide(st.dataframe, match(hist, ["Drug", "By"], q_hist), hide_index=True)

    if sec == "reports":
        st.subheader("Daily and monthly report")
        r1, r2 = st.columns(2)
        kind = r1.radio("Report", ["Daily", "Monthly"], horizontal=True)
        ref = r2.date_input("Date", date.today(), min_value=date.today() - timedelta(days=1095),
                            max_value=date.today(), key="rep_date")
        start, end = period_range(kind, ref)
        st.caption(f"{start:%d %b %Y}" + ("" if start == end else f" to {end:%d %b %Y}") +
                   ". Requested = asked in this period. Received in dispensing = approved in this period. "
                   "Main stock is the balance at the end of the period; dispensing stock is the balance now.")
        floor = cleared_before()
        if floor and start < floor:
            st.warning(f"History before {floor:%d %b %Y} was cleared, so this report may be incomplete.")
        g1, g2 = st.columns(2)
        with g1:
            q_rep = search_box("q_rep")
        with g2:
            who_rep = dispenser_select("p_rep")
        rep = report_table(start, end, q_rep, who_rep)
        if rep.empty:
            st.info("Nothing happened in this period.")
        else:
            wide(st.dataframe, rep, hide_index=True)
            st.download_button(":material/download: Download this report (Excel)", to_excel({"Report": rep}),
                               f"report_{kind.lower()}_{start}.xlsx", mime=XLSX, key="dl_rep")
        st.subheader("Download all data")
        st.caption("Stock, usage, requests and stock movements, one sheet each. Download a copy before you "
                   "clear history if you want to keep it.")
        if st.button("Prepare Excel file"):
            st.session_state.export = (to_excel(export_sheets()), str(date.today()))
        ex = st.session_state.get("export")
        if ex:
            st.download_button(":material/download: Download all data (Excel)", ex[0],
                               f"cyarubare_stock_{ex[1]}.xlsx", mime=XLSX, key="dl_all")

    if sec == "settings":
        me = st.session_state.uid
        st.subheader("Users")
        users = df("SELECT id, username, role, email FROM users ORDER BY role, username")
        q_usr = search_box("q_usr", "Search user", "Type part of a name or email…")
        shown_users = users.drop(columns="id").rename(columns={"username": "User name", "role": "Role", "email": "Email"})
        wide(st.dataframe, match(shown_users, ["User name", "Email", "Role"], q_usr), hide_index=True)
        with st.expander("Add a user"):
            a1, a2 = st.columns(2)
            nu = a1.text_input("User name", key=K("nu"))
            nr = a2.selectbox("Role", [DISPENSER, MANAGER], key=K("nr"))
            ne = a1.text_input("Email (optional, used to reset a forgotten password)", key=K("ne"))
            npw = a2.text_input("Temporary password (at least 8 characters)", type="password", key=K("np"))
            if st.button("Add user"):
                err = add_user(nu, nr, ne, npw)
                if err:
                    st.error(err)
                else:
                    done(f"User {nu.strip()} added.")
        with st.expander("Change a password or delete a user"):
            labels = {f"{r.username} ({r.role})": int(r.id) for r in users.itertuples()}
            who = st.selectbox("User", list(labels), index=None, placeholder="Choose a user", key=K("wu"))
            pw = st.text_input("New password (at least 8 characters)", type="password", key=K("wp"))
            sure = st.checkbox("I am sure I want to delete this user", key=K("ws"))
            w1, w2 = st.columns(2)
            if w1.button("Set new password"):
                if not who:
                    st.warning("Choose a user first.")
                else:
                    err = set_user_password(labels[who], pw)
                    if err:
                        st.error(err)
                    else:
                        done(f"Password changed for {who}.")
            if w2.button("Delete this user"):
                if not who or not sure:
                    st.warning("Choose a user and tick the box to confirm.")
                else:
                    err = delete_user(labels[who], me)
                    if err:
                        st.error(err)
                    else:
                        done(f"{who} deleted.")

        st.subheader("Clear history")
        st.write("Deletes old activity: usage records, requests and stock history (deliveries and reconciliations). "
                 "Stock quantities, the drug list and user accounts are never deleted. Nothing is deleted "
                 "automatically: records stay until you clear them.")
        info = storage_info()
        st.caption(f"Stored now: {info['usage records']} usage records, {info['requests']} requests, "
                   f"{info['stock history lines']} stock history lines."
                   + (f" Oldest record: {info['oldest']}." if info["oldest"] else "")
                   + (f" Database size: {info['mb']} MB." if info["mb"] is not None else ""))
        choice = st.radio("What should be deleted?", ["Older than 3 months", "Older than 6 months", "All history"],
                          key=K("hc"))
        cutoff = {"Older than 3 months": months_ago(3), "Older than 6 months": months_ago(6), "All history": None}[choice]
        will = count_history(cutoff)
        st.write("This will delete: " + ", ".join(f"{v} {k}" for k, v in will.items()) + ".")
        if cutoff is None:
            st.caption("All history also removes requests that are still waiting for a decision.")
        else:
            st.caption(f"Records dated before {cutoff:%d %b %Y} are deleted. Requests still waiting for a decision are kept.")
        word = st.text_input("Type DELETE to confirm", key=K("dw"))
        if st.button("Clear history", type="primary"):
            if word.strip() != "DELETE":
                st.error("Type the word DELETE (in capitals) to confirm.")
            else:
                res = clear_history(cutoff)
                done("History cleared: " + ", ".join(f"{v} {k}" for k, v in res.items()) + " deleted.")


# ---------- dispensing ----------
def dispensing_page():
    user = st.session_state.user
    sec, title = side_menu([("usage", "Record usage", "medication"), ("request", "Request stock", "assignment"),
                            ("stock", "My stock", "inventory_2")])
    header(title)
    show_flash()
    d = drugs_df()
    idx = d.set_index("name")

    if sec == "usage":
        st.subheader("What was used?")
        pick = st.selectbox("Drug name", d.name.tolist(), index=None,
                            placeholder="Start typing the drug name…", key=K("u"))
        if pick:
            st.caption(f"In dispensing stock: **{int(idx.loc[pick, 'disp_qty'])}**")
        qty = st.number_input("Quantity used", min_value=1, step=1, value=None,
                              placeholder="Enter quantity", key=K("uq"))
        day = st.date_input("Date", date.today(), min_value=usage_floor(), max_value=date.today(), key=K("ud"))
        if st.button("Save", type="primary", key="usave"):
            if not pick or not qty:
                st.warning("Choose a drug and enter the quantity.")
            else:
                err = record_usage(int(idx.loc[pick, "id"]), int(qty), day, user)
                if err:
                    st.error(err)
                else:
                    done(f"Saved: {int(qty)} of {pick}.")
        st.subheader("Recorded today")
        wide(st.dataframe, df("""SELECT d.name AS "Drug", u.qty AS "Quantity" FROM usage_log u JOIN drugs d ON d.id=u.drug_id
            WHERE u.used_on=? AND u.by_user=? ORDER BY u.id DESC""", (str(date.today()), user)), hide_index=True)

    if sec == "request":
        st.subheader("Request drugs from the main store")
        cart = st.session_state.setdefault("cart", {})
        pick = st.selectbox("Drug name", d.name.tolist(), index=None,
                            placeholder="Start typing the drug name…", key=K("r"))
        if pick:
            st.caption(f"You have: **{int(idx.loc[pick, 'disp_qty'])}**")
        qty = st.number_input("Quantity needed", min_value=1, step=1, value=None,
                              placeholder="Enter quantity", key=K("rq2"))
        if st.button(":material/add: Add to request"):
            if not pick or not qty:
                st.warning("Choose a drug and enter the quantity.")
            else:
                cart[pick] = cart.get(pick, 0) + int(qty)
                st.session_state.rk = st.session_state.get("rk", 0) + 1
                st.rerun()
        if cart:
            st.subheader(f"Your request: {len(cart)} drug(s)")
            cv = st.session_state.get("cv", 0)
            tbl = pd.DataFrame({"Drug": list(cart), "Quantity": list(cart.values()),
                                "In your stock": [int(idx.loc[n, "disp_qty"]) for n in cart], "Remove": False})
            ed = wide(st.data_editor, tbl, hide_index=True, key=f"cart{cv}", disabled=["Drug", "In your stock"],
                      column_config={"Quantity": st.column_config.NumberColumn(min_value=1, step=1),
                                     "Remove": st.column_config.CheckboxColumn()})
            st.caption("Change a quantity if needed. Tick **Remove** to drop a drug. Add more drugs above, then send.")
            s1, s2 = st.columns(2)
            if s1.button(":material/send: Send request to the store manager", type="primary"):
                items = {int(idx.loc[n, "id"]): int(q) for n, q, rm in zip(ed.Drug, ed.Quantity, ed.Remove)
                         if not rm and q}
                if not items:
                    st.warning("Your request is empty.")
                else:
                    b = create_request(items, user)
                    st.session_state.cart, st.session_state.cv = {}, cv + 1
                    done(f"Request #{b} sent to the store manager with {len(items)} drug(s).")
            if s2.button("Clear list"):
                st.session_state.cart, st.session_state.cv = {}, cv + 1
                st.rerun()
        mine = df("""SELECT r.id, r.batch, d.name, r.qty_requested FROM requests r JOIN drugs d ON d.id=r.drug_id
                     WHERE r.status='Pending' AND r.requested_by=? ORDER BY r.batch, d.name""", (user,))
        st.subheader("Waiting for the store manager")
        if mine.empty:
            st.caption("You have no pending requests.")
        else:
            st.caption("You can still change these. Edit a quantity or tick **Remove**, then press **Save changes**.")
            tbl2 = pd.DataFrame({"Request #": mine.batch.astype(int).values, "Drug": mine.name.values,
                                 "Quantity": mine.qty_requested.values, "Remove": False})
            ed2 = wide(st.data_editor, tbl2, hide_index=True, key=f"mine{K('m')}_{len(mine)}_{int(mine.id.iloc[0])}",
                       disabled=["Request #", "Drug"],
                       column_config={"Quantity": st.column_config.NumberColumn(min_value=1, step=1),
                                      "Remove": st.column_config.CheckboxColumn()})
            if st.button("Save changes", type="primary", key="msave"):
                removes = [int(i) for i, rm in zip(mine.id, ed2["Remove"]) if rm]
                changes = {int(i): int(q) for i, q, rm, old in zip(mine.id, ed2["Quantity"], ed2["Remove"],
                                                                    mine.qty_requested)
                           if not rm and pd.notna(q) and int(q) != int(old)}
                if not removes and not changes:
                    st.info("Nothing to change.")
                else:
                    skipped = update_pending(user, changes, removes)
                    done("Changes saved." + (f" {skipped} line(s) could not be changed because the store manager "
                                             "already decided them." if skipped else ""))
            batches = sorted({int(b) for b in mine.batch})
            cc1, cc2 = st.columns([2, 1])
            which = cc1.selectbox("Cancel a whole request", [f"Request #{b}" for b in batches], index=None,
                                  placeholder="Choose a request", key=K("cb"))
            cc2.write("")
            if cc2.button("Cancel request", key="mcancel"):
                if not which:
                    st.warning("Choose the request to cancel.")
                else:
                    cancel_request(int(which.split("#")[1]), user)
                    done(f"{which} was cancelled.")

        st.subheader("My past requests")
        q_my = search_box("q_my")
        my_past = df("""SELECT r.batch AS "Request #", r.requested_on AS "Date", d.name AS "Drug",
            r.qty_requested AS "Requested", r.qty_approved AS "Approved", r.status AS "Status",
            r.decided_by AS "Decided by", r.note AS "Note"
            FROM requests r JOIN drugs d ON d.id=r.drug_id WHERE r.status!='Pending' AND r.requested_by=?
            ORDER BY r.batch DESC, d.name LIMIT 500""", (user,))
        wide(st.dataframe, match(my_past, ["Drug"], q_my), hide_index=True)

    if sec == "stock":
        d["Status"] = [status(q, l) for q, l in zip(d.disp_qty, d.reorder_level)]
        t1c, t2c = st.columns([2, 3])
        low_only = t1c.toggle("Show only low or out of stock")
        with t2c:
            q_my_stock = search_box("q_mystock")
        v = d[d.Status != "OK"] if low_only else d
        v = match(v, ["name"], q_my_stock)
        wide(st.dataframe, style_status(v[["name", "disp_qty", "Status"]].rename(
            columns={"name": "Drug", "disp_qty": "In stock"})), hide_index=True)


# ---------- main ----------
SIDEBAR_CSS = """
<style>
[data-testid="stSidebar"] {background:#cfe5f7;}
[data-testid="stSidebar"] * {color:#0a3a66;}
[data-testid="stSidebar"] hr {border-color:#9cc7ea;}
[data-testid="stSidebar"] .stButton {width:100%;}
[data-testid="stSidebar"] .stButton button {width:100%; justify-content:flex-start; border:0; border-radius:8px;
  background:transparent; padding:.55rem .8rem;}
[data-testid="stSidebar"] .stButton button:hover {background:#b9d9f2;}
[data-testid="stSidebar"] .stButton button p {text-align:left;}
[data-testid="stSidebar"] .stButton button[kind="primary"],
[data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"] {background:#2b7bb9;}
[data-testid="stSidebarUserContent"] {padding-bottom:4rem;}
.st-key-userbox {position:fixed; bottom:1rem; left:1rem; width:auto !important; max-width:13rem; z-index:99;}
.st-key-userbox button {border:1px solid #9cc7ea; background:#e8f3fb; border-radius:999px; min-height:0;
  padding:.2rem .8rem;}
.st-key-userbox button p {font-size:.85rem; margin:0;}
[data-testid="stSidebar"][aria-expanded="false"] .st-key-userbox {display:none;}
[data-testid="stSidebar"] .stButton button[kind="primary"] *,
[data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"] * {color:#ffffff !important;}
</style>
"""

def main():
    init_db()
    if "user" not in st.session_state:
        login()
        return
    me = get_user(st.session_state.uid)
    if me is None:  # the account was deleted
        st.session_state.clear()
        st.rerun()
    if me and not me["email"] and not st.session_state.get("skip_email"):
        email_prompt()
        return
    st.markdown(SIDEBAR_CSS, unsafe_allow_html=True)
    with st.sidebar:
        st.markdown("### CYARUBARE Health Center")
        st.caption("Stock Management System")
        st.divider()
    if st.session_state.role == MANAGER:
        manager_page()
    else:
        dispensing_page()
    with st.sidebar:
        user_box()


if __name__ == "__main__":
    main()
