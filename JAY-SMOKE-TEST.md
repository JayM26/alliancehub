# JAY-SMOKE-TEST.md — Django 5.2 Upgrade Manual Pass

Branch: `django-5.2-upgrade` · Upgrade executed 2026-07-06 per `../django-5.2-upgrade-plan.md`.

The upgrade was verified **headlessly** as far as anything reachable without a real EVE SSO
login (see "Already verified" below). After the initial pass, the data-path flows (items 2–4)
were ALSO run headlessly against **real killmails via public ESI** — killmail fetch needs only
id+hash, no auth. **The only thing left for a human is item 1: the EVE SSO OAuth2 round-trip
itself.** An automated login attempt was blocked by Cloudflare's CAPTCHA on login.eveonline.com
(screenshots `sso-01/02-*.png`) — expected and not fought; do this one in your normal browser.
Note: the dev DB now contains the test data from that pass (2 claims, 3 ship payouts, 1 doctrine
fit, the `smoketest` superuser) — wipe the volume or delete via admin before any real use.

Run this on the `django-5.2-upgrade` branch **before merging to main.** If anything here fails,
`git checkout requirements.txt` + `docker compose build web` reverts to 5.0.7 (each stop is one
commit; no schema migrations were introduced, so there is nothing to un-migrate).

---

## Setup

```bash
cd /home/jay/claudeCLI/alliancehub/repo
git checkout django-5.2-upgrade
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
# app at http://localhost:8000/
```

Requires a `.env` with **real** `EVE_CLIENT_ID` / `EVE_CLIENT_SECRET` and a callback URL
registered in the EVE developer app that matches `EVE_CALLBACK_URL`. The committed dev `.env`
has stub EVE keys — SSO login will NOT work until real keys are in place. That is the one
prerequisite this checklist can't remove.

---

## Checklist — EVE-SSO-gated flows (only a real login can verify these)

### 1. SSO login + callback  → exercises `eve_sso/views.py`, `eve_sso/utils.py`, Django `login()`
- [ ] Click **Login with EVE** on the landing page → redirects to `login.eveonline.com`.
- [ ] Authorize → EVE redirects back to `/sso/callback/` → you land logged in (nav shows your
      character, not "Log in").
- [ ] Token exchange completed without a 500 (the hand-rolled OAuth2 flow on `requests`).
- [ ] Link an **alt** character (link-character flow) → alt attaches to the same account.

*Why manual:* the OAuth2 round-trip needs a real EVE identity provider + registered app
credentials. The local superuser bypasses this path entirely.

### ~~2. Submit an SRP claim (with real ESI enrichment)~~ — DONE headlessly 2026-07-06
Ran with a **real killmail** (Imperial Navy Slicer loss in Amamake, killmail 136829709 from
zKillboard) as the local superuser — killmail ESI fetch is public (id+hash), no SSO needed.
ESI enrichment resolved ship, victim character, corp, and system from live ESI ("ESI pull OK");
ESI type/entity caches wrote; a ShipPayout record auto-created; claim landed in My Claims as
PENDING. A doctrine fit was first imported through the **EFT parser** (10-line Slicer fit →
correctly normalized into HIGH/MID/LOW/RIG slot groups), and the **fit check auto-ran with the
correct verdict**: Fit Mismatch, best match "TNT Kite Slicer" (0.33) — right answer, since the
real kill was beam-fit and the test doctrine fit was pulse; the Missing/Expected vs Extra/On-Kill
breakdown was item-for-item correct. Screenshot: `sso-03-claim-detail-fitcheck.png`.

### ~~3. Review → approve / deny / pay~~ — DONE headlessly 2026-07-06
Full state machine exercised on the two real-killmail claims as superuser (passes
`permission_required` implicitly): claim #1 PENDING → **Approved** (reviewer + Processed
timestamp set, `ClaimReview` audit row with note) → **Paid** (Paid timestamp, 2nd audit row).
Claim #2 (real Tristan loss) → **Denied** with note. Auto-check flags computed from the real
killmail (NPC present, damage split 30.9%/69.1%, blues check, corps match). Admin overview
aggregates then showed correct live numbers: PAID 1 / DENIED 1, category rollup, reviewer
activity 3 actions.

