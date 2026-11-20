"""Court availability and the booking lifecycle:

    online:  pending (slot held HOLD_MIN) --paid--> confirmed --slot ends--> (done)
                  \\--hold lapses--> expired          \\--cancel--> cancelled (+ automatic refund)
    staff:   confirmed straight away — paid at desk, unpaid (phone reservation, pay later) or complimentary

Every confirmed booking gets a door code, however it was paid.
"""
import re
import secrets
from datetime import date as Date, datetime, timedelta

from . import access, clock, config, db, notify, payments


class BookingError(Exception):
    pass


def _slot_starts(day):
    midnight = datetime.combine(day, datetime.min.time())
    t, end = midnight + timedelta(hours=config.OPEN_HOUR), midnight + timedelta(hours=config.CLOSE_HOUR)
    while t + timedelta(minutes=config.SLOT_MIN) <= end:
        yield t
        t += timedelta(minutes=config.SLOT_MIN)


def _live_bookings(c, court_ids, start, end):
    """Bookings that occupy a court in [start, end): confirmed, or unpaid but still inside their hold."""
    marks = ",".join("?" * len(court_ids))
    return c.execute(
        f"SELECT court_id, start, end, status FROM bookings WHERE court_id IN ({marks}) "
        "AND start < ? AND end > ? AND (status='confirmed' OR (status IN ('pending','reserved') "
        "AND hold_until > ?))",
        (*court_ids, clock.fmt(end), clock.fmt(start), clock.fmt(clock.now())),
    ).fetchall()


def _blocks(c, court_ids, day):
    """Academy batches (recurring weekly) and one-off blocks on that day → [(court, start, end, reason)]."""
    out = []
    for b in c.execute("SELECT * FROM academy_batches").fetchall():
        if str(day.weekday()) in b["weekdays"].split(","):
            for cid in b["court_ids"].split(","):
                if cid in court_ids:
                    out.append((cid, b["start_time"], b["end_time"], "Academy"))
    for b in c.execute("SELECT * FROM court_blocks WHERE date=?", (day.isoformat(),)).fetchall():
        if b["court_id"] in court_ids:
            out.append((b["court_id"], b["start"], b["end"], b["reason"] or "Blocked"))
    return out


def _blocked(blocks, court_id, s, e):
    for cid, bs, be, reason in blocks:
        if cid == court_id and bs < e.strftime("%H:%M") and be > s.strftime("%H:%M"):
            return reason
    return None


def availability(sport, day):
    """Grid for the booking page: courts × slots with state free | booked | blocked | past."""
    courts = db.all_("SELECT id, name FROM courts WHERE sport=? AND active=1 ORDER BY id", sport)
    if not courts:
        raise BookingError("Unknown sport")
    ids = [c["id"] for c in courts]
    c = db.conn()
    starts = list(_slot_starts(day))
    slot = timedelta(minutes=config.SLOT_MIN)
    taken = _live_bookings(c, ids, starts[0], starts[-1] + slot) if starts else []
    blocks = _blocks(c, ids, day)
    now = clock.now()
    grid = []
    for s in starts:
        e = s + slot
        row = {"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"), "cells": {}}
        for cid in ids:
            state = "free"
            if s < now:
                state = "past"
            elif any(b["court_id"] == cid and b["start"] < clock.fmt(e) and b["end"] > clock.fmt(s) for b in taken):
                state = "booked"
            elif (reason := _blocked(blocks, cid, s, e)):
                state = "blocked:" + reason
            row["cells"][cid] = state
        grid.append(row)
    return {"sport": sport, "date": day.isoformat(), "price": config.PRICES[sport],
            "price_per_hour": config.PRICES_PER_HOUR[sport],
            "slot_min": config.SLOT_MIN, "courts": courts, "slots": grid}


