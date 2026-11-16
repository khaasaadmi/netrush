"""NetRush — landing page, court booking, memberships and academy for a multi-sport arena.

    python server.py   → http://localhost:8000  (landing)  /book  /portal (members, guests)  /admin (staff)
"""
import csv
import io
import json
import re
import sys
import traceback
from datetime import date as Date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import (access, analytics, auth, bookings, clock, config, db, health, members, payments, scheduler,
                 tracking)
from app.auth import AuthError
from app.bookings import BookingError
from app.members import MemberError
from app.payments import PaymentError

STATIC = Path(__file__).parent / "static"
PAGES = {"/": "landing.html", "/book": "book.html", "/portal": "portal.html", "/admin": "admin.html", "/pay": "pay.html",
         "/booking": "booking.html", "/demo": "demo.html", "/door": "door.html"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8",
         ".xml": "application/xml", ".woff2": "font/woff2"}
COOKIE = "nr_session"

ROUTES = []


def route(method, pattern, who="public"):
    """who: public | user (logged-in member/guest) | staff (admin or receptionist) | admin
    | device (signed by the door)."""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), who, fn))
        return fn
    return deco


class Raw:
    """A non-JSON response (file download)."""
    def __init__(self, body, content_type, filename=None):
        self.body, self.content_type, self.filename = body, content_type, filename


class HttpError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


# ------------------------------------------------------------------ public

@route("GET", "/api/config")
def get_config(req):
    courts = db.all_("SELECT id, sport, name FROM courts WHERE active=1 ORDER BY id")
    sports = {}
    for c in courts:
        sports.setdefault(c["sport"], {"sport": c["sport"], "price": config.PRICES[c["sport"]],
                                       "price_per_hour": config.PRICES_PER_HOUR[c["sport"]], "courts": []})
        sports[c["sport"]]["courts"].append(c)
    return {"facility": config.FACILITY_NAME, "sports": list(sports.values()), "today": clock.today().isoformat(),
            "horizon_days": config.BOOKING_HORIZON_DAYS, "slot_min": config.SLOT_MIN,
            "door_code_lead_min": config.DOOR_CODE_LEAD_MIN, "hold_min": config.HOLD_MIN,
            "payment_provider": config.PAYMENT_PROVIDER, "refund_policy": bookings.refund_policy_text(),
            "demo": config.DEMO_MODE, "lock": config.LOCK_ENABLED, "otp": auth.otp_available(),
            "online_pay": config.ONLINE_PAY,
            "first_discount_pct": config.FIRST_BOOKING_DISCOUNT_PCT, "heard_options": tracking.HEARD,
            "public_url": config.PUBLIC_URL,
            "referral_reward": config.REFERRAL_REWARD,
            "reserve_hours": config.RESERVE_HOLD_HOURS, "reserve_min_lead_min": config.RESERVE_MIN_LEAD_MIN,
            "open_hour": config.OPEN_HOUR, "close_hour": config.CLOSE_HOUR,
            "arena": {"phone": config.ARENA_PHONE, "address": config.ARENA_ADDRESS, "maps": config.ARENA_MAPS_URL,
                      "instagram": config.ARENA_INSTAGRAM},
            "membership_months": config.ONLINE_MEMBERSHIP_MONTH_OPTIONS,
            "membership_monthly": config.ONLINE_MEMBERSHIP_MONTHLY}


@route("GET", "/api/availability")
def get_availability(req):
    q = req.query
    try:
        day = Date.fromisoformat(q.get("date") or clock.today().isoformat())
    except ValueError:
        raise HttpError(400, "Bad date")
    return bookings.availability(q.get("sport", "badminton"), day)


@route("POST", "/api/bookings")
def post_booking(req):
    """Guest booking: name + mobile (email optional). mode 'pay' → redirect to the payment page;
    mode 'reserve' → slot held for 3 hours, redirect to the booking page. The first-booking discount
    applies when the caller has verified this mobile by OTP (is logged in with it)."""
    b = req.body
    out = bookings.create(b.get("court_id"), b.get("date"), b.get("start"), b.get("slots", 1),
                          b.get("name"), b.get("phone"), b.get("email"), b.get("mode") or "pay",
                          auth.user_for(req.token), _tag(b.get("src")),
                          b.get("heard") if b.get("heard") in tracking.HEARD else None)
    bk = out["booking"]
    redirect = out["payment"]["redirect"] if out["payment"] else f"/booking?ref={bk['ref']}&k={bk['access_key']}"
    return {"booking": _public_booking(bk), "redirect": redirect}


