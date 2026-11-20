"""Club membership: all three sports, a fixed number of court hours every month.

How it works
  • Each club month includes CLUB_HOURS hours per sport (default 26 badminton + 20 pickleball + 7 padel = 53).
  • When the membership is sold, every month of it gets its own set of 6-digit serial numbers up front, one per
    hour, each tied to one sport and one month: a 12-month membership = 12 × 53 = 636 serials, all in the
    member's name. A club month runs from the start date to the day before the same date next month.
  • Booking a court uses that sport's serials for that month, one per hour. The first serial of the booking is
    its door code: it goes onto the door terminal 10 minutes before the slot and is deleted when the slot ends,
    exactly like a guest code. So a padel hour can't be spent on badminton, and nothing opens the door
    outside a booked slot.
  • At the end of each club month, its unused serials expire. The next month's serials take over.
  • Cancelled at least CLUB_RETURN_HOURS before the start (or by staff): the hours go back to the balance.
    Later than that, they are used up, like a no-show.

Serial states: free (unused) → booked (spent on a booking) | expired (month ended unused).
"""
import calendar
import json
import secrets
from datetime import date as Date, timedelta

from . import clock, config, db, notify, payments

SPORTS = ("badminton", "pickleball", "padel")
PLAN_NAME = "Club Membership"


class ClubError(Exception):
    pass


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return Date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _periods(start, months):
    """[(index, first day, last day)] for each club month."""
    return [(i, add_months(start, i), add_months(start, i + 1) - timedelta(days=1)) for i in range(months)]


def hours_per_month():
    return dict(config.CLUB_HOURS)


# ---------------------------------------------------------------- serial numbers

def _taken(c, code):
    if c.execute("SELECT 1 FROM club_serials WHERE serial=?", (code,)).fetchone():
        return True
    if c.execute("SELECT 1 FROM door_codes WHERE code=? AND status='active'", (code,)).fetchone():
        return True
    return bool(c.execute("SELECT 1 FROM users WHERE lock_user_id=?", (code,)).fetchone())


def _new_serial(c):
    lo = 10 ** (config.DOOR_CODE_DIGITS - 1)
    while True:
        code = str(lo + secrets.randbelow(9 * lo))
        if len(set(code)) > 1 and not _taken(c, code):
            return code


# ---------------------------------------------------------------- selling and renewing

def _next_start(user_id):
    """A renewal starts the day after the current club membership ends, so renewing early loses nothing."""
    today = clock.today()
    cur = db.one("SELECT MAX(end_date) AS e FROM club_memberships WHERE user_id=? AND status='active' AND end_date>=?",
                 user_id, today.isoformat())["e"]
    return Date.fromisoformat(cur) + timedelta(days=1) if cur else today


def _create(c, user_id, months, created_by, start=None):
    start = start or _next_start(user_id)
    end = add_months(start, months) - timedelta(days=1)
    cid = c.execute("INSERT INTO club_memberships(user_id,start_date,end_date,months,status,created_at,created_by) "
                    "VALUES(?,?,?,?,'active',?,?)", (user_id, start.isoformat(), end.isoformat(), months,
                                                    clock.fmt(clock.now()), created_by)).lastrowid
    for i, ps, pe in _periods(start, months):
        for sport in SPORTS:
            for _ in range(config.CLUB_HOURS.get(sport, 0)):
                c.execute("INSERT INTO club_serials(club_id,user_id,period,period_start,period_end,sport,serial,status) "
                          "VALUES(?,?,?,?,?,?,?,'free')",
                          (cid, user_id, i, ps.isoformat(), pe.isoformat(), sport, _new_serial(c)))
    return cid


def _check_months(months):
    months = int(months)
    if not 1 <= months <= 12:
        raise ClubError("Choose 1 to 12 months")
    return months


def sell(user_id, months, staff, method="cash", amount=None):
    """Sold or renewed at the desk. `amount` is what was actually received (defaults to the list price)."""
    months = _check_months(months)
    u = db.one("SELECT id, name, phone FROM users WHERE id=? AND role IS NULL", user_id)
    if not u:
        raise ClubError("Person not found")
    price = config.CLUB_PRICE_MONTHLY * months
    amount = price if amount in (None, "") else int(amount)
    if amount < 0:
        raise ClubError("Amount received can't be negative")
    if not amount and not price:
        raise ClubError("Enter the amount received (no club price is set yet: CLUB_PRICE_MONTHLY)")
    with db.tx() as c:
        cid = _create(c, user_id, months, staff)
    if amount:
        payments.record_desk("club", cid, amount, u["phone"], user_id, staff, {"months": months}, method=method)
    _welcome(cid)
    return get(cid)


def order(user_id, months):
    """Portal: renew online (only for existing club members, and only once a price is set)."""
    months = _check_months(months)
    if months not in config.CLUB_MONTH_OPTIONS:
        raise ClubError("Pick one of the offered durations")
    if not db.one("SELECT 1 AS x FROM club_memberships WHERE user_id=?", user_id):
        raise ClubError("Club memberships are set up at the reception. Online payment is for renewals.")
    if not config.CLUB_PRICE_MONTHLY or not config.ONLINE_PAY:
        raise ClubError(f"Online renewal isn't available yet. Please renew at the reception or call {config.ARENA_PHONE}.")
    u = db.one("SELECT phone FROM users WHERE id=?", user_id)
    return payments.create_order("club", config.CLUB_PRICE_MONTHLY * months, f"CLUB-{user_id}", u["phone"], user_id,
                                 meta={"months": months})