def _clean_email(email):
    email = (email or "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise BookingError("That email address doesn't look right")
    return email or None


def _validate(court_id, day, start_hhmm, slots, name, phone):
    from .auth import normalize_phone, AuthError

    try:
        phone = normalize_phone(phone)
    except AuthError as e:
        raise BookingError(str(e))
    name = (name or "").strip()
    if not name:
        raise BookingError("Please enter a name")
    slots = int(slots)
    if not 1 <= slots <= 4:
        raise BookingError("Book between 1 and 4 slots at a time")
    court = db.one("SELECT * FROM courts WHERE id=? AND active=1", court_id)
    if not court:
        raise BookingError("Unknown court")
    day = Date.fromisoformat(day) if isinstance(day, str) else day
    if not clock.today() <= day <= clock.today() + timedelta(days=config.BOOKING_HORIZON_DAYS):
        raise BookingError(f"You can book up to {config.BOOKING_HORIZON_DAYS} days ahead")
    valid_starts = {s.strftime("%H:%M"): s for s in _slot_starts(day)}
    if start_hhmm not in valid_starts:
        raise BookingError("Pick a start time from the grid")
    start = valid_starts[start_hhmm]
    end = start + timedelta(minutes=config.SLOT_MIN * slots)
    if start < clock.now():
        raise BookingError("That slot has already started")
    if end > datetime.combine(day, datetime.min.time()) + timedelta(hours=config.CLOSE_HOUR):
        raise BookingError("That runs past closing time")
    return court, day, start, end, name, phone, config.PRICES[court["sport"]] * slots


def _insert(c, court, day, start, end, name, phone, amount, status, pay_status, source, email=None,
            hold=None, discount=0, utm=None, heard=None, channel=None):
    if _live_bookings(c, [court["id"]], start, end):
        raise BookingError("Sorry, that slot has just been booked. Please pick another.")
    if (reason := _blocked(_blocks(c, [court["id"]], day), court["id"], start, end)):
        raise BookingError(f"That court is reserved ({reason}) at that time")
    user = c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
    if status == "pending":
        hold = clock.now() + timedelta(minutes=config.HOLD_MIN)
    return c.execute(
        "INSERT INTO bookings(ref,court_id,user_id,name,phone,start,end,amount,status,hold_until,created_at,"
        "pay_status,source,email,access_key,discount,utm,heard,notify_channel) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (secrets.token_hex(3).upper(), court["id"], user and user["id"], name, phone, clock.fmt(start),
         clock.fmt(end), amount - discount, status, hold and clock.fmt(hold), clock.fmt(clock.now()), pay_status,
         source, email, secrets.token_urlsafe(12), discount, (utm or "")[:60] or None,
         (heard or "")[:40] or None, channel if channel in ("whatsapp", "sms") else None)).lastrowid


def first_booking_pct(phone, session_user):
    """15% off the first booking, for a guest who has verified this mobile number by OTP
    (= is logged in with it). 0 if not verified or the number has booked before."""
    if not config.FIRST_BOOKING_DISCOUNT_PCT or not session_user or session_user.get("role"):
        return 0
    if session_user["phone"] != phone:
        return 0
    used = db.one(
        "SELECT 1 AS x FROM bookings WHERE phone=? AND (status='confirmed' OR (status='cancelled' AND "
        "pay_status='paid') OR (status IN ('pending','reserved') AND hold_until > ?)) LIMIT 1",
        phone, clock.fmt(clock.now()))
    return 0 if used else config.FIRST_BOOKING_DISCOUNT_PCT


def reserve_deadline(start):
    """When an unpaid reservation made now for a slot at `start` lapses, or None if too close to reserve."""
    now = clock.now()
    if start - now < timedelta(minutes=config.RESERVE_MIN_LEAD_MIN):
        return None
    return min(now + timedelta(hours=config.RESERVE_HOLD_HOURS), start - timedelta(minutes=30))


def create(court_id, day, start_hhmm, slots, name, phone, email=None, mode="pay", session_user=None, utm=None,
           heard=None, channel=None):
    """Online booking by a guest. mode 'pay': hold the slot and open a payment.
    mode 'reserve': hold it for RESERVE_HOLD_HOURS, to be confirmed by paying or calling."""
    if mode not in ("pay", "reserve"):
        raise BookingError("Choose pay now or reserve")
    court, day, start, end, name, phone, amount = _validate(court_id, day, start_hhmm, slots, name, phone)
    email = _clean_email(email)
    discount = amount * first_booking_pct(phone, session_user) // 100
    if mode == "reserve":
        deadline = reserve_deadline(start)
        if not deadline:
            raise BookingError(f"This slot starts in under {config.RESERVE_MIN_LEAD_MIN} minutes, so it can't be "
                               "reserved. Please pay now to book it.")
        with db.tx() as c:
            bid = _insert(c, court, day, start, end, name, phone, amount, "reserved", "unpaid", "online", email,
                          hold=deadline, discount=discount, utm=utm, heard=heard, channel=channel)
        b = get(bid)
        notify.send("reservation_received", phone, {
            "name": name.split()[0], "ref": b["ref"], "court": b["court_name"], "when": _when(b),
            "deadline": deadline.strftime("%H:%M on %a %d %b"), "phone": config.ARENA_PHONE,
            "amount": f"Rs {b['amount']}"}, dedupe_key=f"reserve:{bid}", booking_channel=b["notify_channel"])
        return {"booking": b, "payment": None}
    if not config.ONLINE_PAY:
        raise BookingError("Online payment isn't available yet. Please reserve the slot and pay at the venue.")
    with db.tx() as c:
        bid = _insert(c, court, day, start, end, name, phone, amount, "pending", "unpaid", "online", email,
                      discount=discount, utm=utm, heard=heard, channel=channel)
    b = get(bid)
    return {"booking": b, "payment": start_payment(b)}


