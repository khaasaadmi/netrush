"""One messaging channel per customer, Firebase SMS login, the LAN-only lock API, and backups."""
import base64
import hashlib
import hmac
import json
import os
import random
import time

from tests.test_core import Base  # first: it sets the test environment before the app reads its config
from app import access, auth, bookings, config, db, firebase, notify  # noqa: E402

# ------------------------------------------------------------------ a throwaway RSA key to sign test tokens

_rng = random.Random(2026)


def _probable_prime(bits):
    while True:
        n = _rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and _miller_rabin(n):
            return n


def _miller_rabin(n, rounds=24):
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(_rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _keypair():
    e = 65537
    while True:
        p, q = _probable_prime(512), _probable_prime(512)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


N, E, D = _keypair()


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(claims, kid="k1", n=N, d=D):
    head = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    k = (n.bit_length() + 7) // 8
    digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{head}.{body}".encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(digest)) + b"\x00" + digest
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return f"{head}.{body}.{_b64(sig)}"


def claims(phone="+919812345678", **over):
    now = int(time.time())
    c = {"aud": "netrush-test", "iss": "https://securetoken.google.com/netrush-test", "sub": "uid1",
         "iat": now, "exp": now + 3600, "auth_time": now, "phone_number": phone,
         "firebase": {"sign_in_provider": "phone"}}
    c.update(over)
    return c


class FirebaseTests(Base):
    def setUp(self):
        super().setUp()
        self.saved = (config.FIREBASE_PROJECT_ID, config.FIREBASE_API_KEY)
        config.FIREBASE_PROJECT_ID, config.FIREBASE_API_KEY = "netrush-test", "test-key"
        firebase._keys.update(keys={"k1": (N, E)}, exp=time.time() + 3600)   # no network in tests

    def tearDown(self):
        config.FIREBASE_PROJECT_ID, config.FIREBASE_API_KEY = self.saved
        firebase._keys.update(keys={}, exp=0)
        super().tearDown()

    def test_valid_token_gives_the_phone(self):
        self.assertEqual(firebase.verify_id_token(make_token(claims())), "+919812345678")

    def test_bad_tokens_rejected(self):
        now = int(time.time())
        n2, e2, d2 = _keypair()
        bad = [
            make_token(claims(), n=n2, d=d2),                                  # signed by someone else
            make_token(claims(aud="other-project")),                           # another project
            make_token(claims(iss="https://securetoken.google.com/other")),
            make_token(claims(exp=now - 120)),                                 # expired
            make_token(claims(auth_time=now - 3600)),                          # verified too long ago
            make_token(claims(firebase={"sign_in_provider": "password"})),     # not a phone sign-in
            make_token(claims(phone_number=None)),
            make_token(claims(), kid="unknown"),
            "not.a.token", "",
        ]
        tampered = make_token(claims()).split(".")
        tampered[1] = _b64(json.dumps(claims(phone="+919999999999")).encode())
        bad.append(".".join(tampered))                                         # phone swapped after signing
        for t in bad:
            with self.assertRaises(firebase.FirebaseError, msg=t[:40]):
                firebase.verify_id_token(t, key_lookup=lambda kid: {"k1": (N, E)}.get(kid))

    def test_login_new_number_asks_name_then_signs_in_with_chosen_channel(self):
        t = make_token(claims())
        self.assertIsNone(auth.login_with_firebase(t))                         # new number, no name yet
        sess = auth.login_with_firebase(t, name="Neha Joshi", pref="sms")
        u = auth.user_for(sess)
        self.assertEqual((u["phone"], u["name"], u["pref_channel"]), ("9812345678", "Neha Joshi", "sms"))

    def test_returning_guest_signs_in_without_name(self):
        auth.login_with_firebase(make_token(claims()), name="Neha Joshi")
        u = auth.user_for(auth.login_with_firebase(make_token(claims())))
        self.assertEqual(u["name"], "Neha Joshi")

    def test_members_cannot_use_sms_codes(self):
        with self.assertRaises(auth.AuthError):
            auth.login_with_firebase(make_token(claims(phone="+919000000001")))

    def test_sms_codes_come_from_firebase_not_the_server(self):
        config.DEMO_MODE = False
        try:
            self.assertIn("sms", auth.otp_channels())
            with self.assertRaises(auth.AuthError):
                self.run_quiet(auth.request_login_code, "9812345678", None, "sms")
        finally:
            config.DEMO_MODE = True


class ChannelTests(Base):
    def test_booking_messages_go_only_to_the_chosen_channel(self):
        out = self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Riya", "9811111111", None,
                             "reserve", None, None, None, "sms")
        self.assertEqual(out["booking"]["notify_channel"], "sms")
        self.assertEqual([r["channel"] for r in self.sent("reservation_received")], ["sms"])
        self.run_quiet(bookings.cancel, out["booking"]["id"])
        self.assertEqual([r["channel"] for r in self.sent("booking_cancelled")], ["sms"])

    def test_door_code_follows_the_booking_choice(self):
        b = self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Riya", "9811111111", None,
                           "pay", None, None, None, "whatsapp")["booking"]
        p = db.one("SELECT id FROM payments WHERE kind='booking' AND ref_id=?", b["id"])
        from app import payments
        self.run_quiet(bookings.fulfil, payments.capture(p["id"], {})[0])
        from datetime import datetime
        from app import clock, access
        clock.set_now(datetime(2026, 9, 28, 19, 50))                          # 10 minutes before the slot
        self.run_quiet(access.issue_due_codes)
        self.assertEqual([r["channel"] for r in self.sent("door_code")], ["whatsapp"])

    def test_otp_goes_only_to_the_chosen_channel(self):
        self.run_quiet(auth.request_login_code, "9812345678", None, "whatsapp")
        self.assertEqual([r["channel"] for r in db.all_("SELECT channel FROM notifications WHERE kind='login_otp'")],
                         ["whatsapp"])

    def test_saved_preference_is_used_for_reminders(self):
        db.conn().execute("UPDATE users SET pref_channel='sms' WHERE phone='9000000001'")
        self.assertEqual(notify.channels_for("9000000001"), ["sms"])

    def test_falls_back_when_the_chosen_channel_is_not_connected(self):
        config.DEMO_MODE = False
        saved = (config.SMS_PROVIDER, config.WHATSAPP_PROVIDER)
        try:
            config.SMS_PROVIDER, config.WHATSAPP_PROVIDER = "console", "meta"      # only WhatsApp connected
            self.assertEqual(notify.channels_for("9811111111", "door_code", "sms"), ["whatsapp"])
            self.assertEqual(notify.message_channels(), ["whatsapp"])
            config.SMS_PROVIDER = "2factor"                                         # OTP-only SMS account
            self.assertEqual(notify.channels_for("9811111111", "door_code", "sms"), ["whatsapp"])
            self.assertEqual(notify.channels_for("9811111111", "login_otp", "sms"), ["sms"])
        finally:
            config.SMS_PROVIDER, config.WHATSAPP_PROVIDER = saved
            config.DEMO_MODE = True


