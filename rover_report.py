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

import netprefs
# Prefer IPv4 before any Google API networking. On the bridged VM broken IPv6 otherwise
# stalls the Gmail send ~16s and intermittently trips its timeout ("email failed:
# timed out"). Must run before the API client resolves googleapis.com. Do not remove.
netprefs.apply()

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------- config ----------------
MY_SITTER_NAME = "Yujie Z."   # exactly as Rover shows it, e.g. "Malik Z."
EMAIL_TO = "malikzhangggg@gmail.com"                  # report recipient (your own inbox)
HEADLESS = True
DEBUG_VERIFY = False   # set True for ONE run to dump card order + save all screenshots,
                       # so you can confirm the rank match; leave False for normal runs.
DELAY = (45.0, 120.0)  # seconds between zip fetches -- minutes-scale spacing is the
                       # main lever against Cloudflare's burst detection. Widen if needed;
                       # a daily report doesn't care that the run takes several minutes.
GOTO_RETRIES = 3           # load attempts per zip before giving up on it.
                           # (Was effectively 1 and UNGUARDED -> a single goto timeout
                           #  crashed the whole run and suppressed the email, 2026-07-27.)
GOTO_BACKOFF = (5.0, 15.0) # seconds (jittered) to wait between load retries
HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# (zip, neighborhood, captured search URL). Capture each URL once on rover.com and
# paste it in. Only start_date/end_date/page are rewritten at runtime.
ZIPS = [
    ("78753", "Windsor Hills", "https://www.rover.com/search/?alternate_results=true&accepts_only_one_client=false&apse=false&bathing_grooming=false&cat_care=false&centerlat=30.3889868&centerlng=-97.6710889&dogs_allowed_on_bed=false&dogs_allowed_on_furniture=false&end_date=2026-06-29&frequency=onetime&morning_availability=false&midday_availability=false&evening_availability=false&fulltime_availability=true&monday=false&tuesday=false&wednesday=false&thursday=false&friday=false&saturday=false&sunday=false&giant_dogs=false&has_fenced_yard=false&has_house=false&has_no_children=false&in_sitters_home=true&is_initial_search=false&is_premier=false&knows_first_aid=false&large_dogs=false&location=78753&medium_dogs=true&minprice=1&no_caged_pets=false&no_cats=false&no_children_0_5=false&no_children_6_12=false&non_smoking=false&page=1&person_does_not_have_dogs=false&pet=&petsitusa=false&pet_type=dog&puppy=false&raw_location_types=postal_code&service_type=overnight-boarding&small_dogs=false&spaces_required=1&star_sitter=false&start_date=2026-06-28&search_score_debug=false&injected_medication=false&search_current_provider=false&in_unlaunched_country=false&special_needs=false&oral_medication=false&more_than_one_client=false&uncrated_dogs=false&unspayed_females=false&non_neutered_males=false&females_in_heat=false&premier_matching=false&premier_or_rover_match=false&is_member_of_sitter_to_sitter=false&is_member_of_sitter_to_sitter_plus=false&is_accepting_new_recurring_clients=false&has_low_booking_rate=false&location_type=zip-code&change_source=search-modal&dog_size=medium&dog_count=2&cat_count=0&puppy_count=0"),
    ("78723", "Windsor Park", "https://www.rover.com/search/?alternate_results=true&accepts_only_one_client=false&apse=false&bathing_grooming=false&cat_care=false&centerlat=30.3081307&centerlng=-97.68194299999999&dogs_allowed_on_bed=false&dogs_allowed_on_furniture=false&end_date=2026-06-29&frequency=onetime&morning_availability=false&midday_availability=false&evening_availability=false&fulltime_availability=true&monday=false&tuesday=false&wednesday=false&thursday=false&friday=false&saturday=false&sunday=false&giant_dogs=false&has_fenced_yard=false&has_house=false&has_no_children=false&in_sitters_home=true&is_initial_search=false&is_premier=false&knows_first_aid=false&large_dogs=false&location=Austin%2C%20TX%2078723%2C%20USA&location_accuracy=5161&medium_dogs=true&minprice=1&no_caged_pets=false&no_cats=false&no_children_0_5=false&no_children_6_12=false&non_smoking=false&page=1&person_does_not_have_dogs=false&pet=&petsitusa=false&pet_type=dog&puppy=false&raw_location_types=postal_code&service_type=overnight-boarding&small_dogs=false&spaces_required=1&star_sitter=false&start_date=2026-06-28&search_score_debug=false&injected_medication=false&search_current_provider=false&in_unlaunched_country=false&special_needs=false&oral_medication=false&more_than_one_client=false&uncrated_dogs=false&unspayed_females=false&non_neutered_males=false&females_in_heat=false&premier_matching=false&premier_or_rover_match=false&is_member_of_sitter_to_sitter=false&is_member_of_sitter_to_sitter_plus=false&is_accepting_new_recurring_clients=false&has_low_booking_rate=false&location_type=zip-code&change_source=search-modal&dog_size=medium&dog_count=2&cat_count=0&puppy_count=0"),
    ("78701", "Downtown", "https://www.rover.com/search/?alternate_results=true&accepts_only_one_client=false&apse=false&bathing_grooming=false&cat_care=false&centerlat=30.2729209&centerlng=-97.74438630000002&dogs_allowed_on_bed=false&dogs_allowed_on_furniture=false&end_date=2026-06-29&frequency=onetime&morning_availability=false&midday_availability=false&evening_availability=false&fulltime_availability=true&monday=false&tuesday=false&wednesday=false&thursday=false&friday=false&saturday=false&sunday=false&giant_dogs=false&has_fenced_yard=false&has_house=false&has_no_children=false&in_sitters_home=true&is_initial_search=false&is_premier=false&knows_first_aid=false&large_dogs=false&location=78701&medium_dogs=true&minprice=1&no_caged_pets=false&no_cats=false&no_children_0_5=false&no_children_6_12=false&non_smoking=false&page=1&person_does_not_have_dogs=false&pet=&petsitusa=false&pet_type=dog&puppy=false&raw_location_types=postal_code&service_type=overnight-boarding&small_dogs=false&spaces_required=1&star_sitter=false&start_date=2026-06-28&search_score_debug=false&injected_medication=false&search_current_provider=false&in_unlaunched_country=false&special_needs=false&oral_medication=false&more_than_one_client=false&uncrated_dogs=false&unspayed_females=false&non_neutered_males=false&females_in_heat=false&premier_matching=false&premier_or_rover_match=false&is_member_of_sitter_to_sitter=false&is_member_of_sitter_to_sitter_plus=false&is_accepting_new_recurring_clients=false&has_low_booking_rate=false&location_type=zip-code&change_source=search-modal&dog_size=medium&dog_count=2&cat_count=0&puppy_count=0"),
    ("78757", "Crestview", "https://www.rover.com/search/?alternate_results=true&accepts_only_one_client=false&apse=false&bathing_grooming=false&cat_care=false&centerlat=30.3568213&centerlng=-97.730807&dogs_allowed_on_bed=false&dogs_allowed_on_furniture=false&end_date=2026-06-29&frequency=onetime&morning_availability=false&midday_availability=false&evening_availability=false&fulltime_availability=true&monday=false&tuesday=false&wednesday=false&thursday=false&friday=false&saturday=false&sunday=false&giant_dogs=false&has_fenced_yard=false&has_house=false&has_no_children=false&in_sitters_home=true&is_initial_search=false&is_premier=false&knows_first_aid=false&large_dogs=false&location=78757&medium_dogs=true&minprice=1&no_caged_pets=false&no_cats=false&no_children_0_5=false&no_children_6_12=false&non_smoking=false&page=1&person_does_not_have_dogs=false&pet=&petsitusa=false&pet_type=dog&puppy=false&raw_location_types=postal_code&service_type=overnight-boarding&small_dogs=false&spaces_required=1&star_sitter=false&start_date=2026-06-28&search_score_debug=false&injected_medication=false&search_current_provider=false&in_unlaunched_country=false&special_needs=false&oral_medication=false&more_than_one_client=false&uncrated_dogs=false&unspayed_females=false&non_neutered_males=false&females_in_heat=false&premier_matching=false&premier_or_rover_match=false&is_member_of_sitter_to_sitter=false&is_member_of_sitter_to_sitter_plus=false&is_accepting_new_recurring_clients=false&has_low_booking_rate=false&location_type=zip-code&change_source=search-modal&dog_size=medium&dog_count=2&cat_count=0&puppy_count=0"),
    ("78751", "Hyde Park", "https://www.rover.com/search/?alternate_results=true&accepts_only_one_client=false&apse=false&bathing_grooming=false&cat_care=false&centerlat=30.3055711&centerlng=-97.725376&dogs_allowed_on_bed=false&dogs_allowed_on_furniture=false&end_date=2026-06-29&frequency=onetime&morning_availability=false&midday_availability=false&evening_availability=false&fulltime_availability=true&monday=false&tuesday=false&wednesday=false&thursday=false&friday=false&saturday=false&sunday=false&giant_dogs=false&has_fenced_yard=false&has_house=false&has_no_children=false&in_sitters_home=true&is_initial_search=false&is_premier=false&knows_first_aid=false&large_dogs=false&location=78751&medium_dogs=true&minprice=1&no_caged_pets=false&no_cats=false&no_children_0_5=false&no_children_6_12=false&non_smoking=false&page=1&person_does_not_have_dogs=false&pet=&petsitusa=false&pet_type=dog&puppy=false&raw_location_types=postal_code&service_type=overnight-boarding&small_dogs=false&spaces_required=1&star_sitter=false&start_date=2026-06-28&search_score_debug=false&injected_medication=false&search_current_provider=false&in_unlaunched_country=false&special_needs=false&oral_medication=false&more_than_one_client=false&uncrated_dogs=false&unspayed_females=false&non_neutered_males=false&females_in_heat=false&premier_matching=false&premier_or_rover_match=false&is_member_of_sitter_to_sitter=false&is_member_of_sitter_to_sitter_plus=false&is_accepting_new_recurring_clients=false&has_low_booking_rate=false&location_type=zip-code&change_source=search-modal&dog_size=medium&dog_count=2&cat_count=0&puppy_count=0"),
]

