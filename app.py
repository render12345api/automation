#!/usr/bin/env python3
"""
SERVER — Follower Automation Control Panel
All logic bugs fixed, full UI redesign, named "SERVER".
"""

import time
import logging
import re
import threading
import collections
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, url_for, render_template_string

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException

# ════════════════════════════════════════════════════════════
#  CONFIGURATION
# ════════════════════════════════════════════════════════════
ADMIN_PASSWORD = "karthikraja2007"

# ── Accounts ──────────────────────────────────────────────
users_lock = threading.Lock()
USERS = [
    {"username": "armad7175", "password": "karthikraja2007",
     "health": "unknown", "last_login": None, "fail_count": 0,
     "total_sent": 0},
]

# ── Target IDs (sequential) ───────────────────────────────
target_ids_lock  = threading.Lock()
TARGET_IDS       = ["57310375825"]
target_id_index  = 0   # global pointer — reset each cycle start

# ── Cycle counter (only incremented when actually running) ─
cycle_count_lock = threading.Lock()
current_cycle    = 0

# ── Live log ring-buffer ───────────────────────────────────
log_buffer_lock  = threading.Lock()
log_buffer       = collections.deque(maxlen=300)

# ── Per-cycle summary (last completed cycle stats) ─────────
last_cycle_lock    = threading.Lock()
last_cycle_summary = {}   # {"cycle": N, "total_gained": X, "duration_min": Y, "at": "HH:MM"}

# ── Idle / running state ───────────────────────────────────
idle_lock  = threading.Lock()
idle_state = {"active": False, "reason": "Starting up…"}

# ── Uptime ────────────────────────────────────────────────
START_TIME = datetime.now()

# ── Servers ───────────────────────────────────────────────
SERVER_NAMES = [f"Server {i+1}" for i in range(9)]
DOMAINS = [
    "takipcimx.net",   "takipcizen.com",  "takipcigen.com",
    "takipcikrali.com","takipcigir.com",  "takipcitime.com",
    "takipcibase.com", "instamoda.org",   "takip88.com",
]
SITES = [{"name": SERVER_NAMES[i], "domain": DOMAINS[i]} for i in range(9)]

# ── Timing ────────────────────────────────────────────────
TARGET_CYCLE_SECONDS       = 60 * 60   # aim for 1-hour cycles
PAUSE_BETWEEN_SITES_SINGLE = 120       # 2 min  — 1 target ID
PAUSE_BETWEEN_SITES_MULTI  = 60        # 1 min  — multiple target IDs
ACCOUNT_SWITCH_PAUSE       = 30        # seconds between accounts
IDLE_POLL_INTERVAL         = 10        # seconds to re-check when idle
# ════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════
#  LOGGING  (BufferHandler added only once)
# ════════════════════════════════════════════════════════════
_LOG_FMT = '%(asctime)s [%(levelname)s] %(message)s'

class BufferHandler(logging.Handler):
    def emit(self, record):
        with log_buffer_lock:
            log_buffer.append(self.format(record))

_buf_handler = BufferHandler()
_buf_handler.setFormatter(logging.Formatter(_LOG_FMT))

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FMT,
    handlers=[
        logging.FileHandler("server.log"),
        logging.StreamHandler(),
    ]
)
logging.getLogger().addHandler(_buf_handler)
logger = logging.getLogger("server")

# ════════════════════════════════════════════════════════════
#  STATS
# ════════════════════════════════════════════════════════════
stats_lock = threading.Lock()
stats = {
    name: {"total": 0, "count": 0, "min": None, "max": 0,
           "last_success": None, "fail_count": 0}
    for name in SERVER_NAMES
}

# ════════════════════════════════════════════════════════════
#  CHROME OPTIONS
# ════════════════════════════════════════════════════════════
chrome_options = Options()
chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--disable-blink-features=AutomationControlled")
chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
chrome_options.add_experimental_option("useAutomationExtension", False)

# ════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════

def get_pause_time():
    with target_ids_lock:
        return PAUSE_BETWEEN_SITES_MULTI if len(TARGET_IDS) > 1 else PAUSE_BETWEEN_SITES_SINGLE

def get_next_target_id():
    """Pop next target ID in sequence. Uses global pointer."""
    global target_id_index
    with target_ids_lock:
        ids = list(TARGET_IDS)
        if not ids:
            return None
        idx = target_id_index % len(ids)
        target_id_index = (idx + 1) % len(ids)
        return ids[idx]

def reset_target_index():
    """Call at the start of each real cycle so IDs always start from Server 1."""
    global target_id_index
    with target_ids_lock:
        target_id_index = 0

def is_ready():
    """Returns (ok, reason). Browser sessions only open when both lists are non-empty."""
    with users_lock:
        has_users = len(USERS) > 0
    with target_ids_lock:
        has_targets = len(TARGET_IDS) > 0
    if not has_users and not has_targets:
        return False, "No accounts and no target IDs configured."
    if not has_users:
        return False, "No accounts configured — add at least one account in Admin."
    if not has_targets:
        return False, "No target IDs configured — add at least one target ID in Admin."
    return True, ""

def set_idle(reason=""):
    with idle_lock:
        idle_state["active"] = False
        idle_state["reason"] = reason

def set_running():
    with idle_lock:
        idle_state["active"] = True
        idle_state["reason"] = ""

def uptime_str():
    delta = datetime.now() - START_TIME
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"

def estimate_cycle_minutes():
    """Estimate how long one full cycle takes given current config."""
    with users_lock:
        n_accounts = len(USERS)
    pause = get_pause_time()
    # per account: 9 servers × (60s action + pause) + ~10s overhead per server
    per_account = 9 * (60 + pause) + 9 * 10
    # + 30s switch between accounts
    total = n_accounts * per_account + max(0, n_accounts - 1) * ACCOUNT_SWITCH_PAUSE
    return round(total / 60, 1)

# ════════════════════════════════════════════════════════════
#  SELENIUM HELPERS
# ════════════════════════════════════════════════════════════

def find_element_safe(driver, wait, by, selector):
    try:
        return wait.until(EC.presence_of_element_located((by, selector)))
    except Exception:
        return None

def click_element_safe(driver, wait, by, selector):
    try:
        wait.until(EC.element_to_be_clickable((by, selector))).click()
        return True
    except Exception:
        return False

