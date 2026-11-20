"""ZKTeco MiniAC Plus (and other ZKTeco standalone terminals on TCP port 4370).

The terminal decides at the door by itself, from the users stored on it. So instead of the terminal
asking us, we keep the terminal in step with the database, every scheduler tick (30 s):

  • Guests' door codes: when a paid booking's window opens (10 min before start) we add a temporary
    user whose user ID and password are the door code; when the slot ends or the booking is
    cancelled we delete it. The terminal has no notion of "valid until", so the timing lives here.
  • Members (face recognition): enrolled once on the terminal, and never deleted by us. While their
    membership is active (and academy fees, if ACADEMY_DOOR_ACCESS) they're in the access group that may open the door
    (ZK_ACTIVE_GROUP); when they lapse they're moved to a group with no access time (ZK_BLOCKED_GROUP),
    and back again when they renew. Their face enrolment stays on the terminal throughout.
  • Entries: the terminal's log is read and stored as access events (attendance, the access log).
  • Clock: the terminal's time is set from ours once a day, so its log times line up.

Talks to the terminal through the open-source `pyzk` library (pip install pyzk), the standard
ZKTeco protocol. Needs the laptop and the terminal on the same network.
"""
import json
import sqlite3
import threading
from datetime import timedelta

from . import clock, config, db

CODE_NAME = "NRCODE"   # name prefix of the temporary users we create for booking codes
_busy = threading.Lock()


class ZKError(Exception):
    pass


def _connect():
    try:
        from zk import ZK
    except ImportError:
        raise ZKError("The ZKTeco driver needs the 'pyzk' package. On the server run:  pip install pyzk")
    zk = ZK(config.ZK_IP, port=config.ZK_PORT, timeout=8, password=config.ZK_COMM_KEY, ommit_ping=True)
    return zk.connect()


connect = _connect   # tests replace this with a simulated terminal


def enabled():
    return config.LOCK_ENABLED and config.LOCK_PROVIDER == "zkteco"


def sync():
    """One pass. Returns a short report dict, or None if the terminal couldn't be reached."""
    if not _busy.acquire(blocking=False):
        return None   # the previous pass is still running
    try:
        try:
            conn = connect()
        except Exception as e:  # noqa: BLE001 — shown on the health page; retried next tick
            db.kv_set("zk_error", f"Can't reach the terminal at {config.ZK_IP}: {e}"[:300])
            return None
        try:
            report = _sync(conn)
            db.kv_set("zk_ok_at", clock.fmt(clock.now()))
            db.kv_set("zk_error", "")
            db.kv_set("zk_report", json.dumps(report))
            return report
        except Exception as e:  # noqa: BLE001
            db.kv_set("zk_error", f"Sync failed: {e}"[:300])
            return None
        finally:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001
                pass
    finally:
        _busy.release()


