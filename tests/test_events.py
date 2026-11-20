"""'Planning to hold an event?' enquiries."""
from datetime import timedelta

from tests.test_core import T0, Base  # first: it sets the test environment
from app import clock, config, db, events  # noqa: E402


class EventEnquiryTests(Base):
    def setUp(self):
        super().setUp()
        events._hits.clear()

    def submit(self, **kw):
        args = dict(name="Priya Menon", phone="98111 22233", people="40", message="Birthday party, Saturday evening")
        args.update(kw)
        return self.run_quiet(events.submit, args["name"], args["phone"], args["people"], args.get("message"),
                              args.get("ip", "5.5.5.5"), args.get("source"), args.get("trap"))

    def test_enquiry_is_stored_and_shows_in_the_admin_list(self):
        eid = self.submit(source="agency-brightads/diwali")
        e = db.one("SELECT * FROM event_enquiries WHERE id=?", eid)
        self.assertEqual((e["name"], e["phone"], e["people"], e["status"], e["source"]),
                         ("Priya Menon", "9811122233", 40, "new", "agency-brightads/diwali"))
        out = events.listing()
        self.assertEqual((len(out["enquiries"]), out["counts"]["new"]), (1, 1))

    def test_message_is_optional(self):
        eid = self.submit(message="   ")
        self.assertIsNone(db.one("SELECT message FROM event_enquiries WHERE id=?", eid)["message"])

    def test_bad_input_is_explained(self):
        for kw in ({"name": " "}, {"phone": "12345"}, {"people": "zero"}, {"people": "0"}, {"people": "5000"}):
            with self.assertRaises(events.EventError, msg=str(kw)):
                self.submit(**kw)

    def test_bots_filling_the_hidden_field_are_ignored(self):
        self.assertIsNone(self.submit(trap="http://spam.example"))
        self.assertEqual(db.all_("SELECT 1 FROM event_enquiries"), [])

    def test_double_submit_updates_instead_of_duplicating(self):
        a = self.submit(people="40")
        b = self.submit(people="55")
        self.assertEqual(a, b)
        self.assertEqual(db.one("SELECT people FROM event_enquiries WHERE id=?", a)["people"], 55)
        clock.set_now(T0 + timedelta(minutes=11))
        self.assertNotEqual(self.submit(), a)                              # a new enquiry later is kept separately

    def test_rate_limited_per_visitor(self):
        for i in range(events.PER_IP_PER_HOUR):
            self.submit(phone=f"98000000{i:02d}")
        with self.assertRaises(events.EventError):
            self.submit(phone="9800000099")

    def test_staff_alert_only_when_a_number_is_set(self):
        self.submit()
        self.assertEqual(self.sent("event_enquiry"), [])
        config.EVENT_NOTIFY_PHONE = "9876500000"
        try:
            self.submit(phone="9811100000")
            sent = self.sent("event_enquiry")
            self.assertEqual([(m["to_phone"], m["channel"]) for m in sent], [("9876500000", "whatsapp")])
            self.assertIn("40 people", sent[0]["body"])
        finally:
            config.EVENT_NOTIFY_PHONE = ""

    def test_staff_update_status_and_note(self):
        eid = self.submit()
        e = events.update(eid, "Asha", status="contacted", note="Called, wants Sat 7pm")
        self.assertEqual((e["status"], e["note"], e["handled_by"]), ("contacted", "Called, wants Sat 7pm", "Asha"))
        with self.assertRaises(events.EventError):
            events.update(eid, "Asha", status="maybe")
        self.assertEqual(events.listing("contacted")["counts"]["contacted"], 1)

    def test_routes(self):
        import server
        who = {fn.__name__: w for _, _, w, fn in server.ROUTES}
        self.assertEqual(who["event_enquiry"], "public")
        self.assertEqual((who["list_events"], who["update_event"]), ("staff", "staff"))
