"""Club membership: monthly hours per sport, one serial per hour, serials as door codes, monthly expiry."""
from datetime import datetime, timedelta

from tests.test_core import T0, Base  # first: it sets the test environment

from app import access, auth, bookings, club, clock, config, db, members  # noqa: E402


class ClubTests(Base):
    def setUp(self):
        super().setUp()
        self.uid = self.run_quiet(members.upsert_person, "Zoya Khan", "9811122233")

    def sell(self, months=12, amount=50000):
        return self.run_quiet(club.sell, self.uid, months, "Asha", "upi", amount)

    def left(self, sport):
        return club.usage(self.uid)["current"]["sports"][sport]["left"]

    def test_serials_generated_per_month_and_sport(self):
        m = self.sell(12)
        self.assertEqual((m["start_date"], m["end_date"]), ("2026-09-28", "2027-09-27"))
        rows = db.all_("SELECT period, sport, COUNT(*) AS n FROM club_serials WHERE club_id=? GROUP BY period, sport",
                       m["id"])
        self.assertEqual(len(rows), 12 * 3)
        for r in rows:
            self.assertEqual(r["n"], config.CLUB_HOURS[r["sport"]])
        serials = [r["serial"] for r in db.all_("SELECT serial FROM club_serials")]
        self.assertEqual(len(serials), 12 * 53)
        self.assertEqual(len(set(serials)), len(serials))
        self.assertTrue(all(len(s) == 6 and s[0] != "0" for s in serials))
        u = club.usage(self.uid)
        self.assertEqual((u["current"]["from"], u["current"]["to"]), ("2026-09-28", "2026-10-27"))
        self.assertEqual(u["current"]["total"]["included"], 53)
        self.assertEqual(db.one("SELECT SUM(amount) AS a FROM payments WHERE kind='club'")["a"], 50000)

    def test_booking_spends_that_sports_hours_only(self):
        self.sell(1)
        b = self.run_quiet(club.book, self.uid, "B1", "2026-09-29", "18:00", 2)
        self.assertEqual((b["status"], b["pay_status"], b["amount"]), ("confirmed", "club", 0))
        self.assertEqual(self.left("badminton"), 24)
        self.assertEqual(self.left("pickleball"), 20)
        self.assertEqual(self.left("padel"), 7)
        used = db.all_("SELECT sport FROM club_serials WHERE booking_id=?", b["id"])
        self.assertEqual([u["sport"] for u in used], ["badminton", "badminton"])

    def test_cannot_book_more_than_the_month_allows(self):
        self.sell(1)
        self.run_quiet(club.book, self.uid, "P1", "2026-09-29", "06:00", 4)
        self.run_quiet(club.book, self.uid, "P1", "2026-09-30", "06:00", 3)
        self.assertEqual(self.left("padel"), 0)
        with self.assertRaises(club.ClubError) as e:
            self.run_quiet(club.book, self.uid, "P1", "2026-10-01", "06:00", 1)
        self.assertIn("0 padel hour", str(e.exception))
        self.run_quiet(club.book, self.uid, "B1", "2026-10-01", "06:00", 1)   # other sports unaffected

    def test_next_club_month_has_its_own_serials(self):
        self.sell(2)
        for i in range(7):
            self.run_quiet(club.book, self.uid, "P1", f"2026-10-{i + 1:02d}", "06:00", 1)
        clock.set_now(datetime(2026, 10, 20, 8, 0))
        b = self.run_quiet(club.book, self.uid, "P1", "2026-10-28", "06:00", 1)    # day 1 of club month 2
        self.assertEqual(db.one("SELECT period FROM club_serials WHERE booking_id=?", b["id"])["period"], 1)

    def test_unused_hours_expire_at_month_end(self):
        self.sell(2)
        self.run_quiet(club.book, self.uid, "K1", "2026-10-02", "07:00", 1)
        clock.set_now(datetime(2026, 10, 28, 8, 0))
        club.expire()
        past = next(m for m in club.usage(self.uid)["months"] if m["from"] == "2026-09-28")
        self.assertEqual((past["sports"]["pickleball"]["booked"], past["sports"]["pickleball"]["expired"]), (1, 19))
        self.assertEqual(past["total"]["left"], 0)
        self.assertEqual(club.usage(self.uid)["current"]["total"]["left"], 53)

    def test_serial_is_the_door_code_inside_its_window_only(self):
        self.sell(1)
        b = self.run_quiet(club.book, self.uid, "B2", "2026-09-28", "10:00", 2)
        first = club.serial_for_booking(b["id"])
        clock.set_now(datetime(2026, 9, 28, 9, 49))
        self.assertEqual(self.run_quiet(access.issue_due_codes), [])
        self.assertFalse(access.verify_code(first)["open"])                  # not on the door yet
        clock.set_now(datetime(2026, 9, 28, 9, 50))
        self.run_quiet(access.issue_due_codes)
        self.assertEqual(db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"], first)
        self.assertIn(first, self.sent("door_code")[0]["body"])
        self.assertTrue(access.verify_code(first)["open"])
        clock.set_now(datetime(2026, 9, 28, 12, 0) + timedelta(minutes=config.DOOR_CODE_GRACE_MIN))
        self.run_quiet(access.expire_codes)
        self.assertFalse(access.verify_code(first)["open"])                  # removed after the slot

    def test_guest_codes_never_reuse_a_club_serial(self):
        self.sell(12)
        serials = {r["serial"] for r in db.all_("SELECT serial FROM club_serials")}
        orig, picks = access.secrets.randbelow, iter([int(next(iter(serials))) - 100000, 234567 - 100000])
        access.secrets.randbelow = lambda n: next(picks)                       # first draw hits a club serial
        try:
            self.assertEqual(access._new_code(db.conn()), "234567")
        finally:
            access.secrets.randbelow = orig

    def test_cancel_early_returns_hours_late_uses_them(self):
        self.sell(1)
        early = self.run_quiet(club.book, self.uid, "B1", "2026-09-29", "18:00", 2)
        self.run_quiet(bookings.cancel, early["id"], "customer")
        self.assertEqual(self.left("badminton"), 26)
        late = self.run_quiet(club.book, self.uid, "B1", "2026-09-28", "12:00", 1)
        clock.set_now(datetime(2026, 9, 28, 9, 0))                            # 3 hours before
        q = self.run_quiet(bookings.cancel, late["id"], "customer")["refund"]
        self.assertFalse(q["club_hours_back"])
        self.assertEqual(self.left("badminton"), 25)
        staff = self.run_quiet(club.book, self.uid, "B1", "2026-09-28", "13:00", 1)
        self.run_quiet(bookings.cancel, staff["id"], "staff")                 # staff: always returned
        self.assertEqual(self.left("badminton"), 25)

    def test_no_club_membership_no_club_booking(self):
        with self.assertRaises(club.ClubError):
            self.run_quiet(club.book, self.uid, "B1", "2026-09-29", "18:00", 1)
        self.sell(1)
        clock.set_now(datetime(2026, 10, 20, 8, 0))
        with self.assertRaises(club.ClubError) as e:                          # after it ends
            self.run_quiet(club.book, self.uid, "B1", "2026-10-28", "18:00", 1)
        self.assertIn("No active club membership", str(e.exception))

    def test_renewal_starts_after_current_end(self):
        self.sell(1)
        m2 = self.sell(3)
        self.assertEqual((m2["start_date"], m2["end_date"]), ("2026-10-28", "2027-01-27"))
        self.assertEqual(club.usage(self.uid)["until"], "2027-01-27")

    def test_club_members_log_in_with_a_password_and_see_usage(self):
        self.sell(1)
        with self.assertRaises(auth.AuthError):
            self.run_quiet(auth.request_login_code, "9811122233")
        s = members.status(self.uid)
        self.assertTrue(s["club"]["active"])
        self.assertEqual(s["club"]["current"]["total"]["left"], 53)
        self.assertFalse(s["fingerprint"]["door"])                           # no face entry: serials only
        roster = club.roster()
        self.assertEqual([(r["name"], r["left"]["padel"]) for r in roster], [("Zoya Khan", 7)])

    def test_online_renewal(self):
        config.CLUB_PRICE_MONTHLY = 9000
        try:
            self.sell(1)
            from app import payments
            from tests.test_core import rzp_proof
            pay = club.order(self.uid, 3)
            self.assertEqual(pay["amount"], 27000)
            p, _ = payments.capture(pay["payment_id"], rzp_proof(pay["payment_id"]))
            m = self.run_quiet(club.complete_online, p)
            self.assertEqual(m["months"], 3)
            self.assertIsNone(self.run_quiet(club.complete_online, p))        # once only
        finally:
            config.CLUB_PRICE_MONTHLY = 0
