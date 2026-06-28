"""
rover_report.py - daily Rover rank & pricing report, emailed to you.

Per the design doc: for each configured Austin zip, fetch page 1 of an overnight-
boarding search for tonight (today -> tomorrow) and email a report containing
  - your rank on page 1 per zip, or "NOT ON FIRST PAGE"
  - the median nightly price per zip
  - the overall median pooled across all zips

Read-only, page 1 only, a few spaced fetches per day. Run by cron once a day.

Setup:
    pip install playwright google-api-python-client google-auth-httplib2 google-auth-oauthlib
    playwright install chromium
    # credentials.json (Desktop OAuth client) next to this file, consent screen
    # published to "In production", then: python gmail_auth.py  (writes token_send.json)

Cron @ 8 AM Central (DST-safe via CRON_TZ). Use absolute paths -- cron's env is bare:
    CRON_TZ=America/Chicago
    0 8 * * * cd /home/USER/Repos/rover-automations && \
      /home/USER/Repos/rover-automations/.venv/bin/python rover_report.py >> cron.log 2>&1
"""

import os
import re
import csv
import time
import base64
import random
import statistics
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------- config ----------------
MY_SITTER_NAME = "Yujie Z."   # exactly as Rover shows it, e.g. "Malik Z."
EMAIL_TO = "malikzhangggg@gmail.com"                  # report recipient (your own inbox)
HEADLESS = True
DEBUG_VERIFY = True   # set True for ONE run to dump card order + save all screenshots,
                       # so you can confirm the rank match; leave False for normal runs.
DELAY = (12.0, 25.0)   # seconds between zip fetches
HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# (zip, neighborhood, captured search URL). Capture each URL once on rover.com and
# paste it in. Only start_date/end_date/page are rewritten at runtime.
ZIPS = [
    ("78753", "Windsor Hills", (
        "https://www.rover.com/search/?alternate_results=true&accepts_only_one_client=false"
        "&at_daycare_facility=true&apse=false&bathing_grooming=false&cat_care=false"
        "&centerlat=30.3889868&centerlng=-97.6710889&dogs_allowed_on_bed=false"
        "&dogs_allowed_on_furniture=false&end_date=2026-06-19&frequency=onetime"
        "&fulltime_availability=true&in_sitters_home=true&is_initial_search=false"
        "&is_premier=false&location=78753&medium_dogs=false&minprice=1&page=1&pet="
        "&pet_type=dog&raw_location_types=postal_code&service_type=overnight-boarding"
        "&start_date=2026-06-15&location_type=zip-code&dog_count=2&cat_count=0&puppy_count=0"
    )),
    ("78723", "Windsor Park", "PASTE_78723_URL"),
    ("78701", "Downtown",     "PASTE_78701_URL"),
    ("78757", "Crestview",    "PASTE_78757_URL"),
    ("78751", "Hyde Park",    "PASTE_78751_URL"),
]

RATE_RE = re.compile(r"\$\s?(\d{1,4})\s*(?:/|per\s+)\s*night", re.I)

# climb from each "night" text node to the card-sized block so it includes the name
JS_CARD = """
els => els.map(e => {
  let n = e;
  for (let k = 0; k < 6 && n.parentElement; k++) {
    const t = n.innerText || '';
    if (t.length > 40 && /night/i.test(t)) break;
    n = n.parentElement;
  }
  return n.innerText || n.textContent || '';
})
"""


# ---------------- scraping ----------------
def build_url(template, start, end):
    u = re.sub(r"(?<=[?&])start_date=[^&]*", f"start_date={start}", template)
    u = re.sub(r"(?<=[?&])end_date=[^&]*", f"end_date={end}", u)
    return re.sub(r"(?<=[?&])page=\d+", "page=1", u)


def extract_cards(page):
    blocks = page.eval_on_selector_all(
        "xpath=//*[not(self::script) and not(self::style)]"
        "[contains(translate(text(),'NIGHT','night'),'night')]",
        JS_CARD,
    )
    cards, seen = [], set()
    for text in blocks:
        text = " ".join((text or "").split())
        m = RATE_RE.search(text)
        if not m:
            continue
        key = text[:80]
        if key in seen:
            continue
        seen.add(key)
        cards.append((int(m.group(1)), text))
    return cards


def fetch_zip(page, z, url, force_shot):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("text=/night/i", timeout=30000)
    except PWTimeout:
        page.screenshot(path=os.path.join(HERE, f"shot_{z}.png"), full_page=True)  # on failure
        return None
    page.wait_for_timeout(2000)
    if force_shot:
        page.screenshot(path=os.path.join(HERE, f"shot_{z}.png"), full_page=True)
    return extract_cards(page)


