# JAY-SMOKE-TEST.md — Django 5.2 Upgrade Manual Pass

Branch: `django-5.2-upgrade` · Upgrade executed 2026-07-06 per `../django-5.2-upgrade-plan.md`.

**STATUS: all four flows verified headlessly (incl. a real EVE SSO login). One Django 5.2 bug
found and fixed. Nothing is strictly required of you before merge — but a confirmation login
with your own main character is worth doing.**

Every checklist item below was exercised for real on the branch: data-path flows (2–4) against
**real killmails via public ESI**, and the **full SSO OAuth2 round-trip** (item 1) via a real EVE
account driven through a headed browser under Xvfb. The first SSO attempt was blocked by
Cloudflare in *headless* Chrome; re-running in a *headed* browser cleared the challenge and the
login completed.

**Bug found (now fixed, commit on branch):** the SSO callback threw `NotSupportedError: FOR
UPDATE cannot be applied to the nullable side of an outer join` on **every** login —
`eve_sso/views.py:eve_callback` used `select_for_update().select_related("user")` across the
nullable `EveCharacter.user` FK, which Django 5.2 rejects. Fixed by scoping the lock with
`of=("self",)`. This was **invisible to all prior testing** because a local Django superuser
never traverses `eve_callback` — only a real OAuth login does. The query predates the upgrade
(v0.1.0) but is fatal specifically on 5.2. Full flow re-verified green after the fix: login →
character select → authorize → callback → register-as-main → dashboard, with User +
EveCharacter + stored OAuth token persisted correctly.

Note: the dev DB now holds test data from these passes (2 claims, 3 ship payouts, 1 doctrine fit,
the SSO-created user `jaymt`/char `JayMT`, and the `smoketest` superuser) — wipe the volume or
delete via admin before any real use.

Run this on the `django-5.2-upgrade` branch **before merging to main.** If anything here fails,
`git checkout requirements.txt` + `docker compose build web` reverts to 5.0.7 (each stop is one
commit; no schema migrations were introduced, so there is nothing to un-migrate). The callback
fix is a separate commit and stands on its own.

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
already has **working** EVE keys (verified — the authorize redirect and callback succeeded end to
end), so `localhost:8000` SSO works out of the box on this branch.

---

## Checklist — EVE-SSO-gated flows (only a real login can verify these)

### ~~1. SSO login + callback~~ — DONE headlessly 2026-07-06 (with a real EVE account; found+fixed a bug)
Verified via a real EVE test account driven through headed Chromium under Xvfb: **Login with EVE
→ login.eveonline.com → character select → Authorize → `/sso/callback/` → register-as-main →
dashboard.** Token exchange succeeded (hand-rolled OAuth2 on `requests`), `login()` established
the session, and a User + EveCharacter + OAuth token persisted. This is the pass that surfaced
the `NotSupportedError` callback bug (see top of file) — fixed and re-verified green.

**Alt-link is now also proven** (2026-07-06, second run): logged in as `JayMT2` → "Link as Alt"
→ re-auth as `JayMT` → callback → "Login Successful". DB confirms both characters under one
account (`user_id=3`, main=JayMT). Nothing left for a human here — a confirmation login with your
own characters is nice-to-have, not required.

*Why it needed a real login at all:* the OAuth2 round-trip needs EVE's identity provider; a local
Django superuser never traverses `eve_callback` — which is exactly why the bug hid until now.

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

**What's actually left in items 2–4 for you:** nothing. Items 2–4 were first run as the
superuser, and item 1's SSO pass proved the real-linked-character path works too.

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
- **Full-view sweep (22 endpoints, 0 failures):** after the callback fix, drove every SRP/eve_sso
  view via Django's test `Client` as superuser — all GETs (incl. filtered/aggregate querystring
  branches) plus the mutating POSTs that browser testing hadn't hit: reviewer edit-claim save,
  ship-payout edit save, and fit-check rerun. Zero 500s. Grep confirmed no other
  `select_for_update`-across-nullable-join (the one sibling lock is single-table) and no other
  Django-5.2 runtime query hazards in this codebase.
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