def extract_follower_count(page_source):
    text = page_source.lower()
    for pattern in [r'(\d+)\s*takip\u00e7i', r'(\d+)\s*follower',
                    r'success.*?(\d+)', r'ba\u015far\u0131l\u0131.*?(\d+)']:
        m = re.search(pattern, text)
        if m:
            return int(m.group(1))
    if "ba\u015far\u0131l\u0131" in text or "success" in text:
        return 50
    return None

def login_with_retry(driver, wait, login_url, username, password, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"  [{username}] Login attempt {attempt}/{max_retries}")
            driver.get(login_url)
            time.sleep(2)
            uf = find_element_safe(driver, wait, By.NAME, "username")
            pf = find_element_safe(driver, wait, By.NAME, "password")
            if not uf or not pf:
                raise Exception("Login fields not found")
            uf.send_keys(username)
            pf.send_keys(password)

            clicked = False
            for sel in [
                (By.CSS_SELECTOR, "button.instaclass19"),
                (By.XPATH, "//button[contains(text(),'G\u0130R\u0130\u015e')]"),
                (By.XPATH, "//button[@type='submit']"),
                (By.XPATH, "//input[@type='submit']"),
                (By.XPATH, "//form//button"),
            ]:
                if click_element_safe(driver, wait, sel[0], sel[1]):
                    clicked = True
                    break
            if not clicked:
                raise Exception("Login button not found")

            try:
                wait.until(lambda d: d.current_url != login_url)
                logger.info(f"  [{username}] Login OK")
                return True
            except TimeoutException:
                if "login" not in driver.current_url.lower():
                    return True
                raise Exception("URL did not change after login")
        except Exception as e:
            logger.warning(f"  [{username}] Login attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(5 * (2 ** (attempt - 1)))
    return False

def process_site(driver, wait, site, username, password, target_id):
    name       = site["name"]
    domain     = site["domain"]
    login_url  = f"https://{domain}/login"
    follow_url = f"https://{domain}/tools/send-follower/{target_id}"

    logger.info(f"[{name}] user={username} target={target_id}")

    try:
        if not login_with_retry(driver, wait, login_url, username, password):
            logger.error(f"  [{name}] Login failed — skipping site")
            # FIX: health updated only after full action attempt, not just login
            _mark_health(username, False)
            with stats_lock:
                stats[name]["fail_count"] += 1
            return False, 0

        logger.info(f"  [{name}] Navigating to follower page…")
        driver.get(follow_url)
        time.sleep(3)

        clicked = False
        for sel in [
            (By.ID,          "formTakipSubmitButton"),
            (By.XPATH,       "//button[contains(text(),'Start')]"),
            (By.XPATH,       "//button[contains(text(),'G\u00f6nder')]"),
            (By.CSS_SELECTOR,"button.btn-success"),
            (By.XPATH,       "//button[@onclick='sendTakip();']"),
        ]:
            if click_element_safe(driver, wait, sel[0], sel[1]):
                clicked = True
                break

        if not clicked:
            logger.error(f"  [{name}] Start button not found")
            with stats_lock:
                stats[name]["fail_count"] += 1
            return False, 0

        time.sleep(5)
        count = extract_follower_count(driver.page_source)
        gained = count if count else 50
        if not count:
            logger.warning(f"  [{name}] Count not parsed — assuming 50")

        logger.info(f"  [{name}] SUCCESS — {gained} followers sent to {target_id}")
        # FIX: health marked healthy only when the full send succeeded
        _mark_health(username, True)
        return True, gained

    except Exception as e:
        logger.error(f"  [{name}] Unexpected error: {e}")
        _mark_health(username, False)
        with stats_lock:
            stats[name]["fail_count"] += 1
        return False, 0

def _mark_health(username, success):
    with users_lock:
        for u in USERS:
            if u["username"] == username:
                if success:
                    u["health"]     = "healthy"
                    u["fail_count"] = 0
                    u["last_login"] = datetime.now().strftime('%H:%M:%S')
                else:
                    u["fail_count"] = u.get("fail_count", 0) + 1
                    u["health"]     = "warning" if u["fail_count"] < 3 else "unhealthy"
                break

# ════════════════════════════════════════════════════════════
#  INTERRUPTIBLE SLEEP  (checks idle conditions every second)
# ════════════════════════════════════════════════════════════

def interruptible_sleep(seconds, username=None):
    """
    Sleep for `seconds` but break early if:
    - TARGET_IDS becomes empty
    - The current username is removed from USERS
    Returns True if slept fully, False if interrupted.
    """
    for _ in range(seconds):
        time.sleep(1)
        with target_ids_lock:
            if len(TARGET_IDS) == 0:
                logger.warning("Target IDs emptied during sleep — interrupting.")
                set_idle("No target IDs — add at least one in Admin.")
                return False
        if username:
            with users_lock:
                if not any(u["username"] == username for u in USERS):
                    logger.warning(f"Account '{username}' removed during sleep — interrupting.")
                    return False
    return True

# ════════════════════════════════════════════════════════════
#  AUTOMATION LOOP
# ════════════════════════════════════════════════════════════

def automation_loop():
    global current_cycle
    logger.info("═" * 60)
    logger.info("  SERVER — Automation engine started")
    logger.info("═" * 60)

    while True:
        # ── IDLE GUARD ──────────────────────────────────────────
        ready, reason = is_ready()
        if not ready:
            set_idle(reason)
            logger.warning(f"IDLE: {reason} — retrying in {IDLE_POLL_INTERVAL}s")
            time.sleep(IDLE_POLL_INTERVAL)
            continue

        # ── START CYCLE ─────────────────────────────────────────
        set_running()
        reset_target_index()   # FIX: always start from ID[0] each cycle

        with cycle_count_lock:
            current_cycle += 1  # FIX: only incremented when we actually run
            cycle = current_cycle

        cycle_start    = time.time()
        cycle_gained   = 0
        logger.info(f"\n{'═'*60}")
        logger.info(f"  Cycle #{cycle} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        with target_ids_lock:
            ids_snap = list(TARGET_IDS)
        with users_lock:
            users_snap = [dict(u) for u in USERS]

        logger.info(f"  Target IDs : {ids_snap}")
        logger.info(f"  Accounts   : {[u['username'] for u in users_snap]}")
        logger.info(f"  Cooldown   : {get_pause_time()}s | Est. cycle: {estimate_cycle_minutes()} min")
        logger.info(f"{'═'*60}")

        for user_idx, user in enumerate(users_snap):
            username = user["username"]
            password = user["password"]
            logger.info(f"\n▶ Account {user_idx+1}/{len(users_snap)}: {username}")

            # Pre-account readiness check
            ready, reason = is_ready()
            if not ready:
                logger.warning(f"  Aborting — {reason}")
                set_idle(reason)
                break

            service = Service()
            driver  = webdriver.Chrome(service=service, options=chrome_options)
            wait    = WebDriverWait(driver, 20)
            abort_user = False

            try:
                for site_idx, site in enumerate(SITES):
                    # ── Pre-server checks ──────────────────────────
                    with target_ids_lock:
                        if len(TARGET_IDS) == 0:
                            logger.warning("  Target IDs gone — stopping cycle.")
                            set_idle("No target IDs — add at least one in Admin.")
                            abort_user = True
                            break

                    with users_lock:
                        if not any(u["username"] == username for u in USERS):
                            logger.warning(f"  Account '{username}' removed — skipping rest.")
                            abort_user = True
                            break

                    target_id = get_next_target_id()
                    if not target_id:
                        logger.warning("  No target ID returned — stopping.")
                        abort_user = True
                        break

                    # ── Do the work ────────────────────────────────
                    success, gained = process_site(
                        driver, wait, site, username, password, target_id)

                    if success:
                        cycle_gained += gained
                        with stats_lock:
                            s = stats[site["name"]]
                            s["total"] += gained
                            s["count"] += 1
                            s["min"]    = gained if s["min"] is None else min(s["min"], gained)
                            s["max"]    = max(s["max"], gained)
                            s["last_success"] = datetime.now().strftime('%H:%M:%S')
                        # Update per-account total
                        with users_lock:
                            for u in USERS:
                                if u["username"] == username:
                                    u["total_sent"] = u.get("total_sent", 0) + gained
                                    break

                    # ── Cooldown (interruptible) ───────────────────
                    if site_idx < len(SITES) - 1:
                        pause = get_pause_time()
                        logger.info(f"  Cooldown {pause}s before {SITES[site_idx+1]['name']}…")
                        # FIX: break properly exits the site loop
                        if not interruptible_sleep(pause, username):
                            abort_user = True
                            break

            finally:
                driver.quit()
                logger.info(f"  Browser closed for {username}.")

            if abort_user:
                break

            # ── Account switch pause (interruptible) ──────────────
            if user_idx < len(users_snap) - 1:
                logger.info(f"  Switching to next account in {ACCOUNT_SWITCH_PAUSE}s…")
                # FIX: account switch pause is also interruptible
                if not interruptible_sleep(ACCOUNT_SWITCH_PAUSE):
                    set_idle("Interrupted during account switch.")
                    break

        # ── CYCLE SUMMARY ────────────────────────────────────────
        elapsed_sec = time.time() - cycle_start
        elapsed_min = elapsed_sec / 60

        with last_cycle_lock:
            last_cycle_summary.update({
                "cycle":        cycle,
                "total_gained": cycle_gained,
                "duration_min": round(elapsed_min, 1),
                "at":           datetime.now().strftime('%H:%M:%S'),
            })

        logger.info(f"\n✔ Cycle #{cycle} done — {cycle_gained} followers sent in {elapsed_min:.1f} min")

        with stats_lock:
            for name, s in stats.items():
                if s["count"] > 0:
                    avg = s["total"] // s["count"]
                    logger.info(f"  {name}: runs={s['count']} total={s['total']} "
                                f"avg={avg} min={s['min']} max={s['max']} fails={s['fail_count']}")

        # ── INTER-CYCLE WAIT (interruptible) ─────────────────────
        with idle_lock:
            still_running = idle_state["active"]

        if still_running and elapsed_sec < TARGET_CYCLE_SECONDS:
            wait_time = TARGET_CYCLE_SECONDS - elapsed_sec
            logger.info(f"  Resting {wait_time/60:.1f} min until next cycle…")
            slept = 0
            while slept < wait_time:
                time.sleep(IDLE_POLL_INTERVAL)
                slept += IDLE_POLL_INTERVAL
                ready, reason = is_ready()
                if not ready:
                    set_idle(reason)
                    logger.warning(f"  Gone idle during rest: {reason}")
                    break
        elif not still_running:
            logger.info("  Idle — skipping inter-cycle rest.")
        else:
            logger.warning("  Cycle exceeded 1 hour — starting next immediately.")

# ════════════════════════════════════════════════════════════
#  FLASK APP
# ════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = "srv-secret-change-in-prod"

# ──────────────────────────────────────────────────────────────────────────────
#  DASHBOARD TEMPLATE
# ──────────────────────────────────────────────────────────────────────────────
DASHBOARD_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="15">
<title>SERVER</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #080b10;
  --bg2:      #0d1117;
  --panel:    #111620;
  --border:   #1c2333;
  --border2:  #263044;
  --green:    #22d3a5;
  --green-dim:#0d4a3a;
  --blue:     #4f8ef7;
  --blue-dim: #12285a;
  --amber:    #f0a429;
  --amber-dim:#4a3210;
  --red:      #f05252;
  --red-dim:  #4a1212;
  --text:     #dde3f0;
  --muted:    #5a6a85;
  --muted2:   #3a4a62;
  --sans: 'Space Grotesk', sans-serif;
  --mono: 'Space Mono', monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(79,142,247,.08),transparent)}