def _tag(src):
    src = src if isinstance(src, dict) else {}
    return tracking.booking_tag(src.get("source"), src.get("campaign"), src.get("referrer"))


@route("POST", "/api/track")
def track(req):
    """The public pages report one visit per browser session, with where the visitor came from."""
    b = req.body
    tracking.record(b.get("vid"), b.get("source"), b.get("medium"), b.get("campaign"), b.get("referrer"),
                    b.get("path"), req.ip)
    return {"ok": True}


@route("GET", "/api/offer")
def get_offer(req):
    """Is the visitor's number verified, and do they still have the first-booking discount?"""
    u = auth.user_for(req.token)
    if not u or u.get("role"):
        return {"verified": False, "pct": 0, "offer_pct": config.FIRST_BOOKING_DISCOUNT_PCT}
    return {"verified": True, "name": u["name"], "phone": u["phone"], "email": u["email"],
            "pct": bookings.first_booking_pct(u["phone"], u), "offer_pct": config.FIRST_BOOKING_DISCOUNT_PCT}


@route("POST", "/api/booking/pay")
def pay_by_link(req):
    """Pay online for a reservation (or a pay-at-venue booking) from its booking link."""
    b = bookings.by_key(req.body.get("ref"), req.body.get("k"))
    if not b:
        raise HttpError(404, "Booking not found")
    return {"redirect": bookings.start_payment(b)["redirect"]}


@route("GET", "/api/booking")
def get_booking(req):
    """The confirmation page (/booking?ref=…&k=…): the link itself is the proof of ownership."""
    b = bookings.by_key(req.query.get("ref"), req.query.get("k"))
    if not b:
        raise HttpError(404, "Booking not found")
    return {"booking": _public_booking(b, with_quote=True), "refund_policy": bookings.refund_policy_text()}


@route("POST", "/api/booking/cancel")
def cancel_by_link(req):
    b = bookings.by_key(req.body.get("ref"), req.body.get("k"))
    if not b:
        raise HttpError(404, "Booking not found")
    out = bookings.cancel(b["id"], "customer")
    return {"booking": _public_booking(out["booking"], with_quote=True), "refund": out["refund"]}


@route("GET", "/api/bookings/lookup")
def lookup_booking(req):
    b = bookings.lookup(req.query.get("ref"), req.query.get("phone"))
    if not b:
        raise HttpError(404, "No booking with that reference and phone number")
    return {"booking": _public_booking(b, with_quote=True)}


@route("POST", "/api/bookings/cancel")
def customer_cancel(req):
    """From 'Find my booking': reference + phone + a one-time code sent to that phone."""
    phone = auth.consume_code(req.body.get("phone"), req.body.get("code"))
    b = bookings.lookup(req.body.get("ref"), phone)
    if not b:
        raise HttpError(404, "No booking with that reference and phone number")
    out = bookings.cancel(b["id"], "customer")
    return {"booking": _public_booking(out["booking"]), "refund": out["refund"]}


# ------------------------------------------------------------------ payment page (/pay?id=…&k=…)

def _payment(req, src):
    p = payments.for_payer(int(src.get("id") or 0), src.get("k"))
    if not p:
        raise HttpError(404, "Payment link not found")
    return p


@route("GET", "/api/pay")
def pay_info(req):
    p = _payment(req, req.query)
    out = {"id": p["id"], "amount": p["amount"], "kind": p["kind"], "status": p["status"],
           "provider": p["provider"], "facility": config.FACILITY_NAME, "phone": p["phone"]}
    if p["kind"] == "booking":
        b = bookings.get(p["ref_id"])
        out["title"] = f"{b['court_name']} booking"
        out["detail"] = f"{clock.parse(b['start']).strftime('%a %d %b, %H:%M')}-{b['end'][11:]} · {b['name']}"
        out["hold_seconds"] = max(0, int((clock.parse(b["hold_until"]) - clock.now()).total_seconds()))             if b["hold_until"] else None
        out["booking_status"] = b["status"]
    else:
        months = json.loads(p["meta"] or "{}").get("months", 1)
        out["title"] = "Membership"
        out["detail"] = f"{months} month{'s' if months > 1 else ''} · all courts · fingerprint entry"
    if p["provider"] == "razorpay":
        out.update(key_id=config.RAZORPAY_KEY_ID, order_id=p["order_id"])
    return out


