import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,       -- 10-digit, no country code
    email         TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    role          TEXT,                       -- admin | reception | NULL (member / guest)
    referred_by   INTEGER,                    -- user id of the member who referred them
    referral_rewarded INTEGER NOT NULL DEFAULT 0,  -- their referrer has been credited (once, ever)
    credit        INTEGER NOT NULL DEFAULT 0, -- rupees off their next membership payment
    pref_channel  TEXT,                       -- whatsapp | sms: the ONE channel they want codes and messages on
    password_hash TEXT,                       -- staff, and members / academy players once a login is created
    username      TEXT,                       -- a login ID the member chose (lower case); NR-xxxx always works too
    must_change_pw INTEGER NOT NULL DEFAULT 0, -- 1 = signed in with a temporary password: must set their own
    pw_failed     INTEGER NOT NULL DEFAULT 0, -- wrong passwords in a row
    pw_locked_until TEXT,                     -- too many wrong passwords: no login until then
    sms_opt       INTEGER NOT NULL DEFAULT 1,
    wa_opt        INTEGER NOT NULL DEFAULT 1,
    lock_user_id  TEXT,                       -- their user ID on the door terminal (face enrolled there)
    lock_enabled  INTEGER NOT NULL DEFAULT 0, -- what we last told the lock
    guardian_name TEXT,                       -- academy juniors
    created_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users(username);

