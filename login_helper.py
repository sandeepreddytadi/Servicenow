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