def complete_online(p):
    """An online club renewal has been paid (checkout or webhook): create it, exactly once."""
    months = json.loads(p["meta"] or "{}").get("months", 1)
    with db.tx() as c:
        if c.execute("SELECT ref_id FROM payments WHERE id=?", (p["id"],)).fetchone()["ref_id"]:
            return None  # already done
        cid = _create(c, p["user_id"], months, "online")
        c.execute("UPDATE payments SET ref_id=? WHERE id=?", (cid, p["id"]))
    _welcome(cid)
    return get(cid)


def _welcome(cid):
    m = get(cid)
    u = db.one("SELECT name, phone FROM users WHERE id=?", m["user_id"])
    h = config.CLUB_HOURS
    notify.send("membership_confirmed", u["phone"], {
        "name": u["name"].split()[0], "plan": PLAN_NAME,
        "end_date": Date.fromisoformat(m["end_date"]).strftime("%d %b %Y"),
        "access": (f"Every month: {h['badminton']}h badminton, {h['pickleball']}h pickleball, {h['padel']}h padel. "
                   f"Book in your member portal ({config.PUBLIC_URL}/portal); each booking's entry code is sent "
                   f"{config.DOOR_CODE_LEAD_MIN} minutes before."),
    }, dedupe_key=f"clubok:{cid}")


def get(cid):
    return db.one("SELECT * FROM club_memberships WHERE id=?", cid)


def cancel_membership(cid, staff):
    """Stops a club membership (refunds, if any, are handled at the desk). Booked hours stay booked."""
    db.conn().execute("UPDATE club_memberships SET status='cancelled' WHERE id=?", (cid,))
    db.conn().execute("UPDATE club_serials SET status='expired' WHERE club_id=? AND status='free'", (cid,))
    return get(cid)


# ---------------------------------------------------------------- booking with club hours

def _period_on(user_id, day):
    """The club month containing `day`: (membership, period index, first day, last day) or None."""
    r = db.one("SELECT s.club_id, s.period, s.period_start, s.period_end FROM club_serials s "
               "JOIN club_memberships m ON m.id=s.club_id WHERE s.user_id=? AND m.status='active' "
               "AND s.period_start<=? AND s.period_end>=? LIMIT 1", user_id, day.isoformat(), day.isoformat())
    return r


