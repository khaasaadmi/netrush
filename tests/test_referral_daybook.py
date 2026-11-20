"""Referral credit, payments taken by staff (cash / UPI QR / card), advances and the day book."""
from app import analytics, auth, bookings, config, db, members, payments
from tests.test_core import Base


class ReferralTests(Base):
    AARAV = 2   # seeded member, NR-0002

    def newcomer(self, phone="9812345678", ref=None):
        code, _, _ = self.run_quiet(auth.request_login_code, phone)
        return auth.user_for(auth.verify_login_code(phone, code, "Neha Joshi", ref))["id"]

    def credit(self, uid):
        return db.one("SELECT credit FROM users WHERE id=?", uid)["credit"]

    def buy_online(self, uid, months=1):
        pay = self.run_quiet(members.order_membership, uid, months)
        if pay["amount"]:
            p, _ = payments.capture(pay["payment_id"], {})
            self.run_quiet(members.complete_membership, p)
        return pay

    def test_referrer_gets_300_when_friend_takes_first_membership(self):
        uid = self.newcomer(ref="nr-0002")                                  # signed up from Aarav's link
        self.assertEqual(self.credit(self.AARAV), 0)                        # signing up alone earns nothing
        self.buy_online(uid)
        self.assertEqual(self.credit(self.AARAV), config.REFERRAL_REWARD)
        self.assertEqual(len(self.sent("referral_reward")), 1)
        self.buy_online(uid)                                                # friend renews: no second reward
        self.assertEqual(self.credit(self.AARAV), 300)

    def test_credit_comes_off_referrers_next_payment_online(self):
        self.buy_online(self.newcomer(ref="NR-0002"))
        pay = self.buy_online(self.AARAV)
        self.assertEqual((pay["amount"], pay["credit_used"]), (config.ONLINE_MEMBERSHIP_MONTHLY - 300, 300))
        self.assertEqual(self.credit(self.AARAV), 0)
        self.assertEqual(self.buy_online(self.AARAV)["amount"], config.ONLINE_MEMBERSHIP_MONTHLY)  # used once

    def test_credit_comes_off_at_the_desk_too(self):
        self.buy_online(self.newcomer(ref="NR-0002"))
        m = self.run_quiet(members.add_membership, self.AARAV, 1, 1, "Reception", "upi")   # ₹2500 plan
        self.assertEqual((m["paid"], m["credit_used"]), (2200, 300))
        self.assertEqual(self.credit(self.AARAV), 0)

    def test_abandoned_checkout_keeps_the_credit(self):
        self.buy_online(self.newcomer(ref="NR-0002"))
        self.run_quiet(members.order_membership, self.AARAV, 1)             # opened the payment page, never paid
        self.assertEqual(self.credit(self.AARAV), 300)

    def test_reward_when_friend_joins_at_the_desk(self):
        uid = self.newcomer()
        self.assertEqual(members.set_referrer(uid, "NR-0002"), "Aarav Mehta")
        self.run_quiet(members.add_membership, uid, 1, 1, "Reception", "cash")
        self.assertEqual(self.credit(self.AARAV), 300)

    def test_rules(self):
        uid = self.newcomer()
        for bad in ("NR-9999", "hello", f"NR-{uid}", "NR-0001"):            # nobody, junk, yourself, a staff login
            with self.assertRaises(members.MemberError):
                members.set_referrer(uid, bad)
        guest = self.newcomer("9800000077")
        with self.assertRaises(members.MemberError):
            members.set_referrer(uid, f"NR-{guest}")                        # referrer must be a member
        members.set_referrer(uid, "NR-0002")
        with self.assertRaises(members.MemberError):
            members.set_referrer(uid, "NR-0003")                            # only once
        with self.assertRaises(members.MemberError):
            members.set_referrer(3, "NR-0002")                              # existing members can't be "referred"

    def test_many_referrals_can_cover_a_whole_month(self):
        db.conn().execute("UPDATE users SET credit=2400 WHERE id=?", (self.AARAV,))
        pay = self.buy_online(self.AARAV)
        self.assertEqual((pay["amount"], pay["credit_used"]), (0, 2000))
        self.assertEqual(self.credit(self.AARAV), 400)                      # the rest carries over
        self.assertEqual(len(members.status(self.AARAV)["memberships"]), 2)

    def test_portal_summary(self):
        self.buy_online(self.newcomer(ref="NR-0002"))
        r = members.status(self.AARAV)["referral"]
        self.assertEqual((r["code"], r["credit"], r["earned"], r["can_share"]), ("NR-0002", 300, 300, True))
        self.assertEqual(r["friends"], [{"name": "Neha", "joined": True}])


