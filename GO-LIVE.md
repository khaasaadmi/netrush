# NetRush go-live walkthrough

The order to do things in, every account and ID you need, and exactly where each one goes. The detailed
click-by-click steps for each part are in [DEPLOY.md](DEPLOY.md); this page is the map.

Every ID or key goes into **one file**, `C:\netrush\.env`, on the arena laptop. After changing it, double-click
`C:\netrush\deploy\windows\restart.cmd`. Never send `.env` to anyone or put it on GitHub (it's git-ignored).

## Start these first: they take days to approve

| What | Where | Typical wait | Needed for |
|---|---|---|---|
| DLT registration (entity + sender name + templates) | Jio TrueConnect, Airtel or Vi DLT portal | 2–7 days | Any SMS in India |
| Meta Business verification + WhatsApp templates | business.facebook.com, developers.facebook.com | 1–5 days | WhatsApp messages |
| Razorpay account KYC | dashboard.razorpay.com | 2–5 days | "Pay now" and online renewals |
| Domain `netrush.in` moved to Cloudflare | Cloudflare + your domain seller | a few hours | The website address |

Keep your **GST certificate, PAN, cancelled cheque / bank details and business address proof** ready: all four
ask for some of them. Until messages or Razorpay are approved the site still works: customers reserve and pay at
the venue, and the desk reads door codes from Admin → Today.

## Every ID, where you get it, where it goes

| Setting in `.env` | What it is | Where you get it |
|---|---|---|
| `ADMIN_PHONE`, `ADMIN_PASSWORD` | Your admin login | You choose (not `admin123`, which is refused) |
| `RECEPTION_PHONE`, `RECEPTION_PASSWORD` | The front-desk login | You choose (not `desk123`) |
| `ARENA_PHONE`, `ARENA_ADDRESS`, `ARENA_MAPS_URL`, `ARENA_INSTAGRAM` | Shown on the site and in messages | Yours; Maps link from Google Maps → Share |
| `EVENT_NOTIFY_PHONE` | Staff mobile that gets event enquiries | Yours |
| `ZK_IP` | The door terminal's address, e.g. `192.168.1.201` | Terminal: Menu → Comm. → Ethernet (fix it in the router) |
| `ZK_COMM_KEY` | The terminal's connection password (`0` = none) | Terminal: Menu → Comm. → PC Connection |
| `DOOR_KEYPAD_STEPS` | What a guest types at the keypad | Confirm with the installer on the real terminal |
| `BACKUP_DIR` | Folder for the daily backup | A Google Drive / OneDrive folder synced on the laptop, e.g. `C:\Users\<you>\Google Drive\NetRush backups` |
| `MSG91_AUTHKEY` | Lets the server send SMS | MSG91 → (top right) Authkey |
| `MSG91_FLOW_LOGIN`, `_DOOR_CODE`, `_BOOKING`, `_RESERVED` (+ optional others) | One ID per approved SMS template | MSG91 → Flow → create a flow per DLT-approved template → copy its template ID |
| `WA_TOKEN` | Permanent WhatsApp access token | Meta Business settings → System users → Generate token (WhatsApp permissions) |
| `WA_PHONE_NUMBER_ID` | Your WhatsApp sender number's ID (not the phone number) | developers.facebook.com → your app → WhatsApp → API setup |
| (template names) | `door_code`, `booking_confirmed`, `login_otp`, … | Create them in WhatsApp Manager with exactly these names |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Live API keys (`rzp_live_…`) | Razorpay → Account & Settings → API keys |
| `RAZORPAY_WEBHOOK_SECRET` | A secret you invent for the webhook | Razorpay → Webhooks → add `https://netrush.in/api/payments/razorpay-webhook`, event `payment.captured` |
| `CLUB_PRICE_MONTHLY` | Club membership price per month | Your decision (leave 0 to type the amount at the desk) |

And switch these on once the matching IDs are in:

| Setting | Set to | When |
|---|---|---|
| `SMS_PROVIDER` | `msg91` | MSG91 authkey + at least the login and door-code flows are in |
| `WHATSAPP_PROVIDER` | `meta` | WhatsApp token, number ID and templates are approved |
| `PAYMENT_PROVIDER` | `razorpay` | Live Razorpay keys are in |

Two more accounts with no ID in `.env`:

- **Cloudflare** (free): owns the `netrush.in` DNS and runs the tunnel `api.netrush.in → laptop`. The tunnel
  token is installed once on the laptop as a Windows service (DEPLOY.md step 3).
- **Netlify** (free): serves the pages from the GitHub repo `khaasaadmi/netrush` (DEPLOY.md step 4).

## Day 1: the spare laptop becomes the server

1. **Laptop:** Windows 10/11, plugged in, on a UPS if possible, cable to the arena router. Log in to Windows once.
2. **Router:** reserve fixed addresses for the laptop and the ZKTeco terminal (DHCP reservation).
3. Copy `deploy\windows\install.ps1` from GitHub onto the laptop (or a USB stick).
4. Start → type PowerShell → right-click → **Run as administrator**, then:
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```
   It installs Python, Git and cloudflared, downloads NetRush to `C:\netrush`, and opens `.env` in Notepad.
   Fill in the staff logins, arena details, `ZK_IP`, `ZK_COMM_KEY` and `BACKUP_DIR`; save; close Notepad.
   It then sets NetRush to start at boot, switches off sleep (including with the lid closed) and prints
   **"NetRush is running"**.
5. On the laptop open `http://localhost:8000/admin`, log in, open **Team & health**. Red rows tell you what's left.

From then on: change settings → `restart.cmd`; new version from GitHub → `update.cmd`.

## Day 1–2: put it on the internet

6. **Cloudflare:** add `netrush.in`, change the nameservers at your domain seller, create the tunnel `netrush`,
   run its install command on the laptop, add public hostname `api.netrush.in → http://localhost:8000`.
   Check `https://api.netrush.in/healthz` shows `{"ok": true}`.
7. **Netlify:** import `khaasaadmi/netrush` (no build settings needed), add the domain `netrush.in`, add the DNS
   records it shows in Cloudflare with the orange cloud **off**. Check `https://netrush.in/healthz`.

## Day 2: the door terminal (with the installer)

8. On the terminal: access group **1** = opening hours, group **2** = no access (DEPLOY.md step 7).
9. Ask the installer what a guest types for a code (user ID + password): write it into `DOOR_KEYPAD_STEPS`.
10. Enrol a staff face as a test, put its user ID in Admin → People → Edit → "User ID on the door terminal".
11. Team & health → "Door terminal (ZKTeco)" should be green.

## As approvals arrive

12. **MSG91 approved:** paste the authkey and flow IDs, `SMS_PROVIDER=msg91`, `restart.cmd`.
13. **WhatsApp approved:** paste token and number ID, `WHATSAPP_PROVIDER=meta`, `restart.cmd`.
14. **Razorpay live:** paste keys and webhook secret, `PAYMENT_PROVIDER=razorpay`, `restart.cmd`.

## Before opening the doors

Run the launch-day checklist at the end of [DEPLOY.md](DEPLOY.md): a real booking on each channel, the door code
opening the door 10 minutes before, a member login, a club booking using up one hour, and the laptop going offline
while the door keeps working for faces.

## Who enters how

| Person | How they get in | What the server does |
|---|---|---|
| Guest (paid or confirmed booking) | 6-digit code, sent 10 min before the slot | Adds the code to the terminal for the slot, deletes it after |
| Club member | The serial of their booked hour, sent 10 min before | Same as a guest code; each hour uses one serial of that sport |
| Monthly member | Face | Keeps them in group 1 while paid up, group 2 when lapsed; never deletes the face |
| Academy player | With the coach (or face, if `ACADEMY_DOOR_ACCESS=1`) | Same as a monthly member when switched on |

**Face data** stays only on the terminal (see DEPLOY.md step 7): not on the laptop, the backups or any cloud.
