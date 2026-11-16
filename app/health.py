"""'Is everything working?' One row per moving part, each ok | warn | bad with a plain reason and,
when something is wrong, what to do about it. Built only from what the system already records
(heartbeat, message log, payments, access log), so it costs nothing to run."""
from datetime import timedelta

from . import clock, config, db

DEMO = ("console", "mock")


def checks():
    now = clock.now()
    day_ago = clock.fmt(now - timedelta(hours=24))
    out = []

    def add(area, status, summary, fix=None):
        out.append({"area": area, "status": status, "summary": summary, "fix": fix})

    # Scheduler: door codes, reminders, refunds and fingerprint sync all depend on it.
    last = db.kv_get("last_tick")
    age = (now - clock.parse(last)).total_seconds() / 60 if last else None
    if age is None:
        add("Automation (scheduler)", "bad", "Has never run.", "Restart the server.")
    elif age > 3:
        add("Automation (scheduler)", "bad", f"Last ran {int(age)} min ago. Door codes and reminders are NOT going out.",
            "Restart the server and check its log for errors.")
    else:
        add("Automation (scheduler)", "ok", "Running. Last pass under a minute ago." if age < 1 else
            f"Running. Last pass {int(age)} min ago.")

    # Messaging, per channel.
    for ch, label, provider in (("sms", "SMS", config.SMS_PROVIDER), ("whatsapp", "WhatsApp", config.WHATSAPP_PROVIDER)):
        r = db.one("SELECT SUM(status='sent') AS ok, SUM(status='failed') AS bad FROM notifications "
                   "WHERE channel=? AND created_at>=?", ch, day_ago)
        sent, failed = r["ok"] or 0, r["bad"] or 0
        err = db.one("SELECT error FROM notifications WHERE channel=? AND status='failed' ORDER BY id DESC LIMIT 1", ch)
        if ch == "sms" and provider == "2factor":
            add(label, "ok" if not failed else "warn",
                f"OTP codes only (2Factor): {sent} sent, {failed} failed in 24h. Other messages need WhatsApp or DLT SMS.")
        elif provider in DEMO:
            what = ("Login codes and the first-booking discount are switched off until it is."
                    if ch == "sms" else "Booking confirmations and reminders are not reaching customers.")
            add(label, "warn", f"Not connected, so no {label} messages are sent. " + what,
                "Add the provider keys in the host's environment settings (see DEPLOY.md).")
        elif failed and failed >= sent:
            add(label, "bad", f"{failed} of {sent + failed} messages failed in 24h. Last error: {err and err['error']}",
                "Check the provider account balance, API key and template approvals.")
        elif failed:
            add(label, "warn", f"{sent} sent, {failed} failed in 24h. Last error: {err and err['error']}")
        else:
            add(label, "ok", f"{sent} sent in the last 24h, none failed.")

    # Payments and refunds.
    paid = db.one("SELECT COUNT(*) AS n FROM payments WHERE status='paid' AND provider<>'desk' AND paid_at>=?",
                  day_ago)["n"]
    failed_ref = db.all_("SELECT id, refund_amount, refund_error FROM payments WHERE refund_status='failed'")
    pending_ref = db.one("SELECT COUNT(*) AS n FROM payments WHERE refund_status='pending'")["n"]
    manual_ref = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(refund_amount),0) AS a FROM payments "
                        "WHERE refund_status='manual'")
    if config.PAYMENT_PROVIDER in DEMO and not config.DEMO_MODE:
        add("Online payments", "warn", "Switched off: no payment gateway is connected, so customers can only "
            "reserve and pay at the venue. Memberships can't be bought online.",
            "Add your Razorpay keys (PAYMENT_PROVIDER=razorpay, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET).")
    elif config.PAYMENT_PROVIDER in DEMO:
        add("Online payments", "warn", "Demo mode: 'Pay now' confirms without charging anyone.",
            "Add Razorpay keys before going live.")
    elif config.RAZORPAY_TEST_KEYS:
        add("Online payments", "bad", f"Razorpay TEST keys are in use: {paid} 'payment(s)' in 24h took no real "
            "money, yet those bookings show as paid.",
            "Replace them with the live keys (rzp_live_...) before taking real bookings.")
    else:
        add("Online payments", "ok", f"{paid} online payment(s) in 24h.")
    if failed_ref:
        add("Refunds", "bad", f"{len(failed_ref)} refund(s) failed after {5} tries. "
                              f"Last error: {failed_ref[-1]['refund_error']}",
            "Retry from the Today tab, or refund from the Razorpay dashboard and mark it done.")
    elif pending_ref:
        add("Refunds", "warn", f"{pending_ref} refund(s) being retried automatically.")
    elif manual_ref["n"]:
        add("Refunds", "warn", f"{manual_ref['n']} cash refund(s) (Rs {manual_ref['a']}) to hand back at the desk.")
    else:
        add("Refunds", "ok", "No refunds waiting.")

    if not config.LOCK_ENABLED:
        add("Door lock", "ok", "Switched off for now: the site runs as booking-only. Staff let players in.")
    else:
        # Door lock.
        lock_fail = db.one("SELECT COUNT(*) AS n, MAX(detail) AS d FROM access_events WHERE method='system' "
                           "AND detail LIKE 'lock %failed%' AND at>=?", day_ago)
        if config.LOCK_PROVIDER == "console":
            add("Door lock connection", "warn",
                "No lock connected for pushing codes. Works only if the keypad asks the server (pull mode).",
                "Set LOCK_PROVIDER once the lock is chosen (see README).")
        elif lock_fail["n"]:
            add("Door lock connection", "bad", f"{lock_fail['n']} lock command(s) failed in 24h: {lock_fail['d']}",
                "Check the lock's gateway / bridge is powered and online.")
        else:
            add("Door lock connection", "ok", "Lock commands going through.")
        seen = db.one("SELECT MAX(at) AS at, device_id FROM access_events WHERE device_id IS NOT NULL "
                      "AND device_id NOT IN ('simulator')")
        if seen and seen["at"]:
            mins = (now - clock.parse(seen["at"])).total_seconds() / 60
            add("Door device", "ok" if mins < 24 * 60 else "warn",
                f"Last heard from '{seen['device_id']}' {_ago(mins)}.",
                None if mins < 24 * 60 else "Nobody has entered in a day. Check the device has power and internet.")
        else:
            add("Door device", "warn", "No door device has reported in yet.", "Connect the keypad / fingerprint device.")
        lockouts = db.one("SELECT COUNT(*) AS n FROM access_events WHERE detail LIKE 'keypad locked out%' AND at>=?",
                          day_ago)["n"]
        if lockouts:
            add("Keypad security", "warn", f"Keypad locked out {lockouts} time(s) in 24h after repeated wrong codes.",
                "Someone may be guessing codes. Check the camera at the door.")

        # Every paid booking that's already started must have a code: this should never fail.
        missing = db.all_("SELECT b.ref FROM bookings b LEFT JOIN door_codes d ON d.booking_id=b.id "
                          "WHERE b.status='confirmed' AND d.id IS NULL AND b.start<=? AND b.end>?",
                          clock.fmt(now + timedelta(minutes=config.DOOR_CODE_LEAD_MIN - 1)), clock.fmt(now))
        if missing:
            add("Door codes", "bad", "No code issued for booking(s) " + ", ".join(m["ref"] for m in missing) + ".",
                "Players may be stuck outside. Read them the code from the Today tab or open the door remotely.")
        else:
            add("Door codes", "ok", "Every current booking has its code.")

    # Reminders ran today (after REMINDER_HOUR).
    if now.hour >= config.REMINDER_HOUR:
        ran = db.kv_get("reminders_ran") == clock.today().isoformat()
        add("Membership reminders", "ok" if ran else "bad",
            "Today's 7-day / last-day reminders have gone out." if ran else "Today's reminders haven't run.",
            None if ran else "Check the scheduler row above.")

    # Security settings left at their defaults.
    weak = [n for n, v, d in (("ADMIN_PASSWORD", config.ADMIN_PASSWORD, "admin123"),
                              ("RECEPTION_PASSWORD", config.RECEPTION_PASSWORD, "desk123")) if v == d]
    if config.LOCK_ENABLED and config.DEVICE_SECRET == "change-me-device-secret":
        weak.append("DEVICE_SECRET")
    if weak:
        add("Security settings", "bad", "Still on the default value: " + ", ".join(weak) + ".",
            "Set your own values in the host's environment settings, then redeploy.")
    else:
        add("Security settings", "ok", "Staff passwords are your own.")
    if config.DEMO_MODE:
        add("Demo mode", "bad", "ON: login codes show on screen and the /demo page lists the staff passwords.",
            "Set DEMO_MODE=0 for the real site.")
    if config.env("RENDER") and not config.env("DB_PATH"):
        add("Data storage", "bad", "The database is on temporary storage: every restart or deploy erases all "
            "bookings, members and payments.", "Add a persistent disk and set DB_PATH (see DEPLOY.md).")

    ok = db.conn().execute("PRAGMA quick_check").fetchone()[0] == "ok"
    add("Database", "ok" if ok else "bad", "Healthy." if ok else "Integrity check failed.",
        None if ok else "Restore from the latest backup.")
    order = {"bad": 0, "warn": 1, "ok": 2}
    return sorted(out, key=lambda c: order[c["status"]])


def _ago(mins):
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{int(mins)} min ago"
    if mins < 48 * 60:
        return f"{int(mins // 60)} h ago"
    return f"{int(mins // 1440)} days ago"
