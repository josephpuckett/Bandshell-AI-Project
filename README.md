# Bandshell Calendar

A small web app that gets your nonprofit's events onto **other organizations' calendars**
without re-typing them into every form.

You show it one website's event form once. An AI fills that form in a browser while you
watch, you approve the result, and the app saves a reusable *routine*. Every event after
that takes about two minutes: it fills the form, you check it, **you** press submit.

> **It never publishes anything by itself.** The app types; a human decides.

## How it works

```
 1. you create an event     2. you add the website     3. one-time setup        4. every event after
 ─────────────────────      ──────────────────────     ─────────────────       ──────────────────
 title, dates, blurb,   →   the calendar you submit →  an AI fills the form  →  the saved routine
 description, image         to (once, not per event)   live; you fix and        fills it in ~2 min;
                                                       approve it               you review + submit
```

Step 3 is the only slow one (5–15 minutes, once per website). It's also where the app
learns: it records the clicks and typing that worked and replays them next time, with no
AI involved.

## What you need

- A browser (Chrome, Edge, Firefox, Safari) — a laptop or desktop, not a phone.
- The link to the app plus the username/password from whoever runs it.
- The event details you'd type by hand anyway: title, date and times, a short blurb, a
  longer description, ticket price, and **a link to an image anyone can open** (Google
  Drive: right-click → Share → *Anyone with the link* → *Viewer*).
- The calendar website's own login, if it has one — you may need to type it into the live
  page during setup. That's normal.

## Using it

1. **+ New Event** — fill in the event. Venue, cost and categories come from Settings.
2. **+ Website** — where events should go (once per calendar website).
3. **Set up this website** — watch the live page, help when needed (a login, a captcha),
   then **Approve**. If it's wrong, **Reject** with a short note and it tries again.
4. **Review and submit** — the routine fills the form; check it, press the site's own
   submit button in the live page, then **✓ Done**.

A longer plain-English guide with a glossary and troubleshooting ships with the app at
**`/static/user-guide.html`** (also linked from the **?** button).

## Good to know

- **The live page is a stream of images**, not a real browser window you can copy from.
  You *can* click, type and scroll it, and paste with <kbd>Ctrl</kbd>+<kbd>V</kbd>.
  Date pickers and dropdowns drawn by the browser itself can't be shown — type instead.
- **One event per run.** Finish and click Done before starting the next.
- **Badges:** *Not set up* → *Setting up* → *Ready*. *Stale* means the shared settings
  (venue, cost, categories) changed, so the routine needs re-learning.
- **Eventbrite** websites are different: events are created there as **drafts** for you to
  publish — no browser, no review step.

## Running it yourself

Locally, for development:

```bash
apt-get install bubblewrap                 # optional but recommended: script sandbox
uv sync                                    # creates .venv from uv.lock (Python 3.12)
.venv/bin/playwright install --with-deps chromium
OPENROUTER_API_KEY=sk-or-... HEADLESS=1 uvicorn main:app --port 8000
```

On a server (Debian/Ubuntu), keep a small `bootstrap.sh` next to your downloaded
`bandshell.env` and Google key. It updates the system, installs `git`, clones or
fast-forwards the project, and then runs `deploy/install.sh` — which sets up nginx with a
login, the systemd service, and the optional public URL:

```bash
# edit REPO / DIR / APP_USER at the top of bootstrap.sh, then:
sudo bash bootstrap.sh
```

`USER_HOME` in that script is what points `--env-file`/`--google-key` at *your* downloads;
under `sudo`, `~` and `$HOME` both mean `/root`. Re-running it is the upgrade path.

See **[docs/deploy.md](docs/deploy.md)** for install options, settings, logs, idle
shutdown, the optional tunnel and troubleshooting.

## Docs

| Where | What |
|---|---|
| `frontend/user-guide.html` | long-form guide for the person using the app (served at `/static/user-guide.html`) |
| [docs/deploy.md](docs/deploy.md) | running it on a server: install, settings, operations, troubleshooting |
| [AGENTS.md](AGENTS.md) | how the code works — architecture, modules, invariants (written for AI coding agents) |
| [docs/sandbox-plan.md](docs/sandbox-plan.md) | why generated scripts run inside bubblewrap, and the threat model |

## License

MIT — see [LICENSE](LICENSE).