@route("POST", "/api/pay/complete")
def pay_complete(req):
    """The gateway says it's paid. Verify, fulfil, and tell the browser where to go next."""
    p = _payment(req, req.body)
    p, _ = payments.capture(p["id"], req.body)
    try:
        _fulfil(p)
    except BookingError:
        pass  # slot lost after the hold ran out: already refunded; the booking page explains
    return {"redirect": _after_payment(p)}


@route("POST", "/api/pay/cancel")
def pay_cancel(req):
    p = _payment(req, req.body)
    if p["status"] != "paid" and p["kind"] == "booking":
        b = bookings.get(p["ref_id"])
        if b["status"] != "pending":  # a reservation or pay-at-venue booking: it stays, just unpaid
            return {"redirect": f"/booking?ref={b['ref']}&k={b['access_key']}"}
        bookings.release_hold(p["ref_id"])
        return {"redirect": "/book?cancelled=1"}
    return {"redirect": _after_payment(p) if p["status"] == "paid" else "/portal"}


def _after_payment(p):
    if p["kind"] == "booking":
        b = bookings.get(p["ref_id"])
        return f"/booking?ref={b['ref']}&k={b['access_key']}"
    return "/portal?paid=1"


@route("POST", "/api/payments/razorpay-webhook")
def razorpay_webhook(req):
    """Settles payments whose browser closed before Checkout reported back."""
    p = payments.capture_from_webhook(req.raw, req.headers.get("X-Razorpay-Signature"))
    if p:
        try:
            _fulfil(p)
        except BookingError:
            pass  # slot lost; refunded automatically
    return {"ok": True}


def _fulfil(p):
    if p["kind"] == "booking":
        return bookings.fulfil(p)
    if p["kind"] == "membership":
        return members.complete_membership(p)


def _public_booking(b, with_quote=False):
    keep = ("id", "ref", "court_id", "court_name", "sport", "name", "start", "end", "amount", "status", "hold_until",
            "pay_status", "refund_amount", "cancelled_by", "phone", "email", "discount")
    out = {k: b.get(k) for k in keep}
    out["phone"] = out["phone"] and "******" + out["phone"][-4:]
    if b["status"] == "reserved" and b["hold_until"]:
        out["hold_seconds"] = max(0, int((clock.parse(b["hold_until"]) - clock.now()).total_seconds()))
    if with_quote:
        out["cancel"] = bookings.refund_quote(b, "customer")
    d = db.one("SELECT valid_from, valid_to, status FROM door_codes WHERE booking_id=?", b["id"])
    # The code itself only travels by SMS/WhatsApp to the booker's phone — never in a web response.
    out["door_code"] = d and {"sent": True, "valid_from": d["valid_from"], "valid_to": d["valid_to"],
                              "status": d["status"]}
    p = payments.for_ref("booking", b["id"])
    out["refund_status"] = p and p["refund_status"]
    return out


# ------------------------------------------------------------------ auth

@route("POST", "/api/auth/request-code")
def request_code(req):
    code, is_new, _ = auth.request_login_code(req.body.get("phone"), req.ip)
    out = {"ok": True, "new": is_new}
    if config.DEMO_MODE:
        out["demo_code"] = code  # demo only: no real SMS goes out, so show it
    return out


@route("POST", "/api/auth/verify")
def verify_code(req):
    token = auth.verify_login_code(req.body.get("phone"), req.body.get("code"), req.body.get("name"),
                                   req.body.get("ref"))
    req.set_cookie = token
    return {"ok": True}


@route("POST", "/api/auth/admin")
def admin_login(req):
    req.set_cookie = auth.admin_login(req.body.get("phone"), req.body.get("password"))
    return {"ok": True}


@route("POST", "/api/auth/logout")
def logout(req):
    auth.logout(req.token)
    req.set_cookie = ""
    return {"ok": True}


@route("GET", "/api/me", "user")
def me(req):
    return members.status(req.user["id"])


@route("POST", "/api/me/prefs", "user")
def me_prefs(req):
    sms, wa = int(bool(req.body.get("sms_opt"))), int(bool(req.body.get("wa_opt")))
    if not (sms or wa):
        raise HttpError(400, "Keep at least one channel on so you get your reminders")
    db.conn().execute("UPDATE users SET sms_opt=?, wa_opt=? WHERE id=?", (sms, wa, req.user["id"]))
    return {"ok": True}


@route("POST", r"/api/me/bookings/(\d+)/cancel", "user")
def me_cancel(req, bid):
    b = bookings.get(int(bid))
    if not b or b["phone"] != req.user["phone"]:
        raise HttpError(404, "Booking not found")
    return bookings.cancel(int(bid), "customer")


