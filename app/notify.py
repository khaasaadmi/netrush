"""SMS + WhatsApp. One call, `send(kind, phone, params)`, fans out to each channel the person
has opted into, logs every attempt, and (with a dedupe_key) guarantees a reminder is sent at
most once per channel no matter how often the scheduler runs.

Each message kind has ordered params so the same data fills a plain-text SMS, an MSG91 DLT
flow, and a Meta-approved WhatsApp template (whose {{1}}, {{2}}… follow PARAMS order).
"""
import base64
import json
import sqlite3
import urllib.parse
import urllib.request

from . import clock, config, db

PARAMS = {
    "door_code": ["name", "code", "court", "valid_from", "valid_to"],
    "booking_confirmed": ["name", "ref", "court", "when", "lead"],
    "membership_reminder": ["name", "plan", "end_date", "when"],
    "academy_fee_reminder": ["name", "batch", "end_date", "when"],
    "login_otp": ["code"],
    "booking_cancelled": ["name", "ref", "court", "when", "refund"],
    "refund_issued": ["amount", "what", "days"],
    "membership_confirmed": ["name", "plan", "end_date", "access"],
    "reservation_received": ["name", "ref", "court", "when", "deadline", "phone", "amount"],
    "reservation_released": ["name", "ref", "court", "when"],
    "referral_reward": ["name", "friend", "amount", "credit"],
}

TEXT = {
    "door_code": "Hi {name}, your entry code for {court} is {code}. Works {valid_from}-{valid_to}. "
    + config.DOOR_KEYPAD_STEPS + " Enjoy your game!",
    "booking_confirmed": "Hi {name}, booking {ref} confirmed: {court}, {when}. " + (
        ("Your door entry code comes in a separate message and opens the door from {lead} min before start."
         if config.DOOR_CODE_AT_BOOKING else "Your door entry code will be sent {lead} minutes before your slot.")
        if config.LOCK_ENABLED else "Show this message at the reception when you arrive. See you on court!"),
    "reservation_received": "Hi {name}, we are holding {court}, {when} for you (ref {ref}, {amount}). "
    "Confirm by {deadline}: pay online from your booking link or call {phone}. After that the slot is released.",
    "referral_reward": "Hi {name}, {friend} just joined NetRush with your referral. {amount} credit added: "
    "you now have {credit} off your next membership payment. Thank you!",
    "reservation_released": "Hi {name}, your reservation {ref} ({court}, {when}) was not confirmed in time and "
    "has been released. Book again any time at " + config.PUBLIC_URL + "/book",
    "membership_reminder": "Hi {name}, your {plan} membership {when} ({end_date}). "
    "Renew online at " + config.PUBLIC_URL + "/portal or at the desk to keep playing.",
    "academy_fee_reminder": "Hi {name}, academy fees for {batch} are paid until {end_date} and {when}. "
    "Please renew to continue training.",
    "login_otp": "{code} is your " + config.FACILITY_NAME + " login code. Valid 5 minutes. Do not share it.",
    "booking_cancelled": "Hi {name}, booking {ref} ({court}, {when}) is cancelled. {refund}",
    "refund_issued": "{amount} has been refunded for {what}. It reaches your account in {days}.",
    "membership_confirmed": "Hi {name}, your {plan} is active until {end_date}. {access}",
}


def render(kind, params):
    return TEXT[kind].format(**params)


def e164(phone):
    p = "".join(ch for ch in phone if ch.isdigit())
    return p if len(p) > 10 else "91" + p


# ---------- providers ----------

def _post(url, data=None, json_body=None, headers=None, auth=None):
    headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = urllib.parse.urlencode(data or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def _sms(kind, phone, params, text):
    p = config.SMS_PROVIDER
    if p == "console":
        print(f"[SMS -> {phone}] {text}", flush=True)
    elif p == "twilio":
        _post(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}/Messages.json",
            data={"To": "+" + e164(phone), "From": config.TWILIO_FROM, "Body": text},
            auth=(config.TWILIO_SID, config.TWILIO_TOKEN),
        )
    elif p == "2factor":
        # 2Factor.in OTP API: uses their pre-approved DLT template, so no DLT registration of your own.
        if kind != "login_otp":
            raise RuntimeError("2factor is set up for OTP messages only")
        url = (f"https://2factor.in/API/V1/{config.TWOFACTOR_API_KEY}/SMS/{e164(phone)[-10:]}/"
               f"{params['code']}/{config.TWOFACTOR_TEMPLATE}")
        with urllib.request.urlopen(url, timeout=15) as r:
            out = json.loads(r.read())
        if out.get("Status") != "Success":
            raise RuntimeError(f"2factor: {out}")
    elif p == "msg91":
        flow = config.MSG91_FLOWS.get(kind)
        if not flow:
            raise RuntimeError(f"no MSG91 flow configured for '{kind}'")
        _post(
            "https://control.msg91.com/api/v5/flow/",
            json_body={"template_id": flow, "short_url": "0",
                       "recipients": [{"mobiles": e164(phone), **{k: str(v) for k, v in params.items()}}]},
            headers={"authkey": config.MSG91_AUTHKEY, "accept": "application/json"},
        )
    else:
        raise RuntimeError(f"unknown SMS_PROVIDER {p}")


