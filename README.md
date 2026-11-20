# NetRush: landing page, court booking, memberships and academy

For an arena with **2 badminton courts (₹1,000/h), 1 padel court (₹1,800/h) and 2 pickleball courts (₹500/h)**.

| Who | What they get |
|---|---|
| **Visitors** | A landing page at `/` and live court availability at `/book`. |
| **Guests** (no account) | Book with name + mobile. **Pay now** online, or **reserve and pay at the venue**: the slot is held 3 hours and must be confirmed in that time (pay online, or call to pay an advance). Verifying the mobile by OTP gives **15% off the first booking**. Cancel online with an automatic refund (100% ≥24h before, 50% ≥6h, none after). |
| **Members** | Log in with member ID (`NR-0002`) or username + password. See membership status and days left, **renew online at ₹2,000/month** (joining is done at the reception), pay for and cancel bookings. **Referrals:** when a friend signs up with their code and takes a first membership, the member gets ₹300 off their next payment. |
| **Academy players** | Batch, coach, schedule and fee status in the same portal. Fee reminders 7 days before and on the last day. |
| **Receptionist** (`/admin`) | Today's bookings, reservations to confirm, phone and walk-in bookings, **payments by cash / UPI QR / card** (full or advance), other income, a **day book** with totals per method, members, academy fees, refunds to hand back. |
| **Admin** (`/admin`) | Everything above, plus **analytics and revenue**, **traffic per agency / source**, **CSV downloads**, staff logins and a health page. |

Python standard library + SQLite, no build step. The one extra package is `pyzk`, for talking to the ZKTeco door terminal (the setup script installs it).

## Run it locally

```bash
python server.py
```

Settings come from a git-ignored `.env` file (copy `.env.example`). A fresh database holds only the courts, the
membership plans and the two staff logins; set your own `ADMIN_PASSWORD` and `RECEPTION_PASSWORD` first, because
the defaults are refused. "Pay now" stays hidden until Razorpay keys are added.

The tests replace Razorpay, SMS and WhatsApp with fakes, so they never call a real service:

```bash
python -m unittest discover -s tests
```

**Running it for real:** the server runs on a laptop / PC at the arena next to the door terminal, the pages are
on Netlify, and a Cloudflare Tunnel connects the two. Start with [GO-LIVE.md](GO-LIVE.md) (the order, every account and ID, and where each goes), then [DEPLOY.md](DEPLOY.md) for the detailed steps.

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
- **Door: ZKTeco MiniAC Plus.** Members (monthly / club membership) enter by face recognition; their access
  follows their membership automatically (blocked when it lapses, unblocked on renewal, face kept enrolled).
  Guests get a door code on their chosen channel 10 minutes before their slot; it works until the slot ends.
  The server keeps the terminal in step every 30 seconds and imports its entry log (`app/zkteco.py`).
- **One channel per customer.** Each person picks WhatsApp or SMS when booking or logging in, and gets every
  code and message once, on that channel: WhatsApp through Meta's Cloud API, SMS through MSG91.
- **Member logins are created by the desk.** Reception or the admin creates each member's or academy player's
  login (member ID + temporary password); at the first sign-in they must set their own password and may pick a
  username. Guests book with their mobile number and can log in with a one-time code.
- **Club membership.** All three sports with monthly hours (26 badminton, 20 pickleball, 7 padel). Every club
  month gets its own 6-digit serials, one per hour per sport; each booking's first serial is its door code, live
  on the terminal only for that slot. Unused hours expire at month end. Members, reception and admin all see the
  usage; members book and renew in the portal.
- **Events and parties.** A "Planning to hold an event?" section on the home page takes enquiries (name, mobile,
  number of people, optional message) into Admin → Events, with an optional WhatsApp alert to a staff phone.
- **Light and dark mode** on every page: follows the device, or the visitor's choice from the sun/moon button.

## Layout

```
server.py            routes: public pages, booking, payment page, portal, staff panel, exports
app/bookings.py      availability, holds, reservations, staff bookings, cancellation and refund policy
app/payments.py      online orders, verification, webhook, desk payments, day book, automatic refunds
app/members.py       portal status, memberships, referrals, academy fees, reminders
app/club.py          club membership: monthly hours per sport, serial numbers, booking, expiry, usage
app/events.py        event / party enquiries: validation, spam guard, staff alert, statuses
app/tracking.py      visit sources and per-source bookings and revenue
app/analytics.py     revenue, utilisation, peak hours, insights
app/health.py        what's working and what isn't
app/auth.py          member passwords (desk-issued, changed at first sign-in), guest one-time codes, staff, roles
app/firebase.py      optional Firebase Phone Auth (not used by default: MSG91 is far cheaper in India)
app/zkteco.py        keeps the ZKTeco terminal in step: guest codes, member access, entry log, clock
app/notify.py        WhatsApp and SMS providers, one chosen channel per customer, de-duplication
app/access.py        door codes, who may enter, lock adapters
app/seed.py          courts, plans, staff logins (first run only)
static/              landing, book, pay, booking, portal, admin, brand/ (logo files)
netlify.toml         Netlify: serve static/, forward /api to the arena server
deploy/windows/      one-time laptop setup (start at boot, no sleep), restart and update scripts
```