def book(user_id, court_id, day, start_hhmm, slots, staff=None, channel=None):
    """Book a court using club hours: no payment, one serial per hour from that sport's monthly allowance."""
    from . import bookings
    u = db.one("SELECT name, phone FROM users WHERE id=?", user_id)
    if not u:
        raise ClubError("Member not found")
    try:
        court, day, start, end, name, phone, _ = bookings._validate(court_id, day, start_hhmm, slots, u["name"], u["phone"])
    except bookings.BookingError as e:
        raise ClubError(str(e))
    minutes = (end - start).total_seconds() / 60
    if minutes % 60:
        raise ClubError("Club bookings are in whole hours")
    hours, sport = int(minutes // 60), court["sport"]
    per = _period_on(user_id, day)
    if not per:
        raise ClubError(f"No active club membership on {day.strftime('%d %b')}. Renew it to book that day.")
    with db.tx() as c:
        free = c.execute("SELECT id FROM club_serials WHERE club_id=? AND period=? AND sport=? AND status='free' "
                         "ORDER BY id LIMIT ?", (per["club_id"], per["period"], sport, hours)).fetchall()
        if len(free) < hours:
            raise ClubError(f"Only {len(free)} {sport} hour(s) left in this club month "
                            f"(until {Date.fromisoformat(per['period_end']).strftime('%d %b')}).")
        try:
            bid = bookings._insert(c, court, day, start, end, name, phone, 0, "confirmed", "club",
                                   "desk" if staff else "online", channel=channel)
        except bookings.BookingError as e:
            raise ClubError(str(e))
        c.execute("UPDATE bookings SET payment_ref=? WHERE id=?", (f"CLUB-{staff or 'member'}", bid))
        c.executemany("UPDATE club_serials SET status='booked', booking_id=?, booked_at=? WHERE id=?",
                      [(bid, clock.fmt(clock.now()), r["id"]) for r in free])
    return bookings._on_confirmed(bid)


def release(booking_id, give_back):
    """A club booking was cancelled: its hours go back to the balance, or are used up if cancelled late."""
    if give_back:
        db.conn().execute("UPDATE club_serials SET status='free', booking_id=NULL, booked_at=NULL "
                          "WHERE booking_id=? AND period_end>=?", (booking_id, clock.today().isoformat()))


def cancel_terms(b, by="customer"):
    hours_ahead = (clock.parse(b["start"]) - clock.now()).total_seconds() / 3600
    n = db.one("SELECT COUNT(*) AS n FROM club_serials WHERE booking_id=?", b["id"])["n"]
    give_back = by == "staff" or hours_ahead >= config.CLUB_RETURN_HOURS
    note = (f"Your {n} club hour(s) go back to your balance" if give_back else
            f"Cancelled under {config.CLUB_RETURN_HOURS}h before, so the {n} club hour(s) are used up")
    return give_back, note


def serial_for_booking(booking_id):
    r = db.one("SELECT serial FROM club_serials WHERE booking_id=? ORDER BY id LIMIT 1", booking_id)
    return r and r["serial"]


def expire():
    """Unused serials of finished club months expire (scheduler, every tick; cheap)."""
    db.conn().execute("UPDATE club_serials SET status='expired' WHERE status='free' AND period_end<?",
                      (clock.today().isoformat(),))


# ---------------------------------------------------------------- usage (member portal, reception, admin)

def usage(user_id, with_serials=False):
    """Everything about a person's club membership, or None if they've never had one."""
    ms = db.all_("SELECT * FROM club_memberships WHERE user_id=? ORDER BY start_date DESC", user_id)
    if not ms:
        return None
    today = clock.today().isoformat()
    rows = db.all_("SELECT period_start, period_end, sport, status, COUNT(*) AS n FROM club_serials WHERE user_id=? "
                   "GROUP BY club_id, period, sport, status ORDER BY period_start", user_id)
    months = {}
    for r in rows:
        k = (r["period_start"], r["period_end"])
        m = months.setdefault(k, {"from": r["period_start"], "to": r["period_end"],
                                  "sports": {s: {"included": 0, "booked": 0, "left": 0, "expired": 0} for s in SPORTS}})
        s = m["sports"][r["sport"]]
        s["included"] += r["n"]
        s[{"free": "left", "booked": "booked", "expired": "expired"}[r["status"]]] += r["n"]
    months = sorted(months.values(), key=lambda m: m["from"])
    for m in months:
        m["state"] = "current" if m["from"] <= today <= m["to"] else ("past" if m["to"] < today else "upcoming")
        m["total"] = {k: sum(s[k] for s in m["sports"].values()) for k in ("included", "booked", "left", "expired")}
    current = next((m for m in months if m["state"] == "current"), None)
    active = [m for m in ms if m["status"] == "active" and m["end_date"] >= today]
    until = max((m["end_date"] for m in active), default=None)
    bookings = db.all_(
        "SELECT b.id, b.ref, b.start, b.end, b.status, c.name AS court, c.sport, "
        "(SELECT COUNT(*) FROM club_serials s WHERE s.booking_id=b.id) AS hours FROM bookings b "
        "JOIN courts c ON c.id=b.court_id WHERE b.user_id=? AND b.pay_status='club' ORDER BY b.start DESC LIMIT 60",
        user_id)
    out = {"plan": PLAN_NAME, "active": bool(until), "until": until,
           "days_left": (Date.fromisoformat(until) - clock.today()).days if until else None,
           "memberships": ms, "months": months, "current": current, "bookings": bookings,
           "hours_per_month": hours_per_month(), "price_monthly": config.CLUB_PRICE_MONTHLY,
           "renew_online": bool(config.CLUB_PRICE_MONTHLY and config.ONLINE_PAY),
           "month_options": config.CLUB_MONTH_OPTIONS, "return_hours": config.CLUB_RETURN_HOURS,
           "renews_from": _next_start(user_id).isoformat()}
    if with_serials and current:
        out["serials"] = db.all_(
            "SELECT s.serial, s.sport, s.status, b.ref, b.start FROM club_serials s LEFT JOIN bookings b "
            "ON b.id=s.booking_id WHERE s.user_id=? AND s.period_start=? ORDER BY s.sport, s.id",
            user_id, current["from"])
    return out


def is_club_member(user_id, day=None):
    day = (day or clock.today()).isoformat()
    return bool(db.one("SELECT 1 AS x FROM club_memberships WHERE user_id=? AND status='active' "
                       "AND start_date<=? AND end_date>=?", user_id, day, day))


def roster():
    """Every current club member with this month's hours left, for the admin Club tab."""
    today = clock.today().isoformat()
    from .auth import member_id
    out = []
    for m in db.all_("SELECT u.id, u.name, u.phone, MAX(m.end_date) AS until FROM club_memberships m "
                     "JOIN users u ON u.id=m.user_id WHERE m.status='active' AND m.end_date>=? "
                     "GROUP BY u.id ORDER BY u.name", today):
        left = {s: 0 for s in SPORTS}
        booked = {s: 0 for s in SPORTS}
        for r in db.all_("SELECT sport, status, COUNT(*) AS n FROM club_serials WHERE user_id=? AND period_start<=? "
                         "AND period_end>=? GROUP BY sport, status", m["id"], today, today):
            if r["status"] == "free":
                left[r["sport"]] += r["n"]
            elif r["status"] == "booked":
                booked[r["sport"]] += r["n"]
        out.append({**m, "member_id": member_id(m["id"]), "left": left, "booked": booked})
    return out