RATE_RE = re.compile(r"\$\s?(\d{1,4})\s*(?:/|per\s+)\s*night", re.I)

# Get the WHOLE card block (name + rating + rate), not just the price line.
# Rover wraps each result card in one <a> to the sitter profile, so the card-level
# anchor is the reliable container. Fall back to climbing if there's no such anchor.
JS_CARD = """
els => els.map(e => {
  const a = e.closest('a');
  if (a) {
    const t = a.innerText || '';
    if (/per\\s+night/i.test(t) && t.length < 600) return t;  // single card
  }
  let n = e;
  for (let k = 0; k < 8 && n.parentElement; k++) {
    n = n.parentElement;
    const t = n.innerText || '';
    if (/per\\s+night/i.test(t) && t.length > 80) return t;
  }
  return (n.innerText || n.textContent || '');
})
"""


# ---------------- scraping ----------------
def build_url(template, start, end):
    u = re.sub(r"(?<=[?&])start_date=[^&]*", f"start_date={start}", template)
    u = re.sub(r"(?<=[?&])end_date=[^&]*", f"end_date={end}", u)
    return re.sub(r"(?<=[?&])page=\d+", "page=1", u)


_BADGES = {"star sitter", "premier", "featured", "sponsored", "new", "verified enhanced background check"}