def start_payment(b):
    """Open an online payment for a booking that still owes money (new, reserved, or pay-at-venue)."""
    if b["pay_status"] != "unpaid" or b["status"] not in ("pending", "reserved", "confirmed"):
        raise BookingError("There is nothing to pay on this booking")
    if not config.ONLINE_PAY:
        raise BookingError(f"Online payment isn't available yet. Please call {config.ARENA_PHONE} or pay at the venue.")
    return payments.create_order("booking", balance(b), b["ref"], b["phone"], b["user_id"], ref_id=b["id"])


def confirm_reservation(booking_id, staff, paid=False, method="cash", advance=None):
    """Reception confirms a held reservation after a call. paid=False: pay at the venue.
    paid=True: money received now, in full or as an advance (`advance` rupees)."""
    b = get(booking_id)
    if not b or b["status"] != "reserved":
        raise BookingError("This reservation is no longer waiting for confirmation")
    got = 0
    if paid:
        got = b["amount"] if advance in (None, "") else int(advance)
        if not 0 < got <= b["amount"]:
            raise BookingError(f"Amount received should be between 1 and {b['amount']}")
    paid = paid and got >= b["amount"]
    with db.tx() as c:
        cur = c.execute("UPDATE bookings SET status='confirmed', hold_until=NULL, payment_ref=?, pay_status=?, "
                        "paid_at=? WHERE id=? AND status='reserved'",
                        (f"CONFIRMED-{staff}", "paid" if paid else "unpaid",
                         clock.fmt(clock.now()) if paid else None, booking_id))
        if cur.rowcount != 1:
            raise BookingError("This reservation is no longer waiting for confirmation")
    if got:
        payments.record_desk("booking", booking_id, got, b["phone"], b["user_id"], staff, method=method)
    return _on_confirmed(booking_id)


STAFF_MODES = {
    "desk_paid": ("paid", "desk"),   # cash / UPI at the counter
    "unpaid": ("unpaid", "phone"),   # reservation taken on a call, pays later
    "comp": ("comp", "desk"),        # complimentary: coaching demo, sponsor, make-good
}


def balance(b):
    """What's still owed on a booking after any advance."""
    return max(0, b["amount"] - payments.paid_total("booking", b["id"]))


def create_by_staff(court_id, day, start_hhmm, slots, name, phone, mode, staff, email=None, method="cash",
                    received=None, channel=None):
    if mode not in STAFF_MODES:
        raise BookingError("Choose how this booking is paid")
    court, day, start, end, name, phone, amount = _validate(court_id, day, start_hhmm, slots, name, phone)
    email = _clean_email(email)
    pay_status, source = STAFF_MODES[mode]
    # Paid at the desk: the amount actually received may be an advance (less than the price).
    received = amount if received in (None, "") else int(received)
    if mode == "desk_paid" and not 0 < received <= amount:
        raise BookingError(f"Amount received should be between 1 and {amount}")
    if mode == "desk_paid" and received < amount:
        pay_status = "unpaid"  # balance still to collect
    with db.tx() as c:
        bid = _insert(c, court, day, start, end, name, phone, amount, "confirmed", pay_status, source, email,
                      channel=channel)
        c.execute("UPDATE bookings SET payment_ref=?, paid_at=? WHERE id=?",
                  (f"{mode.upper()}-{staff}", clock.fmt(clock.now()) if pay_status == "paid" else None, bid))
    if mode == "desk_paid":
        payments.record_desk("booking", bid, received, phone, staff=staff, method=method)
    return _on_confirmed(bid)