### ~~4. Bulk payout CSV import~~ — DONE headlessly 2026-07-06
3-row CSV uploaded through the real form → preview correctly diffed against existing records
(2 UPDATEs with per-field diffs, 1 CREATE with `hull=True` parsed) → Apply: "Created: 1,
Updated: 2, Skipped: 0, Errors: 0", values confirmed in the payout admin table.

**What's actually left in items 2–4 for you:** nothing mechanical. The only untested variation
is a claim submitted by an **SSO-created user with a linked EveCharacter** (submitter identity /
corp-mismatch flag against a real linked character). That's covered by doing item 1 and
submitting one claim afterward.

### ~~5. Static assets under WhiteNoise in the prod-style container~~ — DONE headlessly 2026-07-06
Verified after the initial pass: one-off prod-shaped container (gunicorn, `DEBUG=0` override, no
dev overlay). Gunicorn boots; home + payout pages 200; Django admin emits content-hashed asset
URLs and WhiteNoise serves them 200 from the `collectstatic` manifest; 404s return the plain
production page (confirming `DEBUG=0` was really in effect). Nothing left to check here except
what only a real deployment can exercise (real domain, TLS, proxy headers).

---

## Already verified headlessly (you do NOT need to re-check these)

Done on the `django-5.2-upgrade` branch, 2026-07-06, Docker dev stack, screenshots in
`../upgrade-verification/screenshots/`:

- **Boot / framework:** `manage.py check` clean at 5.1.15 **and** 5.2.15; `makemigrations
  --check --dry-run` → "No changes detected" (zero model drift); `migrate` applies clean;
  `manage.py test` runs green (0 tests — the suite is stubs, see plan §5); `collectstatic`
  processes 381 files (STORAGES/WhiteNoise pipeline intact).
- **Rendering (12 pages screenshotted AND visually inspected, all clean):** home; SRP submit
  form; doctrine-fit import form; add/edit ship-payout form (Bootstrap grid + inputs + buttons);
  admin overview (exercises the `Count`/`Sum`/`Max` aggregates — zero console errors); review
  queue; payouts list; bulk upload form; public payout table; my-claims; doctrine-fit list;
  Django admin changelist (5.2 admin markup renders fine). Every screenshot was individually
  eyeballed for layout/CSS defects, not just captured.
- **Prod-shaped boot:** gunicorn + `DEBUG=0` one-off container — boots clean, pages 200,
  WhiteNoise serves content-hashed manifest assets, production 404 page (see struck item 5).
- **Crispy finding (matters):** `crispy-bootstrap5` was bumped `0.7 → 2026.3` and
  `django-crispy-forms 2.3 → 2.6`, and both load cleanly under 5.2 (they're in `INSTALLED_APPS`;
  `check` passes; app boots). BUT the app **never actually invokes crispy** — there are zero
  `{% crispy %}` tags / `|as_crispy_field` filters / `FormHelper` imports anywhere. Form styling
  comes from Bootstrap classes hand-set in `srp/forms.py` widget `attrs`, not from crispy's
  render path. So the plan's "highest-risk rendering item" doesn't bite: the only thing the
  crispy bump had to do was import cleanly under 5.2, which it does. The submit form's plainer
  look (vs. the fully-styled payout form) is **pre-existing** — it uses `{{ form.as_p }}` with
  only partial widget classes, unchanged by this upgrade and out of scope for it.

### Helper for your manual pass
A local Django superuser exists in the dev DB: **`smoketest` / `smoketest123`**. Log in at
`/admin/login/` to reach the admin-gated *pages* (queue, payout forms, doctrine import) without
EVE SSO — useful for eyeballing layout. It does **not** substitute for the SSO/ESI/real-data
checks above (no linked character, no real killmails). Delete it before any real deployment.
