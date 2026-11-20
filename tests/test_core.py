"""End-to-end checks of the rules that matter: no double booking, door codes only for paid
bookings and only inside their window, fingerprints follow memberships, reminders once each.

    python -m unittest discover -s tests -v
"""
import hashlib
import hmac
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta

os.environ["DEMO_MODE"] = "1"   # tests run against the demo setup (mock gateway, on-screen codes)
os.environ["SEED_DEMO"] = "1"
os.environ["SEED_HISTORY"] = "0"  # tests count money exactly
os.environ["LOCK_ENABLED"] = "1"  # the door tests still run while the lock is switched off in production

from app import (access, analytics, auth, bookings, clock, config, db, health,  # noqa: E402
                 members, payments, scheduler)

T0 = datetime(2026, 9, 28, 8, 0)  # a Monday, 08:00


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clock.set_now(T0)
        access._fails.clear()
        auth._ip_hits.clear()
        self.out = io.StringIO()
        with redirect_stdout(self.out):
            db.init(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        db.conn().close()
        db._local.conn = None
        clock.set_now(None)
        self.tmp.cleanup()

    def run_quiet(self, fn, *a, **kw):
        with redirect_stdout(self.out):
            return fn(*a, **kw)

    def book_and_pay(self, court="B1", start="18:00", day="2026-09-28", slots=1, phone="9811111111"):
        out = self.run_quiet(bookings.create, court, day, start, slots, "Riya Kapoor", phone)
        return self.run_quiet(bookings.confirm_payment, out["booking"]["id"], {})

    def sent(self, kind):
        return db.all_("SELECT * FROM notifications WHERE kind=? AND status='sent'", kind)


class BookingTests(Base):
    def test_no_double_booking(self):
        self.book_and_pay("K1", "18:00")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "K1", "2026-09-28", "18:00", 1, "Other", "9822222222")
        # overlapping multi-slot too
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "K1", "2026-09-28", "17:00", 2, "Other", "9822222222")
        # other court is fine
        self.run_quiet(bookings.create, "K2", "2026-09-28", "18:00", 1, "Other", "9822222222")

    def test_unpaid_hold_lapses(self):
        self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "A", "9811111111")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "B", "9822222222")
        clock.set_now(T0 + timedelta(minutes=config.HOLD_MIN + 1))
        bookings.expire_holds()
        self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "B", "9822222222")

    def test_academy_batch_blocks_courts(self):
        # Seeded junior batch: Mon/Wed/Fri 17:00-19:00 on B1,B2. T0 is a Monday.
        grid = bookings.availability("badminton", date(2026, 9, 28))
        row = next(r for r in grid["slots"] if r["start"] == "17:00")
        self.assertTrue(row["cells"]["B1"].startswith("blocked"))
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B1", "2026-09-28", "17:00", 1, "A", "9811111111")
        # Tuesday is open
        self.run_quiet(bookings.create, "B1", "2026-09-29", "17:00", 1, "A", "9811111111")

    def test_past_and_horizon(self):
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B1", "2026-09-28", "07:00", 1, "A", "9811111111")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B1", "2026-12-01", "07:00", 1, "A", "9811111111")