def card_name(block):
    """Best-effort sitter name = first plausible line of the card block."""
    for line in block.splitlines():
        s = line.strip()
        low = s.lower()
        if not s or low in _BADGES:
            continue
        if low.startswith("from $") or s.startswith("$"):
            continue
        if re.match(r"^[\d.]+(\s|$|\u00b7)", s):   # rating line like "5.0 ·"
            continue
        return s
    return "(name?)"


def extract_cards(page):
    """Return (price, name, normalized_text) per card, in display order."""
    blocks = page.eval_on_selector_all(
        "xpath=//*[not(self::script) and not(self::style)]"
        "[contains(translate(text(),'NIGHT','night'),'night')]",
        JS_CARD,
    )
    cards, seen = [], set()
    for raw in blocks:
        norm = " ".join((raw or "").split())
        m = RATE_RE.search(norm)
        if not m:
            continue
        key = norm[:80]            # now name-prefixed, so distinct sitters stay distinct
        if key in seen:
            continue
        seen.add(key)
        cards.append((int(m.group(1)), card_name(raw or ""), norm))
    return cards


def fetch_zip(page, z, url, force_shot):
    """Load one zip's page-1 search and return its cards.

    Returns a list of (price, name, norm), or None if the zip couldn't be loaded
    after GOTO_RETRIES attempts. NEVER raises: a single flaky zip (goto timeout,
    Cloudflare stall, no results) must degrade to a "fetch failed" row, not abort
    the whole report. The unguarded goto here is what killed the 2026-07-27 run.
    """
    for attempt in range(1, GOTO_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("text=/night/i", timeout=30000)
            break  # page loaded and results are present
        except PWTimeout as e:
            print(f"  [{z}] load timeout on attempt {attempt}/{GOTO_RETRIES}: {e}")
            if attempt < GOTO_RETRIES:
                page.wait_for_timeout(int(random.uniform(*GOTO_BACKOFF) * 1000))
                continue
            # Give up on THIS zip only; capture evidence for later inspection.
            try:
                page.screenshot(path=os.path.join(HERE, f"shot_{z}.png"), full_page=True)
            except Exception:
                pass
            return None
    page.wait_for_timeout(2000)
    if force_shot:
        try:
            page.screenshot(path=os.path.join(HERE, f"shot_{z}.png"), full_page=True)
        except Exception:
            pass
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


SEND_RETRIES = 3            # send attempts before giving up (network can be flaky).
SEND_BACKOFF = (5, 15)      # seconds slept after attempt 1, then attempt 2.


def send_email(service, to, subject, html, text):
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    # Retry the network call: a single transient timeout (broken-IPv6 stall, brief
    # googleapis blip) shouldn't cost the whole day's email. IPv4 preference above
    # makes this rare; the retry covers the residue. Re-raise on final failure so the
    # caller still logs the report to cron.log.
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            service.users().messages().send(
                userId="me", body={"raw": raw}).execute()
            return
        except Exception as e:
            if attempt == SEND_RETRIES:
                raise
            wait = SEND_BACKOFF[attempt - 1]
            print(f"  [send] attempt {attempt}/{SEND_RETRIES} failed: {e}; "
                  f"retrying in {wait}s")
            time.sleep(wait)


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
        for i, (z, hood, template) in enumerate(configured):
            # fresh context per zip = fresh cookies/session, so a "challenged" flag
            # from one zip doesn't ride along into the next (a clean first-visit each time)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()
            try:
                cards = fetch_zip(page, z, build_url(template, start, end), DEBUG_VERIFY)
            except Exception as e:
                # fetch_zip is designed not to raise, but if anything unexpected
                # slips through, this ONE zip fails instead of the whole run.
                print(f"  [{z}] unexpected error, skipping zip: {e}")
                cards = None
            finally:
                context.close()
            if cards is None:
                results.append({"zip": z, "hood": hood, "status": "fail",
                                "rank": None, "median": None, "n": 0})
            else:
                prices = [pr for pr, _, _ in cards]
                rank = next((j for j, (_, _nm, norm) in enumerate(cards, 1)
                             if MY_SITTER_NAME.lower() in norm.lower()), None)
                results.append({"zip": z, "hood": hood, "status": "ok", "rank": rank,
                                "median": statistics.median(prices) if prices else None,
                                "n": len(prices)})
                pooled.extend(prices)
                if DEBUG_VERIFY:
                    print(f"\n[{z} {hood}]")
                    for j, (price, name, norm) in enumerate(cards, 1):
                        mark = "  <-- YOU" if rank == j else ""
                        print(f"  #{j:>2}  ${price:>4}  {name[:24]:<24} | {norm[:45]}{mark}")
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