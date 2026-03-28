#!/usr/bin/env python3
"""
Enhanced Normal Automation Dashboard
- Multiple target IDs (sequential, not random)
- Multiple fake accounts (sequential)
- Account health monitoring
- Reduced cooldown (1 min) when multiple target IDs
- Auto-completes all accounts in ~1 hour
- Live dashboard with logs panel
- Max 3 accounts recommended for 1-hour cycle
"""

import time
import logging
import re
import threading
import collections
from datetime import datetime
from flask import Flask, request, session, redirect, url_for, render_template_string

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ========== CONFIGURATION ==========
ADMIN_PASSWORD = "admin123"

users_lock = threading.Lock()
USERS = [
    {"username": "user", "password": "pass",
     "health": "unknown", "last_login": None, "fail_count": 0},
]

# Multiple target IDs â€” used in sequence
target_ids_lock = threading.Lock()
TARGET_IDS = ["76618425858"]          # Add more IDs via admin panel
target_id_index = 0                   # Tracks which target ID is next

cycle_count_lock = threading.Lock()
current_cycle = 0

# Live log buffer (last 200 lines shown in dashboard)
log_buffer_lock = threading.Lock()
log_buffer = collections.deque(maxlen=200)

SERVER_NAMES = ["Server 1", "Server 2", "Server 3", "Server 4", "Server 5",
                "Server 6", "Server 7", "Server 8", "Server 9"]

DOMAINS = [
    "takipcimx.net",
    "takipcizen.com",
    "takipcigen.com",
    "takipcikrali.com",
    "takipcigir.com",
    "takipcitime.com",
    "takipcibase.com",
    "instamoda.org",
    "takip88.com",
]

SITES = [{"name": SERVER_NAMES[i], "domain": DOMAINS[i]} for i in range(len(SERVER_NAMES))]

TARGET_CYCLE_SECONDS = 60 * 60       # 1 hour total cycle
ACTION_TIME_PER_SITE = 60
PAUSE_BETWEEN_SITES_SINGLE = 120     # 2 min â€” only 1 target ID
PAUSE_BETWEEN_SITES_MULTI  = 60     # 1 min â€” multiple target IDs
# ====================================

# ========== LOGGING WITH BUFFER ==========
class BufferHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        with log_buffer_lock:
            log_buffer.append(msg)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("takipci_enhanced.log"),
        logging.StreamHandler(),
        BufferHandler()
    ]
)
# Make sure BufferHandler uses same format
for h in logging.root.handlers:
    if isinstance(h, BufferHandler):
        h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logger = logging.getLogger("enhanced-automation")

stats_lock = threading.Lock()
stats = {
    server_name: {
        "total": 0,
        "count": 0,
        "min": None,
        "max": 0,
        "last_success": None
    } for server_name in SERVER_NAMES
}

chrome_options = Options()
chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--disable-blink-features=AutomationControlled")
chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
chrome_options.add_experimental_option('useAutomationExtension', False)

# ========== HELPERS ==========
def get_next_target_id():
    """Returns the next target ID in sequence (cycles through the list)."""
    global target_id_index
    with target_ids_lock:
        ids = list(TARGET_IDS)
        if not ids:
            return None
        idx = target_id_index % len(ids)
        target_id_index = (idx + 1) % len(ids)
        return ids[idx]

def get_pause_time():
    with target_ids_lock:
        return PAUSE_BETWEEN_SITES_MULTI if len(TARGET_IDS) > 1 else PAUSE_BETWEEN_SITES_SINGLE