@route("POST", "/api/me/membership/order", "user")
def me_membership_order(req):
    """Buy / renew online. Returns the payment page to redirect to."""
    pay = members.order_membership(req.user["id"], req.body.get("months", 1))
    return {"redirect": pay["redirect"], "amount": pay["amount"], "credit_used": pay["credit_used"]}


@route("POST", "/api/me/referral", "user")
def me_referral(req):
    """A new joiner enters the member ID of the friend who referred them."""
    return {"referred_by": members.set_referrer(req.user["id"], req.body.get("code"))}


@route("POST", r"/api/me/bookings/(\d+)/pay", "user")
def me_pay_booking(req, bid):
    b = bookings.get(int(bid))
    if not b or b["phone"] != req.user["phone"]:
        raise HttpError(404, "Booking not found")
    return {"redirect": bookings.start_payment(b)["redirect"]}


# ------------------------------------------------------------------ demo helpers (DEMO_MODE only)

def _demo_only():
    if not config.DEMO_MODE:
        raise HttpError(404, "Not found")


@route("GET", "/api/demo/info")
def demo_info(req):
    _demo_only()
    return {"logins": [
        {"role": "Admin (everything + analytics)", "login": config.ADMIN_PHONE,
         "secret": "password: " + config.ADMIN_PASSWORD, "where": "/admin"},
        {"role": "Receptionist (bookings + members)", "login": config.RECEPTION_PHONE,
         "secret": "password: " + config.RECEPTION_PASSWORD, "where": "/admin"},
        {"role": "Member (plan ends in 7 days)", "login": auth.member_id(2) + "  or  9000000001",
         "secret": "code shown on screen", "where": "/portal"},
        {"role": "Academy player (junior)", "login": auth.member_id(5) + "  or  9000000004",
         "secret": "code shown on screen", "where": "/portal"},
        {"role": "Guest (new customer)", "login": "any new 10-digit mobile number",
         "secret": "code shown on screen", "where": "/book"},
    ], "lock": config.LOCK_ENABLED, "fingerprints": [{"id": "101", "who": "Aarav Mehta (member, active)"},
                        {"id": "104", "who": "Ishaan Verma (academy, fees paid)"},
                        {"id": "115", "who": "Farhan Sheikh (membership expired)"}]}


@route("GET", "/api/demo/messages")
def demo_messages(req):
    _demo_only()
    phone = "".join(ch for ch in req.query.get("phone", "") if ch.isdigit())[-10:]
    rows = db.all_("SELECT id, created_at, kind, channel, to_phone, body, status FROM notifications "
                   + ("WHERE to_phone=? " if phone else "") + "ORDER BY id DESC LIMIT 80", *([phone] if phone else []))
    return {"messages": rows}


@route("POST", "/api/demo/door")
def demo_door(req):
    """The web keypad: exactly what the door hardware would ask the server."""
    _demo_only()
    b = req.body
    if b.get("fingerprint_user_id"):
        return access.verify_fingerprint(str(b["fingerprint_user_id"]), "web-keypad")
    return access.verify_code(b.get("code"), "web-keypad")


@route("POST", "/api/admin/demo-reset", "admin")
def demo_reset(req):
    _demo_only()
    db.reset()
    access.sync_member_access()
    return {"ok": True}


# ------------------------------------------------------------------ door device

@route("POST", "/api/lock/verify", "device")
def lock_verify(req):
    b, dev = req.body, req.headers.get("X-Device-Id", "door-1")
    if b.get("fingerprint_user_id") is not None:
        return access.verify_fingerprint(b["fingerprint_user_id"], dev)
    return access.verify_code(b.get("code"), dev)


@route("POST", "/api/lock/events", "device")
def lock_events(req):
    dev = req.headers.get("X-Device-Id", "door-1")
    for ev in req.body.get("events", []):
        access.record_device_event(ev, dev)
    return {"ok": True}


# ------------------------------------------------------------------ admin