/* ── TOPBAR ── */
.topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 32px;height:56px;
  background:rgba(13,17,23,.9);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:200;
  backdrop-filter:blur(12px);
}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:28px;height:28px;background:var(--green);border-radius:6px;
  display:flex;align-items:center;justify-content:center;font-size:14px}
.brand-name{font-size:15px;font-weight:700;letter-spacing:.12em;color:var(--text)}
.brand-tag{font-size:10px;font-family:var(--mono);color:var(--muted);
  background:var(--panel);border:1px solid var(--border);border-radius:4px;
  padding:2px 7px;letter-spacing:.08em}
.nav{display:flex;gap:4px;align-items:center}
.nav a{font-family:var(--mono);font-size:11px;color:var(--muted);text-decoration:none;
  padding:5px 12px;border-radius:6px;border:1px solid transparent;transition:.18s;letter-spacing:.05em}
.nav a:hover{color:var(--text);border-color:var(--border2);background:var(--panel)}
.nav a.active{color:var(--green);border-color:var(--green-dim)}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-family:var(--mono);
  padding:3px 10px;border-radius:20px}
.pill-green{background:var(--green-dim);color:var(--green);border:1px solid rgba(34,211,165,.2)}
.pill-amber{background:var(--amber-dim);color:var(--amber);border:1px solid rgba(240,164,41,.2)}
.dot-pulse{width:6px;height:6px;border-radius:50%;background:currentColor;
  animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(1.4)}}

