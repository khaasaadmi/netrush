"""First-run data: the courts, the membership plans and the staff logins. Nothing else: the real site
starts empty and fills up with real customers."""
from datetime import timedelta

from . import clock, config


def seed(c):
    if c.execute("SELECT COUNT(*) FROM courts").fetchone()[0]:
        return
    from .auth import hash_password

    c.executemany("INSERT INTO courts(id,sport,name) VALUES(?,?,?)", config.COURTS)
    c.executemany(
        "INSERT INTO plans(name,sport,duration_days,price) VALUES(?,?,?,?)",
        [
            ("Badminton Monthly", "badminton", 30, 2500),
            ("Badminton Quarterly", "badminton", 90, 6500),
            ("Pickleball Monthly", "pickleball", 30, 3500),
            ("Padel Monthly", "padel", 30, 6000),
            ("All-Access Monthly", None, 30, 9000),
        ],
    )
    ensure_online_plan(c)  # plan 6: the membership sold in the portal
    now = clock.fmt(clock.now())
    c.execute(
        "INSERT INTO users(name,phone,is_admin,role,password_hash,created_at) VALUES(?,?,1,'admin',?,?)",
        ("NetRush Admin", config.ADMIN_PHONE, hash_password(config.ADMIN_PASSWORD), now),
    )


ONLINE_PLAN = "Monthly Membership (online)"


def ensure_online_plan(c):
    """The plan sold in the member portal. Kept in step with ONLINE_MEMBERSHIP_MONTHLY."""
    row = c.execute("SELECT id FROM plans WHERE name=?", (ONLINE_PLAN,)).fetchone()
    if row:
        c.execute("UPDATE plans SET price=? WHERE id=?", (config.ONLINE_MEMBERSHIP_MONTHLY, row[0]))
    else:
        c.execute("INSERT INTO plans(name,sport,duration_days,price) VALUES(?,NULL,30,?)",
                  (ONLINE_PLAN, config.ONLINE_MEMBERSHIP_MONTHLY))


def ensure_reception(c):
    """One receptionist login out of the box (the admin can add more in the panel)."""
    if c.execute("SELECT 1 FROM users WHERE role='reception'").fetchone():
        return
    if c.execute("SELECT 1 FROM users WHERE phone=?", (config.RECEPTION_PHONE,)).fetchone():
        return
    from .auth import hash_password
    c.execute("INSERT INTO users(name,phone,role,password_hash,created_at) VALUES('Reception',?,'reception',?,?)",
              (config.RECEPTION_PHONE, hash_password(config.RECEPTION_PASSWORD), clock.fmt(clock.now())))


def online_plan_id():
    from . import db
    return db.one("SELECT id FROM plans WHERE name=?", ONLINE_PLAN)["id"]
