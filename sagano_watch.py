#!/usr/bin/env python3
"""Seat-availability watcher for the Sagano Romantic Train.

Read-only: it loads public product pages and sends push notifications.
It never selects a date, starts a booking, or pays.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DEFAULT_DATES = [f"2026-11-{d}" for d in range(22, 28)]
BOOKING_URL = "https://ars-saganokanko.triplabo.jp/activity/ja/{}"
# (activity id, label shown in alerts; the main product has no label)
PRODUCTS = [
    ("LINKTIVITY-YRBTL", ""),
    ("LINKTIVITY-G6V4C", "Diorama Kyoto set"),
    ("LINKTIVITY-JVVXN", "Ninna-ji light-up set"),
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# The page never reaches network idle while ad trackers run, so only load the
# booking site's own hosts. This also keeps the watcher out of its analytics.
ALLOWED_HOST = re.compile(r"(^|\.)(triplabo\.jp|linktivity\.io)$")
BLOCKED_RESOURCES = {"image", "media", "font"}
INTERVAL_SECONDS = 15 * 60
REALERT_AFTER = timedelta(hours=2)
FAILURES_BEFORE_ALERT = 4
MAX_MESSAGE_LINES = 40  # ntfy turns bodies over 4096 bytes into attachments
READ_RETRY_SECONDS = 10
NETWORK_IDLE_MS = 15_000
GOTO_TIMEOUT_MS = 45_000

# Vue 2 exposes each component on its root element as __vue__.
READ_VUE_JS = r"""() => {
  const seen = new Set(); let days = null, plans = null;
  for (const el of document.querySelectorAll('*')) {
    let vm = el.__vue__;
    while (vm && !seen.has(vm)) {
      seen.add(vm);
      try {
        const d = vm.dateAvailabilities, p = vm.plans;
        if (!days && Array.isArray(d) && d.length) days = d;
        if (!plans && Array.isArray(p) && p.length)
          plans = p.map(x => ({id: x.id, title: x.title}));
      } catch (e) {}
      vm = vm.$parent;
    }
  }
  return {days, plans};
}"""


class CheckError(Exception):
    """The site could not be read, or its data was not the expected shape."""


@dataclass(frozen=True)
class Hit:
    activity: str
    label: str
    date: str
    plan_id: str
    direction: str
    time: str  # "HH:MM", or "" when the product only reports a per-day count
    quantity: int

    @property
    def key(self) -> str:
        return "|".join((self.activity, self.date, self.plan_id, self.time))


# ---------------------------------------------------------------- parsing

def find_day_objects(node) -> list[dict]:
    """Recursively collect day objects from a calendar JSON response."""
    found: list[dict] = []
    if isinstance(node, dict):
        if "date" in node and ("allotmentQuantity" in node or "timeAvailabilities" in node):
            found.append(node)
        else:
            for value in node.values():
                found.extend(find_day_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(find_day_objects(value))
    return found


def find_plans(node) -> list[dict]:
    """Recursively find the first `plans` list in an activity-info response."""
    if isinstance(node, dict):
        plans = node.get("plans")
        if isinstance(plans, list) and plans and isinstance(plans[0], dict) and "id" in plans[0]:
            return plans
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            plans = find_plans(value)
            if plans:
                return plans
    return []


def validate_days(days: list[dict]) -> None:
    if not days:
        raise CheckError("zero days parsed")
    for day in days:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day.get("date"))) or "status" not in day:
            raise CheckError(f"unexpected day shape: {str(day)[:120]}")
        # A bookable day with no plan rows would otherwise read as "no seats" forever.
        if day["status"] == "OK" and (day.get("allotmentQuantity") or 0) > 0:
            rows = day.get("timeAvailabilities") or []
            if not any(t.get("planAvailabilities") for t in rows):
                raise CheckError(f"{day['date']} is bookable but has no plan rows")


def direction_of(plan_id: str, title: str) -> str:
    """The station named first in the title is the departure station."""
    saga = min((i for i in (title.find("嵯峨"), title.find("嵐山")) if i >= 0), default=-1)
    kameoka = title.find("亀岡")
    if saga >= 0 and kameoka >= 0:
        return "Saga -> Kameoka" if saga < kameoka else "Kameoka -> Saga"
    if plan_id.endswith("-1"):
        return "Saga -> Kameoka"
    if plan_id.endswith("-2"):
        return "Kameoka -> Saga"
    return title or plan_id


def departure_time(raw: str) -> str:
    """'嵯峨野9号 13:02' -> '13:02'; '' stays ''."""
    match = re.search(r"(\d{1,2}):(\d{2})", raw or "")
    return f"{int(match[1]):02d}:{match[2]}" if match else ""


def find_hits(days, titles, activity, label, dates, seats) -> list[Hit]:
    hits = []
    for day in days:
        if day.get("date") not in dates:
            continue
        for row in day.get("timeAvailabilities") or []:
            for plan in row.get("planAvailabilities") or []:
                quantity = plan.get("allotmentQuantity") or 0
                if plan.get("status") == "OK" and quantity >= seats:
                    plan_id = plan.get("planId", "")
                    hits.append(Hit(
                        activity, label, day["date"], plan_id,
                        direction_of(plan_id, titles.get(plan_id, "")),
                        departure_time(row.get("time", "")), quantity,
                    ))
    return hits


# ---------------------------------------------------------------- site access

def fetch_product(browser, activity: str) -> tuple[list[dict], dict[str, str]]:
    """Load one product page once; return (days, {plan id: title})."""
    context = browser.new_context(
        user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
    )
    try:
        page = context.new_page()

        def filter_requests(route):
            request = route.request
            host = request.url.split("/")[2].split(":")[0]
            if ALLOWED_HOST.search(host) and request.resource_type not in BLOCKED_RESOURCES:
                route.continue_()
            else:
                route.abort()

        page.route("**/*", filter_requests)
        api_responses = []
        page.on("response", lambda r: api_responses.append(r)
                if "/v2/activity/" in r.url and r.ok else None)

        try:
            response = page.goto(BOOKING_URL.format(activity),
                                 wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as exc:
            raise CheckError(f"page load failed: {str(exc).splitlines()[0]}") from exc
        if response is None or not response.ok:
            raise CheckError(f"HTTP {response.status if response else 'no response'}")
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
        except Exception:
            pass  # not fatal: the read below retries, then falls back to captured JSON

        result = {"days": None, "plans": None}
        deadline = time.monotonic() + READ_RETRY_SECONDS
        while True:
            result = page.evaluate(READ_VUE_JS)
            if result["days"] or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(1000)

        days, plans = result["days"] or [], result["plans"] or []
        if not days or not plans:
            for api_response in api_responses:
                try:
                    body = api_response.json()
                except Exception:
                    continue
                if not days and "/v2/activity/calendar" in api_response.url:
                    days = find_day_objects(body)
                if not plans and "/v2/activity/info" in api_response.url:
                    plans = find_plans(body)
        validate_days(days)
        return days, {p["id"]: p.get("title", "") for p in plans}
    finally:
        context.close()


def check_products(dates, seats, fetch=None):
    """Return (hits, {activity: error}, days parsed per activity)."""
    hits, errors, parsed = [], {}, {}

    def run(fetch_one):
        for index, (activity, label) in enumerate(PRODUCTS):
            if index:
                time.sleep(random.uniform(3, 8))
            try:
                days, titles = fetch_one(activity)
                parsed[activity] = len(days)
                hits.extend(find_hits(days, titles, activity, label, dates, seats))
            except Exception as exc:
                errors[activity] = str(exc) or type(exc).__name__

    if fetch:
        run(fetch)
        return hits, errors, parsed
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                run(lambda activity: fetch_product(browser, activity))
            finally:
                browser.close()
    except Exception as exc:
        for activity, _ in PRODUCTS:
            errors.setdefault(activity, f"browser error: {exc}")
    return hits, errors, parsed


# ---------------------------------------------------------------- alerts

def describe(hit: Hit) -> str:
    day = date.fromisoformat(hit.date)
    when = f"{day.day} {day:%b}, {hit.direction}"
    if hit.time:
        text = f"{when} @ {hit.time} ({hit.quantity} left)"
    else:
        text = f"{when} @ time not listed ({hit.quantity} left that day)"
    return f"{text} [{hit.label}]" if hit.label else text


def build_message(hits: list[Hit], seats: int) -> str:
    noun = "seat" if seats == 1 else "seats"
    shown = hits[:MAX_MESSAGE_LINES]
    lines = [describe(hit) for hit in shown]
    lines[0] = f"Sagano: {seats} {noun} free — {lines[0]}"
    if len(hits) > len(shown):
        lines.append(f"+{len(hits) - len(shown)} more on the booking site")
    if seats > 1 and any(not hit.time for hit in hits):
        lines.append(f"Day totals may be split across trains: check {seats} are on one train.")
    return "\n".join(lines)


def send_ntfy(topic: str, message: str, click: str | None = None,
              actions: str | None = None, tags: str = "train") -> None:
    headers = {"Title": "Sagano Romantic Train", "Priority": "high", "Tags": tags}
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = actions
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise OSError(f"ntfy returned HTTP {response.status}")


def alert_links(hits: list[Hit]) -> tuple[str, str]:
    """Tap opens the first hit's product; buttons cover each product in the alert."""
    activities = list(dict.fromkeys(hit.activity for hit in hits))
    labels = {activity: label or "Standard ticket" for activity, label in PRODUCTS}
    actions = "; ".join(
        f"view, {labels.get(a, a)}, {BOOKING_URL.format(a)}" for a in activities[:3]
    )
    return BOOKING_URL.format(activities[0]), actions


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        state = {}
    except ValueError as exc:
        print(f"WARNING: ignoring unreadable state file: {exc}")
        state = {}
    return {
        "active": state.get("active") or {},  # hit key -> last alert time (UTC ISO)
        "consecutive_failures": state.get("consecutive_failures") or 0,
        "broken_alert_sent": bool(state.get("broken_alert_sent")),
    }


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def due_for_alert(hits: list[Hit], active: dict, now: datetime) -> list[Hit]:
    """Hits that are newly available, or whose last alert is REALERT_AFTER old."""
    return [
        hit for hit in hits
        if hit.key not in active
        or now - datetime.fromisoformat(active[hit.key]) >= REALERT_AFTER
    ]


