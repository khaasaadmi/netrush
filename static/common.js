const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); } catch (e) { serverDown(); throw new Error("No connection. Please check your internet and try again."); }
  // The pages are on Netlify and the booking server is at the arena: 502/503/504/530 means it can't be reached right now.
  if ([502, 503, 504, 530].includes(r.status)) { serverDown(); const e = new Error("Online booking is briefly unavailable. Please try again in a few minutes."); e.status = r.status; throw e; }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || "Request failed"); e.status = r.status; throw e; }
  return data;
}

function serverDown() {
  if ($(".down-bar")) return;
  const bar = document.createElement("div");
  bar.className = "down-bar";
  bar.innerHTML = `<div class="wrap"><b>Online booking is briefly unavailable.</b> <span>Please try again in a few minutes, or call the arena.</span></div>`;
  const header = $("header.top");
  header ? header.after(bar) : document.body.prepend(bar);
}

// ---- One-time codes, on the channel the customer picks.
// SMS: Firebase sends and checks the code in the browser, then we verify Firebase's token on the server.
// WhatsApp (or SMS when Firebase isn't set up): our server sends and checks the code.
const CHANNEL_LABEL = { whatsapp: "WhatsApp", sms: "SMS" };
const FIREBASE_SDK = "https://www.gstatic.com/firebasejs/10.14.1/";
function loadScript(src) {
  return new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok;
    s.onerror = () => bad(new Error("Couldn't load SMS verification. Please check your connection or choose WhatsApp.")); document.head.append(s); });
}
const FB_ERRORS = { "auth/invalid-verification-code": "Wrong code. Please check and try again.",
  "auth/code-expired": "That code has expired. Please request a new one.",
  "auth/too-many-requests": "Too many attempts from this phone. Please wait a while or choose WhatsApp.",
  "auth/quota-exceeded": "SMS limit reached for now. Please choose WhatsApp.",
  "auth/invalid-phone-number": "That mobile number doesn't look right.",
  "auth/captcha-check-failed": "Verification check failed. Please reload the page and try again.",
  "auth/network-request-failed": "No connection. Please check your internet and try again." };
const Otp = {
  _fb: null, _confirm: null, _verifier: null, token: null,
  async _auth(cfg) {
    if (!this._fb) this._fb = (async () => {
      await loadScript(FIREBASE_SDK + "firebase-app-compat.js");
      await loadScript(FIREBASE_SDK + "firebase-auth-compat.js");
      firebase.initializeApp(cfg.firebase);
      const auth = firebase.auth(); auth.useDeviceLanguage(); return auth;
    })();
    return this._fb;
  },
  // Returns {demo_code?, new?}. Throws with a readable message.
  async send(cfg, phone, channel) {
    this._confirm = null; this.token = null;
    if (channel === "sms" && cfg.firebase && !cfg.demo) {
      const auth = await this._auth(cfg);
      let box = $("#recaptcha-box");
      if (!box) { box = document.createElement("div"); box.id = "recaptcha-box"; document.body.append(box); }
      if (!this._verifier) this._verifier = new firebase.auth.RecaptchaVerifier("recaptcha-box", { size: "invisible" });
      try { this._confirm = await auth.signInWithPhoneNumber("+91" + phone.replace(/\D/g, "").slice(-10), this._verifier); }
      catch (e) { try { this._verifier.clear(); } catch (x) {} this._verifier = null; throw new Error(FB_ERRORS[e.code] || e.message); }
      return {};
    }
    return api("/api/auth/request-code", { phone, channel });
  },
  // Returns {ok:true} when signed in, or {need_name:true} for a new number (call finish() with the name).
  async verify(phone, code, extra) {
    if (this._confirm) {
      let cred;
      try { cred = await this._confirm.confirm(String(code).trim()); } catch (e) { throw new Error(FB_ERRORS[e.code] || e.message); }
      this.token = await cred.user.getIdToken();
      return api("/api/auth/firebase", { id_token: this.token, ...extra });
    }
    return api("/api/auth/verify", { phone, code, ...extra });
  },
  finish(extra) { return api("/api/auth/firebase", { id_token: this.token, ...extra }); },
};

