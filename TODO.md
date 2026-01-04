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
- [ ] Add custom SRP permissions:
  - can_manage_srp_payouts
  - can_view_srp_reports
- [ ] Decide whether payout management lives in Django Admin or in-app UI
- [ ] If Django Admin:
  - selectively re-enable add/change permissions for SRP models only
- [ ] If in-app:
  - build /srp/payouts/manage/ UI gated by can_manage_srp_payouts
- [ ] Auto-create default groups:
  - SRP Reviewer
  - SRP Admin

## SRP buildout
- ESI Ship pull
- Claim ESI pull
- Approve, Paid, Denied, but no All catagory
- No real data is displayed on who approved or paid it, or when. 
- Error checking on submit form