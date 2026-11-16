"""Reserve-and-confirm, the first-booking discount, staff roles and booking-only mode.

    python -m unittest discover -s tests -v
"""
from datetime import datetime, timedelta

from app import access, analytics, auth, bookings, clock, config, db, payments
from tests.test_core import T0, Base


class ReserveTests(Base):
    def reserve(self, court="P1", start="20:00", day="2026-09-28", phone="9811111111", user=None):
        return self.run_quiet(bookings.create, court, day, start, 1, "Riya Kapoor", phone, None, "reserve",
                              user)["booking"]

    def test_reservation_holds_slot_three_hours_then_releases(self):
        b = self.reserve()                                                  # 08:00 for a 20:00 slot
        self.assertEqual((b["status"], b["hold_until"]), ("reserved", "2026-09-28 11:00"))
        self.assertEqual(len(self.sent("reservation_received")), 2)
        with self.assertRaises(bookings.BookingError):                      # slot is held
            self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Other", "9822222222")
        clock.set_now(T0 + timedelta(hours=3, minutes=1))
        self.run_quiet(bookings.expire_holds)
        self.assertEqual(bookings.get(b["id"])["status"], "expired")
        self.assertEqual(len(self.sent("reservation_released")), 2)
        self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Other", "9822222222")  # free again

    def test_reception_confirms_pay_at_venue_then_collects(self):
        b = self.reserve()
        c = self.run_quiet(bookings.confirm_reservation, b["id"], "Reception", False)
        self.assertEqual((c["status"], c["pay_status"]), ("confirmed", "unpaid"))
        clock.set_now(T0 + timedelta(hours=4))
        self.run_quiet(bookings.expire_holds)
        self.assertEqual(bookings.get(b["id"])["status"], "confirmed")      # confirmed bookings never lapse
        self.assertEqual(analytics.report(1)["money"]["gross"], 0)
        self.run_quiet(bookings.mark_paid, b["id"], "Reception")
        self.assertEqual(analytics.report(1)["money"]["gross"], config.PRICES["padel"])

    def test_reception_confirms_with_advance(self):
        b = self.reserve()
        c = self.run_quiet(bookings.confirm_reservation, b["id"], "Reception", True)
        self.assertEqual((c["status"], c["pay_status"]), ("confirmed", "paid"))
        self.assertEqual(payments.for_ref("booking", b["id"])["provider"], "desk")

    def test_customer_pays_reservation_online(self):
        b = self.reserve()
        pay = self.run_quiet(bookings.start_payment, bookings.get(b["id"]))
        p, _ = payments.capture(pay["payment_id"], {})
        done = self.run_quiet(bookings.fulfil, p)
        self.assertEqual((done["status"], done["pay_status"]), ("confirmed", "paid"))
        with self.assertRaises(bookings.BookingError):
            bookings.start_payment(done)                                    # nothing left to pay

    def test_pay_at_venue_booking_can_still_pay_online(self):
        b = self.reserve()
        self.run_quiet(bookings.confirm_reservation, b["id"], "Reception", False)
        pay = self.run_quiet(bookings.start_payment, bookings.get(b["id"]))
        p, _ = payments.capture(pay["payment_id"], {})
        self.assertEqual(self.run_quiet(bookings.fulfil, p)["pay_status"], "paid")

    def test_cannot_reserve_a_slot_about_to_start(self):
        clock.set_now(datetime(2026, 9, 28, 19, 20))
        with self.assertRaises(bookings.BookingError):
            self.reserve()
        self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Riya", "9811111111")  # pay now is fine

    def test_deadline_never_runs_into_the_slot(self):
        clock.set_now(datetime(2026, 9, 28, 18, 0))                         # 2h before
        self.assertEqual(self.reserve()["hold_until"], "2026-09-28 19:30")

    def test_customer_can_cancel_a_reservation(self):
        b = self.reserve()
        self.assertEqual(self.run_quiet(bookings.cancel, b["id"])["refund"]["amount"], 0)
        self.assertEqual(bookings.get(b["id"])["status"], "cancelled")


class DiscountTests(Base):
    def guest(self, phone="9812345678"):
        code, _, _ = self.run_quiet(auth.request_login_code, phone)
        return auth.user_for(auth.verify_login_code(phone, code, "New Guest"))

    def book(self, user, phone, court="P1", start="20:00", day="2026-09-28", mode="pay"):
        return self.run_quiet(bookings.create, court, day, start, 1, "New Guest", phone, None, mode, user)

    def test_verified_guest_gets_15_percent_once(self):
        u = self.guest()
        out = self.book(u, u["phone"])
        self.assertEqual(out["booking"]["discount"], 270)                   # 15% of 1800
        self.assertEqual(out["booking"]["amount"], 1530)
        self.assertEqual(out["payment"]["amount"], 1530)                    # the gateway charges the reduced price
        p, _ = payments.capture(out["payment"]["payment_id"], {})
        self.run_quiet(bookings.fulfil, p)
        second = self.book(u, u["phone"], start="21:00")
        self.assertEqual(second["booking"]["discount"], 0)                  # first booking only

    def test_no_discount_without_verification_or_for_another_number(self):
        self.assertEqual(self.book(None, "9812345678")["booking"]["discount"], 0)
        u = self.guest("9800000001")
        self.assertEqual(self.book(u, "9800000002", start="21:00")["booking"]["discount"], 0)

    def test_abandoned_payment_does_not_burn_the_offer(self):
        u = self.guest()
        self.book(u, u["phone"])
        self.assertEqual(bookings.first_booking_pct(u["phone"], u), 0)      # while the first one is in progress
        clock.set_now(T0 + timedelta(minutes=config.HOLD_MIN + 1))
        bookings.expire_holds()
        self.assertEqual(bookings.first_booking_pct(u["phone"], u), 15)     # they never paid: still theirs

    def test_existing_customer_is_not_a_first_booking(self):
        self.run_quiet(bookings.create_by_staff, "K1", "2026-09-28", "19:00", 1, "Walk", "9812345678",
                       "desk_paid", "Desk")
        u = self.guest()
        self.assertEqual(self.book(u, u["phone"])["booking"]["discount"], 0)

    def test_discount_applies_to_reservations_too(self):
        u = self.guest()
        self.assertEqual(self.book(u, u["phone"], mode="reserve")["booking"]["amount"], 1530)

    def test_staff_never_get_the_guest_discount(self):
        admin = auth.user_for(auth.admin_login(config.ADMIN_PHONE, config.ADMIN_PASSWORD))
        self.assertEqual(bookings.first_booking_pct(admin["phone"], admin), 0)


