# AllianceHub TODO

This file tracks active work, planned features, and future ideas for AllianceHub.
SRP is treated as a first-class module.

## AllianceHub – Core (Short-Term / Active)

- Create dashboard page with character list
- Allow switching / promoting mains
- Add user profile editing
- Display active corp / alliance on home

## AllianceHub – Core (Medium-Term)

- Fleet Tracker module
- Discord bot SSO link
- Scheduled token refresh (Celery or cron, if not handled by auth provider)

## Tech Debt / Quality

- Move hardcoded URLs to settings
- Add typing hints and docstrings
- Write unit tests for:
  - utils.py
  - SRP business logic (payout calculation, category handling, workflow)
- Add basic developer documentation (README / setup notes)

## SRP Module

## SRP – Reviewer Quality of Life (High Priority)

- Fit checker (flag-only, not enforcement)
  - Compare submitted fit vs stored doctrine
  - Allow small module variations (green/blue mods)
  - Initial scope limited to:
    - Mainline doctrines
    - Capital ships
- Fit importer
  - Accept standard formats (EFT / Pyfa)
  - Store per-ship doctrine fits
- Implant checker
  - Automatically validate pod losses
  - Flag non-pod cases for manual review
- Optional "Show attackers" page
  - Lazy-loaded (not part of main review flow)
  - Used only when zKill fails or for edge cases
- Display which blue or NPC triggered flags
  - On demand (detail page only)

## SRP – Admin and Roles (Post-MVP)

- Auto-create default SRP groups:
  - SRP Reviewer
  - SRP Admin
- Notification when new ships are added with payout = 0
- Hide default Django permissions in admin (database unchanged)

## SRP – Workflow Enhancements (Medium Priority)

- Payout recipient handling
  - Default payout to submitter
  - Optional payout to victim pilot
  - Store recipient choice on claim
- Bulk reviewer actions
  - Bulk approve
  - Bulk mark paid
- Derived payout recompute tool
  - Recalculate existing claims after payout table changes

## SRP – Performance and Architecture

- Cache corporation ID to name
- Cache alliance ID to name
- Keep character name resolution on-demand only
- Avoid background workers unless clearly justified
- Optional background job for slow ESI enrichment (deferred)

## SRP – Reporting and Analytics (Future)

- Enhanced SRP reports
  - Category breakdowns
  - Reviewer activity metrics
- Export tools (CSV for finance / leadership)
- Trend analysis
  - Repeat attackers
  - Time-of-day patterns
  - Frequent blue involvement

## Experimental / Long-Term Ideas

- SeAT API integration (read-only, selective)
- Threat analysis engine
- Automated SRP anomaly detection

## Design Principles (Do Not Remove)

- Prefer flags over enforcement
- Optimize for reviewer speed
- Avoid background workers unless justified
- MVP first, features second
- Humans make final decisions