class DoorCodeTests(Base):
    def test_code_only_inside_window_and_only_when_paid(self):
        unpaid = self.run_quiet(bookings.create, "K2", "2026-09-28", "10:00", 1, "U", "9833333333")
        b = self.book_and_pay("K1", "10:00")                               # 08:00, two hours ahead
        self.assertEqual(len(self.sent("door_code")), 0)                   # nothing at booking time
        clock.set_now(datetime(2026, 9, 28, 9, 49))
        self.assertEqual(self.run_quiet(access.issue_due_codes), [])       # 11 min before: not yet
        clock.set_now(datetime(2026, 9, 28, 9, 50))
        issued = self.run_quiet(access.issue_due_codes)                    # 10 min before: sent, once
        self.assertEqual([i["booking_id"] for i in issued], [b["id"]])     # the unpaid one gets nothing
        self.assertIsNone(db.one("SELECT 1 AS x FROM door_codes WHERE booking_id=?", unpaid["booking"]["id"]))
        self.assertEqual(len(self.sent("door_code")), 1)
        self.run_quiet(access.issue_due_codes)
        self.assertEqual(len(self.sent("door_code")), 1)                   # only once
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]

        self.assertTrue(access.verify_code(code)["open"])
        clock.set_now(datetime(2026, 9, 28, 10, 59))
        self.assertTrue(access.verify_code(code)["open"])                  # re-entry during slot
        clock.set_now(datetime(2026, 9, 28, 11, 0))
        self.assertFalse(access.verify_code(code)["open"])                 # slot over
        self.run_quiet(access.expire_codes)
        self.assertEqual(db.one("SELECT status FROM door_codes")["status"], "expired")

    def test_last_minute_booking_gets_code_immediately(self):
        clock.set_now(datetime(2026, 9, 28, 9, 57))
        self.book_and_pay("P1", "10:00")
        self.assertEqual(len(self.sent("door_code")), 1)

    def test_cancel_revokes(self):
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.run_quiet(bookings.cancel, b["id"])
        self.assertFalse(access.verify_code(code)["open"])

    def test_keypad_lockout(self):
        for _ in range(access.MAX_FAILS):
            access.verify_code("000001", "door-1")
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.assertEqual(access.verify_code(code, "door-1")["reason"], "locked_out")
        self.assertTrue(access.verify_code(code, "door-2")["open"])

    def test_device_signature(self):
        body = b'{"code":"123456"}'
        ts = str(int(time.time()))
        sig = hmac.new(config.DEVICE_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertTrue(access.check_device_signature({"X-Timestamp": ts, "X-Signature": sig}, body))
        self.assertFalse(access.check_device_signature({"X-Timestamp": ts, "X-Signature": sig}, b'{"code":"1"}'))
        old = str(int(time.time()) - 600)
        sig_old = hmac.new(config.DEVICE_SECRET.encode(), f"{old}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertFalse(access.check_device_signature({"X-Timestamp": old, "X-Signature": sig_old}, body))


class MemberTests(Base):
    def test_fingerprint_follows_membership(self):
        self.run_quiet(access.sync_member_access)
        aarav = db.one("SELECT * FROM users WHERE phone='9000000001'")  # plan ends T0+7
        self.assertEqual(aarav["lock_enabled"], 1)
        self.assertTrue(access.verify_fingerprint("101")["open"])
        clock.set_now(T0 + timedelta(days=8))
        self.run_quiet(access.sync_member_access)
        self.assertEqual(db.one("SELECT lock_enabled FROM users WHERE id=?", aarav["id"])["lock_enabled"], 0)
        self.assertFalse(access.verify_fingerprint("101")["open"])
        self.run_quiet(members.add_membership, aarav["id"], 1)             # renews → back on
        self.assertEqual(db.one("SELECT lock_enabled FROM users WHERE id=?", aarav["id"])["lock_enabled"], 1)

    def test_early_renewal_keeps_days(self):
        aarav = db.one("SELECT id FROM users WHERE phone='9000000001'")
        m = self.run_quiet(members.add_membership, aarav["id"], 1)
        self.assertEqual(m["start_date"], "2026-10-06")                    # day after current end

    def test_reminders_7day_and_last_day_once(self):
        self.run_quiet(members.send_reminders)
        rem = self.sent("membership_reminder")
        bodies = " | ".join(r["body"] for r in rem)
        self.assertEqual(len(rem), 2)                                      # Aarav + Diya, one channel each
        self.assertIn("ends in 7 days", bodies)
        self.assertIn("ends today", bodies)
        self.assertEqual(len(self.sent("academy_fee_reminder")), 1)        # Ishaan, fee due in 7 days
        self.run_quiet(members.send_reminders)
        self.assertEqual(len(self.sent("membership_reminder")), 2)         # no repeats
        clock.set_now(T0 + timedelta(days=7))                              # Aarav's last day
        self.run_quiet(members.send_reminders)
        self.assertEqual(len(self.sent("membership_reminder")), 3)

    def test_renewed_member_gets_no_reminder(self):
        aarav = db.one("SELECT id FROM users WHERE phone='9000000001'")
        self.run_quiet(members.add_membership, aarav["id"], 1)
        self.run_quiet(members.send_reminders)
        self.assertNotIn("9000000001", [r["to_phone"] for r in self.sent("membership_reminder")])

    def test_channel_preference(self):
        db.conn().execute("UPDATE users SET wa_opt=0 WHERE phone='9000000002'")
        self.run_quiet(members.send_reminders)
        chans = [r["channel"] for r in self.sent("membership_reminder") if r["to_phone"] == "9000000002"]
        self.assertEqual(chans, ["sms"])


class AuthTests(Base):
    def test_guest_code_login(self):
        code, is_new, _ = self.run_quiet(auth.request_login_code, "+91 98123 45678")
        self.assertTrue(is_new)
        with self.assertRaises(auth.AuthError):
            auth.verify_login_code("9812345678", "000000" if code != "000000" else "111111", "Neha")
        self.assertEqual(auth.user_for(auth.verify_login_code("9812345678", code, "Neha"))["name"], "Neha")

    def test_members_and_academy_players_cannot_use_the_code_route(self):
        for phone in ("9000000001", "9000000004"):                     # member, academy player
            with self.assertRaises(auth.AuthError):
                self.run_quiet(auth.request_login_code, phone)
        self.assertEqual(db.all_("SELECT 1 FROM notifications WHERE kind='login_otp'"), [])   # no code wasted
        with self.assertRaises(auth.AuthError):
            self.run_quiet(auth.request_login_code, "NR-0002")

    def test_member_password_login_by_id_username_or_mobile(self):
        for ident in ("NR-0002", "nr2", "AARAV", "9000000001"):
            token, must_change = auth.member_login(ident, "member123")
            self.assertEqual((auth.user_for(token)["name"], must_change), ("Aarav Mehta", False))
        with self.assertRaises(auth.AuthError):
            auth.member_login("NR-0002", "wrong-password")

    def test_temporary_password_must_be_changed_first(self):
        token, must_change = auth.member_login("NR-0005", "welcome1")       # Ishaan, temp from the reception
        self.assertTrue(must_change)
        uid = auth.user_for(token)["id"]
        for bad in ("short", "welcome1"):                                    # too short / same as the temp
            with self.assertRaises(auth.AuthError):
                auth.change_member_credentials(uid, "welcome1", bad)
        with self.assertRaises(auth.AuthError):
            auth.change_member_credentials(uid, "wrong", "a-good-password")
        with self.assertRaises(auth.AuthError):                              # someone else's username
            auth.change_member_credentials(uid, "welcome1", "a-good-password", "aarav")
        with self.assertRaises(auth.AuthError):                              # looks like a member ID
            auth.change_member_credentials(uid, "welcome1", "a-good-password", "nr-0009")
        auth.change_member_credentials(uid, "welcome1", "a-good-password", "Ishaan.V")
        token, must_change = auth.member_login("ishaan.v", "a-good-password")
        self.assertFalse(must_change)
        with self.assertRaises(auth.AuthError):
            auth.member_login("NR-0005", "welcome1")                         # the temp no longer works

    def test_staff_issue_and_reset_logins(self):
        kabir = db.one("SELECT id FROM users WHERE phone='9000000003'")["id"]
        with self.assertRaises(auth.AuthError):
            auth.member_login("NR-0004", "anything")                         # no login created yet
        temp = auth.issue_member_login(kabir)
        self.assertEqual(len(temp), 8)
        token, must_change = auth.member_login(auth.member_id(kabir), temp)
        self.assertTrue(must_change)
        auth.issue_member_login(kabir, "reset123")                            # reset signs them out
        self.assertIsNone(auth.user_for(token))
        self.assertTrue(auth.member_login(auth.member_id(kabir), "reset123")[1])

    def test_wrong_passwords_lock_the_login_for_a_while(self):
        for _ in range(auth.PW_MAX_FAILS):
            with self.assertRaises(auth.AuthError):
                auth.member_login("NR-0002", "nope")
        with self.assertRaises(auth.AuthError):
            auth.member_login("NR-0002", "member123")                        # even the right one, for now
        clock.set_now(T0 + timedelta(minutes=auth.PW_LOCK_MIN + 1))
        self.assertTrue(auth.member_login("NR-0002", "member123")[0])

    def test_new_number_signs_up(self):
        code, is_new, _ = self.run_quiet(auth.request_login_code, "9876543210")
        self.assertTrue(is_new)
        with self.assertRaises(auth.AuthError):  # a new account needs a name
            auth.verify_login_code("9876543210", code)
        u = auth.user_for(auth.verify_login_code("9876543210", code, "Neha Joshi"))
        self.assertEqual(u["name"], "Neha Joshi")

    def test_code_request_rate_limited_per_ip(self):
        for i in range(auth.IP_MAX_REQUESTS):
            self.run_quiet(auth.request_login_code, f"98000000{i:02d}", "1.2.3.4")
        with self.assertRaises(auth.AuthError):
            self.run_quiet(auth.request_login_code, "9800000099", "1.2.3.4")

    def test_scheduler_tick_runs_clean(self):
        clock.set_now(datetime(2026, 9, 28, 10, 30))
        self.run_quiet(scheduler.tick)
        self.assertEqual(db.kv_get("reminders_ran"), "2026-09-28")


if __name__ == "__main__":
    unittest.main()
