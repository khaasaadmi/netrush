"""Event and party enquiries from the website ("Planning to hold an event?").

Every enquiry is stored and shown in Admin → Events. If EVENT_NOTIFY_PHONE is set, a short alert also
goes to that staff number (WhatsApp by default), so nobody has to keep the panel open to notice.
"""
import threading
import time
from datetime import timedelta

from . import clock, config, db, notify

STATUSES = ("new", "contacted", "booked", "closed")
MAX_PEOPLE = 2000
PER_IP_PER_HOUR = 5

_hits, _lock = {}, threading.Lock()


class EventError(Exception):
    pass


def _allowed(ip):
    if not ip:
        return True
    t = time.time()
    with _lock:
        recent = [x for x in _hits.get(ip, []) if t - x < 3600]
        if len(recent) >= PER_IP_PER_HOUR:
            _hits[ip] = recent
            return False
        _hits[ip] = recent + [t]
        return True


def submit(name, phone, people, message=None, ip=None, source=None, trap=None):
    """Store an enquiry. Returns its id (or None for an obvious bot)."""
    from .auth import AuthError, normalize_phone
    if trap:   # a hidden field people never see; bots fill it in. Pretend it worked.
        return None
    name = " ".join((name or "").split())[:80]
    if len(name) < 2:
        raise EventError("Please enter your full name")
    try:
        phone = normalize_phone(phone)
    except AuthError:
        raise EventError("Please enter a 10-digit mobile number")
    try:
        people = int(str(people).strip())
    except (TypeError, ValueError):
        raise EventError("Please enter the number of people")
    if not 1 <= people <= MAX_PEOPLE:
        raise EventError(f"Number of people should be between 1 and {MAX_PEOPLE}")
    message = (message or "").strip()[:1000] or None
    now = clock.now()
    # a double tap or a re-submit within 10 minutes updates the same enquiry instead of adding another
    dup = db.one("SELECT id FROM event_enquiries WHERE phone=? AND created_at >= ? ORDER BY id DESC LIMIT 1",
                 phone, clock.fmt(now - timedelta(minutes=10)))
    if dup:
        db.conn().execute("UPDATE event_enquiries SET name=?, people=?, message=COALESCE(?, message) WHERE id=?",
                          (name, people, message, dup["id"]))
        return dup["id"]
    if not _allowed(ip):
        raise EventError("We've received several enquiries from here already. Please call us instead.")
    eid = db.conn().execute(
        "INSERT INTO event_enquiries(created_at,name,phone,people,message,status,source) VALUES(?,?,?,?,?,'new',?)",
        (clock.fmt(now), name, phone, people, message, (source or "")[:60] or None)).lastrowid
    if config.EVENT_NOTIFY_PHONE:
        notify.send("event_enquiry", config.EVENT_NOTIFY_PHONE, {
            "name": name, "phone": phone, "people": people, "message": (message or "-")[:200]},
            dedupe_key=f"event:{eid}", booking_channel=config.EVENT_NOTIFY_CHANNEL)
    return eid


def listing(status=None, limit=200):
    if status in STATUSES:
        rows = db.all_("SELECT * FROM event_enquiries WHERE status=? ORDER BY id DESC LIMIT ?", status, limit)
    else:
        rows = db.all_("SELECT * FROM event_enquiries ORDER BY id DESC LIMIT ?", limit)
    counts = {s: 0 for s in STATUSES}
    for r in db.all_("SELECT status, COUNT(*) AS n FROM event_enquiries GROUP BY status"):
        counts[r["status"]] = r["n"]
    return {"enquiries": rows, "counts": counts}


def update(enquiry_id, staff, status=None, note=None):
    e = db.one("SELECT * FROM event_enquiries WHERE id=?", enquiry_id)
    if not e:
        raise EventError("Enquiry not found")
    if status is not None and status not in STATUSES:
        raise EventError("Unknown status")
    db.conn().execute("UPDATE event_enquiries SET status=COALESCE(?, status), note=COALESCE(?, note), "
                      "handled_by=?, updated_at=? WHERE id=?",
                      (status, None if note is None else note.strip()[:500], staff, clock.fmt(clock.now()), enquiry_id))
    return db.one("SELECT * FROM event_enquiries WHERE id=?", enquiry_id)
