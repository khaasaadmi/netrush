"""Where visitors and bookings come from.

Every marketing partner gets their own link (netrush.in/?utm_source=agency-name&utm_campaign=diwali).
A visitor arriving on such a link is tagged with that source; the tag stays in their browser for 30 days,
so a booking made a few days later is still credited. Visitors without a tagged link are classified from
the site that sent them (Google, Instagram, WhatsApp…) or as 'direct' (typed the address, or no referrer).
Word of mouth can't be seen technically (it looks like 'direct'), so the booking form also asks
"How did you hear about us?".

First-party only: no cookies from third parties, no personal data. Just a random browser id.
"""
import re
import threading
import time
from collections import defaultdict
from datetime import timedelta

from . import clock, db

HEARD = ["Friend or family", "Instagram", "Google search", "Poster / hoarding", "Event / tournament", "Other"]

_REFERRERS = [("google.", "google"), ("bing.", "bing"), ("instagram.", "instagram"), ("facebook.", "facebook"),
              ("fb.", "facebook"), ("whatsapp.", "whatsapp"), ("wa.me", "whatsapp"), ("youtube.", "youtube"),
              ("t.co", "x"), ("twitter.", "x"), ("linkedin.", "linkedin"), ("justdial.", "justdial")]

_hits, _lock = defaultdict(list), threading.Lock()


def clean(v, n=40):
    """Lower-case tag made of safe characters only (these end up in reports and CSV files)."""
    return re.sub(r"[^a-z0-9_.\- ]", "", (v or "").strip().lower())[:n].strip()


def classify(source, referrer_host):
    source = clean(source)
    if source:
        return source
    host = clean(referrer_host, 80)
    if not host:
        return "direct"
    for needle, name in _REFERRERS:
        if needle in host:
            return name
    return host


def record(vid, source, medium, campaign, referrer_host, path, ip=None):
    vid = re.sub(r"[^A-Za-z0-9]", "", vid or "")[:32]
    if not vid:
        return None
    t = time.time()
    with _lock:  # a browser reports once per session; this only stops someone flooding the table
        _hits[ip] = [x for x in _hits[ip] if t - x < 3600]
        if len(_hits[ip]) >= 120:
            return None
        _hits[ip].append(t)
    src = classify(source, referrer_host)
    db.conn().execute("INSERT INTO visits(at,vid,source,medium,campaign,referrer,path) VALUES(?,?,?,?,?,?,?)",
                      (clock.fmt(clock.now()), vid, src, clean(medium) or None, clean(campaign) or None,
                       clean(referrer_host, 80) or None, (path or "")[:80]))
    return src


def booking_tag(source, campaign, referrer_host):
    """What gets stored on a booking: 'source' or 'source/campaign'."""
    src, camp = classify(source, referrer_host), clean(campaign)
    return f"{src}/{camp}" if camp else src


def report(days):
    """Per source: visitors, online bookings made, how many were confirmed, and money collected."""
    lo = (clock.today() - timedelta(days=days - 1)).isoformat()
    rows = defaultdict(lambda: {"visitors": 0, "bookings": 0, "confirmed": 0, "revenue": 0, "campaigns": set()})
    for v in db.all_("SELECT source, COUNT(DISTINCT vid) AS n FROM visits WHERE at>=? GROUP BY source", lo):
        rows[v["source"]]["visitors"] = v["n"]
    for v in db.all_("SELECT DISTINCT source, campaign FROM visits WHERE at>=? AND campaign IS NOT NULL", lo):
        rows[v["source"]]["campaigns"].add(v["campaign"])
    for b in db.all_(
            "SELECT b.utm, b.status, b.pay_status, COALESCE((SELECT SUM(p.amount - p.refund_amount) FROM payments p "
            "WHERE p.kind='booking' AND p.ref_id=b.id AND p.status='paid'),0) AS paid FROM bookings b "
            "WHERE b.source='online' AND b.created_at>=? AND b.status IN ('confirmed','reserved','cancelled')", lo):
        src, _, camp = (b["utm"] or "unknown").partition("/")
        r = rows[src]
        r["bookings"] += 1
        r["confirmed"] += b["status"] == "confirmed"
        r["revenue"] += b["paid"]
        if camp:
            r["campaigns"].add(camp)
    out = [{"source": k, **{**v, "campaigns": sorted(v["campaigns"])},
            "conversion": round(100 * v["bookings"] / v["visitors"], 1) if v["visitors"] else None}
           for k, v in rows.items()]
    out.sort(key=lambda r: (-r["revenue"], -r["bookings"], -r["visitors"]))
    heard = db.all_("SELECT heard AS answer, COUNT(*) AS n FROM bookings WHERE heard IS NOT NULL AND created_at>=? "
                    "AND status IN ('confirmed','reserved','cancelled') GROUP BY heard ORDER BY n DESC", lo)
    return {"sources": out, "heard": heard,
            "visitors": db.one("SELECT COUNT(DISTINCT vid) AS n FROM visits WHERE at>=?", lo)["n"]}
