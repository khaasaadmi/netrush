# NetRush: landing page, court booking, memberships and academy

For an arena with **2 badminton courts (₹1,000/h), 1 padel court (₹1,800/h) and 2 pickleball courts (₹500/h)**.

| Who | What they get |
|---|---|
| **Visitors** | A landing page at `/` and live court availability at `/book`. |
| **Guests** (no account) | Book with name + mobile. **Pay now** online, or **reserve and pay at the venue**: the slot is held 3 hours and must be confirmed in that time (pay online, or call to pay an advance). Verifying the mobile by OTP gives **15% off the first booking**. Cancel online with an automatic refund (100% ≥24h before, 50% ≥6h, none after). |
| **Members** | Log in with mobile or member ID (`NR-0002`) + OTP. See membership status and days left, **buy or renew online at ₹2,000/month**, pay for and cancel bookings. **Referrals:** when a friend signs up with their code and takes a first membership, the member gets ₹300 off their next payment. |
| **Academy players** | Batch, coach, schedule and fee status in the same portal. Fee reminders 7 days before and on the last day. |
| **Receptionist** (`/admin`) | Today's bookings, reservations to confirm, phone and walk-in bookings, **payments by cash / UPI QR / card** (full or advance), other income, a **day book** with totals per method, members, academy fees, refunds to hand back. |
| **Admin** (`/admin`) | Everything above, plus **analytics and revenue**, **traffic per agency / source**, **CSV downloads**, staff logins and a health page. |

Python standard library + SQLite. No dependencies, no build step.

## Run it locally

```bash
python server.py
```

A git-ignored `.env` file with `DEMO_MODE=1` gives a local demo: demo people and a month of history, a mock payment
page, login codes shown on screen, and a `/demo` page showing every message. Demo logins: admin
`9999999999` / `admin123`, reception `8888888888` / `desk123`, member `NR-0002`, academy player `NR-0005`.

Without `DEMO_MODE=1` the site runs as the real thing: empty, no demo pages, and online payment hidden until a
gateway is connected.

```bash
python -m unittest discover -s tests
```

**Going live:** see [DEPLOY.md](DEPLOY.md).

## How it behaves

- **No double booking.** A slot being paid for is held 10 minutes; a reservation is held 3 hours (never past
  30 minutes before the slot); both release automatically.
- **Money is what was actually received.** Every payment is a row: website, cash, UPI QR or card, for bookings,
  memberships, academy fees or other income. Advances leave a balance to collect. Unpaid reservations and
  complimentary bookings never count as revenue.
- **Refunds.** Online payments are refunded through the gateway automatically (retried, then flagged). Cash and
  UPI QR payments are listed for staff to hand back.
- **No free bookings by accident.** Without a real payment gateway, "Pay now" is unavailable and the site offers
  reserve-and-pay-at-venue only.
- **Tracking.** A tagged link (`/?utm_source=agency&utm_campaign=x`) credits that source for 30 days. Untagged
  visitors are classified by the referring site, or as `direct`. The booking form also asks "How did you hear
  about us?", because word of mouth can't be detected technically.
- **Door lock** (codes and fingerprints) is built but switched off (`LOCK_ENABLED=0`) until the hardware is chosen.

## Layout

```
server.py            routes: public pages, booking, payment page, portal, staff panel, exports
app/bookings.py      availability, holds, reservations, staff bookings, cancellation and refund policy
app/payments.py      online orders, verification, webhook, desk payments, day book, automatic refunds
app/members.py       portal status, memberships, referrals, academy fees, reminders
app/tracking.py      visit sources and per-source bookings and revenue
app/analytics.py     revenue, utilisation, peak hours, insights
app/health.py        what's working and what isn't
app/auth.py          OTP login, staff logins and roles, rate limits
app/notify.py        SMS and WhatsApp providers, logging and de-duplication
app/access.py        door codes and fingerprint sync (off for now)
app/seed.py          courts, plans, staff logins, demo data
static/              landing, book, pay, booking, portal, admin, brand/ (logo files)
```
