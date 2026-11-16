"""The real site before a payment gateway is connected: nobody may get a 'paid' booking for free."""
from app import bookings, config, members
from tests.test_core import Base


class LiveWithoutGatewayTests(Base):
    def setUp(self):
        super().setUp()
        config.ONLINE_PAY = False

    def tearDown(self):
        config.ONLINE_PAY = True
        super().tearDown()

    def test_pay_now_refused_reserve_still_works(self):
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Riya", "9811111111")
        b = self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Riya", "9811111111", None,
                           "reserve")["booking"]
        self.assertEqual(b["status"], "reserved")
        with self.assertRaises(bookings.BookingError):
            bookings.start_payment(b)
        done = self.run_quiet(bookings.confirm_reservation, b["id"], "Asha", True, "upi")
        self.assertEqual(done["pay_status"], "paid")                       # the desk can still take the money

    def test_membership_cannot_be_bought_online_but_desk_works(self):
        with self.assertRaises(members.MemberError):
            members.order_membership(2, 1)
        self.assertEqual(self.run_quiet(members.add_membership, 2, 1, 1, "Asha", "cash")["paid"], 2500)


class DefaultPasswordTests(Base):
    def test_default_staff_passwords_refused_on_the_real_site(self):
        from app import auth
        config.DEMO_MODE = False
        try:
            for phone, pw in ((config.ADMIN_PHONE, "admin123"), (config.RECEPTION_PHONE, "desk123")):
                with self.assertRaises(auth.AuthError):
                    auth.admin_login(phone, pw)
        finally:
            config.DEMO_MODE = True
        self.assertTrue(auth.admin_login(config.ADMIN_PHONE, "admin123"))   # fine in a demo copy


class DefaultsTests(Base):
    def test_demo_is_off_unless_asked_for(self):
        import os
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if k not in ("DEMO_MODE", "SEED_DEMO", "PAYMENT_PROVIDER")}
        env["DB_PATH"] = os.path.join(self.tmp.name, "fresh.db")
        code = ("from app import config, db; db.init(); "
                "print(config.DEMO_MODE, config.SEED_DEMO, config.ONLINE_PAY, "
                "db.one('SELECT COUNT(*) AS n FROM users WHERE role IS NULL')['n'], "
                "db.one('SELECT COUNT(*) AS n FROM bookings')['n'])")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # run from a folder with no .env so only the built-in defaults apply
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); %s" % (root, code)],
                             env={**env, "NETRUSH_NO_DOTENV": "1"}, capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(out.stdout.split(), ["False", "False", "False", "0", "0"], out.stderr)