class DayBookTests(Base):
    DAY = "2026-09-28"

    def test_cash_upi_card_and_website_all_land_in_the_day_book(self):
        self.book_and_pay("P1", "20:00")                                                     # website 1800
        self.run_quiet(bookings.create_by_staff, "K1", self.DAY, "19:00", 1, "Walk-in", "9800000001",
                       "desk_paid", "Asha", None, "upi")                                     # UPI QR 500
        self.run_quiet(bookings.create_by_staff, "K2", self.DAY, "19:00", 1, "Walk-in 2", "9800000002",
                       "desk_paid", "Asha", None, "cash")                                    # cash 500
        payments.record_desk("other", None, 150, None, None, "Asha", method="cash", note="Shuttle tube")
        self.run_quiet(members.add_membership, 3, 1, 1, "Asha", "card")                      # card 2500
        d = payments.daybook(self.DAY)
        self.assertEqual(d["totals"], {"cash": 650, "upi": 500, "card": 2500, "online": 1800})
        self.assertEqual(d["total"], 5450)
        self.assertEqual(len(d["payments"]), 5)
        other = next(p for p in d["payments"] if p["kind"] == "other")
        self.assertEqual((other["note"], other["by"]), ("Shuttle tube", "Asha"))
        self.assertEqual(analytics.report(1)["money"]["other"], 150)

    def test_advance_then_balance(self):
        b = self.run_quiet(bookings.create_by_staff, "P1", self.DAY, "20:00", 1, "Caller", "9800000003",
                           "desk_paid", "Asha", None, "upi", 500)                            # ₹500 advance on ₹1800
        self.assertEqual((b["pay_status"], bookings.balance(b)), ("unpaid", 1300))
        b = self.run_quiet(bookings.mark_paid, b["id"], "Asha", "cash")
        self.assertEqual((b["pay_status"], bookings.balance(b)), ("paid", 0))
        self.assertEqual(payments.daybook(self.DAY)["totals"], {"cash": 1300, "upi": 500, "card": 0, "online": 0})

    def test_reservation_confirmed_with_advance_then_cancelled_refunds_the_advance(self):
        b = self.run_quiet(bookings.create, "P1", self.DAY, "20:00", 1, "Riya", "9811111111", None,
                           "reserve")["booking"]
        b = self.run_quiet(bookings.confirm_reservation, b["id"], "Asha", True, "upi", 600)
        self.assertEqual((b["status"], b["pay_status"], bookings.balance(b)), ("confirmed", "unpaid", 1200))
        self.assertEqual(self.run_quiet(bookings.start_payment, b)["amount"], 1200)          # online = balance only
        out = self.run_quiet(bookings.cancel, b["id"], "staff")
        self.assertEqual(out["refund"]["amount"], 600)
        self.assertEqual(db.one("SELECT refund_status FROM payments WHERE ref_id=? AND kind='booking' AND "
                                "status='paid'", b["id"])["refund_status"], "manual")

    def test_bad_amounts_and_methods_rejected(self):
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create_by_staff, "P1", self.DAY, "20:00", 1, "X", "9800000004",
                           "desk_paid", "Asha", None, "cash", 5000)                          # more than the price
        with self.assertRaises(payments.PaymentError):
            payments.record_desk("other", None, 100, None, None, "Asha", method="cheque", note="x")
