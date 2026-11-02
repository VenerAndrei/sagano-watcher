# Sagano Romantic Train seat watcher

Checks the official booking site every 15 minutes and sends a push notification to your phone when seats free up. **It only reads and notifies. It never books or pays.** You book by hand from the link in the alert.

- **Dates:** 22–27 Nov 2026, both directions
- **Products:** standard one-way ticket, Diorama Kyoto set, Ninna-ji light-up set
- **Stops:** by itself after 27 Nov 2026 (Japan time); the workflow then disables its own schedule

## What an alert means

The site reports the two kinds of product differently, and this affects how far you can trust "2 seats":

| Product | What the site reports | Example alert line |
|---|---|---|
| Diorama and Ninna-ji sets | Seats per train | `25 Nov, Saga -> Kameoka @ 13:02 (3 left) [Diorama Kyoto set]` |
| Standard ticket | One total per direction for the whole day, no train times | `25 Nov, Saga -> Kameoka @ time not listed (3 left that day)` |

So a standard-ticket alert does **not** guarantee 2 seats on the same train: "2 left that day" could be 1 + 1 on different trains. Those alerts carry a reminder. The site only shows standard-ticket trains after you start a booking, and this watcher deliberately stays out of the booking flow.

## Setup

### 1. Install ntfy on your iPhone

1. Install **ntfy** from the App Store and allow notifications.
2. Tap **+** and subscribe to a long random topic name, for example `sagano-` followed by 20 or more random letters and digits. Leave the server as `ntfy.sh`.

Anyone who knows a topic name can read it or post to it, so treat the name like a password.

### 2. Create the repo and push

```sh
git init -b main
git add .
git commit -m "Add Sagano seat watcher"
gh repo create sagano-watcher --public --source . --push
```

The repo is public because Actions minutes are then free and unlimited; private repos get 2,000 a month and this needs about 4–5k. The code contains no secrets, but run logs are public and show which seats were found.

### 3. Add the secret

```sh
gh secret set NTFY_TOPIC
```

Paste the topic name when prompted.

### 4. Run it once by hand

This uses a date that has seats and asks for just 1 seat, so a real alert should reach your phone:

```sh
gh workflow run sagano.yml -f seats=1 -f dates=YYYY-MM-DD
gh run watch
```

Choose a date in the next few weeks that the booking site shows as available. To test the normal 22–27 Nov watch instead, run `gh workflow run sagano.yml` with no inputs; it stays silent unless seats are free.

The schedule then runs by itself, with your laptop off. GitHub can start scheduled runs some minutes late.

## Running locally

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # on macOS/Linux: .venv/bin/python
.venv/Scripts/python -m playwright install chromium
```

Confirm parsing works, using a date that has seats:

```sh
python sagano_watch.py --once --seats 1 --dates 2026-11-04
```

| Flag | Meaning |
|---|---|
| `--once` | Check once and exit. Without it, checks every 15 minutes. |
| `--dates` | Comma-separated `YYYY-MM-DD`. Default: 22–27 Nov 2026. |
| `--seats` | Seats needed. Default: 2. |
| `--state-file` | Where to keep alert history. Default: `state.json`. |
| `--dry-run` | Print what would be alerted. Sends nothing and saves nothing. |

If `NTFY_TOPIC` isn't set locally, the script behaves as `--dry-run`. On GitHub a missing secret fails the run.

Offline tests: `python -m unittest test_sagano_watch`

## Alert rules

- A hit is `status == "OK"` and `allotmentQuantity >= seats` for a plan on a watched date.
- You get an alert when a hit newly appears, or is still there 2 hours after its last alert. Every current hit goes in the same message.
- Tapping an alert opens the booking page for its first product, and each product in the alert also gets a button.
- A check that fails 4 times in a row (site down, layout changed, zero days parsed, including just one of the three products) sends one "watcher broken" alert. A later good check re-arms it, without sending a message.

A failed check doesn't turn the GitHub run red, because that would email you every 15 minutes during an outage. Look for a warning on the run instead. A run does go red if an alert can't be delivered or the secret is missing.

## Stopping early

```sh
gh workflow disable sagano.yml
```