@route("GET", "/api/admin/overview", "staff")
def overview(req):
    day = req.query.get("date") or clock.today().isoformat()
    todays = db.all_(
        "SELECT b.id,b.ref,b.court_id,c.name AS court,b.name,b.phone,b.email,b.start,b.end,b.amount,b.status,b.payment_ref,"
        " b.pay_status,b.source,b.discount,b.hold_until, d.code, d.status AS code_status, d.valid_from, d.uses FROM bookings b "
        "JOIN courts c ON c.id=b.court_id LEFT JOIN door_codes d ON d.booking_id=b.id "
        "WHERE substr(b.start,1,10)=? AND b.status IN ('confirmed','pending','reserved') "
        "ORDER BY b.start, b.court_id", day)
    reserved = db.all_("SELECT b.id,b.ref,b.name,b.phone,b.start,b.end,b.amount,b.hold_until,c.name AS court "
                       "FROM bookings b JOIN courts c ON c.id=b.court_id WHERE b.status='reserved' "
                       "ORDER BY b.hold_until")
    soon = (clock.today() + timedelta(days=max(config.REMINDER_DAYS))).isoformat()
    today = clock.today().isoformat()
    expiring = db.all_(
        "SELECT u.id AS user_id, u.name, u.phone, p.name AS plan, m.end_date FROM memberships m "
        "JOIN users u ON u.id=m.user_id JOIN plans p ON p.id=m.plan_id "
        "WHERE m.status='active' AND m.end_date BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM memberships m2 WHERE m2.user_id=m.user_id AND m2.status='active' "
        "AND m2.end_date>m.end_date) ORDER BY m.end_date", today, soon)
    fees_due = db.all_(
        "SELECT e.id, u.name, u.phone, b.name AS batch, e.fee_paid_until FROM academy_enrollments e "
        "JOIN users u ON u.id=e.user_id JOIN academy_batches b ON b.id=e.batch_id "
        "WHERE e.status='active' AND e.fee_paid_until <= ? ORDER BY e.fee_paid_until", soon)
    collected = db.one("SELECT COALESCE(SUM(amount),0) AS r FROM payments WHERE status='paid' "
                       "AND substr(paid_at,1,10)=?", day)["r"]
    unpaid = db.all_("SELECT b.id,b.ref,b.name,b.phone,b.start,b.end,b.amount,c.name AS court FROM bookings b "
                     "JOIN courts c ON c.id=b.court_id WHERE b.status='confirmed' AND b.pay_status='unpaid' "
                     "ORDER BY b.start")
    for b in unpaid + todays:
        b["balance"] = bookings.balance(b) if b.get("pay_status", "unpaid") == "unpaid" else 0
    refunds = db.all_(
        "SELECT p.id, p.kind, p.phone, p.provider, p.refund_amount, p.refund_status, p.refund_error, b.ref, b.name "
        "FROM payments p LEFT JOIN bookings b ON p.kind='booking' AND b.id=p.ref_id "
        "WHERE p.refund_status IN ('manual','failed','pending') ORDER BY p.id")
    return {
        "date": day, "now": clock.fmt(clock.now()), "bookings": todays, "collected": collected,
        "expiring": expiring, "fees_due": fees_due, "unpaid": unpaid, "refunds": refunds, "reserved": reserved,
        "notifications": db.all_("SELECT created_at,kind,channel,to_phone,body,status,error FROM notifications "
                                 "ORDER BY id DESC LIMIT 60"),
        "access": db.all_("SELECT a.at,a.device_id,a.method,a.granted,a.detail,u.name,b.ref FROM access_events a "
                          "LEFT JOIN users u ON u.id=a.user_id LEFT JOIN bookings b ON b.id=a.booking_id "
                          "ORDER BY a.id DESC LIMIT 40"),
        "providers": {"sms": config.SMS_PROVIDER, "whatsapp": config.WHATSAPP_PROVIDER,
                      "lock": config.LOCK_PROVIDER, "payment": config.PAYMENT_PROVIDER},
    }


@route("GET", "/api/admin/analytics", "admin")
def get_analytics(req):
    days = int(req.query.get("days") or 30)
    return {**analytics.report(days), "traffic": tracking.report(max(1, min(days, 366)))}


@route("GET", "/api/admin/health", "admin")
def get_health(req):
    return {"checks": health.checks(), "now": clock.fmt(clock.now())}


@route("GET", "/api/admin/catalog", "staff")
def catalog(req):
    return {"plans": db.all_("SELECT * FROM plans ORDER BY id"),
            "batches": db.all_("SELECT * FROM academy_batches ORDER BY id"),
            "courts": db.all_("SELECT * FROM courts ORDER BY id")}