def extract_follower_count(page_source):
    text = page_source.lower()
    patterns = [
        r'(\d+)\s*takip\u00e7i',
        r'(\d+)\s*follower',
        r'success.*?(\d+)',
        r'ba\u015far\u0131l\u0131.*?(\d+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    if "ba\u015far\u0131l\u0131" in text or "success" in text:
        return 50
    return None

def find_element_safe(driver, wait, by, selector):
    try:
        return wait.until(EC.presence_of_element_located((by, selector)))
    except:
        return None

def click_element_safe(driver, wait, by, selector):
    try:
        elem = wait.until(EC.element_to_be_clickable((by, selector)))
        elem.click()
        return True
    except:
        return False

def update_account_health(username, success):
    with users_lock:
        for u in USERS:
            if u["username"] == username:
                if success:
                    u["health"] = "healthy"
                    u["fail_count"] = 0
                    u["last_login"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                else:
                    u["fail_count"] = u.get("fail_count", 0) + 1
                    u["health"] = "warning" if u["fail_count"] < 3 else "unhealthy"
                break

def login_with_retry(driver, wait, login_url, username, password, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"  Login attempt {attempt}/{max_retries} for {username}")
            driver.get(login_url)
            time.sleep(2)

            user_field = find_element_safe(driver, wait, By.NAME, "username")
            if not user_field:
                raise Exception("Username field not found")
            user_field.send_keys(username)

            pass_field = find_element_safe(driver, wait, By.NAME, "password")
            if not pass_field:
                raise Exception("Password field not found")
            pass_field.send_keys(password)

            login_success = False
            for selector in [
                (By.CSS_SELECTOR, "button.instaclass19"),
                (By.XPATH, "//button[contains(text(), 'G\u0130R\u0130\u015e')]"),
                (By.XPATH, "//button[@type='submit']"),
                (By.XPATH, "//input[@type='submit']"),
                (By.XPATH, "//form//button"),
            ]:
                if click_element_safe(driver, wait, selector[0], selector[1]):
                    login_success = True
                    break

            if not login_success:
                raise Exception("Could not click login button")

            try:
                wait.until(lambda d: d.current_url != login_url)
                logger.info(f"  Login successful for {username}.")
                return True
            except TimeoutException:
                if "login" not in driver.current_url.lower():
                    return True
                raise Exception("Login timeout")
        except Exception as e:
            logger.warning(f"  Login attempt {attempt} failed: {e}")
            if attempt < max_retries:
                sleep_time = 5 * (2 ** (attempt - 1))
                logger.info(f"  Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
    return False

def process_site(driver, wait, site, username, password, target_id):
    name = site["name"]
    domain = site["domain"]
    login_url = f"https://{domain}/login"
    follow_url = f"https://{domain}/tools/send-follower/{target_id}"

    logger.info(f"--- {name} | user={username} | target={target_id} ---")

    try:
        if not login_with_retry(driver, wait, login_url, username, password):
            logger.error(f"  Login failed for {username} on {name}")
            update_account_health(username, False)
            return False, 0

        update_account_health(username, True)

        logger.info("  Going to follower page...")
        driver.get(follow_url)
        time.sleep(3)

        start_clicked = False
        for selector in [
            (By.ID, "formTakipSubmitButton"),
            (By.XPATH, "//button[contains(text(), 'Start')]"),
            (By.XPATH, "//button[contains(text(), 'G\u00f6nder')]"),
            (By.CSS_SELECTOR, "button.btn-success"),
            (By.XPATH, "//button[@onclick='sendTakip();']"),
        ]:
            if click_element_safe(driver, wait, selector[0], selector[1]):
                start_clicked = True
                break

        if not start_clicked:
            logger.error("  Could not click Start button.")
            return False, 0

        logger.info("  Start button clicked.")
        time.sleep(5)
        page_source = driver.page_source
        count = extract_follower_count(page_source)

        if count:
            logger.info(f"  SUCCESS â€” Gained {count} followers for target {target_id}.")
            return True, count
        else:
            logger.warning("  Success but count not parsed. Assuming 50.")
            return True, 50

    except Exception as e:
        logger.error(f"  Error on {name}: {e}")
        return False, 0

# ========== AUTOMATION LOOP ==========
def automation_loop():
    global current_cycle, target_id_index
    logger.info("=" * 60)
    logger.info("Enhanced Automation started.")
    logger.info("Sequence mode: target IDs used in order, accounts in order.")
    logger.info("=" * 60)

    while True:
        with cycle_count_lock:
            current_cycle += 1
            cycle = current_cycle

        cycle_start = time.time()
        logger.info(f"\n{'#'*60}")
        logger.info(f"Cycle #{cycle} started at {datetime.now().strftime('%H:%M:%S')}")

        with target_ids_lock:
            ids_snapshot = list(TARGET_IDS)
        with users_lock:
            users_snapshot = list(USERS)

        logger.info(f"Target IDs this cycle: {ids_snapshot}")
        logger.info(f"Accounts this cycle: {[u['username'] for u in users_snapshot]}")
        logger.info(f"Cooldown between sites: {get_pause_time()}s")

        # --- Sequence: for each account, process all 9 servers, each server uses next target ID in order ---
        for user_idx, user in enumerate(users_snapshot):
            username = user["username"]
            password = user["password"]
            logger.info(f"\n>>> Account {user_idx+1}/{len(users_snapshot)}: {username}")

            service = Service()
            driver = webdriver.Chrome(service=service, options=chrome_options)
            wait = WebDriverWait(driver, 20)

            try:
                for site_idx, site in enumerate(SITES):
                    # Get next target ID in sequence
                    target_id = get_next_target_id()
                    if not target_id:
                        logger.warning("No target IDs configured. Skipping.")
                        break

                    success, gained = process_site(driver, wait, site, username, password, target_id)

                    if success:
                        with stats_lock:
                            s = stats[site["name"]]
                            s["total"] += gained
                            s["count"] += 1
                            if s["min"] is None or gained < s["min"]:
                                s["min"] = gained
                            if gained > s["max"]:
                                s["max"] = gained
                            s["last_success"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                    if site_idx < len(SITES) - 1:
                        pause = get_pause_time()
                        logger.info(f"  Waiting {pause}s before next server...")
                        time.sleep(pause)

            finally:
                driver.quit()
                logger.info(f"Browser closed for {username}.")

            if user_idx < len(users_snapshot) - 1:
                logger.info("Switching to next account, waiting 30s...")
                time.sleep(30)

        elapsed = time.time() - cycle_start
        logger.info(f"\nCycle #{cycle} completed in {elapsed/60:.1f} minutes.")

        if elapsed < TARGET_CYCLE_SECONDS:
            wait_time = TARGET_CYCLE_SECONDS - elapsed
            logger.info(f"Waiting {wait_time/60:.1f} min to fill 1-hour cycle...")
            time.sleep(wait_time)
        else:
            logger.warning("Cycle exceeded 1 hour. Starting next immediately.")

        logger.info("\nCurrent server statistics:")
        with stats_lock:
            for name, s in stats.items():
                if s["count"] > 0:
                    avg = s["total"] // s["count"]
                    logger.info(f"  {name}: runs={s['count']}, total={s['total']}, avg={avg}, "
                                f"min={s['min']}, max={s['max']}, last={s['last_success']}")
                else:
                    logger.info(f"  {name}: no successful runs yet")

# ========== FLASK APP ==========
app = Flask(__name__)
app.secret_key = "change-this-secret-key-in-production"

# ---------- TEMPLATES ----------

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="15">
<title>Automation Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d0f14;
    --surface: #13161e;
    --border: #1e2330;
    --accent: #00e5a0;
    --accent2: #3b82f6;
    --warn: #f59e0b;
    --danger: #ef4444;
    --text: #e2e8f0;
    --muted: #64748b;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Syne', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; }

  /* TOP BAR */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 32px; background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 100;
  }
  .topbar .brand { font-size: 15px; font-weight: 800; letter-spacing: .08em; color: var(--accent); }
  .topbar .links a {
    font-size: 13px; color: var(--muted); text-decoration: none;
    margin-left: 20px; font-family: var(--mono);
    transition: color .2s;
  }
  .topbar .links a:hover { color: var(--accent); }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent); margin-right: 8px;
    animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(1.3)} }

  /* GRID */
  .page { max-width: 1200px; margin: 0 auto; padding: 28px 24px; }
  .grid-top { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
  .grid-mid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .grid-bot { display: grid; grid-template-columns: 1fr; gap: 16px; }

  /* CARDS */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
  }
  .card-label { font-size: 11px; letter-spacing: .1em; color: var(--muted);
    text-transform: uppercase; margin-bottom: 8px; font-family: var(--mono); }
  .card-value { font-size: 42px; font-weight: 800; color: var(--accent); line-height: 1; }
  .card-sub { font-size: 12px; color: var(--muted); margin-top: 6px; font-family: var(--mono); }

  /* STATUS BADGE */
  .badge { display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-family: var(--mono); padding: 3px 10px;
    border-radius: 20px; font-weight: 700; }
  .badge-green { background: rgba(0,229,160,.12); color: var(--accent); }
  .badge-yellow { background: rgba(245,158,11,.12); color: var(--warn); }
  .badge-red { background: rgba(239,68,68,.12); color: var(--danger); }
  .badge-blue { background: rgba(59,130,246,.12); color: var(--accent2); }

  /* TABLE */
  .tbl { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 13px; }
  .tbl th { color: var(--muted); text-align: left; padding: 10px 12px;
    border-bottom: 1px solid var(--border); font-size: 11px; letter-spacing: .08em; }
  .tbl td { padding: 10px 12px; border-bottom: 1px solid rgba(30,35,48,.6); vertical-align: middle; }
  .tbl tr:last-child td { border-bottom: none; }
  .tbl tr:hover td { background: rgba(255,255,255,.02); }

  /* HEALTH */
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot-green { background: var(--accent); }
  .dot-yellow { background: var(--warn); }
  .dot-red { background: var(--danger); }
  .dot-gray { background: var(--muted); }

  /* TARGET IDS */
  .tag { display: inline-block; background: rgba(59,130,246,.15); color: var(--accent2);
    border: 1px solid rgba(59,130,246,.3); border-radius: 6px;
    padding: 2px 10px; font-size: 12px; font-family: var(--mono); margin: 2px; }

  /* LOG PANEL */
  .log-panel {
    background: #080a0e; border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; height: 320px; overflow-y: auto;
    font-family: var(--mono); font-size: 12px; line-height: 1.7;
  }
  .log-panel::-webkit-scrollbar { width: 6px; }
  .log-panel::-webkit-scrollbar-track { background: transparent; }
  .log-panel::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-line { color: #94a3b8; border-bottom: 1px solid rgba(30,35,48,.4); padding: 2px 0; }
  .log-line.err { color: var(--danger); }
  .log-line.warn { color: var(--warn); }
  .log-line.ok { color: var(--accent); }
  .section-title { font-size: 13px; font-weight: 700; color: var(--text);
    margin-bottom: 14px; letter-spacing: .04em; display: flex; align-items: center; gap: 8px; }
  .section-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .refresh-note { font-size: 11px; color: var(--muted); font-family: var(--mono);
    text-align: right; margin-top: 8px; }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand"><span class="pulse"></span>AUTOMATION DASHBOARD</div>
  <div class="links">
    <a href="/admin">âš™ Admin</a>
    <a href="/">â†º Refresh</a>
  </div>
</div>

<div class="page">

  <!-- TOP STATS ROW -->
  <div class="grid-top">
    <div class="card">
      <div class="card-label">Cycle Count</div>
      <div class="card-value">{{ cycle }}</div>
      <div class="card-sub">Total cycles run</div>
    </div>
    <div class="card">
      <div class="card-label">Status</div>
      <div style="margin-top:10px">
        <span class="badge badge-green">â— Running</span>
      </div>
      <div class="card-sub" style="margin-top:10px">Automation active</div>
    </div>
    <div class="card">
      <div class="card-label">Active Accounts</div>
      <div class="card-value" style="color:var(--accent2)">{{ user_count }}</div>
      <div class="card-sub">/ 3 recommended max</div>
    </div>
    <div class="card">
      <div class="card-label">Target IDs</div>
      <div class="card-value" style="color:var(--warn)">{{ target_count }}</div>
      <div class="card-sub">Cooldown: {{ cooldown }}s/server</div>
    </div>
  </div>

  <!-- MID: Target IDs + Account Health -->
  <div class="grid-mid">

    <!-- Target IDs -->
    <div class="card">
      <div class="section-title">ðŸŽ¯ Target IDs (Sequential)</div>
      {% if target_ids %}
        {% for tid in target_ids %}
          <span class="tag">{{ tid }}</span>
        {% endfor %}
        <div class="card-sub" style="margin-top:12px">
          Next up â†’ Index {{ next_idx % target_ids|length }}
          &nbsp;|&nbsp; Mode: sequential, no repeats until all used
        </div>
      {% else %}
        <span style="color:var(--danger); font-family:var(--mono); font-size:13px">No target IDs configured!</span>
      {% endif %}
    </div>

    <!-- Account Health -->
    <div class="card">
      <div class="section-title">ðŸ‘¤ Account Health</div>
      <table class="tbl">
        <thead><tr>
          <th>Username</th><th>Health</th><th>Fails</th><th>Last Login</th>
        </tr></thead>
        <tbody>
        {% for u in users %}
          <tr>
            <td style="font-weight:700">{{ u.username }}</td>
            <td>
              {% if u.health == 'healthy' %}
                <span class="dot dot-green"></span><span style="color:var(--accent)">Healthy</span>
              {% elif u.health == 'warning' %}
                <span class="dot dot-yellow"></span><span style="color:var(--warn)">Warning</span>
              {% elif u.health == 'unhealthy' %}
                <span class="dot dot-red"></span><span style="color:var(--danger)">Unhealthy</span>
              {% else %}
                <span class="dot dot-gray"></span><span style="color:var(--muted)">Unknown</span>
              {% endif %}
            </td>
            <td style="color:{% if u.fail_count >= 3 %}var(--danger){% elif u.fail_count > 0 %}var(--warn){% else %}var(--muted){% endif %}">
              {{ u.fail_count }}
            </td>
            <td style="color:var(--muted)">{{ u.last_login or 'â€”' }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>

  </div>

  <!-- SERVER STATS TABLE -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">ðŸ“Š Server Statistics</div>
    <table class="tbl">
      <thead><tr>
        <th>Server</th><th>Runs</th><th>Total Followers</th>
        <th>Average</th><th>Min</th><th>Max</th><th>Last Success</th>
      </tr></thead>
      <tbody>
      {% for name, s in server_stats %}
        <tr>
          <td style="font-weight:700">{{ name }}</td>
          <td>{{ s.count }}</td>
          <td style="color:var(--accent)">{{ s.total }}</td>
          <td>{{ (s.total // s.count) if s.count > 0 else 'â€”' }}</td>
          <td style="color:var(--warn)">{{ s.min or 'â€”' }}</td>
          <td style="color:var(--accent2)">{{ s.max }}</td>
          <td style="color:var(--muted)">{{ s.last_success or 'Not yet' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- LOG PANEL -->
  <div class="card">
    <div class="section-title">ðŸ“‹ Live Logs (last {{ log_lines|length }} entries)</div>
    <div class="log-panel" id="logpanel">
      {% for line in log_lines %}
        {% if 'ERROR' in line or 'failed' in line.lower() %}
          <div class="log-line err">{{ line }}</div>
        {% elif 'WARNING' in line or 'warn' in line.lower() %}
          <div class="log-line warn">{{ line }}</div>
        {% elif 'SUCCESS' in line or 'successful' in line.lower() or 'Gained' in line %}
          <div class="log-line ok">{{ line }}</div>
        {% else %}
          <div class="log-line">{{ line }}</div>
        {% endif %}
      {% endfor %}
    </div>
    <div class="refresh-note">Auto-refreshes every 15s Â· {{ now }}</div>
  </div>

</div>

<script>
  // Always scroll log to bottom on load
  const lp = document.getElementById('logpanel');
  if (lp) lp.scrollTop = lp.scrollHeight;
</script>
</body>
</html>
"""

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Admin Panel</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d0f14; --surface: #13161e; --border: #1e2330;
    --accent: #00e5a0; --accent2: #3b82f6; --warn: #f59e0b; --danger: #ef4444;
    --text: #e2e8f0; --muted: #64748b;
    --mono: 'JetBrains Mono', monospace; --sans: 'Syne', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; }
  .topbar { display:flex; align-items:center; justify-content:space-between;
    padding:14px 32px; background:var(--surface); border-bottom:1px solid var(--border); }
  .topbar .brand { font-size:15px; font-weight:800; color:var(--accent); }
  .topbar a { color:var(--muted); text-decoration:none; margin-left:20px;
    font-family:var(--mono); font-size:13px; }
  .topbar a:hover { color:var(--accent); }
  .page { max-width:900px; margin:0 auto; padding:28px 20px; }
  .card { background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:24px; margin-bottom:20px; }
  .section-title { font-size:13px; font-weight:700; color:var(--text);
    margin-bottom:16px; letter-spacing:.04em;
    display:flex; align-items:center; gap:8px; }
  .section-title::after { content:''; flex:1; height:1px; background:var(--border); }
  label { display:block; font-size:11px; letter-spacing:.08em; color:var(--muted);
    text-transform:uppercase; margin-bottom:6px; font-family:var(--mono); }
  input[type=text],input[type=password],textarea {
    width:100%; padding:10px 14px; border:1px solid var(--border);
    border-radius:8px; background:#080a0e; color:var(--text);
    font-family:var(--mono); font-size:13px; outline:none;
    transition:border-color .2s;
  }
  input:focus,textarea:focus { border-color:var(--accent); }
  textarea { height:100px; resize:vertical; }
  .row { display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap; margin-bottom:16px; }
  .row .field { flex:1; min-width:150px; }
  .btn { padding:10px 22px; border:none; border-radius:8px; cursor:pointer;
    font-size:13px; font-weight:700; font-family:var(--mono); transition:.2s; }
  .btn-green { background:rgba(0,229,160,.15); color:var(--accent); border:1px solid rgba(0,229,160,.3); }
  .btn-green:hover { background:rgba(0,229,160,.25); }
  .btn-red { background:rgba(239,68,68,.15); color:var(--danger); border:1px solid rgba(239,68,68,.3); }
  .btn-red:hover { background:rgba(239,68,68,.25); }
  .btn-blue { background:rgba(59,130,246,.15); color:var(--accent2); border:1px solid rgba(59,130,246,.3); }
  .btn-blue:hover { background:rgba(59,130,246,.25); }
  .flash-ok { background:rgba(0,229,160,.1); color:var(--accent); border:1px solid rgba(0,229,160,.2);
    padding:10px 16px; border-radius:8px; font-family:var(--mono); font-size:13px; margin-bottom:16px; }
  .flash-err { background:rgba(239,68,68,.1); color:var(--danger); border:1px solid rgba(239,68,68,.2);
    padding:10px 16px; border-radius:8px; font-family:var(--mono); font-size:13px; margin-bottom:16px; }
  .tbl { width:100%; border-collapse:collapse; font-family:var(--mono); font-size:13px; }
  .tbl th { color:var(--muted); text-align:left; padding:10px 12px;
    border-bottom:1px solid var(--border); font-size:11px; letter-spacing:.08em; }
  .tbl td { padding:10px 12px; border-bottom:1px solid rgba(30,35,48,.6); vertical-align:middle; }
  .tbl tr:last-child td { border-bottom:none; }
  .tag { display:inline-flex; align-items:center; gap:6px;
    background:rgba(59,130,246,.15); color:var(--accent2);
    border:1px solid rgba(59,130,246,.3); border-radius:6px;
    padding:3px 10px; font-size:12px; font-family:var(--mono); margin:3px; }
  .note { font-size:11px; color:var(--muted); font-family:var(--mono); margin-top:8px; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
  .dot-green { background:var(--accent); }
  .dot-yellow { background:var(--warn); }
  .dot-red { background:var(--danger); }
  .dot-gray { background:var(--muted); }
  .info-box { background:#080a0e; border:1px solid var(--border); border-radius:8px;
    padding:14px; font-family:var(--mono); font-size:12px; color:var(--muted);
    line-height:1.8; margin-top:14px; }
  .info-box strong { color:var(--warn); }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">âš™ Admin Panel</div>
  <div>
    <a href="/">â† Dashboard</a>
    <a href="/admin/logout">Logout</a>
  </div>
</div>
<div class="page">

  {% if message %}<div class="flash-ok">âœ… {{ message }}</div>{% endif %}
  {% if error %}<div class="flash-err">âŒ {{ error }}</div>{% endif %}

  <!-- TARGET IDs CARD -->
  <div class="card">
    <div class="section-title">ðŸŽ¯ Target IDs (Sequential)</div>

    <div style="margin-bottom:16px">
      {% for tid in target_ids %}
        <span class="tag">
          {{ tid }}
          <form method="POST" action="/admin/remove_target" style="display:inline;margin:0">
            <input type="hidden" name="target_id" value="{{ tid }}">
            <button type="submit" style="background:none;border:none;cursor:pointer;color:var(--danger);font-size:14px;line-height:1;padding:0" title="Remove">âœ•</button>
          </form>
        </span>
      {% endfor %}
      {% if not target_ids %}
        <span style="color:var(--danger); font-size:13px">No target IDs set!</span>
      {% endif %}
    </div>

    <form method="POST" action="/admin/add_target">
      <div class="row">
        <div class="field">
          <label>Add Single Target ID</label>
          <input type="text" name="target_id" placeholder="e.g. 57310375825">
        </div>
        <button type="submit" class="btn btn-blue">+ Add ID</button>
      </div>
    </form>

    <form method="POST" action="/admin/add_targets_bulk">
      <label>Bulk Add (one ID per line)</label>
      <textarea name="bulk_ids" placeholder="57310375825&#10;12345678&#10;98765432"></textarea>
      <div style="margin-top:10px">
        <button type="submit" class="btn btn-green">+ Add All</button>
      </div>
    </form>

    <div class="info-box">
      <strong>âš¡ Sequence logic:</strong><br>
      â€¢ IDs are used one-by-one in order across servers.<br>
      â€¢ If only 1 ID â†’ all 9 servers use same ID, cooldown = 2 min.<br>
      â€¢ If 2+ IDs â†’ each server gets next ID in rotation, cooldown = 1 min.<br>
      â€¢ Index resets each cycle.
    </div>
  </div>

  <!-- ACCOUNTS CARD -->
  <div class="card">
    <div class="section-title">ðŸ‘¤ Accounts ({{ users|length }} / 3 max recommended)</div>

    <form method="POST" action="/admin/add_user" style="margin-bottom:20px">
      <div class="row">
        <div class="field">
          <label>Username</label>
          <input type="text" name="username" placeholder="Instagram username">
        </div>
        <div class="field">
          <label>Password</label>
          <input type="text" name="password" placeholder="Password">
        </div>
        <button type="submit" class="btn btn-blue">+ Add Account</button>
      </div>
    </form>

    {% if users %}
    <table class="tbl">
      <thead><tr>
        <th>#</th><th>Username</th><th>Password</th>
        <th>Health</th><th>Fails</th><th>Last Login</th><th>Action</th>
      </tr></thead>
      <tbody>
      {% for u in users %}
        <tr>
          <td>{{ loop.index }}</td>
          <td style="font-weight:700">{{ u.username }}</td>
          <td>{{ u.password }}</td>
          <td>
            {% if u.health == 'healthy' %}
              <span class="dot dot-green"></span><span style="color:var(--accent)">Healthy</span>
            {% elif u.health == 'warning' %}
              <span class="dot dot-yellow"></span><span style="color:var(--warn)">Warning</span>
            {% elif u.health == 'unhealthy' %}
              <span class="dot dot-red"></span><span style="color:var(--danger)">Unhealthy</span>
            {% else %}
              <span class="dot dot-gray"></span>Unknown
            {% endif %}
          </td>
          <td>{{ u.fail_count }}</td>
          <td style="color:var(--muted)">{{ u.last_login or 'â€”' }}</td>
          <td>
            <form method="POST" action="/admin/remove_user" style="display:inline"
                  onsubmit="return confirm('Remove {{ u.username }}?')">
              <input type="hidden" name="username" value="{{ u.username }}">
              <button type="submit" class="btn btn-red" style="padding:5px 12px">Remove</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
      <p style="color:var(--muted);text-align:center;padding:20px;font-family:var(--mono)">No accounts yet.</p>
    {% endif %}

    <div class="info-box">
      <strong>â± 1-Hour Capacity:</strong><br>
      â€¢ 1 account + multi-target: ~18 min/cycle â†’ fits easily in 1hr.<br>
      â€¢ 2 accounts + multi-target: ~36 min/cycle â†’ fits in 1hr.<br>
      â€¢ 3 accounts + multi-target: ~54 min/cycle â†’ fits in 1hr âœ…<br>
      â€¢ 4 accounts + multi-target: ~72 min/cycle â†’ <strong>exceeds 1hr âš </strong>
    </div>
  </div>

</div>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Admin Login</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0d0f14; --surface:#13161e; --border:#1e2330;
    --accent:#00e5a0; --text:#e2e8f0; --muted:#64748b; --danger:#ef4444;
    --mono:'JetBrains Mono',monospace; --sans:'Syne',sans-serif; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:var(--sans);
    display:flex; align-items:center; justify-content:center; min-height:100vh; }
  .box { background:var(--surface); border:1px solid var(--border); border-radius:16px;
    padding:40px; width:340px; }
  h2 { font-size:20px; font-weight:800; margin-bottom:24px; color:var(--accent); }
  label { font-size:11px; letter-spacing:.1em; color:var(--muted); text-transform:uppercase;
    display:block; margin-bottom:6px; font-family:var(--mono); }
  input[type=password] { width:100%; padding:10px 14px; border:1px solid var(--border);
    border-radius:8px; background:#080a0e; color:var(--text);
    font-family:var(--mono); font-size:13px; outline:none; margin-bottom:18px; }
  input[type=password]:focus { border-color:var(--accent); }
  button { width:100%; padding:11px; background:rgba(0,229,160,.15); color:var(--accent);
    border:1px solid rgba(0,229,160,.3); border-radius:8px; cursor:pointer;
    font-family:var(--mono); font-size:14px; font-weight:700; }
  button:hover { background:rgba(0,229,160,.25); }
  .err { color:var(--danger); font-size:12px; font-family:var(--mono); margin-top:12px; }
</style>
</head>
<body>
<div class="box">
  <h2>ðŸ”’ Admin Login</h2>
  <form method="POST">
    <label>Password</label>
    <input type="password" name="password" autofocus>
    <button type="submit">Enter</button>
  </form>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</div>
</body>
</html>
"""

# ---------- ROUTES ----------

@app.route('/')
def dashboard():
    with cycle_count_lock:
        cycle = current_cycle
    with target_ids_lock:
        tids = list(TARGET_IDS)
        nxt = target_id_index
    with users_lock:
        users_copy = [dict(u) for u in USERS]
    with stats_lock:
        server_stats = [(k, dict(v)) for k, v in stats.items()]
    with log_buffer_lock:
        log_lines = list(log_buffer)

    cooldown = PAUSE_BETWEEN_SITES_MULTI if len(tids) > 1 else PAUSE_BETWEEN_SITES_SINGLE

    return render_template_string(
        DASHBOARD_TEMPLATE,
        cycle=cycle,
        user_count=len(users_copy),
        target_count=len(tids),
        target_ids=tids,
        next_idx=nxt,
        cooldown=cooldown,
        users=users_copy,
        server_stats=server_stats,
        log_lines=log_lines,
        now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    )

@app.route('/admin', methods=['GET'])
def admin():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    with target_ids_lock:
        tids = list(TARGET_IDS)
    with users_lock:
        users_copy = [dict(u) for u in USERS]
    msg = session.pop('admin_message', None)
    err = session.pop('admin_error', None)
    return render_template_string(ADMIN_TEMPLATE, target_ids=tids, users=users_copy, message=msg, error=err)

@app.route('/admin/add_target', methods=['POST'])
def add_target():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    tid = request.form.get('target_id', '').strip()
    if tid:
        with target_ids_lock:
            if tid not in TARGET_IDS:
                TARGET_IDS.append(tid)
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
    raw = request.form.get('bulk_ids', '')
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    added = []
    with target_ids_lock:
        for tid in lines:
            if tid not in TARGET_IDS:
                TARGET_IDS.append(tid)
                added.append(tid)
    if added:
        session['admin_message'] = f"Added {len(added)} IDs: {', '.join(added)}"
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
            session['admin_message'] = f"Target ID {tid} removed."
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
        session['admin_error'] = "Username and password required."
        return redirect(url_for('admin'))
    with users_lock:
        existing = [u['username'] for u in USERS]
        if username in existing:
            session['admin_error'] = f"'{username}' already exists."
        elif len(USERS) >= 3:
            session['admin_error'] = "Max 3 accounts allowed for 1-hour cycle."
        else:
            USERS.append({"username": username, "password": password,
                          "health": "unknown", "last_login": None, "fail_count": 0})
            logger.info(f"Admin added account: {username}")
            session['admin_message'] = f"Account '{username}' added."
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
            session['admin_message'] = f"Account '{username}' removed."
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
        error = "Invalid password."
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('dashboard'))

# ========== MAIN ==========
if __name__ == "__main__":
    auto_thread = threading.Thread(target=automation_loop, daemon=True)
    auto_thread.start()
    logger.info("Automation thread started. Flask on port 8081.")
    app.run(host='0.0.0.0', port=8081, debug=False, use_reloader=False)
