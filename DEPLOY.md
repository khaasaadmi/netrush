# Going live with NetRush on netrush.in

The site is hosted on Render and deploys itself every time `main` is pushed to GitHub
(`khaasaadmi/netrush`). This guide takes it from the demo to the real site.

## What is live, and what waits for an account

| Feature | Works at launch | Needs |
|---|---|---|
| Landing page, live court availability | Yes | nothing |
| Reserve a slot (held 3 hours), confirm by phone | Yes | nothing |
| Staff panel: bookings, cash / UPI QR / card payments, day book, members, analytics, downloads | Yes | nothing |
| Traffic tracking per agency link | Yes | nothing |
| **Pay now** online, buy membership online, automatic refunds | Switched off | Razorpay keys (step 4) |
| Member / guest login, 15% first-booking discount | Switched off | an SMS provider for OTP (step 5) |
| Confirmation and reminder messages | Not sent | WhatsApp or DLT SMS (step 6) |
| Door codes and fingerprint entry | Switched off | the lock (`LOCK_ENABLED=1`, see README) |

Each "switched off" feature hides itself on the site until its keys are added. Nothing shows a demo label.

## 1. Keep the data: add a disk (do this before taking real bookings)

Render's free plan has no storage. **Every restart, deploy or sleep erases all bookings, members and payments.**
That is fine for a demo and not for a real arena.

1. Render dashboard → the `netrush` service → **Settings → Instance Type → Starter** ($7/month).
2. **Disks → Add Disk**: name `netrush-data`, mount path `/var/data`, size 1 GB.
3. **Environment → Add** `DB_PATH` = `/var/data/netrush.db`.

The Starter plan also never sleeps, so the reservation timer and reminders always run. Admin → Team & health
shows a red "Data storage" row until this is done.

## 2. Set the live values

Render dashboard → service → **Environment**. Set these, then **Save, rebuild and deploy**:

| Key | Value |
|---|---|
| `DEMO_MODE` | `0` |
| `SEED_DEMO` | `0` (the site starts empty: no demo people or bookings) |
| `PUBLIC_URL` | `https://netrush.in` |
| `ADMIN_PHONE` | the owner's mobile number (this is the admin login) |
| `ADMIN_PASSWORD` | a strong password of your own |
| `RECEPTION_PHONE` | the reception desk's mobile number |
| `RECEPTION_PASSWORD` | another password |
| `ARENA_PHONE` | the number customers call to confirm a reservation, e.g. `+91 98xxx xxxxx` |
| `ARENA_ADDRESS` | full address, shown on the landing page |
| `ARENA_MAPS_URL` | Google Maps link (optional) |
| `ARENA_INSTAGRAM` | Instagram profile link (optional) |

Staff logins are created from these values the first time the site starts with an empty database. Later
changes to staff are made in Admin → Team & health.

## 3. Point netrush.in at the site

1. Render dashboard → service → **Settings → Custom Domains → Add** `netrush.in`, then add `www.netrush.in`.
2. Render shows the DNS records to create. At the company where the domain was bought, open DNS settings and add:
   - for `www`: a **CNAME** record pointing to the `…onrender.com` address Render shows;
   - for the bare `netrush.in`: an **ALIAS / ANAME** record to the same address, or the **A** record IP Render shows.
3. Back in Render, press **Verify**. The HTTPS certificate is issued automatically, usually within minutes.
   DNS changes can take up to a few hours to reach everyone.

## 4. Online payments (Razorpay)

1. Razorpay dashboard → **Account & Settings → API Keys → Generate key**.
2. In Render → Environment set `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
3. Razorpay → **Webhooks → Add**: URL `https://netrush.in/api/payments/razorpay-webhook`, event
   `payment.captured`, and a secret of your choice. Put that secret in `RAZORPAY_WEBHOOK_SECRET`.

"Pay now", online membership purchase and automatic refunds switch on as soon as the keys are in.

**Test keys on the live site:** with `rzp_test_…` keys, the payment page accepts test cards and takes no money,
but the booking is still confirmed as paid. Use test keys only to try the flow, on a day you are not
advertising. Admin → Team & health shows a red warning while test keys are in use. Switch to the
`rzp_live_…` pair (available once Razorpay has approved the account's KYC) before promoting "Pay now".

## 5. OTP for login and the first-booking discount

OTP is used in two places only: member/guest login, and proving a number for the 15% first-booking discount.
Ordinary booking needs no OTP.

The quickest provider for India is 2Factor.in (no DLT paperwork for OTP): create an account, copy the API key,
and set `SMS_PROVIDER=2factor` and `TWOFACTOR_API_KEY`. This connection is written from 2Factor's
documentation and has not yet been run against a real account, so send yourself one login code before
announcing it.

## 6. Confirmation and reminder messages

Booking confirmations, reservation deadlines, membership reminders and refund notices need either a WhatsApp
Business (Cloud API) number with approved templates, or DLT-registered SMS templates through MSG91. Both
take a few days of approvals. The template names and settings are in `.env.example`. Until then customers
see their confirmation on screen, and staff see every booking in the panel.

## 7. Marketing links

Admin → Analytics → **Tracking link for an agency or campaign** builds a link per partner, for example
`https://netrush.in/?utm_source=brightads&utm_campaign=launch`. Give each agency its own. Analytics then shows
visitors, bookings and revenue per source, and the same table downloads as a spreadsheet.

## Launch-day checklist

1. `https://netrush.in/healthz` shows `{"ok": true}`.
2. Log in at `/admin` as the admin. **Team & health** should show no red rows.
3. Make a test reservation on your own phone, confirm it from the receptionist login, then cancel it.
4. Record a ₹1 "other" payment under Payments, and check it appears in the day book.
5. Open an agency link in a private window, and check the visit appears under Analytics.

## A private demo copy

To keep a demo for showing people, create a second Render service from the same repository with
`DEMO_MODE=1`. It loads demo data, shows login codes on screen, uses a mock payment page, and adds a
`/demo` page that displays every message. Never set `DEMO_MODE=1` on the real site.