# ---------------- email ----------------
def gmail_service():
    tok = os.path.join(HERE, "token_send.json")
    creds = Credentials.from_authorized_user_file(tok, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(tok, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("token_send.json invalid; re-run gmail_auth.py")
    return build("gmail", "v1", credentials=creds)


def send_email(service, to, subject, html, text):
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ---------------- report ----------------
def rank_cell(r):
    if r["status"] == "fail":
        return "fetch failed"
    return f"#{r['rank']}" if r["rank"] else "NOT ON FIRST PAGE"


def med_cell(r):
    return f"${r['median']:g}" if r["median"] is not None else "&mdash;"


def build_report(day, results, agg_med, pooled_n, missing):
    rows = ""
    for r in results:
        rank = rank_cell(r)
        color = "#b00" if rank in ("NOT ON FIRST PAGE", "fetch failed") else "#222"
        rows += (
            "<tr>"
            f"<td>{r['zip']}</td><td>{r['hood']}</td>"
            f"<td style='text-align:center;color:{color}'>{rank}</td>"
            f"<td style='text-align:right'>{med_cell(r)}</td>"
            f"<td style='text-align:right'>{r['n']}</td></tr>"
        )
    miss = ""
    if missing:
        miss = f"<p style='color:#b00'>No URL configured yet for: {', '.join(missing)}</p>"
    html = (
        '<html><body style="font-family:system-ui,Arial,sans-serif;color:#222">'
        f"<h2>Rover daily report &mdash; {day}</h2>"
        f"<p>Overnight boarding, tonight ({day} &rarr; next day), page 1.</p>"
        '<table cellpadding="6" border="1" style="border-collapse:collapse">'
        '<tr style="background:#f3f3f3"><th>Zip</th><th>Area</th><th>Your rank</th>'
        "<th>Median / night</th><th>Sitters</th></tr>"
        f"{rows}</table>"
        f'<p style="margin-top:14px"><b>Overall (pooled across all areas):</b> '
        f"median {('$'+format(agg_med,'g')) if agg_med is not None else '&mdash;'} "
        f"/ night over {pooled_n} sitters.</p>"
        f"{miss}"
        '<p style="color:#888;font-size:12px">Rank reflects displayed order, '
        "including sponsored placements.</p></body></html>"
    )

    def tr(r):
        if r["status"] == "fail":
            return "fetch failed"
        return f"#{r['rank']}" if r["rank"] else "NOT ON FIRST PAGE"

    def tm(r):
        return f"${r['median']:g}" if r["median"] is not None else "-"

    lines = [f"Rover daily report - {day}", "Overnight boarding, tonight, page 1.", ""]
    for r in results:
        lines.append(f"  {r['zip']} {r['hood']}: rank {tr(r)}  median {tm(r)}  ({r['n']} sitters)")
    agg = f"${agg_med:g}" if agg_med is not None else "-"
    lines += ["", f"Overall pooled median: {agg}/night over {pooled_n} sitters."]
    if missing:
        lines.append(f"(no URL yet for: {', '.join(missing)})")
    return html, "\n".join(lines)


def log_csv(day, results, agg_med):
    path = os.path.join(HERE, "report_log.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "zip", "neighborhood", "rank", "median", "n_sitters"])
        for r in results:
            if r["status"] == "fail":
                rank = "FAIL"
            else:
                rank = r["rank"] if r["rank"] else "NOT ON FIRST PAGE"
            med = r["median"] if r["median"] is not None else ""
            w.writerow([day, r["zip"], r["hood"], rank, med, r["n"]])
        w.writerow([day, "ALL", "AGGREGATE", "", agg_med if agg_med is not None else "", ""])


# ---------------- main ----------------
def main():
    if "PUT YOUR ROVER NAME" in MY_SITTER_NAME or "you@example.com" in EMAIL_TO:
        print("Set MY_SITTER_NAME and EMAIL_TO at the top first.")
        return

    configured = [(z, h, u) for (z, h, u) in ZIPS if "PASTE_" not in u]
    missing = [z for (z, h, u) in ZIPS if "PASTE_" in u]
    if not configured:
        print("No zip URLs configured -- paste captured search URLs into ZIPS.")
        return

    today = date.today()
    start, end = today.isoformat(), (today + timedelta(days=1)).isoformat()

    results, pooled = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_context(user_agent=UA).new_page()
        for i, (z, hood, template) in enumerate(configured):
            cards = fetch_zip(page, z, build_url(template, start, end), DEBUG_VERIFY)
            if cards is None:
                results.append({"zip": z, "hood": hood, "status": "fail",
                                "rank": None, "median": None, "n": 0})
            else:
                prices = [pr for pr, _ in cards]
                rank = next((j for j, (_, t) in enumerate(cards, 1)
                             if MY_SITTER_NAME.lower() in t.lower()), None)
                results.append({"zip": z, "hood": hood, "status": "ok", "rank": rank,
                                "median": statistics.median(prices) if prices else None,
                                "n": len(prices)})
                pooled.extend(prices)
                if DEBUG_VERIFY:
                    print(f"\n[{z} {hood}]")
                    for j, (price, text) in enumerate(cards, 1):
                        mark = "  <-- YOU" if rank == j else ""
                        print(f"  #{j:>2}  ${price:>4}  {text[:55]}{mark}")
            if i < len(configured) - 1:
                time.sleep(random.uniform(*DELAY))
        browser.close()

    agg_med = statistics.median(pooled) if pooled else None
    subject = f"Rover daily \u2014 {start}: ranks & median pricing"
    html, text = build_report(start, results, agg_med, len(pooled), missing)
    log_csv(start, results, agg_med)

    try:
        send_email(gmail_service(), EMAIL_TO, subject, html, text)
        print(f"[{start}] report emailed to {EMAIL_TO}")
    except Exception as e:
        print(f"[{start}] email failed: {e}\n\n{text}")  # keep the data in cron.log


if __name__ == "__main__":
    main()