def _whatsapp(kind, phone, params, text):
    p = config.WHATSAPP_PROVIDER
    if p == "console":
        print(f"[WhatsApp -> {phone}] {text}", flush=True)
    elif p == "meta":
        values = [str(params[k]) for k in PARAMS[kind]]
        components = [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}]
        if kind == "login_otp":  # Meta authentication templates also need the code on the copy button
            components.append({"type": "button", "sub_type": "url", "index": "0",
                               "parameters": [{"type": "text", "text": values[0]}]})
        _post(
            f"https://graph.facebook.com/{config.WA_API_VERSION}/{config.WA_PHONE_NUMBER_ID}/messages",
            json_body={
                "messaging_product": "whatsapp",
                "to": e164(phone),
                "type": "template",
                "template": {"name": config.WA_TEMPLATES.get(kind, kind), "language": {"code": config.WA_LANG},
                             "components": components},
            },
            headers={"Authorization": f"Bearer {config.WA_TOKEN}"},
        )
    else:
        raise RuntimeError(f"unknown WHATSAPP_PROVIDER {p}")


SENDERS = {"sms": _sms, "whatsapp": _whatsapp}


# ---------- public ----------

CHANNELS = ("whatsapp", "sms")


def can_send(ch, kind=""):
    """Is a real provider connected for this channel (and message type)?"""
    if config.DEMO_MODE:
        return True
    if ch == "sms":
        if config.SMS_PROVIDER == "console":
            return False
        if config.SMS_PROVIDER == "2factor":
            return kind == "login_otp"
        if config.SMS_PROVIDER == "msg91":   # each SMS type needs its own DLT-approved template (flow)
            return bool(config.MSG91_FLOWS.get(kind)) if kind else any(config.MSG91_FLOWS.values())
        return True
    return config.WHATSAPP_PROVIDER != "console"


def message_channels():
    """Channels a customer can choose for booking messages and door codes."""
    return [ch for ch in CHANNELS if can_send(ch, "booking_confirmed")]


def preferred(phone, booking_channel=None):
    """The ONE channel this person wants: the booking's choice, then their saved choice, then the
    channel of their latest booking, then the default."""
    if booking_channel in CHANNELS:
        return booking_channel
    u = db.one("SELECT pref_channel, sms_opt, wa_opt FROM users WHERE phone=?", phone)
    if u and u["pref_channel"] in CHANNELS:
        return u["pref_channel"]
    if u and u["sms_opt"] != u["wa_opt"]:
        return "sms" if u["sms_opt"] else "whatsapp"
    b = db.one("SELECT notify_channel FROM bookings WHERE phone=? AND notify_channel IS NOT NULL "
               "ORDER BY id DESC LIMIT 1", phone)
    if b:
        return b["notify_channel"]
    return config.DEFAULT_CHANNEL if config.DEFAULT_CHANNEL in CHANNELS else "whatsapp"


def channels_for(phone, kind="", booking_channel=None):
    """Exactly one channel, so each message is paid for once. If the chosen channel isn't connected
    the other one is used, so nobody is left without their door code."""
    pref = preferred(phone, booking_channel)
    if can_send(pref, kind):
        return [pref]
    other = "sms" if pref == "whatsapp" else "whatsapp"
    if can_send(other, kind):
        return [other]
    return [pref]  # nothing connected yet: printed to the server log only


def send(kind, phone, params, dedupe_key=None, channels=None, booking_channel=None):
    """Returns {channel: 'sent'|'failed'|'duplicate'}. Never raises — a messaging outage
    must not break a booking or the scheduler."""
    params = {k: params[k] for k in PARAMS[kind]}
    text = render(kind, params)
    result = {}
    for ch in channels or channels_for(phone, kind, booking_channel):
        key = f"{dedupe_key}:{ch}" if dedupe_key else None
        c = db.conn()
        try:
            cur = c.execute(
                "INSERT INTO notifications(dedupe_key,kind,channel,to_phone,body,status,created_at) "
                "VALUES(?,?,?,?,?,'sending',?)",
                (key, kind, ch, phone, _redact(kind, text), clock.fmt(clock.now())),
            )
        except sqlite3.IntegrityError:
            result[ch] = "duplicate"
            continue
        nid = cur.lastrowid
        try:
            SENDERS[ch](kind, phone, params, text)
            c.execute("UPDATE notifications SET status='sent' WHERE id=?", (nid,))
            result[ch] = "sent"
        except Exception as e:  # noqa: BLE001 — provider errors are logged, not fatal
            # Free the dedupe key so the next scheduler pass retries this channel.
            c.execute("UPDATE notifications SET status='failed', error=?, dedupe_key=NULL WHERE id=?",
                      (str(e)[:500], nid))
            result[ch] = "failed"
    return result


def _redact(kind, text):
    """Login codes don't belong in a log staff can read. Door codes stay — the desk needs them
    to help someone stuck at the door."""
    if kind == "login_otp" and not config.DEMO_MODE:  # demo: the "demo phone" page needs to show it
        return "(login code sent)"
    return text
