"""The ZKTeco MiniAC Plus driver, against a simulated terminal that behaves like pyzk's connection."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from tests.test_core import T0, Base  # first: it sets the test environment
from app import bookings, clock, config, db, zkteco  # noqa: E402


class FakeTerminal:
    """The subset of pyzk's ZK connection the driver uses."""

    def __init__(self):
        self.users = {}       # uid -> user
        self.log = []         # attendance records
        self.time = None
        self.calls = []

    def add(self, uid, user_id, name, group="1", privilege=0):
        self.users[uid] = SimpleNamespace(uid=uid, user_id=str(user_id), name=name, privilege=privilege,
                                          password="", group_id=group, card=0)

    def get_users(self):
        return [SimpleNamespace(**vars(u)) for u in self.users.values()]

    def set_user(self, uid, name, privilege=0, password="", group_id="", user_id="", card=0):
        self.calls.append(("set_user", user_id, group_id))
        self.users[uid] = SimpleNamespace(uid=uid, user_id=str(user_id), name=name, privilege=privilege,
                                          password=password, group_id=group_id, card=card)

    def delete_user(self, uid=0, user_id=""):
        self.calls.append(("delete_user", user_id))
        self.users.pop(uid, None)

    def get_attendance(self):
        return list(self.log)

    def clear_attendance(self):
        self.log = []

    def set_time(self, t):
        self.time = t

    def disconnect(self):
        pass

    def by_user_id(self, user_id):
        return next((u for u in self.users.values() if u.user_id == str(user_id)), None)


class ZKTecoTests(Base):
    def setUp(self):
        super().setUp()
        self.saved = (config.LOCK_PROVIDER, zkteco.connect)
        config.LOCK_PROVIDER = "zkteco"
        self.t = FakeTerminal()
        zkteco.connect = lambda: self.t
        # members enrolled on the terminal by staff (face), plus the terminal's own administrator
        self.t.add(1, "1", "Admin", privilege=14)
        self.t.add(2, "101", "Aarav")    # seeded member, plan ends T0+7
        self.t.add(3, "104", "Ishaan")   # academy player, fees paid to T0+7

    def tearDown(self):
        config.LOCK_PROVIDER, zkteco.connect = self.saved
        super().tearDown()

    def sync(self):
        from app import access
        self.run_quiet(access.issue_due_codes)   # the scheduler issues codes, then syncs the terminal
        return self.run_quiet(zkteco.sync)

    def test_code_is_on_the_terminal_only_inside_its_window(self):
        b = self.book_and_pay("P1", "10:00")                                  # 08:00 now, slot 10:00–11:00
        self.sync()
        self.assertEqual([u for u in self.t.users.values() if u.name.startswith("NRCODE")], [])   # too early
        clock.set_now(datetime(2026, 9, 28, 9, 50))
        self.assertEqual(self.sync()["codes_added"], 1)
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        u = self.t.by_user_id(code)
        self.assertEqual((u.password, u.group_id, u.name[:6]), (code, config.ZK_ACTIVE_GROUP, "NRCODE"))
        self.assertEqual(self.sync()["codes_added"], 0)                       # idempotent
        clock.set_now(datetime(2026, 9, 28, 11, 0))
        self.run_quiet(__import__("app.access", fromlist=["x"]).expire_codes)
        self.assertEqual(self.sync()["codes_removed"], 1)
        self.assertIsNone(self.t.by_user_id(code))

    def test_cancelled_booking_code_is_removed(self):
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        self.sync()
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.assertIsNotNone(self.t.by_user_id(code))
        self.run_quiet(bookings.cancel, b["id"])
        self.sync()
        self.assertIsNone(self.t.by_user_id(code))

    def test_lapsed_member_is_blocked_not_deleted_and_unblocked_on_renewal(self):
        from app import members
        self.assertEqual(self.sync()["members_blocked"], 1)                   # Ishaan: academy only, no face entry
        self.assertEqual(self.t.by_user_id("104").group_id, config.ZK_BLOCKED_GROUP)
        self.assertEqual(self.t.by_user_id("101").group_id, config.ZK_ACTIVE_GROUP)
        clock.set_now(T0 + timedelta(days=8))                                 # Aarav's plan has ended
        self.assertEqual(self.sync()["members_blocked"], 1)
        self.assertEqual(self.t.by_user_id("101").group_id, config.ZK_BLOCKED_GROUP)
        self.assertNotIn(("delete_user", "101"), self.t.calls)                # face enrolment kept
        self.run_quiet(members.add_membership, 2, 1, 1, "Desk", "cash")
        self.assertEqual(self.sync()["members_unblocked"], 1)
        self.assertEqual(self.t.by_user_id("101").group_id, config.ZK_ACTIVE_GROUP)

    def test_terminal_admin_and_unknown_users_are_never_touched(self):
        self.t.add(9, "555", "Someone staff enrolled")
        clock.set_now(T0 + timedelta(days=40))
        self.sync()
        self.assertEqual(self.t.by_user_id("1").group_id, "1")
        self.assertEqual(self.t.by_user_id("555").group_id, "1")
        self.assertFalse([c for c in self.t.calls if c[1] in ("1", "555")])

    def test_entry_log_is_imported_once(self):
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.t.log = [SimpleNamespace(user_id=code, timestamp=datetime(2026, 9, 28, 9, 58)),
                      SimpleNamespace(user_id="101", timestamp=datetime(2026, 9, 28, 9, 30))]
        self.assertEqual(self.sync()["entries"], 2)
        rows = db.all_("SELECT method, user_id, booking_id FROM access_events WHERE device_id='zkteco' ORDER BY at")
        self.assertEqual([(r["method"], r["booking_id"] is not None) for r in rows], [("face", False), ("code", True)])
        self.assertEqual(db.one("SELECT uses FROM door_codes WHERE booking_id=?", b["id"])["uses"], 1)
        clock.set_now(datetime(2026, 9, 28, 10, 5))
        self.assertEqual(self.sync()["entries"], 0)                           # same records: not stored twice

    def test_unreachable_terminal_is_reported_and_retried(self):
        def down():
            raise OSError("timed out")
        zkteco.connect = down
        self.assertIsNone(self.sync())
        self.assertIn("Can't reach the terminal", zkteco.status()["error"])
        zkteco.connect = lambda: self.t
        self.assertIsNotNone(self.sync())
        self.assertEqual(zkteco.status()["error"], "")

    def test_codes_never_collide_with_terminal_user_ids(self):
        from app import access
        db.conn().execute("UPDATE users SET lock_user_id='123456' WHERE phone='9000000003'")
        codes = {access._new_code(db.conn()) for _ in range(300)}
        self.assertNotIn("123456", codes)
        self.assertTrue(all(len(c) == 6 and c[0] != "0" for c in codes))

    def test_academy_players_get_face_entry_only_when_switched_on(self):
        config.ACADEMY_DOOR_ACCESS = True
        try:
            self.sync()
            self.assertEqual(self.t.by_user_id("104").group_id, config.ZK_ACTIVE_GROUP)
        finally:
            config.ACADEMY_DOOR_ACCESS = False
