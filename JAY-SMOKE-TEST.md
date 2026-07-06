# JAY-SMOKE-TEST.md — Django 5.2 Upgrade Manual Pass

Branch: `django-5.2-upgrade` · Upgrade executed 2026-07-06 per `../django-5.2-upgrade-plan.md`.

The upgrade was verified **headlessly** as far as anything reachable without a real EVE SSO
login (see "Already verified" below). This file covers **only the flows that a real EVE login
is required to exercise** — the parts a local Django superuser can't stand in for, because they
depend on the live EVE SSO OAuth2 round-trip, real killmail/ESI data, and reviewer permissions
granted to real characters.

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

### 2. Submit an SRP claim (with real ESI enrichment)  → `srp/views.py:submit_claim`, `srp/esi.py`
- [ ] Submit a claim with a **real killmail / ESI link** from a recent loss.
- [ ] ESI enrichment fills ship name / type / value (not left blank).
- [ ] Fit check auto-runs and shows a result against the doctrine fit.
- [ ] The claim appears in **My Claims** with correct status (PENDING).

*Why manual:* needs a live ESI call against a real killmail ID + hash, and a character on the
claim. The form **layout** is already verified headless; this checks the ESI/data path.

### 3. Review → approve / deny / pay  → `srp/views.py` state machine + `ClaimReview` audit
- [ ] As a user with `srp.can_review_srp`, open the **Review Queue** — the submitted claim shows
      with its flags (NPC / blues / corp mismatch as applicable).
- [ ] **Approve** a claim → status → APPROVED, a `ClaimReview` audit row is written.
- [ ] **Deny** a different claim (with a comment) → status → DENIED, audit row written.
- [ ] **Pay** an approved claim → status → PAID, payout amount recorded.
- [ ] Confirm the audit trail (`ClaimReview`) reflects each transition with reviewer + timestamp.

*Why manual:* needs real claims in review states and a reviewer-permissioned real account. The
`.save()`/`set_status()` transitions run on real data here.

### 4. Bulk payout CSV import  → `srp/views.py:admin_payouts_bulk*`, `PayoutImportJob`
- [ ] Go to **Bulk Upload Payouts**, upload a real payout CSV.
- [ ] Preview screen lists the parsed rows / diffs correctly.
- [ ] Apply selected rows → `ShipPayout` records created/updated; a `PayoutImportJob` is logged.
- [ ] Spot-check a few payout values on the public **Ship Payouts** table.

*Why manual:* the upload/preview/apply pipeline is gated and easiest to trust against a real CSV.
The upload **form and empty table render** are already verified headless.

### 5. Static assets under WhiteNoise in the prod-style container (optional but recommended)
- [ ] Run the **prod** compose (gunicorn, `DEBUG=0`) instead of dev runserver:
      `docker compose up -d --build` (no dev overlay), then load a page.
- [ ] Confirm CSS/JS still serve (WhiteNoise + `STORAGES` manifest storage). `collectstatic`
      was verified to run + post-process 381 files headless; this confirms serving under gunicorn.

---

## Already verified headlessly (you do NOT need to re-check these)

Done on the `django-5.2-upgrade` branch, 2026-07-06, Docker dev stack, screenshots in
`../upgrade-verification/screenshots/`:

- **Boot / framework:** `manage.py check` clean at 5.1.15 **and** 5.2.15; `makemigrations
  --check --dry-run` → "No changes detected" (zero model drift); `migrate` applies clean;
  `manage.py test` runs green (0 tests — the suite is stubs, see plan §5); `collectstatic`
  processes 381 files (STORAGES/WhiteNoise pipeline intact).
- **Rendering (12 pages screenshotted, all clean):** home; SRP submit form; doctrine-fit import
  form; add/edit ship-payout form (Bootstrap grid + inputs + buttons); admin overview (exercises
  the `Count`/`Sum`/`Max` aggregates — zero console errors); review queue; payouts list; bulk
  upload form; public payout table; my-claims; doctrine-fit list; Django admin changelist (5.2
  admin markup renders fine).
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