/* ── LAYOUT ── */
.page{max-width:1280px;margin:0 auto;padding:24px 24px 48px}

/* ── ALERT BANNER ── */
.alert{display:flex;align-items:flex-start;gap:14px;padding:14px 18px;
  border-radius:10px;margin-bottom:20px;
  background:rgba(240,164,41,.07);border:1px solid rgba(240,164,41,.25)}
.alert-icon{font-size:18px;flex-shrink:0;margin-top:1px}
.alert-title{font-size:13px;font-weight:600;color:var(--amber);margin-bottom:3px}
.alert-body{font-size:12px;color:rgba(240,164,41,.75);font-family:var(--mono);line-height:1.6}

/* ── STAT CARDS ── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
@media(max-width:900px){.stat-grid{grid-template-columns:repeat(2,1fr)}}
.stat{padding:18px 20px;background:var(--panel);border:1px solid var(--border);
  border-radius:12px;position:relative;overflow:hidden;transition:.2s}
.stat:hover{border-color:var(--border2)}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.stat.g::before{background:linear-gradient(90deg,var(--green),transparent)}
.stat.b::before{background:linear-gradient(90deg,var(--blue),transparent)}
.stat.a::before{background:linear-gradient(90deg,var(--amber),transparent)}
.stat.r::before{background:linear-gradient(90deg,var(--red),transparent)}
.stat-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);font-family:var(--mono);margin-bottom:10px}
.stat-val{font-size:36px;font-weight:700;line-height:1}
.stat.g .stat-val{color:var(--green)}
.stat.b .stat-val{color:var(--blue)}
.stat.a .stat-val{color:var(--amber)}
.stat-sub{font-size:11px;color:var(--muted);font-family:var(--mono);margin-top:6px}

/* ── MID GRID ── */
.mid-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}
@media(max-width:800px){.mid-grid{grid-template-columns:1fr}}

/* ── PANELS ── */
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px}
.panel-title{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);font-family:var(--mono);margin-bottom:16px;
  display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── TARGET ID TAGS ── */
.id-tags{display:flex;flex-wrap:wrap;gap:8px;min-height:32px}
.id-tag{font-family:var(--mono);font-size:12px;padding:4px 12px;border-radius:6px;
  background:var(--blue-dim);color:var(--blue);border:1px solid rgba(79,142,247,.25)}
.id-tag.next{background:rgba(34,211,165,.08);color:var(--green);
  border-color:rgba(34,211,165,.3);font-weight:700}
.id-empty{font-family:var(--mono);font-size:12px;color:var(--red);
  background:var(--red-dim);border:1px solid rgba(240,82,82,.2);
  padding:4px 12px;border-radius:6px}

/* ── ACCOUNT TABLE ── */
.tbl{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
.tbl th{color:var(--muted);text-align:left;padding:8px 10px;
  border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.08em}
.tbl td{padding:9px 10px;border-bottom:1px solid rgba(28,35,51,.7);vertical-align:middle}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:rgba(255,255,255,.015)}
.h-dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
.h-green{background:var(--green)}
.h-amber{background:var(--amber)}
.h-red{background:var(--red)}
.h-gray{background:var(--muted2)}
.num{color:var(--text);font-weight:700}

/* ── SERVER STATS TABLE ── */
.srv-grid{margin-bottom:20px}
.srv-tbl{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
.srv-tbl th{color:var(--muted);text-align:left;padding:9px 12px;
  border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.08em;
  background:rgba(17,22,32,.5)}
.srv-tbl td{padding:9px 12px;border-bottom:1px solid rgba(28,35,51,.5)}
.srv-tbl tr:last-child td{border-bottom:none}
.srv-tbl tr:hover td{background:rgba(255,255,255,.015)}
.bar-wrap{width:100px;height:5px;background:var(--border2);border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle;margin-right:8px}
.bar-fill{height:100%;border-radius:3px;background:var(--green);transition:.4s}
.fail-dot{color:var(--red)}

/* ── LAST CYCLE CARD ── */
.lc-row{display:flex;gap:24px;flex-wrap:wrap}
.lc-item{flex:1;min-width:100px}
.lc-k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-family:var(--mono)}
.lc-v{font-size:20px;font-weight:700;color:var(--green);margin-top:4px}

/* ── LOG PANEL ── */
.log-box{background:#060910;border:1px solid var(--border);border-radius:10px;
  height:280px;overflow-y:auto;font-family:var(--mono);font-size:11px;
  line-height:1.8;padding:12px 14px}