def run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in
                         ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


# ---------------------------------------------------------------- one check

def run_once(dates: set[str], seats: int, state_path: Path, topic: str | None,
             dry_run: bool, fetch=None, send=send_ntfy, now: datetime | None = None) -> int:
    """Run one check. Returns a process exit code."""
    now = now or datetime.now(timezone.utc)
    state = load_state(state_path)
    hits, errors, parsed = check_products(dates, seats, fetch)
    order = [activity for activity, _ in PRODUCTS]
    hits.sort(key=lambda h: (order.index(h.activity), h.date, h.time, h.plan_id))

    for activity, count in parsed.items():
        print(f"{activity}: parsed {count} days")
    for activity, error in errors.items():
        print(f"{activity}: FAILED: {error}")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{activity}: {error}")
    print(f"Hits for {seats}+ seats on {', '.join(sorted(dates))}: {len(hits)}")
    for hit in hits:
        print(f"  {describe(hit)}")

    exit_code = 0

    # Seat alert. Any due hit sends the whole current list in one message.
    due = due_for_alert(hits, state["active"], now)
    alerted = False
    if due:
        message = build_message(hits, seats)
        click, actions = alert_links(hits)
        if dry_run:
            print(f"[dry run] would alert (tap opens {click}):\n{message}")
        else:
            try:
                send(topic, message, click=click, actions=actions)
                alerted = True
                print(f"Alert sent ({len(due)} new or re-alerted of {len(hits)} hits)")
            except Exception as exc:
                print(f"ERROR: could not send alert, will retry next run: {exc}")
                exit_code = 1
    elif hits:
        print("Nothing to alert: all hits were alerted under 2 hours ago")

    # Keep entries for products that could not be read: a failed page load says
    # nothing about their seats, so it must not trigger a "new" alert afterwards.
    current = {hit.key for hit in hits}
    active = {
        key: stamp for key, stamp in state["active"].items()
        if key in current or key.split("|")[0] in errors
    }
    if alerted:
        active.update({key: now.isoformat() for key in current})
    state["active"] = active

    # Failure tracking.
    if errors:
        state["consecutive_failures"] += 1
        print(f"Consecutive failed checks: {state['consecutive_failures']}")
        if state["consecutive_failures"] >= FAILURES_BEFORE_ALERT and not state["broken_alert_sent"]:
            summary = "; ".join(f"{a}: {e}" for a, e in errors.items())
            message = (f"Sagano watcher broken: {state['consecutive_failures']} failed checks "
                       f"in a row. It may be missing seats. Last error: {summary}")[:1000]
            if dry_run:
                print(f"[dry run] would alert:\n{message}")
            else:
                try:
                    send(topic, message, click=run_url(), tags="warning")
                    state["broken_alert_sent"] = True
                    print("Broken-watcher alert sent")
                except Exception as exc:
                    print(f"ERROR: could not send broken-watcher alert: {exc}")
                    exit_code = 1
    else:
        state["consecutive_failures"] = 0
        state["broken_alert_sent"] = False

    if dry_run:
        print("[dry run] state file not changed")
    else:
        save_state(state_path, state)
    return exit_code


