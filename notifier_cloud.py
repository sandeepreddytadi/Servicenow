"""
login_helper.py  —  SSO / MFA Cookie Exporter
-----------------------------------------------
Run this script ONCE on your local machine whenever your SSO session
expires. It opens a real Chrome window so you can complete SSO / MFA
normally, then exports your session cookies as a JSON string that you
paste into GitHub Secrets as SNOW_SESSION_COOKIES.

Requirements (install once):
  pip install playwright
  python -m playwright install chromium

Usage:
  python login_helper.py
"""

import json
import os
import sys
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright is not installed. Run:")
    print("  pip install playwright")
    print("  python -m playwright install chromium")
    sys.exit(1)

DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_FILE = os.path.join(DIR, "snow_cookies_export.json")


def main():
    snow_url = input(
        "Enter your ServiceNow URL  (e.g. https://acme.service-now.com): "
    ).strip().rstrip("/")

    if not snow_url.startswith("http"):
        print("[ERROR] URL must start with https://")
        sys.exit(1)

    snow_domain = urlparse(snow_url).hostname
    print(f"\n[INFO] Opening Chrome browser for  {snow_url}")
    print("[INFO] Complete your SSO / MFA login in the browser window.")
    print("[INFO] Once you can see the ServiceNow home page, come back here.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        context = browser.new_context()
        page = context.new_page()
        page.goto(snow_url)

        input(">>> Press ENTER here AFTER you have fully logged in to ServiceNow... ")

        all_cookies = context.cookies()
        browser.close()

    # Keep only cookies for the ServiceNow domain
    snow_cookies = [
        c for c in all_cookies
        if snow_domain in c.get("domain", "").lstrip(".")
        or c.get("domain", "").lstrip(".") in snow_domain
    ]

    if not snow_cookies:
        print("\n[WARNING] No cookies found for the ServiceNow domain.")
        print("          Using ALL captured cookies instead (may include SSO provider cookies).")
        snow_cookies = all_cookies

    cookies_json = json.dumps(snow_cookies)

    # Save locally (gitignored)
    with open(EXPORT_FILE, "w") as f:
        json.dump(snow_cookies, f, indent=2)

    print("\n" + "=" * 65)
    print(" SUCCESS — Copy the JSON below into GitHub Secrets")
    print("=" * 65)
    print(f"\n  Secret name:  SNOW_SESSION_COOKIES")
    print(f"  Secret value: (see below — it's long)\n")
    print(cookies_json)
    print("\n" + "=" * 65)
    print(f"\n[INFO] Also saved to: {EXPORT_FILE}  (this file is gitignored)")
    print("\nHow to set the GitHub Secret:")
    print("  1. Go to your GitHub repo")
    print("  2. Settings → Secrets and variables → Actions")
    print("  3. New repository secret")
    print("  4. Name:  SNOW_SESSION_COOKIES")
    print("  5. Value: paste the JSON printed above")
    print("\nNote: SSO cookies typically expire every 8–30 days depending on")
    print("      your organisation's policy. Re-run this script when they do.")
    print("      The notifier will send a Teams alert when expiry is detected.\n")


if __name__ == "__main__":
    main()

"""
ServiceNow → Teams Notifier  (cloud / GitHub Actions edition)
--------------------------------------------------------------
Runs ONCE per invocation. All config comes from environment variables
so credentials never touch the code — set them as GitHub Secrets.

TWO AUTH MODES — set whichever applies to your ServiceNow instance:

  Standard login (username + password form):
    SNOW_URL            https://your-company.service-now.com
    SNOW_USERNAME       your ServiceNow login username
    SNOW_PASSWORD       your ServiceNow password
    TEAMS_WEBHOOK_URL   Teams Incoming Webhook URL

  SSO / Azure AD / Okta (browser-based login with MFA):
    SNOW_URL              https://your-company.service-now.com
    SNOW_USERNAME         your username (still needed for ticket query)
    SNOW_SESSION_COOKIES  JSON cookie string exported by login_helper.py
    TEAMS_WEBHOOK_URL     Teams Incoming Webhook URL

    → Run login_helper.py locally once to generate SNOW_SESSION_COOKIES.
    → SSO cookies typically last 8–30 days depending on your org's policy.
    → When they expire the script sends a Teams alert asking you to refresh.

State is persisted in known_tickets.json which is committed back to the
repo after each run by the GitHub Actions workflow.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "known_tickets.json")

PRIORITY_LABELS = {
    "1": "1 - Critical",
    "2": "2 - High",
    "3": "3 - Moderate",
    "4": "4 - Low",
    "5": "5 - Planning",
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def require_env(name):
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"[ERROR] Environment variable '{name}' is not set.")
        sys.exit(1)
    return val


def load_known_tickets():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_known_tickets(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── ServiceNow ─────────────────────────────────────────────────────────────

def _session_base():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    return s


def create_session_standard(snow_url, username, password):
    """Form-based login — works when ServiceNow uses its own username/password."""
    session = _session_base()
    session.get(f"{snow_url}/login.do", timeout=30)

    resp = session.post(
        f"{snow_url}/login.do",
        data={
            "user_name": username,
            "user_password": password,
            "sys_action": "sysverb_login",
            "ni.simple": "true",
        },
        allow_redirects=True,
        timeout=30,
    )

    if "login.do" in resp.url:
        raise RuntimeError(
            "Standard login failed — check SNOW_USERNAME / SNOW_PASSWORD, "
            "or your instance may require SSO (use SNOW_SESSION_COOKIES instead)."
        )

    print(f"[{now()}] Logged in via standard form as '{username}'")
    return session


def create_session_sso(snow_url, cookies_json):
    """
    Cookie-based auth — for SSO/MFA instances.
    Loads cookies exported by login_helper.py, then verifies the session
    is still valid.  Raises a descriptive error if cookies have expired.
    """
    session = _session_base()
    snow_domain = urlparse(snow_url).hostname  # e.g. acme.service-now.com

    try:
        cookies = json.loads(cookies_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SNOW_SESSION_COOKIES is not valid JSON: {exc}")

    loaded = 0
    for c in cookies:
        c_domain = c.get("domain", "").lstrip(".")
        if c_domain in snow_domain or snow_domain in c_domain or not c_domain:
            session.cookies.set(c["name"], c["value"])
            loaded += 1

    if loaded == 0:
        raise RuntimeError(
            "No matching cookies found for this ServiceNow domain. "
            "Re-run login_helper.py and update the SNOW_SESSION_COOKIES secret."
        )

    # Verify the session is alive by hitting the home page
    check = session.get(f"{snow_url}/now/nav/ui/classic/params/target/home.do",
                        allow_redirects=True, timeout=30)
    if "login.do" in check.url or check.status_code in (401, 403):
        raise SessionExpiredError(
            "SSO session cookies have expired. "
            "Re-run login_helper.py locally and update the SNOW_SESSION_COOKIES secret."
        )

    print(f"[{now()}] Session restored via SSO cookies ({loaded} cookie(s) loaded)")
    return session


class SessionExpiredError(RuntimeError):
    """Raised when stored SSO cookies are no longer valid."""


def fetch_assigned_tickets(session, snow_url, username):
    query = (
        f"assigned_to.user_name={username}"
        "^active=true"
        "^ORDERBYDESCsys_created_on"
    )
    params = {
        "JSON": "",
        "sysparm_query": query,
        "sysparm_fields": (
            "number,short_description,priority,state,"
            "sys_id,opened_at,sys_class_name,caller_id"
        ),
        "sysparm_limit": "100",
    }
    resp = session.get(f"{snow_url}/task_list.do", params=params, timeout=30)
    resp.raise_for_status()

    try:
        return resp.json().get("records", [])
    except ValueError:
        raise RuntimeError("Could not parse ServiceNow response — session may have expired.")


# ── Teams ───────────────────────────────────────────────────────────────────

def build_ticket_card(ticket, snow_url):
    sys_id = ticket.get("sys_id", "")
    ticket_url = f"{snow_url}/nav_to.do?uri=task.do%3Fsys_id%3D{sys_id}"

    priority = PRIORITY_LABELS.get(str(ticket.get("priority", "")), ticket.get("priority", "N/A"))
    class_label = ticket.get("sys_class_name", "Task").replace("_", " ").title()

    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": f"New {class_label} assigned: {ticket.get('number', 'N/A')}",
        "sections": [
            {
                "activityTitle": (
                    f"**New {class_label} Assigned to You: "
                    f"{ticket.get('number', 'N/A')}**"
                ),
                "activitySubtitle": ticket.get("short_description", "No description"),
                "facts": [
                    {"name": "Type",        "value": class_label},
                    {"name": "Priority",    "value": priority},
                    {"name": "State",       "value": ticket.get("state", "N/A")},
                    {"name": "Opened",      "value": ticket.get("opened_at", "N/A")},
                ],
                "markdown": True,
            }
        ],
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "Open in ServiceNow",
                "targets": [{"os": "default", "uri": ticket_url}],
            }
        ],
    }


def build_expired_cookie_alert(error_msg):
    """Card sent to Teams when SSO cookies have expired."""
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "FF0000",
        "summary": "ServiceNow session expired — action required",
        "sections": [
            {
                "activityTitle": "**ServiceNow Session Expired**",
                "activitySubtitle": (
                    "Your SSO cookies have expired. "
                    "The notifier cannot check for new tickets until you refresh them."
                ),
                "facts": [
                    {"name": "Error",  "value": error_msg},
                    {"name": "Fix",    "value": "Run `login_helper.py` locally, then update the `SNOW_SESSION_COOKIES` GitHub Secret."},
                    {"name": "Where",  "value": "GitHub repo → Settings → Secrets and variables → Actions"},
                ],
                "markdown": True,
            }
        ],
    }


def post_to_teams(webhook_url, card):
    resp = requests.post(webhook_url, json=card, timeout=10)
    if resp.status_code != 200:
        print(f"[{now()}] Teams POST failed: HTTP {resp.status_code} – {resp.text[:200]}")
    return resp.status_code == 200


# ── main ────────────────────────────────────────────────────────────────────

def main():
    snow_url        = require_env("SNOW_URL").rstrip("/")
    username        = require_env("SNOW_USERNAME")
    webhook_url     = require_env("TEAMS_WEBHOOK_URL")
    cookies_json    = os.environ.get("SNOW_SESSION_COOKIES", "").strip()
    password        = os.environ.get("SNOW_PASSWORD", "").strip()

    # Decide auth mode
    use_sso = bool(cookies_json)
    if not use_sso and not password:
        print("[ERROR] Set either SNOW_SESSION_COOKIES (SSO) or SNOW_PASSWORD (standard login).")
        sys.exit(1)

    print(f"[{now()}] ── ServiceNow cloud check started  "
          f"[{'SSO cookie' if use_sso else 'standard login'} mode] ──")

    # ── Authenticate ──────────────────────────────────────────────────────
    try:
        if use_sso:
            session = create_session_sso(snow_url, cookies_json)
        else:
            session = create_session_standard(snow_url, username, password)
    except SessionExpiredError as exc:
        print(f"[{now()}] SSO cookies expired: {exc}")
        post_to_teams(webhook_url, build_expired_cookie_alert(str(exc)))
        sys.exit(0)   # exit 0 so the workflow doesn't mark as failed

    # ── Fetch tickets ─────────────────────────────────────────────────────
    tickets = fetch_assigned_tickets(session, snow_url, username)
    print(f"[{now()}] Found {len(tickets)} active assigned ticket(s)")

    known     = load_known_tickets()
    new_count = 0

    for ticket in tickets:
        key = ticket.get("sys_id") or ticket.get("number")
        if not key or key in known:
            continue

        desc = ticket.get("short_description", "")[:70]
        print(f"[{now()}] NEW → {ticket.get('number')}  {desc}")

        if post_to_teams(webhook_url, build_ticket_card(ticket, snow_url)):
            known[key] = {
                "number":      ticket.get("number"),
                "description": ticket.get("short_description", "")[:120],
                "notified_at": now(),
            }
            new_count += 1
            print(f"[{now()}] Teams notification sent for {ticket.get('number')} ✓")

    save_known_tickets(known)

    if new_count == 0:
        print(f"[{now()}] No new assignments — nothing to notify.")
    else:
        print(f"[{now()}] Done — {new_count} notification(s) sent.")


if __name__ == "__main__":
    main()
