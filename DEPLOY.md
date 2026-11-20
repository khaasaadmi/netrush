# Running NetRush: laptop at the arena + Netlify + MSG91 / WhatsApp

```
 customer's phone ──► netrush.in (Netlify: the pages)
                          │  /api/…  forwarded
                          ▼
                 api.netrush.in (Cloudflare Tunnel, free)
                          │  outbound connection from the laptop: no router ports opened
                          ▼
         NetRush server on the laptop / PC at the arena ◄──► ZKTeco MiniAC Plus (same network)
           • all bookings, members and payments live here (data/netrush.db)
           • daily backup copy (BACKUP_DIR)
```

- **No Render needed.** The server and every piece of customer data stay on your own machine.
- **Netlify** only serves the pages and forwards `/api` requests; it stores nothing.
- **MSG91 and WhatsApp** deliver the messages: guests' door codes, booking messages and login codes, each customer
  on the one channel they chose. The server sends the login codes itself, so no Firebase or Supabase is needed
  (they cost about ₹6 per SMS in India against about ₹0.25 through MSG91).
- **The door works even if the internet drops.** The laptop talks to the terminal over the arena's own network.
  Guests' codes reach them before their slot, so they already have them.
- **If the laptop is off or offline, online booking pauses** (the pages show "briefly unavailable"). Keep it on a
  UPS, plugged in, and wired to the router if possible.

## 1. Network at the arena

1. Connect the laptop and the ZKTeco terminal to the same router, by cable if possible.
2. In the router's settings, give both a **fixed address (DHCP reservation)**, for example laptop `192.168.1.10`,
   terminal `192.168.1.201`. On the terminal: Menu → Comm. → Ethernet shows its address.
3. On the terminal note the **Comm Key** (Menu → Comm. → PC Connection; 0 means none).

## 2. The NetRush server on the laptop

1. Install **Python 3.12+ from python.org**, ticking "Install for all users" and "Add python.exe to PATH".
   The Microsoft Store version can't start at boot.
2. Install **Git for Windows**, then in a terminal:
   ```bash
   git clone https://github.com/khaasaadmi/netrush.git C:\netrush
   ```
3. Copy `C:\netrush\.env.example` to `C:\netrush\.env` and fill it in (staff logins, arena phone and address,
   `ZK_IP`, `ZK_COMM_KEY`, `BACKUP_DIR`). Leave the MSG91 / WhatsApp / Razorpay lines empty for now.
4. Open **PowerShell as Administrator** and run:
   ```bash
   powershell -ExecutionPolicy Bypass -File C:\netrush\deploy\windows\setup.ps1
   ```
   It installs the ZKTeco library, switches off sleep (including with the lid closed), and makes NetRush start
   at boot and restart itself if it ever stops. At the end it should print "NetRush is running".
5. On the laptop open <http://localhost:8000/admin>, log in, and check **Team & health**.

Later: after editing `.env` run `deploy\windows\restart.cmd`; to install a new version run `deploy\windows\update.cmd`.

**Moving to the PC later:** do steps 2–4 on the PC, then stop the laptop's task, copy `C:\netrush\data\netrush.db`
across, start the PC's task, and move the Cloudflare Tunnel (step 3) to the PC.

## 3. Cloudflare Tunnel: makes the laptop reachable as api.netrush.in

The tunnel is an outgoing connection from the laptop to Cloudflare, so no router ports are opened and your
home/arena address stays private.

1. Create a free Cloudflare account and **add the netrush.in domain**. Cloudflare asks you to change the domain's
   nameservers at the company you bought it from. Do that; it takes up to a few hours.
2. Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel** → Cloudflared → name `netrush`.
3. Choose Windows. It shows a command starting `cloudflared.exe service install …`. Install cloudflared as it
   instructs and run that command in an Administrator PowerShell on the laptop (it runs as a Windows service).
4. Add a **public hostname**: subdomain `api`, domain `netrush.in`, service `HTTP` → `localhost:8000`.
5. Check <https://api.netrush.in/healthz> shows `{"ok": true}`.

## 4. Netlify: the pages at netrush.in

1. Netlify → **Add new site → Import from GitHub** → `khaasaadmi/netrush`. Leave build settings empty:
   `netlify.toml` already says "publish the `static` folder, forward `/api` to api.netrush.in".
2. **Domain management → Add a domain** `netrush.in` (and `www`). Because the domain's DNS is now at Cloudflare,
   add the records Netlify shows **in Cloudflare's DNS page**, with the proxy (orange cloud) switched **off** for them.
3. Check <https://netrush.in> loads and <https://netrush.in/healthz> shows `{"ok": true}` (that one goes all the
   way to the laptop).

## 5. SMS through MSG91 (login codes, door codes, booking messages)

Every business SMS in India must use a sender name and message wording registered on the government's
**DLT** system. You register once; MSG91 then sends with those templates.