class LockApiTests(Base):
    def signed(self, body=b'{"code":"123456"}'):
        ts = str(int(time.time()))
        return {"X-Timestamp": ts, "X-Signature": hmac.new(config.DEVICE_SECRET.encode(), f"{ts}.".encode() + body,
                                                           hashlib.sha256).hexdigest()}, body

    def test_signed_request_on_the_lan_is_allowed(self):
        h, body = self.signed()
        self.assertTrue(access.device_allowed("192.168.1.50", h, body)[0])

    def test_anything_through_netlify_or_the_tunnel_is_refused(self):
        h, body = self.signed()
        for extra in ({"X-Forwarded-For": "1.2.3.4"}, {"CF-Connecting-IP": "1.2.3.4"},
                      {"X-Nf-Client-Connection-Ip": "1.2.3.4"}):
            self.assertFalse(access.device_allowed("127.0.0.1", {**h, **extra}, body)[0])

    def test_ip_mode_for_controllers_that_cannot_sign(self):
        saved = (config.LOCK_AUTH, config.LOCK_ALLOWED_IPS)
        try:
            config.LOCK_AUTH, config.LOCK_ALLOWED_IPS = "ip", ["192.168.1.50", "127.0.0.1"]
            self.assertTrue(access.device_allowed("192.168.1.50", {}, b"")[0])
            self.assertFalse(access.device_allowed("192.168.1.51", {}, b"")[0])
            self.assertFalse(access.device_allowed("127.0.0.1", {}, b"")[0])      # the tunnel arrives from localhost
        finally:
            config.LOCK_AUTH, config.LOCK_ALLOWED_IPS = saved


class BackupTests(Base):
    def test_daily_backup_is_a_working_copy_and_old_ones_are_pruned(self):
        import sqlite3
        folder = os.path.join(self.tmp.name, "backups")
        os.makedirs(folder)
        open(os.path.join(folder, "netrush-2000-01-01.db"), "w").close()      # an ancient one
        path = db.backup(folder, keep_days=30)
        c = sqlite3.connect(path)
        try:
            self.assertGreater(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        finally:
            c.close()
        self.assertEqual(db.backup(folder, keep_days=30), path)               # once per day
        self.assertFalse(os.path.exists(os.path.join(folder, "netrush-2000-01-01.db")))


class Msg91Tests(Base):
    def test_sms_types_without_an_approved_template_fall_back_to_whatsapp(self):
        config.DEMO_MODE = False
        saved = (config.SMS_PROVIDER, config.WHATSAPP_PROVIDER, dict(config.MSG91_FLOWS))
        try:
            config.SMS_PROVIDER, config.WHATSAPP_PROVIDER = "msg91", "meta"
            for k in config.MSG91_FLOWS:
                config.MSG91_FLOWS[k] = ""
            config.MSG91_FLOWS.update(login_otp="flow-otp", door_code="flow-door")
            self.assertEqual(notify.channels_for("9811111111", "door_code", "sms"), ["sms"])
            self.assertEqual(notify.channels_for("9811111111", "referral_reward", "sms"), ["whatsapp"])
            self.assertIn("sms", auth.otp_channels())
        finally:
            config.SMS_PROVIDER, config.WHATSAPP_PROVIDER = saved[0], saved[1]
            config.MSG91_FLOWS.clear(); config.MSG91_FLOWS.update(saved[2])
            config.DEMO_MODE = True