def mark_paid(booking_id, staff, method="cash"):
    """The balance on a pay-at-venue booking is collected at the desk (cash / UPI QR / card)."""
    b = get(booking_id)
    if not b or b["pay_status"] != "unpaid" or b["status"] != "confirmed":
        raise BookingError("Nothing to collect on this booking")
    payments.record_desk("booking", booking_id, balance(b), b["phone"], b["user_id"], staff, method=method)
    db.conn().execute("UPDATE bookings SET pay_status='paid', paid_at=? WHERE id=?",
                      (clock.fmt(clock.now()), booking_id))
    return get(booking_id)


def get(booking_id):
    return db.one("SELECT b.*, c.name AS court_name, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
                  "WHERE b.id=?", booking_id)


def confirm_payment(booking_id, payload):
    p = db.one("SELECT * FROM payments WHERE kind='booking' AND ref_id=? ORDER BY id DESC LIMIT 1", booking_id)
    if not p:
        raise BookingError("Booking not found")
    p, _ = payments.capture(p["id"], payload)
    return fulfil(p)


def fulfil(p):
    """Payment for a booking arrived (checkout or webhook). Confirm it, or refund it if we can't."""
    b = get(p["ref_id"])
    if b["status"] == "confirmed":
        if b["pay_status"] == "unpaid":  # confirmed as pay-at-venue, then paid online after all
            db.conn().execute("UPDATE bookings SET pay_status='paid', payment_ref=?, paid_at=? WHERE id=?",
                              (p["payment_id"], clock.fmt(clock.now()), b["id"]))
        return get(b["id"])
    if b["status"] == "cancelled":
        payments.request_refund(p["id"], p["amount"])
        raise BookingError("This booking was cancelled; your payment is being refunded.")
    with db.tx() as c:
        # Paid after the hold lapsed: still fine unless someone else has confirmed the slot since.
        # (An unpaid hold by someone else loses to money in hand.)
        taken = c.execute(
            "SELECT 1 FROM bookings WHERE court_id=? AND id<>? AND status='confirmed' AND start<? AND end>?",
            (b["court_id"], b["id"], b["end"], b["start"])).fetchone()
        if taken:
            c.execute("UPDATE bookings SET status='cancelled', cancelled_at=?, cancelled_by='system', "
                      "refund_amount=?, hold_until=NULL WHERE id=?",
                      (clock.fmt(clock.now()), p["amount"], b["id"]))
        else:
            c.execute("UPDATE bookings SET status='confirmed', pay_status='paid', payment_ref=?, paid_at=?, "
                      "hold_until=NULL WHERE id=?", (p["payment_id"], clock.fmt(clock.now()), b["id"]))
    if taken:
        payments.request_refund(p["id"], p["amount"])
        raise BookingError("Payment received, but the slot was taken after your hold ran out. "
                           "Your money is being refunded automatically.")
    return _on_confirmed(b["id"])


def _on_confirmed(booking_id):
    b = get(booking_id)
    notify.send("booking_confirmed", b["phone"], {
        "name": b["name"].split()[0], "ref": b["ref"], "court": b["court_name"],
        "when": _when(b), "lead": config.DOOR_CODE_LEAD_MIN,
    }, dedupe_key=f"booking:{b['id']}", booking_channel=b["notify_channel"])
    access.issue_due_codes()  # booked at the last minute? the code goes out right now
    return get(booking_id)


def _when(b):
    return clock.parse(b["start"]).strftime("%a %d %b, %H:%M") + "-" + b["end"][11:]


# ---------------------------------------------------------------- cancellation + refunds

def refund_quote(b, by="customer"):
    """What a cancellation right now would refund: {amount, pct, can_cancel, note}."""
    started = clock.parse(b["start"]) <= clock.now()
    if b["status"] not in ("confirmed", "pending", "reserved"):
        return {"can_cancel": False, "amount": 0, "pct": 0, "note": "Already " + b["status"]}
    if by == "customer" and started:
        return {"can_cancel": False, "amount": 0, "pct": 0, "note": "The slot has already started"}
    paid = payments.paid_total("booking", b["id"])  # may be an advance, less than the full price
    if not paid:
        return {"can_cancel": True, "amount": 0, "pct": 0, "note": "Nothing was charged"}
    if by == "staff":
        pct = 100
    else:
        hours = (clock.parse(b["start"]) - clock.now()).total_seconds() / 3600
        pct = 100 if hours >= config.REFUND_FULL_HOURS else (
            config.REFUND_PARTIAL_PCT if hours >= config.REFUND_PARTIAL_HOURS else 0)
    amount = paid * pct // 100
    note = (f"Full refund of Rs {amount}" if pct == 100 else
            f"{pct}% refund: Rs {amount} (cancelled under {config.REFUND_FULL_HOURS}h before)" if pct else
            f"No refund (under {config.REFUND_PARTIAL_HOURS}h before start)")
    return {"can_cancel": True, "amount": amount, "pct": pct, "note": note}