CREATE TABLE IF NOT EXISTS courts (
    id    TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    name  TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    sport         TEXT,                        -- NULL = all sports
    duration_days INTEGER NOT NULL,
    price         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    plan_id    INTEGER NOT NULL REFERENCES plans(id),
    start_date TEXT NOT NULL,                  -- YYYY-MM-DD
    end_date   TEXT NOT NULL,                  -- last valid day, inclusive
    status     TEXT NOT NULL DEFAULT 'active', -- active | cancelled
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS academy_batches (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    coach       TEXT,
    weekdays    TEXT NOT NULL,     -- '0,2,4' = Mon,Wed,Fri
    start_time  TEXT NOT NULL,     -- HH:MM
    end_time    TEXT NOT NULL,
    court_ids   TEXT NOT NULL,     -- 'B1,B2' — blocked from public booking at those times
    monthly_fee INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS academy_enrollments (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    batch_id       INTEGER NOT NULL REFERENCES academy_batches(id),
    level          TEXT,
    fee_paid_until TEXT NOT NULL,  -- YYYY-MM-DD inclusive
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS court_blocks (
    id       INTEGER PRIMARY KEY,
    court_id TEXT NOT NULL,
    date     TEXT NOT NULL,        -- one-off block (maintenance, tournament)
    start    TEXT NOT NULL,        -- HH:MM
    end      TEXT NOT NULL,
    reason   TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY,
    ref          TEXT NOT NULL UNIQUE,          -- short public reference
    court_id     TEXT NOT NULL REFERENCES courts(id),
    user_id      INTEGER REFERENCES users(id),
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL,
    start        TEXT NOT NULL,                 -- YYYY-MM-DD HH:MM
    end          TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    status       TEXT NOT NULL,                 -- pending | reserved | confirmed | cancelled | expired
    hold_until   TEXT,
    payment_ref  TEXT,
    paid_at      TEXT,
    created_at   TEXT NOT NULL,
    pay_status   TEXT NOT NULL DEFAULT 'unpaid', -- unpaid | paid | comp (complimentary)
    source       TEXT NOT NULL DEFAULT 'online', -- online | desk | phone
    cancelled_at TEXT,
    cancelled_by TEXT,                          -- customer | staff | system
    refund_amount INTEGER NOT NULL DEFAULT 0,
    email        TEXT,
    access_key   TEXT,                          -- secret in the confirmation link (no login needed)
    discount     INTEGER NOT NULL DEFAULT 0,    -- rupees taken off (first-booking offer); amount is what's due
    utm          TEXT,                          -- where the visitor came from: "source" or "source/campaign"
    heard        TEXT,                          -- their answer to "How did you hear about us?"
    notify_channel TEXT                         -- whatsapp | sms, chosen by the customer when booking
);
CREATE INDEX IF NOT EXISTS ix_bookings_court_start ON bookings(court_id, start);

-- Every rupee in (and back out). Bookings, memberships and academy fees, online or at the desk.
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,     -- booking | membership | academy | other
    method          TEXT,              -- cash | upi | card | online | credit
    note            TEXT,              -- what an 'other' payment was for; who recorded it
    ref_id          INTEGER,           -- booking / membership / enrollment id (membership: set once fulfilled)
    user_id         INTEGER,
    phone           TEXT,
    amount          INTEGER NOT NULL,  -- rupees
    provider        TEXT NOT NULL,     -- razorpay | desk (taken by staff)
    order_id        TEXT,              -- gateway order id
    payment_id      TEXT,              -- gateway payment id
    status          TEXT NOT NULL,     -- created | paid
    meta            TEXT,              -- JSON, e.g. {"months": 3}
    created_at      TEXT NOT NULL,
    paid_at         TEXT,
    refund_status   TEXT,              -- NULL | pending | done | failed | manual (cash to hand back)
    refund_amount   INTEGER NOT NULL DEFAULT 0,
    refund_id       TEXT,
    refund_error    TEXT,
    refund_attempts INTEGER NOT NULL DEFAULT 0,
    refunded_at     TEXT,
    secret          TEXT               -- in the payment page link, so only the payer can open it
);
CREATE INDEX IF NOT EXISTS ix_payments_ref ON payments(kind, ref_id);

CREATE TABLE IF NOT EXISTS door_codes (
    id          INTEGER PRIMARY KEY,
    booking_id  INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    code        TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    status      TEXT NOT NULL,     -- active | revoked | expired
    lock_ref    TEXT,              -- id the lock vendor gave the pushed passcode
    uses        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_events (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    device_id  TEXT,
    method     TEXT NOT NULL,      -- code | fingerprint | manual
    user_id    INTEGER,
    booking_id INTEGER,
    granted    INTEGER NOT NULL,
    detail     TEXT,
    ext_id     TEXT                -- id the door controller gave the event (stops double-counting)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_access_ext ON access_events(ext_id);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE,        -- stops a reminder going out twice
    kind       TEXT NOT NULL,
    channel    TEXT NOT NULL,      -- sms | whatsapp
    to_phone   TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL,      -- sent | failed
    error      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_otps (
    phone      TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

-- One row per visitor session on the public site, with where they came from.
CREATE TABLE IF NOT EXISTS visits (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    vid       TEXT NOT NULL,       -- random id kept in the visitor's browser (no personal data)
    source    TEXT NOT NULL,       -- agency / channel: utm_source, or worked out from the referrer, or 'direct'
    medium    TEXT,
    campaign  TEXT,
    referrer  TEXT,                -- referring site's host name
    path      TEXT
);
CREATE INDEX IF NOT EXISTS ix_visits_at ON visits(at);

-- "Planning to hold an event?" enquiries from the website
CREATE TABLE IF NOT EXISTS event_enquiries (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    people     INTEGER NOT NULL,
    message    TEXT,
    status     TEXT NOT NULL DEFAULT 'new',   -- new | contacted | booked | closed
    note       TEXT,                          -- staff's own note
    handled_by TEXT,
    updated_at TEXT,
    source     TEXT                           -- marketing source of the visit (agency link, Instagram, …)
);

-- Club membership (app/club.py): one row per purchase / renewal
CREATE TABLE IF NOT EXISTS club_memberships (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    start_date TEXT NOT NULL,                  -- YYYY-MM-DD
    end_date   TEXT NOT NULL,                  -- last valid day, inclusive
    months     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active', -- active | cancelled
    created_at TEXT NOT NULL,
    created_by TEXT                            -- staff name, or 'online'
);

-- One 6-digit serial per included court hour, per sport, per club month, generated at purchase
CREATE TABLE IF NOT EXISTS club_serials (
    id           INTEGER PRIMARY KEY,
    club_id      INTEGER NOT NULL REFERENCES club_memberships(id),
    user_id      INTEGER NOT NULL REFERENCES users(id),
    period       INTEGER NOT NULL,             -- 0 = first club month
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,                -- inclusive; unused serials expire after this day
    sport        TEXT NOT NULL,
    serial       TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,                -- free | booked | expired
    booking_id   INTEGER REFERENCES bookings(id),
    booked_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_club_serials_user ON club_serials(user_id, period_start, sport, status);
CREATE INDEX IF NOT EXISTS ix_club_serials_booking ON club_serials(booking_id);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_local = threading.local()
_path = None


def init(path=None):
    """Open (and create) the database. Call once at startup; tests pass ':memory:'-like temp paths."""
    global _path
    _path = path or config.DB_PATH
    if _path != ":memory:":
        Path(_path).parent.mkdir(parents=True, exist_ok=True)
    _local.conn = None
    c = conn()
    _upgrade(c)
    c.executescript(SCHEMA)
    from . import seed
    c.execute("BEGIN")  # first-run setup in one transaction
    seed.seed(c)
    seed.ensure_online_plan(c)
    seed.ensure_reception(c)
    c.execute("COMMIT")


# Columns added after the first release, for databases created before them.
_ADDED = {
    "bookings": [
        ("pay_status", "TEXT NOT NULL DEFAULT 'unpaid'"), ("source", "TEXT NOT NULL DEFAULT 'online'"),
        ("cancelled_at", "TEXT"), ("cancelled_by", "TEXT"), ("refund_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("email", "TEXT"), ("access_key", "TEXT"), ("discount", "INTEGER NOT NULL DEFAULT 0"), ("utm", "TEXT"),
        ("heard", "TEXT"), ("notify_channel", "TEXT"),
    ],
    "payments": [("secret", "TEXT"), ("method", "TEXT"), ("note", "TEXT")],
    "users": [("role", "TEXT"), ("referred_by", "INTEGER"), ("referral_rewarded", "INTEGER NOT NULL DEFAULT 0"),
              ("credit", "INTEGER NOT NULL DEFAULT 0"), ("pref_channel", "TEXT"), ("username", "TEXT"),
              ("must_change_pw", "INTEGER NOT NULL DEFAULT 0"), ("pw_failed", "INTEGER NOT NULL DEFAULT 0"),
              ("pw_locked_until", "TEXT")],
    "access_events": [("ext_id", "TEXT")],
}


def _upgrade(c):
    for table, cols in _ADDED.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # fresh database; SCHEMA creates it complete
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                if (table, name) == ("bookings", "pay_status"):
                    c.execute("UPDATE bookings SET pay_status='paid' WHERE paid_at IS NOT NULL")
                if (table, name) == ("users", "role"):
                    c.execute("UPDATE users SET role='admin' WHERE is_admin=1")


def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _path:
        c = sqlite3.connect(_path, timeout=10, isolation_level=None)  # autocommit; explicit BEGINs
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")
        _local.conn = c
        _local.path = _path
    return c


class tx:
    """`with tx() as c:` — BEGIN IMMEDIATE so two people can't grab the same slot."""

    def __enter__(self):
        self.c = conn()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, *_):
        self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def one(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def all_(sql, *args):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def kv_get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", k)
    return r["v"] if r else default


def kv_set(k, v):
    conn().execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def backup(folder, keep_days=30):
    """A consistent copy of the live database (SQLite's online backup), one file per day."""
    import datetime
    import os
    os.makedirs(folder, exist_ok=True)
    today = datetime.date.today().isoformat()
    target = os.path.join(folder, f"netrush-{today}.db")
    if not os.path.exists(target):
        tmp = target + ".part"
        dst = sqlite3.connect(tmp)
        try:
            conn().backup(dst)
        finally:
            dst.close()
        os.replace(tmp, target)
    cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
    for f in os.listdir(folder):
        if f.startswith("netrush-") and f.endswith(".db") and f[8:18] < cutoff:
            os.remove(os.path.join(folder, f))
    return target