@route("GET", "/api/admin/people", "staff")
def people(req):
    q = "%" + (req.query.get("q") or "") + "%"
    rows = db.all_("SELECT id,name,phone,email,lock_user_id,lock_enabled,guardian_name,sms_opt,wa_opt FROM users "
                   "WHERE role IS NULL AND (name LIKE ? OR phone LIKE ?) ORDER BY name LIMIT 200", q, q)
    for r in rows:
        s = members.status(r["id"])
        r["memberships"], r["academy"], r["access_until"] = s["memberships"], s["academy"], s["fingerprint"]["until"]
        r["credit"], r["referred_by"] = s["referral"]["credit"], s["referral"]["referred_by"]
        r["referrals"] = len(s["referral"]["friends"])
        r["member_id"] = auth.member_id(r["id"])
    return {"people": rows}


@route("POST", "/api/admin/people", "staff")
def save_person(req):
    b = req.body
    uid = members.upsert_person(b.get("name"), b.get("phone"), b.get("email") or None, b.get("lock_user_id"),
                                b.get("guardian_name") or None, b.get("sms_opt", 1), b.get("wa_opt", 1))
    return {"id": uid}


@route("POST", "/api/admin/memberships", "staff")
def add_membership(req):
    return {"membership": members.add_membership(int(req.body["user_id"]), int(req.body["plan_id"]),
                                                 int(req.body.get("months", 1)), req.user["name"],
                                                 req.body.get("method") or "cash")}


@route("POST", "/api/admin/enroll", "staff")
def enroll(req):
    b = req.body
    return {"enrollment": members.enroll(int(b["user_id"]), int(b["batch_id"]), b.get("level"),
                                         int(b.get("months", 1)), req.user["name"],
                                         b.get("method") or "cash")}


@route("POST", "/api/admin/academy-fee", "staff")
def academy_fee(req):
    return {"fee_paid_until": members.pay_academy_fee(int(req.body["enrollment_id"]),
                                                      int(req.body.get("months", 1)), req.user["name"],
                                                      req.body.get("method") or "cash")}


@route("POST", "/api/admin/bookings", "staff")
def desk_booking(req):
    """Booking made by staff. mode: desk_paid (cash/UPI now) | unpaid (phone reservation, pays later)
    | comp (free). All of them get a door code like any other booking."""
    b = req.body
    return {"booking": bookings.create_by_staff(b.get("court_id"), b.get("date"), b.get("start"), b.get("slots", 1),
                                                b.get("name"), b.get("phone"), b.get("mode"), req.user["name"],
                                                b.get("email"), b.get("method") or "cash", b.get("received"))}


@route("GET", "/api/admin/me", "staff")
def staff_me(req):
    return {"name": req.user["name"], "role": req.user["role"], "phone": req.user["phone"]}


@route("POST", r"/api/admin/bookings/(\d+)/confirm", "staff")
def confirm_reservation(req, bid):
    """A held reservation is confirmed after a call: paid=true (advance received) or pay at the venue."""
    b = req.body
    return {"booking": bookings.confirm_reservation(int(bid), req.user["name"], bool(b.get("paid")),
                                                    b.get("method") or "cash", b.get("advance"))}


@route("GET", "/api/admin/daybook", "staff")
def daybook(req):
    """All payments taken on a day (website + cash + UPI QR + card), with totals per method."""
    return payments.daybook(req.query.get("date") or clock.today().isoformat())


@route("POST", "/api/admin/payments", "staff")
def other_payment(req):
    """Money received that isn't a court booking, membership or academy fee: racket hire, shuttles, café…"""
    b = req.body
    if not (b.get("note") or "").strip():
        raise HttpError(400, "Say what the payment was for")
    if int(b.get("amount") or 0) <= 0:
        raise HttpError(400, "Enter the amount received")
    return {"payment": payments.record_desk("other", None, int(b["amount"]), b.get("phone") or None, None,
                                            req.user["name"], method=b.get("method") or "cash", note=b["note"])}


@route("GET", "/api/admin/staff", "admin")
def list_staff(req):
    return {"staff": db.all_("SELECT id, name, phone, role FROM users WHERE role IS NOT NULL ORDER BY role, name")}


@route("POST", "/api/admin/staff", "admin")
def add_staff(req):
    b = req.body
    return {"id": auth.add_staff(b.get("name"), b.get("phone"), b.get("password"), b.get("role") or "reception")}


@route("POST", r"/api/admin/staff/(\d+)/remove", "admin")
def remove_staff(req, uid):
    auth.remove_staff(int(uid), req.user["id"])
    return {"ok": True}