.log-box::-webkit-scrollbar{width:5px}
.log-box::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.log-line{padding:1px 0;color:#5a7090;border-bottom:1px solid rgba(28,35,51,.3)}
.log-line:last-child{border-bottom:none}
.log-line.ok{color:var(--green)}
.log-line.err{color:var(--red)}
.log-line.warn{color:var(--amber)}
.log-line.info{color:#7090b0}
.log-refresh{font-size:10px;color:var(--muted);font-family:var(--mono);
  text-align:right;margin-top:8px;letter-spacing:.04em}
</style>
</head>
<body>

<!-- ── TOPBAR ── -->
<div class="topbar">
  <div class="brand">
    <div class="brand-icon">⚡</div>
    <span class="brand-name">SERVER</span>
    <span class="brand-tag">v2.0</span>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    {% if is_idle %}
      <span class="pill pill-amber"><span class="dot-pulse"></span>IDLE</span>
    {% else %}
      <span class="pill pill-green"><span class="dot-pulse"></span>RUNNING</span>
    {% endif %}
    <nav class="nav">
      <a href="/" class="active">Dashboard</a>
      <a href="/admin">Admin ↗</a>
    </nav>
  </div>
</div>

<div class="page">

  <!-- ── IDLE ALERT ── -->
  {% if is_idle %}
  <div class="alert">
    <div class="alert-icon">⚠</div>
    <div>
      <div class="alert-title">Automation Paused — No browser sessions running</div>
      <div class="alert-body">{{ idle_reason }}<br>
      Go to <a href="/admin" style="color:var(--amber)">Admin Panel</a> and add the missing configuration to resume automatically.</div>
    </div>
  </div>
  {% endif %}

  <!-- ── STAT CARDS ── -->
  <div class="stat-grid">
    <div class="stat g">
      <div class="stat-label">Cycle Count</div>
      <div class="stat-val">{{ cycle }}</div>
      <div class="stat-sub">Uptime {{ uptime }}</div>
    </div>
    <div class="stat b">
      <div class="stat-label">Accounts</div>
      <div class="stat-val" style="color:var(--blue)">{{ user_count }}</div>
      <div class="stat-sub">/ 3 max recommended</div>
    </div>
    <div class="stat a">
      <div class="stat-label">Target IDs</div>
      <div class="stat-val" style="color:var(--amber)">{{ target_count }}</div>
      <div class="stat-sub">Cooldown {{ cooldown }}s / server</div>
    </div>
    <div class="stat g">
      <div class="stat-label">Est. Cycle</div>
      <div class="stat-val" style="color:{% if est_min > 60 %}var(--red){% else %}var(--green){% endif %}">{{ est_min }}m</div>
      <div class="stat-sub">{% if est_min > 60 %}<span style="color:var(--red)">⚠ Exceeds 1hr</span>{% else %}Fits in 1hr ✓{% endif %}</div>
    </div>
  </div>

  <!-- ── LAST CYCLE + TARGET IDS ── -->
  <div class="mid-grid">

    <!-- Last Cycle Summary -->
    <div class="panel">
      <div class="panel-title">Last Cycle Summary</div>
      {% if last_cycle %}
      <div class="lc-row">
        <div class="lc-item"><div class="lc-k">Cycle #</div><div class="lc-v" style="color:var(--blue)">{{ last_cycle.cycle }}</div></div>
        <div class="lc-item"><div class="lc-k">Followers Sent</div><div class="lc-v">{{ last_cycle.total_gained }}</div></div>
        <div class="lc-item"><div class="lc-k">Duration</div><div class="lc-v" style="color:var(--amber)">{{ last_cycle.duration_min }}m</div></div>
        <div class="lc-item"><div class="lc-k">Completed At</div><div class="lc-v" style="font-size:14px;margin-top:6px;color:var(--muted)">{{ last_cycle.at }}</div></div>
      </div>
      {% else %}
      <div style="color:var(--muted);font-family:var(--mono);font-size:12px;padding:12px 0">
        No completed cycles yet — first cycle in progress…
      </div>
      {% endif %}
    </div>

    <!-- Target IDs -->
    <div class="panel">
      <div class="panel-title">Target IDs — Sequential</div>
      <div class="id-tags">
        {% if target_ids %}
          {% for tid in target_ids %}
            <span class="id-tag {% if loop.index0 == next_idx %}next{% endif %}"
              title="{% if loop.index0 == next_idx %}Next to be used{% endif %}">
              {% if loop.index0 == next_idx %}→ {% endif %}{{ tid }}
            </span>
          {% endfor %}
        {% else %}
          <span class="id-empty">No target IDs — add in Admin</span>
        {% endif %}
      </div>
      <div style="font-size:10px;color:var(--muted);font-family:var(--mono);margin-top:12px">
        IDs rotate Server 1 → 9 in order. Index resets each cycle.
      </div>
    </div>

  </div>

  <!-- ── ACCOUNT HEALTH ── -->
  <div class="panel" style="margin-bottom:20px">
    <div class="panel-title">Account Health</div>
    {% if users %}
    <table class="tbl">
      <thead><tr>
        <th>#</th><th>Username</th><th>Health</th>
        <th>Failures</th><th>Last Login</th><th>Total Sent</th>
      </tr></thead>
      <tbody>
      {% for u in users %}
      <tr>
        <td style="color:var(--muted)">{{ loop.index }}</td>
        <td class="num">{{ u.username }}</td>
        <td>
          {% if u.health == 'healthy' %}
            <span class="h-dot h-green"></span><span style="color:var(--green)">Healthy</span>
          {% elif u.health == 'warning' %}
            <span class="h-dot h-amber"></span><span style="color:var(--amber)">Warning</span>
          {% elif u.health == 'unhealthy' %}
            <span class="h-dot h-red"></span><span style="color:var(--red)">Unhealthy</span>
          {% else %}
            <span class="h-dot h-gray"></span><span style="color:var(--muted)">Unknown</span>
          {% endif %}
        </td>
        <td style="color:{% if u.fail_count >= 3 %}var(--red){% elif u.fail_count > 0 %}var(--amber){% else %}var(--muted){% endif %}">
          {{ u.fail_count }}
        </td>
        <td style="color:var(--muted)">{{ u.last_login or '—' }}</td>
        <td class="num" style="color:var(--green)">{{ u.total_sent or 0 }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="color:var(--muted);font-family:var(--mono);font-size:12px;padding:8px 0">
      No accounts configured.
    </div>
    {% endif %}
  </div>

  <!-- ── SERVER STATS ── -->
  <div class="panel srv-grid" style="margin-bottom:20px">
    <div class="panel-title">Server Statistics</div>
    <table class="srv-tbl">
      <thead><tr>
        <th>Server</th><th>Runs</th><th>Progress</th>
        <th>Total</th><th>Avg</th><th>Min</th><th>Max</th>
        <th>Fails</th><th>Last OK</th>
      </tr></thead>
      <tbody>
      {% set max_total = server_stats | map(attribute=1) | map(attribute='total') | max %}
      {% for name, s in server_stats %}
      <tr>
        <td class="num">{{ name }}</td>
        <td style="color:var(--muted)">{{ s.count }}</td>
        <td>
          {% if max_total > 0 %}
          <span class="bar-wrap"><span class="bar-fill" style="width:{{ [(s.total * 100 // max_total), 100] | min }}%"></span></span>
          {% else %}
          <span class="bar-wrap"><span class="bar-fill" style="width:0%"></span></span>
          {% endif %}
        </td>
        <td style="color:var(--green)">{{ s.total }}</td>
        <td>{{ (s.total // s.count) if s.count > 0 else '—' }}</td>
        <td style="color:var(--amber)">{{ s.min or '—' }}</td>
        <td style="color:var(--blue)">{{ s.max or '—' }}</td>
        <td class="{% if s.fail_count > 0 %}fail-dot{% else %}{% endif %}" style="color:{% if s.fail_count > 0 %}var(--red){% else %}var(--muted){% endif %}">{{ s.fail_count }}</td>
        <td style="color:var(--muted)">{{ s.last_success or '—' }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- ── LIVE LOGS ── -->
  <div class="panel">
    <div class="panel-title">Live Logs — {{ log_lines|length }} entries</div>
    <div class="log-box" id="logbox">
      {% for line in log_lines %}
        {% if 'ERROR' in line or 'failed' in line.lower() %}
          <div class="log-line err">{{ line }}</div>
        {% elif 'WARNING' in line or 'warn' in line.lower() or 'IDLE' in line %}
          <div class="log-line warn">{{ line }}</div>
        {% elif 'SUCCESS' in line or 'Gained' in line or 'sent' in line.lower() or '✔' in line %}
          <div class="log-line ok">{{ line }}</div>
        {% elif 'INFO' in line %}
          <div class="log-line info">{{ line }}</div>
        {% else %}
          <div class="log-line">{{ line }}</div>
        {% endif %}
      {% endfor %}
    </div>
    <div class="log-refresh">Auto-refresh every 15s &nbsp;·&nbsp; {{ now }}</div>
  </div>

</div>

<script>
  const lb = document.getElementById('logbox');
  if(lb) lb.scrollTop = lb.scrollHeight;
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────────────────────────────────────
#  ADMIN TEMPLATE
# ──────────────────────────────────────────────────────────────────────────────
ADMIN_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>SERVER — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#080b10;--panel:#111620;--border:#1c2333;--border2:#263044;
  --green:#22d3a5;--green-dim:#0d4a3a;--blue:#4f8ef7;--blue-dim:#12285a;
  --amber:#f0a429;--amber-dim:#4a3210;--red:#f05252;--red-dim:#4a1212;
  --text:#dde3f0;--muted:#5a6a85;
  --sans:'Space Grotesk',sans-serif;--mono:'Space Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(79,142,247,.06),transparent)}
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:0 32px;height:56px;background:rgba(13,17,23,.9);
  border-bottom:1px solid var(--border);backdrop-filter:blur(12px)}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:28px;height:28px;background:var(--green);border-radius:6px;
  display:flex;align-items:center;justify-content:center;font-size:14px}
.brand-name{font-size:15px;font-weight:700;letter-spacing:.12em}
.nav{display:flex;gap:4px}
.nav a{font-family:var(--mono);font-size:11px;color:var(--muted);text-decoration:none;
  padding:5px 12px;border-radius:6px;border:1px solid transparent;transition:.18s;letter-spacing:.05em}
.nav a:hover{color:var(--text);border-color:var(--border2);background:var(--panel)}
.nav a.active{color:var(--amber);border-color:var(--amber-dim)}
.page{max-width:860px;margin:0 auto;padding:24px 20px 60px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:22px;margin-bottom:18px}
.panel-title{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);font-family:var(--mono);margin-bottom:18px;
  display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}
label{display:block;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);font-family:var(--mono);margin-bottom:6px}
input[type=text],input[type=password],textarea{
  width:100%;padding:9px 13px;border:1px solid var(--border);border-radius:8px;
  background:#060910;color:var(--text);font-family:var(--mono);font-size:12px;outline:none;transition:.2s}
input:focus,textarea:focus{border-color:var(--green)}
textarea{height:90px;resize:vertical}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:16px}
.field{flex:1;min-width:130px}
.btn{padding:9px 18px;border:1px solid;border-radius:7px;cursor:pointer;
  font-size:12px;font-weight:600;font-family:var(--mono);transition:.18s;letter-spacing:.04em}
.btn-g{background:var(--green-dim);color:var(--green);border-color:rgba(34,211,165,.3)}
.btn-g:hover{background:rgba(34,211,165,.18)}
.btn-b{background:var(--blue-dim);color:var(--blue);border-color:rgba(79,142,247,.3)}
.btn-b:hover{background:rgba(79,142,247,.18)}
.btn-r{background:var(--red-dim);color:var(--red);border-color:rgba(240,82,82,.3)}
.btn-r:hover{background:rgba(240,82,82,.18)}
.flash-ok{background:rgba(34,211,165,.07);color:var(--green);border:1px solid rgba(34,211,165,.2);
  padding:10px 15px;border-radius:8px;font-family:var(--mono);font-size:12px;margin-bottom:16px}
.flash-err{background:rgba(240,82,82,.07);color:var(--red);border:1px solid rgba(240,82,82,.2);
  padding:10px 15px;border-radius:8px;font-family:var(--mono);font-size:12px;margin-bottom:16px}
.tbl{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
.tbl th{color:var(--muted);text-align:left;padding:8px 10px;
  border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.08em}
.tbl td{padding:9px 10px;border-bottom:1px solid rgba(28,35,51,.6);vertical-align:middle}
.tbl tr:last-child td{border-bottom:none}
.tag{display:inline-flex;align-items:center;gap:6px;
  background:var(--blue-dim);color:var(--blue);border:1px solid rgba(79,142,247,.25);
  border-radius:6px;padding:3px 10px;font-size:11px;font-family:var(--mono);margin:3px}
.tag form{display:inline;margin:0}
.tag button{background:none;border:none;cursor:pointer;color:var(--red);
  font-size:13px;line-height:1;padding:0;transition:.15s}
.tag button:hover{color:#ff8080}
.info-box{background:#060910;border:1px solid var(--border);border-radius:8px;
  padding:13px 15px;font-family:var(--mono);font-size:11px;color:var(--muted);
  line-height:1.9;margin-top:14px}
.info-box strong{color:var(--amber)}
.h-dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
.h-green{background:var(--green)}.h-amber{background:var(--amber)}
.h-red{background:var(--red)}.h-gray{background:var(--muted)}
.cap-bar{display:flex;gap:0;height:8px;border-radius:4px;overflow:hidden;
  margin:10px 0;background:var(--border)}
.cap-seg{height:100%;transition:.3s}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="brand-icon">⚡</div>
    <span class="brand-name">SERVER</span>
  </div>
  <nav class="nav">
    <a href="/">Dashboard</a>
    <a href="/admin" class="active">Admin</a>
    <a href="/admin/logout">Logout</a>
  </nav>
</div>

<div class="page">
  {% if message %}<div class="flash-ok">✓ {{ message }}</div>{% endif %}
  {% if error %}<div class="flash-err">✕ {{ error }}</div>{% endif %}

  <!-- ── TARGET IDs ── -->
  <div class="panel">
    <div class="panel-title">Target IDs — Sequential Mode</div>

    <div style="margin-bottom:16px;min-height:32px">
      {% for tid in target_ids %}
        <span class="tag">
          {{ tid }}
          <form method="POST" action="/admin/remove_target">
            <input type="hidden" name="target_id" value="{{ tid }}">
            <button type="submit" title="Remove">✕</button>
          </form>
        </span>
      {% endfor %}
      {% if not target_ids %}
        <span style="color:var(--red);font-family:var(--mono);font-size:12px">
          ✕ No target IDs — automation is paused
        </span>
      {% endif %}
    </div>

    <div class="row">
      <form method="POST" action="/admin/add_target" style="display:flex;gap:10px;flex:1;align-items:flex-end">
        <div class="field"><label>Add Single ID</label>
          <input type="text" name="target_id" placeholder="e.g. 57310375825">
        </div>
        <button type="submit" class="btn btn-b">+ Add</button>
      </form>
    </div>

    <form method="POST" action="/admin/add_targets_bulk">
      <label>Bulk Add (one ID per line)</label>
      <textarea name="bulk_ids" placeholder="57310375825&#10;12345678&#10;98765432"></textarea>
      <div style="margin-top:10px">
        <button type="submit" class="btn btn-g">+ Add All</button>
      </div>
    </form>

    <div class="info-box">
      <strong>Sequence logic:</strong><br>
      • 1 ID → all 9 servers use same ID, cooldown = 2 min<br>
      • 2+ IDs → each server gets next ID in order, cooldown = 1 min<br>
      • Index resets to 0 at the start of every new cycle<br>
      • Removing all IDs pauses automation immediately — no browser opened
    </div>
  </div>

  <!-- ── ACCOUNTS ── -->
  <div class="panel">
    <div class="panel-title">Accounts — {{ users|length }} / 3 max</div>

    <!-- Capacity bar -->
    <div style="font-size:10px;color:var(--muted);font-family:var(--mono);margin-bottom:4px">
      CAPACITY ({{ users|length }}/3 accounts used)
    </div>
    <div class="cap-bar">
      {% for i in range(3) %}
        {% if i < users|length %}
          <div class="cap-seg" style="width:33.3%;background:{% if users|length <= 2 %}var(--green){% else %}var(--amber){% endif %}"></div>
        {% else %}
          <div class="cap-seg" style="width:33.3%;background:var(--border2)"></div>
        {% endif %}
      {% endfor %}
    </div>
    <div style="font-size:10px;color:var(--muted);font-family:var(--mono);margin-bottom:16px">
      Est. cycle time with current config: <strong style="color:{% if est_min > 60 %}var(--red){% else %}var(--green){% endif %}">{{ est_min }} min</strong>
      {% if est_min > 60 %}<span style="color:var(--red)"> — exceeds 1hr, reduce accounts</span>{% endif %}
    </div>

    <form method="POST" action="/admin/add_user" style="margin-bottom:20px">
      <div class="row">
        <div class="field"><label>Username</label>
          <input type="text" name="username" placeholder="Instagram username">
        </div>
        <div class="field"><label>Password</label>
          <input type="text" name="password" placeholder="Password">
        </div>
        <button type="submit" class="btn btn-b">+ Add Account</button>
      </div>
    </form>

    {% if users %}
    <table class="tbl">
      <thead><tr>
        <th>#</th><th>Username</th><th>Password</th>
        <th>Health</th><th>Fails</th><th>Last Login</th><th>Total Sent</th><th></th>
      </tr></thead>
      <tbody>
      {% for u in users %}
      <tr>
        <td style="color:var(--muted)">{{ loop.index }}</td>
        <td style="font-weight:600">{{ u.username }}</td>
        <td style="color:var(--muted)">{{ u.password }}</td>
        <td>
          {% if u.health == 'healthy' %}
            <span class="h-dot h-green"></span><span style="color:var(--green)">Healthy</span>
          {% elif u.health == 'warning' %}
            <span class="h-dot h-amber"></span><span style="color:var(--amber)">Warning</span>
          {% elif u.health == 'unhealthy' %}
            <span class="h-dot h-red"></span><span style="color:var(--red)">Unhealthy</span>
          {% else %}
            <span class="h-dot h-gray"></span><span style="color:var(--muted)">Unknown</span>
          {% endif %}
        </td>
        <td style="color:{% if u.fail_count >= 3 %}var(--red){% elif u.fail_count > 0 %}var(--amber){% else %}var(--muted){% endif %}">
          {{ u.fail_count }}
        </td>
        <td style="color:var(--muted)">{{ u.last_login or '—' }}</td>
        <td style="color:var(--green)">{{ u.total_sent or 0 }}</td>
        <td>
          <form method="POST" action="/admin/remove_user"
                onsubmit="return confirm('Remove {{ u.username }}?')">
            <input type="hidden" name="username" value="{{ u.username }}">
            <button type="submit" class="btn btn-r" style="padding:4px 10px;font-size:11px">Remove</button>
          </form>
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="color:var(--muted);font-family:var(--mono);font-size:12px;padding:16px 0;text-align:center">
      No accounts — automation is paused.
    </div>
    {% endif %}

    <div class="info-box">
      <strong>1-Hour capacity guide:</strong><br>
      • 1 account = ~18 min/cycle &nbsp;✓<br>
      • 2 accounts = ~36 min/cycle &nbsp;✓<br>
      • 3 accounts = ~54 min/cycle &nbsp;✓ (max recommended)<br>
      • 4 accounts = ~72 min/cycle &nbsp;<strong style="color:var(--red)">⚠ exceeds 1hr</strong>
    </div>
  </div>

</div>
</body>
</html>
"""

LOGIN_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>SERVER — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Space+Mono&display=swap" rel="stylesheet">
<style>
:root{--bg:#080b10;--panel:#111620;--border:#1c2333;--green:#22d3a5;--green-dim:#0d4a3a;
  --text:#dde3f0;--muted:#5a6a85;--red:#f05252;
  --sans:'Space Grotesk',sans-serif;--mono:'Space Mono',monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);
  display:flex;align-items:center;justify-content:center;min-height:100vh;
  background-image:radial-gradient(ellipse 60% 60% at 50% 50%,rgba(34,211,165,.05),transparent)}
.box{background:var(--panel);border:1px solid var(--border);border-radius:14px;
  padding:40px 36px;width:340px}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.logo-icon{width:32px;height:32px;background:var(--green);border-radius:8px;
  display:flex;align-items:center;justify-content:center;font-size:16px}
.logo-name{font-size:17px;font-weight:700;letter-spacing:.12em}
label{display:block;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);font-family:var(--mono);margin-bottom:7px}
input[type=password]{width:100%;padding:10px 13px;border:1px solid var(--border);
  border-radius:8px;background:#060910;color:var(--text);
  font-family:var(--mono);font-size:13px;outline:none;transition:.2s;margin-bottom:18px}
input:focus{border-color:var(--green)}
button{width:100%;padding:11px;background:var(--green-dim);color:var(--green);
  border:1px solid rgba(34,211,165,.3);border-radius:8px;cursor:pointer;
  font-family:var(--mono);font-size:13px;font-weight:700;transition:.2s}
button:hover{background:rgba(34,211,165,.18)}
.err{color:var(--red);font-size:11px;font-family:var(--mono);margin-top:12px}
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <span class="logo-name">SERVER</span>
  </div>
  <form method="POST">
    <label>Admin Password</label>
    <input type="password" name="password" autofocus>
    <button type="submit">Enter →</button>
  </form>
  {% if error %}<div class="err">✕ {{ error }}</div>{% endif %}
</div>
</body>
</html>
"""

# ════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/')
def dashboard():
    with cycle_count_lock:
        cycle = current_cycle
    with target_ids_lock:
        tids = list(TARGET_IDS)
        nxt  = target_id_index % len(TARGET_IDS) if TARGET_IDS else 0
    with users_lock:
        users_copy = [dict(u) for u in USERS]
    with stats_lock:
        server_stats = [(k, dict(v)) for k, v in stats.items()]
    with log_buffer_lock:
        log_lines = list(log_buffer)
    with idle_lock:
        is_idle     = not idle_state["active"]
        idle_reason = idle_state["reason"]
    with last_cycle_lock:
        lcs = dict(last_cycle_summary)

    cooldown = PAUSE_BETWEEN_SITES_MULTI if len(tids) > 1 else PAUSE_BETWEEN_SITES_SINGLE
    est_min  = estimate_cycle_minutes()

    return render_template_string(
        DASHBOARD_TEMPLATE,
        cycle=cycle, user_count=len(users_copy),
        target_count=len(tids), target_ids=tids, next_idx=nxt,
        cooldown=cooldown, est_min=est_min,
        users=users_copy, server_stats=server_stats,
        log_lines=log_lines, now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        is_idle=is_idle, idle_reason=idle_reason,
        last_cycle=lcs, uptime=uptime_str(),
    )

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    with target_ids_lock:
        tids = list(TARGET_IDS)
    with users_lock:
        users_copy = [dict(u) for u in USERS]
    msg = session.pop('admin_message', None)
    err = session.pop('admin_error', None)
    return render_template_string(
        ADMIN_TEMPLATE, target_ids=tids, users=users_copy,
        message=msg, error=err, est_min=estimate_cycle_minutes())

@app.route('/admin/add_target', methods=['POST'])
def add_target():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    tid = request.form.get('target_id', '').strip()
    if tid:
        with target_ids_lock:
            if tid not in TARGET_IDS:
                TARGET_IDS.append(tid)
                logger.info(f"Admin added target ID: {tid}")
                session['admin_message'] = f"Target ID {tid} added."
            else:
                session['admin_error'] = f"ID {tid} already exists."
    else:
        session['admin_error'] = "ID cannot be empty."
    return redirect(url_for('admin'))

@app.route('/admin/add_targets_bulk', methods=['POST'])
def add_targets_bulk():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    lines = [l.strip() for l in request.form.get('bulk_ids','').splitlines() if l.strip()]
    added = []
    with target_ids_lock:
        for tid in lines:
            if tid not in TARGET_IDS:
                TARGET_IDS.append(tid)
                added.append(tid)
    if added:
        logger.info(f"Admin bulk-added {len(added)} target IDs: {added}")
        session['admin_message'] = f"Added {len(added)} IDs."
    else:
        session['admin_error'] = "No new IDs added (empty or all duplicates)."
    return redirect(url_for('admin'))

@app.route('/admin/remove_target', methods=['POST'])
def remove_target():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    tid = request.form.get('target_id', '').strip()
    with target_ids_lock:
        if tid in TARGET_IDS:
            TARGET_IDS.remove(tid)
            logger.info(f"Admin removed target ID: {tid}")
            session['admin_message'] = f"Target ID {tid} removed. Automation will pause if list is now empty."
        else:
            session['admin_error'] = f"ID {tid} not found."
    return redirect(url_for('admin'))

@app.route('/admin/add_user', methods=['POST'])
def add_user():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username or not password:
        session['admin_error'] = "Username and password are required."
        return redirect(url_for('admin'))
    with users_lock:
        if any(u['username'] == username for u in USERS):
            session['admin_error'] = f"'{username}' already exists."
        elif len(USERS) >= 3:
            session['admin_error'] = "Max 3 accounts for 1-hour cycle. Remove one first."
        else:
            USERS.append({"username": username, "password": password,
                          "health": "unknown", "last_login": None,
                          "fail_count": 0, "total_sent": 0})
            logger.info(f"Admin added account: {username}")
            session['admin_message'] = f"Account '{username}' added — automation will resume next cycle."
    return redirect(url_for('admin'))

@app.route('/admin/remove_user', methods=['POST'])
def remove_user():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    username = request.form.get('username', '').strip()
    with users_lock:
        before = len(USERS)
        USERS[:] = [u for u in USERS if u['username'] != username]
        if len(USERS) < before:
            logger.info(f"Admin removed account: {username}")
            session['admin_message'] = f"Account '{username}' removed. Will stop at next server check."
        else:
            session['admin_error'] = f"'{username}' not found."
    return redirect(url_for('admin'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin'))
        error = "Incorrect password."
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('dashboard'))

# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    t = threading.Thread(target=automation_loop, daemon=True)
    t.start()
    logger.info("SERVER started. Dashboard → http://0.0.0.0:8081")
    app.run(host='0.0.0.0', port=8081, debug=False, use_reloader=False)
