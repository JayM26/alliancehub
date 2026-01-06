# AllianceHub TODO

## 🚀 Short-Term
- Create dashboard page with character list
- Allow switching / promoting mains
- Add user profile editing
- Display active corp / alliance on home

## 🧩 Medium-Term
- Fleet Tracker module
- SRP integration
- Discord bot SSO link
- Scheduled token refresh (Celery or cron)

## 🧰 Tech Debt / Refactors
- Move hardcoded URLs to settings
- Add typing hints & docstrings
- Write unit tests for utils.py

## SRP – Admin & Roles (Post-MVP)
- [ ] Add SRP custom permissions (role-oriented):
  - [ ] can_manage_srp_payouts
  - [ ] can_view_srp_reports
- [ ] Decide payout management approach:
  - [ ] Django Admin vs in-app UI
  - [ ] If in-app: build `/srp/payouts/manage/` gated by can_manage_srp_payouts
- [ ] Auto-create default groups (migration or management command):
  - [ ] SRP Reviewer
  - [ ] SRP Admin
- [ ] (Optional) Filter/hide default Django add/change/delete/view perms in admin (keep DB intact)

## SRP – Workflow & UX Improvements (MVP+)
### Reviewer Queue
- [ ] Add `status=ALL` (default) so actions don’t “disappear” after approve/deny/pay
- [ ] Add explicit success messages for approve/deny/pay (and keep user on same filtered view)
- [ ] Display processing metadata in queue:
  - [ ] reviewer name
  - [ ] approved/denied timestamp
  - [ ] paid timestamp (separate from processed_at)
- [ ] Add reviewer notes input on approve/deny/pay (stored + shown)
- [ ] Add claim detail page from queue (`/srp/claim/<id>/`) with full info + history
- [ ] Add claim history / audit trail display (use ClaimReview records)

### Claim Data & ESI / Killmail
- [ ] Parse ESI/killmail link and fetch killmail data server-side
- [ ] Pull and store key killmail fields on submit:
  - [ ] victim character name (actual pilot)
  - [ ] ship type
  - [ ] system/region
  - [ ] fit / items (at least a raw JSON blob for now)
- [ ] Show ESI/killmail data in claim detail view (fit is required for some SRP)
- [ ] Consider auto-creating ShipPayout rows if ship not in table yet (payout defaults to 0 + flag)

### Submitter vs Victim + Payout Recipient
- [ ] Track both identities:
  - [ ] submitter (logged-in main) = existing `submitter`
  - [ ] victim pilot (from killmail) = new field (e.g., `victim_character_name`)
- [ ] Add payout recipient option:
  - [ ] default recipient = submitter
  - [ ] allow toggle: pay submitter vs pay victim
  - [ ] store recipient choice in claim (future: integrate wallet/contract workflow)

### Validation & Robustness
- [ ] Improve submit form validation and error messaging
- [ ] Enforce broadcast requirement for Strategic/Peacetime (already) + show clearer UI hints
- [ ] Handle bad/unsupported ESI links gracefully (user-friendly errors)