@route("GET", "/api/admin/export", "admin")
def export(req):
    """CSV downloads for the owner: bookings | payments | members | summary."""
    what, days = req.query.get("what", "bookings"), max(1, min(int(req.query.get("days") or 30), 730))
    since = (clock.today() - timedelta(days=days - 1)).isoformat()
    if what == "bookings":
        head = ["ref", "created_at", "court", "sport", "start", "end", "name", "phone", "email", "status",
                "pay_status", "booked_via", "amount", "discount", "refund_amount", "cancelled_by",
                "traffic_source", "heard_about_us"]
        rows = db.all_("SELECT b.ref,b.created_at,c.name AS court,c.sport,b.start,b.end,b.name,b.phone,b.email,"
                       "b.status,b.pay_status,b.source AS booked_via,b.amount,b.discount,b.refund_amount,"
                       "b.cancelled_by,b.utm AS traffic_source,b.heard AS heard_about_us FROM bookings b JOIN courts c ON c.id=b.court_id WHERE b.start>=? "
                       "ORDER BY b.start", since)
    elif what == "payments":
        head = ["id", "paid_at", "kind", "method", "amount", "note", "phone", "provider", "gateway_id",
                "refund_status", "refund_amount", "refunded_at"]
        rows = db.all_("SELECT id,paid_at,kind,COALESCE(method, CASE WHEN provider='desk' THEN 'cash' ELSE 'online' "
                       "END) AS method,note,provider,amount,phone,payment_id AS gateway_id,refund_status,"
                       "refund_amount,refunded_at FROM payments WHERE status='paid' AND paid_at>=? ORDER BY paid_at",
                       since)
    elif what == "members":
        head = ["member_id", "name", "phone", "email", "plan", "start_date", "end_date", "status"]
        rows = db.all_("SELECT u.id,u.name,u.phone,u.email,p.name AS plan,m.start_date,m.end_date,"
                       "CASE WHEN m.end_date<? THEN 'expired' WHEN m.start_date>? THEN 'upcoming' ELSE 'active' END "
                       "AS status FROM memberships m JOIN users u ON u.id=m.user_id JOIN plans p ON p.id=m.plan_id "
                       "WHERE m.status='active' ORDER BY m.end_date DESC", clock.today().isoformat(),
                       clock.today().isoformat())
        for r in rows:
            r["member_id"] = auth.member_id(r.pop("id"))
    elif what == "summary":
        r = analytics.report(days)
        head = ["date", "collected", "refunded"]
        rows = r["daily"] + [{"date": "TOTAL", "collected": r["money"]["gross"], "refunded": r["money"]["refunded"]}]
    elif what == "sources":
        head = ["source", "campaigns", "visitors", "bookings", "confirmed", "conversion", "revenue"]
        rows = [{**r, "campaigns": " ".join(r["campaigns"])} for r in tracking.report(min(days, 366))["sources"]]
    else:
        raise HttpError(400, "Unknown export")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=head, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        # Stop spreadsheet apps treating customer-typed text as a formula.
        w.writerow({k: ("'" + v if isinstance(v, str) and v and v[0] in "=+-@" else v) for k, v in r.items()})
    name = f"netrush-{what}-{clock.today().isoformat()}.csv"
    return Raw(("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", name)


@route("POST", r"/api/admin/bookings/(\d+)/mark-paid", "staff")
def mark_paid(req, bid):
    return {"booking": bookings.mark_paid(int(bid), req.user["name"], req.body.get("method") or "cash")}


@route("POST", r"/api/admin/bookings/(\d+)/cancel", "staff")
def cancel_booking(req, bid):
    return bookings.cancel(int(bid), "staff")


@route("POST", r"/api/admin/refunds/(\d+)/retry", "staff")
def retry_refund(req, pid):
    return {"payment": payments.retry_refund(int(pid))}


@route("POST", r"/api/admin/refunds/(\d+)/done", "staff")
def refund_done(req, pid):
    return {"payment": payments.mark_manual_refund_done(int(pid), req.user["name"])}


@route("POST", "/api/admin/blocks", "staff")
def add_block(req):
    b = req.body
    db.conn().execute("INSERT INTO court_blocks(court_id,date,start,end,reason) VALUES(?,?,?,?,?)",
                      (b["court_id"], b["date"], b["start"], b["end"], b.get("reason") or "Maintenance"))
    return {"ok": True}


@route("POST", "/api/admin/run-reminders", "staff")
def run_reminders(req):
    return {"sent": [{"type": t, "name": n, "bucket": b, "result": r} for t, n, b, r in members.send_reminders()]}


@route("POST", "/api/admin/tick", "admin")
def run_tick(req):
    scheduler.tick()
    return {"ok": True}


@route("POST", "/api/admin/simulate-door", "staff")
def simulate_door(req):
    """Try the door without hardware: behaves exactly like the keypad / fingerprint reader calling in."""
    b = req.body
    if b.get("fingerprint_user_id"):
        return access.verify_fingerprint(b["fingerprint_user_id"], "simulator")
    return access.verify_code(b.get("code"), "simulator")


# ------------------------------------------------------------------ plumbing

class Req:
    def __init__(self, handler, query, body_raw):
        self.headers = handler.headers
        self.query = {k: v[0] for k, v in parse_qs(query).items()}
        self.raw = body_raw
        try:
            self.body = json.loads(body_raw or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, "Body must be JSON")
        jar = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
        self.token = jar[COOKIE].value if COOKIE in jar else None
        self.user = None
        self.set_cookie = None
        self.ip = handler.client_address[0]
        if config.TRUST_PROXY:
            # The proxy appends the address it saw, so the LAST entry is the one a client can't forge.
            fwd = [p.strip() for p in handler.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            self.ip = fwd[-1] if fwd else self.ip


class Handler(BaseHTTPRequestHandler):
    server_version = "NetRush"

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]):
            sys.stderr.write("%s %s\n" % (clock.fmt(clock.now()), fmt % args))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def _handle(self, method):
        url = urlparse(self.path)
        if url.path == "/healthz":  # for the host's uptime check
            return self._json(200, {"ok": True})
        if method == "GET" and not url.path.startswith("/api/"):
            return self._static(url.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            for m, rx, who, fn in ROUTES:
                match = rx.match(url.path)
                if m != method or not match:
                    continue
                req = Req(self, url.query, raw)
                if who == "device":
                    if not access.check_device_signature(self.headers, raw):
                        raise HttpError(401, "Bad device signature")
                elif who in ("user", "staff", "admin"):
                    req.user = auth.user_for(req.token)
                    if not req.user:
                        raise HttpError(401, "Please log in")
                    if who == "staff" and req.user["role"] not in ("admin", "reception"):
                        raise HttpError(403, "Staff only")
                    if who == "admin" and req.user["role"] != "admin":
                        raise HttpError(403, "Only the admin can do this")
                out = fn(req, *match.groups())
                if isinstance(out, Raw):
                    return self._raw(out)
                return self._json(200, out, req.set_cookie)
            raise HttpError(404, "Not found")
        except HttpError as e:
            self._json(e.status, {"error": str(e)})
        except (BookingError, AuthError, MemberError, PaymentError) as e:
            self._json(400, {"error": str(e)})
        except (KeyError, ValueError) as e:
            self._json(400, {"error": f"Bad request: {e}"})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong"})

    def _json(self, status, obj, set_cookie=None):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie is not None:
            age = 60 * 60 * 24 * auth.SESSION_DAYS if set_cookie else 0
            secure = "; Secure" if config.PUBLIC_URL.startswith("https://") else ""
            self.send_header("Set-Cookie", f"{COOKIE}={set_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}")
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, out):
        self.send_response(200)
        self.send_header("Content-Type", out.content_type)
        self.send_header("Content-Length", str(len(out.body)))
        self.send_header("Cache-Control", "no-store")
        if out.filename:
            self.send_header("Content-Disposition", f'attachment; filename="{out.filename}"')
        self.end_headers()
        self.wfile.write(out.body)

    def _static(self, path):
        if (path == "/demo" and not config.DEMO_MODE) or \
                (path == "/door" and not (config.DEMO_MODE and config.LOCK_ENABLED)):
            return self.send_error(404)
        name = PAGES.get(path, path.lstrip("/"))
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    db.init()
    scheduler.start()
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    srv.daemon_threads = True
    print(f"{config.FACILITY_NAME} running on http://localhost:{config.PORT}\n"
          f"  landing  /   booking  /book   members  /portal   staff  /admin  (login {config.ADMIN_PHONE} / "
          f"{'<ADMIN_PASSWORD>' if config.ADMIN_PASSWORD != 'admin123' else 'admin123'})\n"
          f"  demo={'ON' if config.DEMO_MODE else 'off'} online_pay={'on' if config.ONLINE_PAY else 'OFF'} "
          f"lock={'on' if config.LOCK_ENABLED else 'off'}\n"
          f"  sms={config.SMS_PROVIDER} whatsapp={config.WHATSAPP_PROVIDER} lock={config.LOCK_PROVIDER} "
          f"payment={config.PAYMENT_PROVIDER}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