1. **MSG91 account:** sign up at <https://msg91.com>, complete the business KYC, and buy an SMS pack.
2. **DLT entity:** register NetRush as a business ("Principal Entity") on one operator's DLT portal, for example
   Jio TrueConnect, Airtel or Vi. You need the GST certificate or other business proof. You get an **Entity ID**.
3. **Sender name (header):** register a 6-letter header such as `NETRSH`, type "Transactional / Service".
4. **Templates:** register the messages below as "Service Implicit" (or Transactional for the login code). In DLT
   wording each changing part is written `{#var#}`.
5. **In MSG91:** add the Entity ID and header, then for each approved template create a **Flow**, pasting the same
   text with the variables named exactly as in the table (MSG91 writes them `##name##`). Copy each Flow's **template
   ID** into `.env`.
6. Set `SMS_PROVIDER=msg91` and `MSG91_AUTHKEY` (MSG91 → Authkey), then run `restart.cmd`.

Templates (start with the first four; any type without a flow is sent by WhatsApp instead):

| `.env` setting | Message | Variables |
|---|---|---|
| `MSG91_FLOW_LOGIN` | `##code## is your NetRush login code. Valid 5 minutes. Do not share it.` | code |
| `MSG91_FLOW_DOOR_CODE` | `Hi ##name##, your entry code for ##court## is ##code##. Works ##valid_from##-##valid_to##. <your keypad steps> Enjoy your game!` | name, code, court, valid_from, valid_to |
| `MSG91_FLOW_BOOKING` | `Hi ##name##, booking ##ref## confirmed: ##court##, ##when##. Your door entry code will be sent ##lead## minutes before your slot.` | name, ref, court, when, lead |
| `MSG91_FLOW_RESERVED` | `Hi ##name##, we are holding ##court##, ##when## for you (ref ##ref##, ##amount##). Confirm by ##deadline##: pay online from your booking link or call ##phone##. After that the slot is released.` | name, ref, court, when, deadline, phone, amount |
| `MSG91_FLOW_CANCELLED` | `Hi ##name##, booking ##ref## (##court##, ##when##) is cancelled. ##refund##` | name, ref, court, when, refund |
| `MSG91_FLOW_RELEASED` | `Hi ##name##, your reservation ##ref## (##court##, ##when##) was not confirmed in time and has been released. Book again any time at https://netrush.in/book` | name, ref, court, when |
| `MSG91_FLOW_REFUND` | `##amount## has been refunded for ##what##. It reaches your account in ##days##.` | amount, what, days |
| `MSG91_FLOW_MEMBERSHIP` | `Hi ##name##, your ##plan## membership ##when## (##end_date##). Renew online at https://netrush.in/portal or at the desk to keep playing.` | name, plan, end_date, when |
| `MSG91_FLOW_MEMBERSHIP_OK` | `Hi ##name##, your ##plan## is active until ##end_date##. ##access##` | name, plan, end_date, access |
| `MSG91_FLOW_ACADEMY` | `Hi ##name##, academy fees for ##batch## are paid until ##end_date## and ##when##. Please renew to continue training.` | name, batch, end_date, when |
| `MSG91_FLOW_EVENT` (to staff) | `New event enquiry from ##name## (##phone##) for ##people## people. Message: ##message##. Details in Admin > Events.` | name, phone, people, message |
| `MSG91_FLOW_REFERRAL` | `Hi ##name##, ##friend## just joined NetRush with your referral. ##amount## credit added: you now have ##credit## off your next membership payment. Thank you!` | name, friend, amount, credit |

Keep the wording identical to what DLT approved: operators block messages that don't match. The door-code wording
must include your real keypad steps (the same text as `DOOR_KEYPAD_STEPS`).

## 6. WhatsApp (Meta Cloud API)

Each customer chooses **WhatsApp or SMS** when they book or log in, and gets every message **once, on that one
channel**. If the channel they chose isn't connected (or has no template for that message), the other one is used,
so nobody misses a door code.

1. Create a **Meta Business** account and verify the business (Business settings → Security centre).
2. <https://developers.facebook.com> → Create app → Business → add **WhatsApp**. Add your NetRush phone number
   (one not already used on the WhatsApp app) and verify it.
3. Create a **permanent access token** (System user with the WhatsApp permission). Note the **Phone number ID**.
4. In WhatsApp Manager create templates with the names in `.env.example` (`door_code`, `booking_confirmed`,
   `login_otp`, …). Use category **Utility**, and **Authentication** for `login_otp`. The `{{1}}, {{2}}…`
   placeholders follow the variable order in the table above.