// A "WhatsApp | SMS" chooser. Returns the chosen channel via el.dataset.value.
function channelPicker(el, channels, chosen) {
  el.dataset.value = channels.includes(chosen) ? chosen : channels[0] || "";
  if (channels.length < 2) {
    el.innerHTML = channels.length ? `<span class="small muted">We'll send it by <b>${CHANNEL_LABEL[channels[0]]}</b>.</span>` : "";
    return;
  }
  el.innerHTML = `<div class="methods" role="group">${channels.map(c =>
    `<button type="button" data-c="${c}" aria-pressed="${c === el.dataset.value}">${CHANNEL_LABEL[c]}</button>`).join("")}</div>`;
  $$("button", el).forEach(b => b.onclick = () => {
    el.dataset.value = b.dataset.c; $$("button", el).forEach(x => x.setAttribute("aria-pressed", x === b));
    el.dispatchEvent(new Event("change"));
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let toastTimer;
function toast(msg, err) {
  let t = $(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
  t.textContent = msg; t.classList.toggle("err", !!err); t.classList.remove("hidden");
  t.style.animation = "none"; t.offsetHeight; t.style.animation = "";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

const rupees = n => "₹" + Number(n || 0).toLocaleString("en-IN");
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function niceDate(iso) {   // "2026-09-28" → "Mon 28 Sep"
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return `${DAYS[dt.getDay()]} ${d} ${MONTHS[m - 1]}`;
}
const hhmm = s => s.slice(11, 16);
const SPORT_LABEL = { badminton: "Badminton", padel: "Padel", pickleball: "Pickleball" };

// Simple line icons, drawn in currentColor.
const SPORT_ICON = {
  badminton: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M9 23l-3 3"/><circle cx="10.5" cy="21.5" r="2.5"/><path d="M12.3 19.7L20 6l6 6-13.7 7.7"/><path d="M16 11l5 5M18 8.5l5.5 5.5"/></svg>`,
  padel: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <ellipse cx="18" cy="12" rx="8" ry="9" transform="rotate(35 18 12)"/><path d="M12.5 19.5L6 26"/>
    <circle cx="16" cy="10" r=".8" fill="currentColor"/><circle cx="19.5" cy="12.5" r=".8" fill="currentColor"/><circle cx="17" cy="14.5" r=".8" fill="currentColor"/>
    <circle cx="26" cy="25" r="2.5"/></svg>`,
  pickleball: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="16" cy="16" r="10"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="11" r="1.3" fill="currentColor"/>
    <circle cx="20" cy="18.5" r="1.3" fill="currentColor"/><circle cx="13" cy="19.5" r="1.3" fill="currentColor"/><circle cx="16" cy="15.5" r="1.3" fill="currentColor"/></svg>`,
};
const ICON = {
  party: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20l5-14 9 9-14 5z"/><path d="M14 4l1 2M19 9l2 1M17 3l-1 3M21 7l-3 1"/></svg>`,
  today: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>`,
  people: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><path d="M16 4.5a3.5 3.5 0 010 7M18 14.8c1.8.7 3 2.5 3.5 5.2"/></svg>`,
  chart: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>`,
  pulse: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h4l3-7 5 14 3-7h5"/></svg>`,
  door: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 21V4a1 1 0 011-1h12a1 1 0 011 1v17M3 21h18"/><circle cx="15" cy="12" r="1" fill="currentColor"/></svg>`,
  chat: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z"/></svg>`,
};

// Demo mode: a lime strip under the header linking the demo phone and demo logins.
function demoBar(cfg) {
  if (!cfg || !cfg.demo || $(".demo-bar")) return;
  const bar = document.createElement("div");
  bar.className = "demo-bar";
  bar.innerHTML = `<div class="wrap"><b>DEMO</b><span>No real SMS or payments. Everything else is live.</span>
    <span class="spacer"></span><a href="/demo" target="_blank">📱 Demo phone</a>
    ${cfg.lock ? `<a href="/door" target="_blank">🚪 Door keypad</a>` : ""}<a href="/demo#logins" target="_blank">🔑 Demo logins</a></div>`;
  const header = $("header.top");
  header ? header.after(bar) : document.body.prepend(bar);
}

// ---- Where did this visitor come from? (first-party, no personal data)
// A tagged link (?utm_source=agency&utm_campaign=x) or an outside referrer sets the source; it is kept for
// 30 days so a booking made later is still credited. One visit is reported per browser session.
const store = { get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };
function rememberCampaign() {
  const q = new URLSearchParams(location.search);
  let ref = "";
  try { const h = new URL(document.referrer).hostname; if (h && h !== location.hostname) ref = h; } catch (e) {}
  const source = q.get("utm_source") || q.get("src") || "";
  let src = store.get("nr_src");
  if (src && Date.now() - src.ts > 30 * 864e5) src = null;
  if (source || ref || !src)
    src = (source || ref) ? { source, medium: q.get("utm_medium") || "", campaign: q.get("utm_campaign") || "", referrer: ref, ts: Date.now() }
      : { source: "", medium: "", campaign: "", referrer: "", ts: Date.now() };
  store.set("nr_src", src);
  let vid = store.get("nr_vid");
  if (!vid) { vid = Math.random().toString(36).slice(2) + Date.now().toString(36); store.set("nr_vid", vid); }
  try {
    const key = (src.source || src.referrer || "direct") + "/" + src.campaign;   // once per session per source
    if (sessionStorage.getItem("nr_seen") !== key) {
      sessionStorage.setItem("nr_seen", key);
      fetch("/api/track", { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify({ vid, source: src.source, medium: src.medium, campaign: src.campaign, referrer: src.referrer, path: location.pathname }) });
    }
  } catch (e) {}
}
const campaign = () => store.get("nr_src") || {};

// ---- light / dark: follows the device until the visitor picks one, then remembers it on this device
const SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`;
const MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/></svg>`;
function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function themeButton() {
  const b = document.createElement("button");
  b.type = "button"; b.className = "theme-btn";
  const paint = () => { const dark = currentTheme() === "dark";
    b.innerHTML = dark ? SUN : MOON; b.title = b.ariaLabel = dark ? "Switch to light mode" : "Switch to dark mode"; };
  b.onclick = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("nr_theme", next); } catch (e) {}
    $$(".theme-btn").forEach(x => x.dispatchEvent(new Event("repaint")));
  };
  b.addEventListener("repaint", paint);
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", paint);
  paint();
  return b;
}
document.addEventListener("DOMContentLoaded", () => {
  const nav = $("header.top nav");
  if (nav) nav.prepend(themeButton());
  const foot = $(".side .foot");
  if (foot) { const b = themeButton(); b.style.marginBottom = "8px"; foot.prepend(b); }
  const login = $("#login .card .logo");   // admin login screen
  if (login && !nav && !foot) { const b = themeButton(); b.style.cssText = "position:fixed;top:14px;right:14px"; document.body.append(b); }
});