# ---------------------------------------------------------------- CLI

def parse_dates(text: str) -> list[str]:
    try:
        dates = sorted({date.fromisoformat(d.strip()).isoformat()
                        for d in text.split(",") if d.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"dates must be YYYY-MM-DD,YYYY-MM-DD: {exc}")
    if not dates:
        raise argparse.ArgumentTypeError("at least one date is required")
    return dates


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Sagano Romantic Train seat watcher (never books).")
    parser.add_argument("--once", action="store_true", help="check once and exit (default: every 15 minutes)")
    parser.add_argument("--dates", type=parse_dates, default=DEFAULT_DATES,
                        help="comma-separated YYYY-MM-DD (default: 22-27 Nov 2026)")
    parser.add_argument("--seats", type=positive_int, default=2, help="seats needed on one train (default: 2)")
    parser.add_argument("--state-file", type=Path, default=Path("state.json"))
    parser.add_argument("--dry-run", action="store_true", help="print what would be alerted; send and save nothing")
    args = parser.parse_args(argv)

    topic = os.environ.get("NTFY_TOPIC", "").strip() or None
    dry_run = args.dry_run
    if not topic and not dry_run:
        if os.environ.get("GITHUB_ACTIONS"):
            print("ERROR: the NTFY_TOPIC secret is not set, so no alert could be delivered.")
            return 2
        print("NTFY_TOPIC is not set: running as --dry-run, nothing will be sent.")
        dry_run = True

    while True:
        today = datetime.now(JST).date().isoformat()
        if today > max(args.dates):
            print(f"Last watched date {max(args.dates)} has passed (today is {today} JST). Stopping.")
            return 0
        dates = {d for d in args.dates if d >= today}
        code = run_once(dates, args.seats, args.state_file, topic, dry_run)
        if args.once:
            return code
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