def _sync(conn):
    from .access import door_access
    now = clock.now()
    nowf = clock.fmt(now)
    report = {"codes_added": 0, "codes_removed": 0, "members_blocked": 0, "members_unblocked": 0,
              "entries": 0, "unsafe": []}
    users = conn.get_users()
    by_id = {str(u.user_id): u for u in users}
    used_uids = {u.uid for u in users}

    def free_uid(prefer=None):
        if prefer and prefer not in used_uids:
            used_uids.add(prefer)
            return prefer
        uid = max(used_uids or {0}) + 1
        used_uids.add(uid)
        return uid

    # 1. booking codes: exactly the codes whose window is open right now
    want = {d["code"]: d for d in db.all_(
        "SELECT d.code, b.ref FROM door_codes d JOIN bookings b ON b.id=d.booking_id "
        "WHERE d.status='active' AND d.valid_from <= ? AND d.valid_to > ?", nowf, nowf)}
    have = {uid: u for uid, u in by_id.items() if (u.name or "").startswith(CODE_NAME)}
    for code in sorted(set(want) - set(have)):
        if code in by_id:   # clashes with a real person's ID on the terminal: never overwrite them
            report["unsafe"].append(f"door code {code} clashes with terminal user {code}")
            continue
        conn.set_user(uid=free_uid(), name=f"{CODE_NAME} {want[code]['ref']}"[:24], privilege=0,
                      password=code, group_id=config.ZK_ACTIVE_GROUP, user_id=code)
        report["codes_added"] += 1
    for code in sorted(set(have) - set(want)):
        conn.delete_user(uid=have[code].uid, user_id=code)
        report["codes_removed"] += 1

    # 2. members: never deleted (their face enrolment can't be copied back to the terminal), only moved
    #    between the access group that may open the door and the one that may not
    members = db.all_("SELECT id, name, lock_user_id FROM users WHERE lock_user_id IS NOT NULL "
                      "AND lock_user_id <> '' AND role IS NULL")
    for m in members:
        lid = str(m["lock_user_id"])
        u = by_id.get(lid)
        if u is None:
            continue   # not enrolled on the terminal yet
        if u.privilege:
            continue   # a terminal administrator: never touched
        entitled = bool(door_access(m["id"]))
        group = config.ZK_ACTIVE_GROUP if entitled else config.ZK_BLOCKED_GROUP
        if str(u.group_id or "") != group:
            conn.set_user(uid=u.uid, name=u.name, privilege=u.privilege, password=u.password or "",
                          group_id=group, user_id=lid, card=u.card or 0)
            report["members_unblocked" if entitled else "members_blocked"] += 1
        db.conn().execute("UPDATE users SET lock_enabled=? WHERE id=?", (int(entitled), m["id"]))

    # 3. the terminal's entry log → access events (each one stored once)
    last = db.kv_get("zk_logs_at")
    if not last or clock.parse(last) <= now - timedelta(minutes=config.ZK_LOG_EVERY_MIN):
        report["entries"] = _read_log(conn)
        db.kv_set("zk_logs_at", nowf)

    # 4. keep the terminal's clock right
    if db.kv_get("zk_time_day") != now.date().isoformat():
        conn.set_time(now)
        db.kv_set("zk_time_day", now.date().isoformat())
    if report["unsafe"]:
        db.kv_set("zk_warn", "; ".join(report["unsafe"])[:500])
    else:
        db.kv_set("zk_warn", "")
    return report


def _read_log(conn):
    added = 0
    since = clock.fmt(clock.now() - timedelta(days=config.ZK_LOG_DAYS))
    for a in conn.get_attendance():
        at = a.timestamp.strftime("%Y-%m-%d %H:%M")
        if at < since:
            continue
        uid = str(a.user_id)
        code = db.one("SELECT booking_id FROM door_codes WHERE code=? AND valid_from<=? AND valid_to>=? "
                      "ORDER BY id DESC LIMIT 1", uid, at, at)
        member = None if code else db.one("SELECT id FROM users WHERE lock_user_id=?", uid)
        try:
            db.conn().execute(
                "INSERT INTO access_events(at,device_id,method,user_id,booking_id,granted,detail,ext_id) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (at, "zkteco", "code" if code else "face", member and member["id"], code and code["booking_id"],
                 "door code" if code else ("member" if member else f"terminal user {uid}"),
                 f"zk:{uid}:{a.timestamp.isoformat()}"))
        except sqlite3.IntegrityError:
            continue   # already stored
        if code:
            db.conn().execute("UPDATE door_codes SET uses=uses+1 WHERE booking_id=?", (code["booking_id"],))
        added += 1
    if added and config.ZK_CLEAR_LOGS:
        conn.clear_attendance()
    return added


def status():
    """For the health page."""
    return {"ok_at": db.kv_get("zk_ok_at"), "error": db.kv_get("zk_error") or "", "warn": db.kv_get("zk_warn") or "",
            "report": json.loads(db.kv_get("zk_report") or "{}")}