def refund_policy_text():
    return (f"Free cancellation up to {config.REFUND_FULL_HOURS} hours before. "
            f"{config.REFUND_PARTIAL_PCT}% back if cancelled {config.REFUND_PARTIAL_HOURS}–{config.REFUND_FULL_HOURS} "
            f"hours before. No refund after that.")


def cancel(booking_id, by="customer"):
    """by: customer (refund per policy) | staff (always full refund)."""
    b = get(booking_id)
    if not b:
        raise BookingError("Booking not found")
    q = refund_quote(b, by)
    if not q["can_cancel"]:
        raise BookingError(f"This booking can't be cancelled: {q['note'].lower()}")
    with db.tx() as c:
        cur = c.execute("UPDATE bookings SET status='cancelled', cancelled_at=?, cancelled_by=?, refund_amount=?, "
                        "hold_until=NULL WHERE id=? AND status IN ('confirmed','pending','reserved')",
                        (clock.fmt(clock.now()), by, q["amount"], booking_id))
        if cur.rowcount != 1:
            raise BookingError("This booking was already cancelled")
    access.revoke_for_booking(booking_id, f"cancelled by {by}")
    p, left = None, q["amount"]
    for pay in db.all_("SELECT * FROM payments WHERE kind='booking' AND ref_id=? AND status='paid' ORDER BY id DESC",
                       booking_id):  # usually one payment; an advance plus a balance makes two
        if left <= 0:
            break
        part = min(left, pay["amount"])
        p = payments.request_refund(pay["id"], part) or pay
        left -= part
    if b["status"] in ("confirmed", "reserved"):
        if q["amount"] and p and p["provider"] == "desk":
            refund = f"Rs {q['amount']} will be handed back to you at the front desk."
        elif q["amount"]:
            refund = f"Rs {q['amount']} is being refunded to your original payment method."
        elif payments.paid_total("booking", booking_id):
            refund = q["note"] + "."
        else:
            refund = "Nothing was charged."
        notify.send("booking_cancelled", b["phone"], {
            "name": b["name"].split()[0], "ref": b["ref"], "court": b["court_name"], "when": _when(b),
            "refund": refund}, dedupe_key=f"cancel:{booking_id}", booking_channel=b["notify_channel"])
    return {"booking": get(booking_id), "refund": q}


def expire_holds():
    now = clock.fmt(clock.now())
    db.conn().execute("UPDATE bookings SET status='expired' WHERE status='pending' AND hold_until <= ?", (now,))
    # Reservations nobody confirmed in time: release the slot and tell the customer.
    for b in db.all_("SELECT b.*, c.name AS court_name FROM bookings b JOIN courts c ON c.id=b.court_id "
                     "WHERE b.status='reserved' AND b.hold_until <= ?", now):
        cur = db.conn().execute("UPDATE bookings SET status='expired' WHERE id=? AND status='reserved'", (b["id"],))
        if cur.rowcount == 1:
            notify.send("reservation_released", b["phone"], {
                "name": b["name"].split()[0], "ref": b["ref"], "court": b["court_name"], "when": _when(b)},
                dedupe_key=f"released:{b['id']}", booking_channel=b["notify_channel"])


def lookup(ref, phone):
    from .auth import normalize_phone
    return db.one("SELECT b.*, c.name AS court_name, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
                  "WHERE b.ref=? AND b.phone=?", (ref or "").upper().strip(), normalize_phone(phone))


def release_hold(booking_id):
    """Customer backed out on the payment page: free the slot straight away."""
    db.conn().execute("UPDATE bookings SET status='expired', hold_until=NULL WHERE id=? AND status='pending'",
                      (booking_id,))


def by_key(ref, key):
    """The booking behind a confirmation link."""
    import hmac
    b = db.one("SELECT b.*, c.name AS court_name, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
               "WHERE b.ref=?", (ref or "").upper())
    if not b or not b["access_key"] or not hmac.compare_digest(b["access_key"], key or ""):
        return None
    return b