5. Set `WHATSAPP_PROVIDER=meta`, `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, then run `restart.cmd`.

Template approval usually takes minutes to a day. Going to Meta directly (as the system does) avoids the monthly
platform fee that WhatsApp resellers charge.

**Guests can't receive door codes until at least one of WhatsApp or MSG91 is connected.**

## 7. ZKTeco MiniAC Plus: one-time terminal setup (with your installer)

The server keeps the terminal in step automatically: every 30 seconds it adds the door codes whose window is open,
removes finished or cancelled ones, blocks or unblocks members, and reads the entry log.

1. **Access groups** (Menu → Access Control): group **1** with a time zone covering opening hours (who may enter),
   and group **2** with a time zone that never allows entry (lapsed members). These match `ZK_ACTIVE_GROUP=1`
   and `ZK_BLOCKED_GROUP=2`.
2. **Members** enrol their face on the terminal once, at the desk. Note the **user ID** the terminal gives them and
   enter it in Admin → People → Edit → "User ID on the door terminal". From then on their access follows their
   membership automatically. Their face stays enrolled while they're blocked.
3. **Guests' codes:** each code is added as a temporary user whose user ID and password are both the code. Ask the
   installer to confirm **what a guest types at the keypad**, then put those exact words in `DOOR_KEYPAD_STEPS`
   (it is printed in the door-code message). Test it with a real booking before launch.
4. Admin → Team & health shows a "Door terminal (ZKTeco)" row: green when the last sync worked.

The terminal driver uses the standard ZKTeco network protocol (port 4370) and was built and tested against a
simulated terminal. Test it on the real terminal before relying on it: make a booking for 15 minutes ahead, wait
until 10 minutes before, and type the code; then expire a test member and check they're blocked.

## 8. Member and academy logins

Members and academy players never sign themselves up. The desk does it:

1. **Admin → People → + Add person** (or find them), and save their details. If a member referred them, enter that
   member's ID in "Referred by".
2. Sell the plan or enroll them in a batch from the same row.
3. Press **Create login**. A window shows their **member ID** (e.g. NR-0014) and a **temporary password**, once.
   Give both to the member (or press Copy and send them).
4. The member logs in at netrush.in/portal → **Member / academy**. On that first sign-in they must choose their own
   password, and can pick a username to use instead of the member ID.
5. **Forgot password:** the desk presses **Reset password** on their row and gives them a new temporary one.
   Five wrong passwords lock that login for 15 minutes.

Guests don't need any of this: they book with their mobile number, and can log in with a one-time code to see
their bookings.

## 9. Event and party enquiries

The home page has a **"Planning to hold an event?"** section with a form (full name, mobile, number of people, optional
message). Every enquiry appears in **Admin → Events** (reception and admin), with a count of new ones on the sidebar,
one-tap **Call** and **WhatsApp** buttons, a status (new → contacted → booked → closed) and a note for the team.

To also get each enquiry on a staff phone, set `EVENT_NOTIFY_PHONE` to that mobile number and create the WhatsApp
template `event_enquiry` (Utility; variables: name, phone, people, message), then run `restart.cmd`. For SMS instead,
set `EVENT_NOTIFY_CHANNEL=sms` and create the `MSG91_FLOW_EVENT` template above.

- **Photo:** save a landscape photo of the lounge as `static/brand/events.jpg` (about 1600 px wide) and it replaces
  the drawn string lights.
- **Marketing link straight to the form:** `https://netrush.in/#event-enquiry`. Add `?utm_source=<agency>` in front
  of the `#` to credit an agency; each enquiry records where the visitor came from.

## 10. Online payments (Razorpay)

Leave `PAYMENT_PROVIDER=mock` until Razorpay is ready: the site then hides "Pay now" and offers
reserve-and-pay-at-venue only. When ready, set `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
and add a webhook (event `payment.captured`) to `https://netrush.in/api/payments/razorpay-webhook` with its secret in
`RAZORPAY_WEBHOOK_SECRET`. Test keys (`rzp_test_…`) take no money but still mark bookings paid, so use them only
for a quiet test; the health page shows a red warning while they're in.

## 11. Backups

The server saves a copy of the database every day into `BACKUP_DIR` and keeps 30 days. Point `BACKUP_DIR` at a
folder that leaves the laptop, such as a Google Drive or OneDrive folder synced on that machine, or a USB drive.
To restore: stop the task, copy a backup over `data\netrush.db`, start the task.

## Launch-day checklist

1. <https://netrush.in/healthz> shows `{"ok": true}`.
2. Admin → **Team & health**: no red rows (staff passwords set, terminal reachable, backup done).
3. Book a slot for later today on your own phone, choosing WhatsApp; book another choosing SMS. Each message
   arrives once, on the chosen channel.
4. 10 minutes before the slot, the door code arrives. Type it on the terminal: the door opens.
5. Create a login for a test member, log in with the temporary password, and set a new one. Log in as a guest
   with an SMS code and with a WhatsApp code.
6. Turn the laptop's Wi-Fi/cable off for a minute: the pages show "briefly unavailable", and the door still
   opens for a member's face. Reconnect.
