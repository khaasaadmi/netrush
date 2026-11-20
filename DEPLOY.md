# Running NetRush: laptop at the arena + Netlify + Firebase

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
- **Firebase** sends SMS login codes. Firebase keeps a record of the phone numbers that verify by SMS.
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
   `ZK_IP`, `ZK_COMM_KEY`, `BACKUP_DIR`). Leave the Firebase / WhatsApp / Razorpay lines empty for now.
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

## 5. Firebase: SMS login codes

1. <https://console.firebase.google.com> → **Add project** `netrush`.
2. **Build → Authentication → Get started → Sign-in method → Phone → Enable.**
3. **Authentication → Settings → Authorized domains → Add** `netrush.in`.
4. **Project settings → General → Your apps → Add app → Web.** Copy `apiKey`, `projectId` and `appId` into
   `.env` as `FIREBASE_API_KEY`, `FIREBASE_PROJECT_ID`, `FIREBASE_APP_ID`, then run `restart.cmd`.
5. Firebase sends real SMS only on the pay-as-you-go **Blaze** plan (billed per SMS). Upgrade under ⚙ → Usage and billing.
   Set a budget alert there.
6. Test: on <https://netrush.in/portal> choose SMS, enter your number, and log in with the code.

**What Firebase does and doesn't do:** it sends and checks *login/verification* codes only. It cannot send our own
messages, so it can't deliver guests' door codes or booking confirmations. Those go by WhatsApp or SMS (step 6).

## 6. WhatsApp and SMS for door codes and booking messages

Each customer chooses **WhatsApp or SMS** when they book or log in, and gets every message **once, on that one
channel**. If their chosen channel isn't connected, the other one is used so nobody misses a door code.

- **WhatsApp** (usually the cheaper choice in India): a WhatsApp Business number on Meta's Cloud API, with approved
  message templates named as in `.env.example`. Set `WHATSAPP_PROVIDER=meta`, `WA_TOKEN`, `WA_PHONE_NUMBER_ID`.
  WhatsApp login codes need an "authentication" category template.
- **SMS**: MSG91 (or Twilio) with DLT-registered templates, one per message type. Set `SMS_PROVIDER=msg91` and
  the flow IDs.

Template approvals take a few days. Until a channel is connected, customers only see their confirmation on screen.
**Guests can't receive door codes until at least one channel is connected.**

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

## 8. Online payments (Razorpay)

Leave `PAYMENT_PROVIDER=mock` until Razorpay is ready: the site then hides "Pay now" and offers
reserve-and-pay-at-venue only. When ready, set `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
and add a webhook (event `payment.captured`) to `https://netrush.in/api/payments/razorpay-webhook` with its secret in
`RAZORPAY_WEBHOOK_SECRET`. Test keys (`rzp_test_…`) take no money but still mark bookings paid, so use them only
for a quiet test; the health page shows a red warning while they're in.

## 9. Backups

The server saves a copy of the database every day into `BACKUP_DIR` and keeps 30 days. Point `BACKUP_DIR` at a
folder that leaves the laptop, such as a Google Drive or OneDrive folder synced on that machine, or a USB drive.
To restore: stop the task, copy a backup over `data\netrush.db`, start the task.

## Launch-day checklist

1. <https://netrush.in/healthz> shows `{"ok": true}`.
2. Admin → **Team & health**: no red rows (staff passwords set, terminal reachable, backup done).
3. Book a slot for later today on your own phone, choosing WhatsApp; book another choosing SMS. Each message
   arrives once, on the chosen channel.
4. 10 minutes before the slot, the door code arrives. Type it on the terminal: the door opens.
5. Log in to the portal with an SMS code (Firebase) and with a WhatsApp code.
6. Turn the laptop's Wi-Fi/cable off for a minute: the pages show "briefly unavailable", and the door still
   opens for a member's face. Reconnect.