class RoleTests(Base):
    def test_seeded_roles_and_member_ids_unchanged(self):
        self.assertEqual(auth.user_for(auth.admin_login(config.ADMIN_PHONE, config.ADMIN_PASSWORD))["role"], "admin")
        r = auth.user_for(auth.admin_login(config.RECEPTION_PHONE, config.RECEPTION_PASSWORD))
        self.assertEqual(r["role"], "reception")
        self.assertEqual(db.one("SELECT id FROM users WHERE phone='9000000001'")["id"], 2)   # NR-0002

    def test_admin_adds_and_removes_receptionist(self):
        uid = auth.add_staff("Asha", "9777700001", "secret1")
        self.assertEqual(auth.user_for(auth.admin_login("9777700001", "secret1"))["role"], "reception")
        auth.remove_staff(uid, 1)
        with self.assertRaises(auth.AuthError):
            auth.admin_login("9777700001", "secret1")
        with self.assertRaises(auth.AuthError):
            auth.remove_staff(1, 1)                                         # not yourself
        with self.assertRaises(auth.AuthError):
            auth.add_staff("X", "9000000001", "secret1")                    # a member's number
        with self.assertRaises(auth.AuthError):
            auth.add_staff("X", "9777700002", "123")                        # weak password

    def test_customers_cannot_use_staff_login(self):
        with self.assertRaises(auth.AuthError):
            auth.admin_login("9000000001", "anything")

    def test_route_guards(self):
        import server
        who = {fn.__name__: w for _, _, w, fn in server.ROUTES}
        for admin_only in ("get_analytics", "get_health", "export", "add_staff", "remove_staff", "demo_reset"):
            self.assertEqual(who[admin_only], "admin", admin_only)
        for staff in ("overview", "desk_booking", "confirm_reservation", "mark_paid", "cancel_booking", "people"):
            self.assertEqual(who[staff], "staff", staff)


class BookingOnlyModeTests(Base):
    def test_no_door_codes_when_lock_is_off(self):
        config.LOCK_ENABLED = False
        try:
            b = self.book_and_pay("P1", "20:00")
            self.assertIsNone(db.one("SELECT 1 AS x FROM door_codes WHERE booking_id=?", b["id"]))
            self.assertEqual(self.sent("door_code"), [])
            self.assertEqual(len(self.sent("booking_confirmed")), 2)
            self.assertEqual(self.run_quiet(access.sync_member_access), [])
        finally:
            config.LOCK_ENABLED = True

    def test_otp_unavailable_without_a_provider_outside_demo(self):
        config.DEMO_MODE = False
        try:
            with self.assertRaises(auth.AuthError):
                self.run_quiet(auth.request_login_code, "9812345678")
        finally:
            config.DEMO_MODE = True


class TrackingTests(Base):
    def test_sources_visitors_bookings_revenue(self):
        from app import tracking
        self.assertEqual(tracking.classify("Agency-One!", ""), "agency-one")
        self.assertEqual(tracking.classify("", "www.google.com"), "google")
        self.assertEqual(tracking.classify("", "l.instagram.com"), "instagram")
        self.assertEqual(tracking.classify("", ""), "direct")
        for vid in ("a1", "a2", "a3"):
            tracking.record(vid, "agency-one", "paid", "launch", "", "/", "1.1.1.1")
        tracking.record("a1", "agency-one", "paid", "launch", "", "/book", "1.1.1.1")     # same visitor again
        tracking.record("d1", "", "", "", "", "/", "2.2.2.2")
        out = self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Riya", "9811111111", None, "pay",
                             None, tracking.booking_tag("agency-one", "launch", ""), "Instagram")
        p, _ = payments.capture(out["payment"]["payment_id"], {})
        self.run_quiet(bookings.fulfil, p)
        r = tracking.report(7)
        agency = next(s for s in r["sources"] if s["source"] == "agency-one")
        self.assertEqual((agency["visitors"], agency["bookings"], agency["confirmed"], agency["revenue"]),
                         (3, 1, 1, config.PRICES["padel"]))
        self.assertEqual(agency["campaigns"], ["launch"])
        self.assertEqual(next(s for s in r["sources"] if s["source"] == "direct")["visitors"], 1)
        self.assertEqual(r["heard"], [{"answer": "Instagram", "n": 1}])
