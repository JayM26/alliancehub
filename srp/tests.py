"""
SRP money-rule tests.

Covers the three P0 correctness fixes:
  - Fix 1: payout_amount is a live provisional preview ONLY while PENDING, then
    FROZEN (snapshot at approval) — changing ShipPayout afterwards never drifts it.
  - Fix 2: needs_manual_payout flags computed-to-zero (unfunded) claims.
  - Fix 3: APPROVED→DENIED and PAID→APPROVED (un-pay) transitions are legal;
    un-pay preserves the frozen payout; illegal transitions still raise.

All tests are self-contained (own ShipPayout / SRPClaim / User) and run with the
SRPConfig singleton's auto_calculate_payouts ON (the default).
"""

from decimal import Decimal  # pyright: ignore[reportMissingModuleSource]

from django.contrib.auth import get_user_model  # pyright: ignore[reportMissingModuleSource]
from django.test import TestCase  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

from .checks import (
    CheckResult,
    blue_check,
    category_ceiling_status,
    corp_mismatch_check,
    non_tnt_check,
    npc_check,
    ownership_check,
    submitter_character_ids,
)
from .models import ClaimReview, ShipPayout, SRPClaim, SRPConfig

User = get_user_model()

M100 = Decimal("100000000.00")
M200 = Decimal("200000000.00")
M999 = Decimal("999000000.00")
M50 = Decimal("50000000.00")


class SRPPayoutRulesTests(TestCase):
    def setUp(self):
        # Ensure the singleton exists with auto-calc ON.
        cfg = SRPConfig.get()
        cfg.auto_calculate_payouts = True
        cfg.save()

        self.user = User.objects.create_user(username="pilot", password="x")

    # -- helpers ---------------------------------------------------------
    def _ship(self, name="Rifter", **cols):
        defaults = dict(strategic=0, peacetime=0, shitstack=0, tnt_special=0)
        defaults.update(cols)
        return ShipPayout.objects.create(ship_name=name, **defaults)

    def _claim(self, category="STRATEGIC", ship=None, status="PENDING", **extra):
        return SRPClaim.objects.create(
            submitter=self.user,
            character_name="Pilot",
            category=category,
            ship=ship,
            status=status,
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            **extra,
        )

    # -- Fix 1: freeze ---------------------------------------------------
    def test_approval_snapshots_payout_to_current_shippayout(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        # Provisional preview while pending.
        self.assertEqual(claim.payout_amount, M100)

        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()
        self.assertEqual(claim.status, "APPROVED")
        self.assertEqual(claim.payout_amount, M100)

    def test_freeze_holds_through_approved_and_paid(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)

        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()
        self.assertEqual(claim.payout_amount, M100)

        # Master table changes underneath an already-approved claim.
        ship.strategic = M999
        ship.save()

        claim.refresh_from_db()
        claim.save()  # re-save must NOT rewrite the frozen amount
        self.assertEqual(claim.payout_amount, M100)

        # And it stays frozen through PAID.
        claim.set_status("PAID", reviewer=self.user)
        claim.save()
        self.assertEqual(claim.payout_amount, M100)

        claim.refresh_from_db()
        self.assertEqual(claim.payout_amount, M100)

    def test_manual_payout_never_overwritten_in_any_status(self):
        # Manual claim, reviewer-entered amount, ship has a nonzero strategic col.
        ship = self._ship(strategic=M100)
        claim = self._claim(
            category="MANUAL", ship=ship, payout_amount=M50, reviewer=self.user
        )
        # PENDING save leaves it alone.
        self.assertEqual(claim.payout_amount, M50)

        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()
        self.assertEqual(claim.payout_amount, M50)

        claim.set_status("PAID", reviewer=self.user)
        claim.save()
        claim.refresh_from_db()
        self.assertEqual(claim.payout_amount, M50)

    def test_pending_non_manual_recomputes_live(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        self.assertEqual(claim.payout_amount, M100)

        # Still pending: provisional preview tracks the master table.
        ship.strategic = M200
        ship.save()
        claim.save()
        self.assertEqual(claim.payout_amount, M200)

    # -- Fix 2: needs_manual_payout -------------------------------------
    def test_needs_manual_payout_no_ship(self):
        claim = self._claim(ship=None)  # calculate_payout() -> 0
        self.assertTrue(claim.needs_manual_payout)

    def test_needs_manual_payout_zero_column(self):
        ship = self._ship(strategic=0)  # category column is 0
        claim = self._claim(category="STRATEGIC", ship=ship)
        self.assertTrue(claim.needs_manual_payout)

    def test_needs_manual_payout_false_when_funded(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        self.assertFalse(claim.needs_manual_payout)

    def test_needs_manual_payout_false_for_manual(self):
        claim = self._claim(category="MANUAL", payout_amount=M50, reviewer=self.user)
        self.assertFalse(claim.needs_manual_payout)

    def test_needs_manual_payout_false_when_denied(self):
        claim = self._claim(ship=None, status="DENIED")
        self.assertFalse(claim.needs_manual_payout)

    # -- Fix 3: transitions ---------------------------------------------
    def test_approved_to_denied_transition(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()

        claim.set_status("DENIED", reviewer=self.user)  # must not raise
        claim.save()
        self.assertEqual(claim.status, "DENIED")

    def test_unpay_preserves_frozen_payout(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()
        claim.set_status("PAID", reviewer=self.user)
        claim.paid_at = timezone.now()
        claim.save()
        self.assertEqual(claim.payout_amount, M100)

        # Master table drifts, then un-pay (PAID -> APPROVED).
        ship.strategic = M999
        ship.save()

        claim.set_status("APPROVED", reviewer=self.user)  # must not raise
        claim.paid_at = None  # mirrors pay_claim view bookkeeping
        claim.save()
        self.assertEqual(claim.status, "APPROVED")
        self.assertIsNone(claim.paid_at)
        # Un-pay must NOT recompute — frozen value survives the round-trip.
        self.assertEqual(claim.payout_amount, M100)

    def test_illegal_transition_still_raises(self):
        claim = self._claim(status="PENDING")
        with self.assertRaises(ValueError):
            claim.set_status("PAID", reviewer=self.user)


# ---------------------------------------------------------------------------
# A1 — Auto-checks: "unconfigured" must render NEUTRAL, never false-green.
# ---------------------------------------------------------------------------
class SRPAutoCheckTests(TestCase):
    """
    Cluster A applied to the reviewer auto-checks. Each check is tri-state:
    WARN / CLEAN / neutral (NA). An empty SRPConfig must yield NA (neutral),
    NOT a green "clean" that a reviewer misreads as "checked & safe".
    """

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="x")

    def _claim(self, km):
        return SRPClaim.objects.create(
            submitter=self.user,
            character_name="Pilot",
            category="STRATEGIC",
            status="PENDING",
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            killmail_raw=km,
        )

    def _cfg(self, **cols):
        cfg = SRPConfig.get()
        for k, v in cols.items():
            setattr(cfg, k, v)
        cfg.save()
        return cfg

    def _link_main(self, corp_id):
        from eve_sso.models import EveCharacter

        ch = EveCharacter.objects.create(
            user=self.user,
            character_id=corp_id * 10 + 1,
            character_name="Main",
            corporation_id=corp_id,
        )
        self.user.main_character = ch
        self.user.save()
        # Reload so main_character is attached fresh.
        return SRPClaim.objects.select_related(
            "submitter", "submitter__main_character"
        )

    # -- Blue check ------------------------------------------------------
    def test_blue_unconfigured_is_neutral_not_green(self):
        cfg = self._cfg(blue_alliance_ids=[], blue_corp_ids=[])
        claim = self._claim(
            {"attackers": [{"character_id": 5, "alliance_id": 111}], "victim": {}}
        )
        res = blue_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.NA)
        self.assertEqual(res.badge_class, "secondary")  # NOT success/green
        self.assertIn("not configured", res.label.lower())

    def test_blue_configured_and_clean_is_green(self):
        cfg = self._cfg(blue_alliance_ids=[999])
        claim = self._claim(
            {"attackers": [{"character_id": 5, "alliance_id": 111}], "victim": {}}
        )
        res = blue_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.CLEAN)
        self.assertEqual(res.badge_class, "success")

    def test_blue_configured_and_hit_is_warn(self):
        cfg = self._cfg(blue_alliance_ids=[111])
        claim = self._claim(
            {"attackers": [{"character_id": 5, "alliance_id": 111}], "victim": {}}
        )
        res = blue_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.WARN)

    # -- Non-TNT check ---------------------------------------------------
    def test_non_tnt_unconfigured_is_neutral_not_green(self):
        cfg = self._cfg(self_alliance_ids=[])
        claim = self._claim({"attackers": [], "victim": {"alliance_id": 222}})
        res = non_tnt_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.NA)
        self.assertEqual(res.badge_class, "secondary")

    def test_non_tnt_configured_victim_outside_is_warn(self):
        cfg = self._cfg(self_alliance_ids=[100])
        claim = self._claim({"attackers": [], "victim": {"alliance_id": 222}})
        res = non_tnt_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.WARN)

    def test_non_tnt_configured_victim_inside_is_green(self):
        cfg = self._cfg(self_alliance_ids=[222])
        claim = self._claim({"attackers": [], "victim": {"alliance_id": 222}})
        res = non_tnt_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.CLEAN)

    # -- Corp mismatch check --------------------------------------------
    def test_corp_uncomputable_is_neutral_not_green(self):
        # No linked main character -> submitter corp unknown -> NA.
        claim = self._claim({"attackers": [], "victim": {"corporation_id": 500}})
        res = corp_mismatch_check(claim)
        self.assertEqual(res.state, CheckResult.NA)
        self.assertEqual(res.badge_class, "secondary")

    def test_corp_mismatch_is_warn(self):
        qs = self._link_main(corp_id=500)
        claim = self._claim({"attackers": [], "victim": {"corporation_id": 999}})
        claim = qs.get(id=claim.id)
        res = corp_mismatch_check(claim)
        self.assertEqual(res.state, CheckResult.WARN)

    def test_corp_match_is_green(self):
        qs = self._link_main(corp_id=500)
        claim = self._claim({"attackers": [], "victim": {"corporation_id": 500}})
        claim = qs.get(id=claim.id)
        res = corp_mismatch_check(claim)
        self.assertEqual(res.state, CheckResult.CLEAN)


# ---------------------------------------------------------------------------
# A2 — NPC flag gated by SRPConfig.npc_damage_threshold (no more over-firing).
# ---------------------------------------------------------------------------
class SRPNpcCheckTests(TestCase):
    """
    The NPC flag must not fire on any NPC attacker regardless of damage. It
    fires only when NPC-only or NPC damage share >= threshold; below-threshold
    involvement is neutral info that carries the %. Missing config -> neutral
    sentinel, never a false-clean.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="x")

    def _claim(self, attackers):
        return SRPClaim.objects.create(
            submitter=self.user,
            character_name="Pilot",
            category="STRATEGIC",
            status="PENDING",
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            killmail_raw={"attackers": attackers, "victim": {}},
        )

    def _cfg(self, threshold=50):
        cfg = SRPConfig.get()
        cfg.npc_damage_threshold = threshold
        cfg.save()
        return cfg

    def test_no_npc_is_clean(self):
        cfg = self._cfg()
        claim = self._claim([{"character_id": 1, "damage_done": 100}])
        res = npc_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.CLEAN)

    def test_npc_only_always_warns(self):
        cfg = self._cfg(threshold=90)  # even with a high threshold
        claim = self._claim([{"damage_done": 100}])  # no character_id -> NPC
        res = npc_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.WARN)
        self.assertIn("NPC only", res.label)

    def test_npc_above_threshold_warns_and_carries_pct(self):
        cfg = self._cfg(threshold=50)
        claim = self._claim(
            [
                {"damage_done": 90},  # NPC, 90%
                {"character_id": 1, "damage_done": 10},  # player, 10%
            ]
        )
        res = npc_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.WARN)
        self.assertEqual(res.npc_damage_pct, 90.0)
        self.assertIn("90", res.label)

    def test_npc_below_threshold_is_neutral_info_not_warn(self):
        cfg = self._cfg(threshold=50)
        claim = self._claim(
            [
                {"damage_done": 10},  # NPC, 10% (e.g. a gate gun tick)
                {"character_id": 1, "damage_done": 90},  # player, 90%
            ]
        )
        res = npc_check(claim, cfg)
        self.assertEqual(res.state, CheckResult.INFO)
        self.assertEqual(res.badge_class, "secondary")  # neutral, NOT warning
        self.assertTrue(res.npc_present)
        self.assertIn("10", res.label)

    def test_threshold_unconfigured_cfg_none_is_neutral_sentinel(self):
        # Defensive: no SRPConfig at all -> neutral sentinel, never false-clean.
        claim = self._claim(
            [
                {"damage_done": 90},
                {"character_id": 1, "damage_done": 10},
            ]
        )
        res = npc_check(claim, None)
        self.assertEqual(res.state, CheckResult.NA)
        self.assertEqual(res.badge_class, "secondary")
        self.assertIn("not configured", res.label.lower())


# ---------------------------------------------------------------------------
# A3 — dead multiplier + ceilings are now wired in.
# ---------------------------------------------------------------------------
class SRPMultiplierCeilingTests(TestCase):
    def setUp(self):
        cfg = SRPConfig.get()
        cfg.auto_calculate_payouts = True
        cfg.default_multiplier = Decimal("1")
        cfg.monthly_ceiling_peacetime = Decimal("0")
        cfg.monthly_ceiling_strategic = Decimal("0")
        cfg.save()
        self.user = User.objects.create_user(username="pilot", password="x")

    def _ship(self, name="Rifter", **cols):
        defaults = dict(strategic=0, peacetime=0, shitstack=0, tnt_special=0)
        defaults.update(cols)
        return ShipPayout.objects.create(ship_name=name, **defaults)

    def _claim(self, category="STRATEGIC", ship=None, status="PENDING", **extra):
        return SRPClaim.objects.create(
            submitter=self.user,
            character_name="Pilot",
            category=category,
            ship=ship,
            status=status,
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            **extra,
        )

    def _set_multiplier(self, m):
        cfg = SRPConfig.get()
        cfg.default_multiplier = Decimal(str(m))
        cfg.save()

    # -- multiplier ------------------------------------------------------
    def test_multiplier_default_one_is_behavior_neutral(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        self.assertEqual(claim.payout_amount, M100)

    def test_multiplier_scales_pending_payout(self):
        self._set_multiplier(2)
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)  # save() recomputes while PENDING
        self.assertEqual(claim.payout_amount, M200)
        self.assertEqual(claim.calculate_payout(), M200)

    def test_multiplier_respects_freeze(self):
        # Approve at 1x -> frozen 100M. Later multiplier bump must NOT re-scale.
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()
        self.assertEqual(claim.payout_amount, M100)

        self._set_multiplier(3)
        claim.refresh_from_db()
        claim.save()  # re-save of an APPROVED claim: frozen, no re-scale
        self.assertEqual(claim.payout_amount, M100)

    def test_multiplier_never_scales_manual(self):
        self._set_multiplier(5)
        claim = self._claim(
            category="MANUAL", payout_amount=M50, reviewer=self.user
        )
        self.assertEqual(claim.payout_amount, M50)

    # -- ceilings --------------------------------------------------------
    def _approved_peacetime(self, amount):
        ship = self._ship(name=f"P{amount}", peacetime=amount)
        claim = self._claim(category="PEACETIME", ship=ship, broadcast_text="op")
        claim.set_status("APPROVED", reviewer=self.user)
        claim.save()
        return claim

    def test_ceiling_disabled_when_zero(self):
        cfg = SRPConfig.get()  # monthly_ceiling_peacetime == 0
        self.assertIsNone(category_ceiling_status("PEACETIME", cfg))

    def test_ceiling_no_field_for_category(self):
        cfg = SRPConfig.get()
        cfg.monthly_ceiling_peacetime = M200
        cfg.save()
        # SHITSTACK has no ceiling field -> always None.
        self.assertIsNone(category_ceiling_status("SHITSTACK", cfg))

    def test_ceiling_under_is_not_over(self):
        cfg = SRPConfig.get()
        cfg.monthly_ceiling_peacetime = M200
        cfg.save()
        self._approved_peacetime(M100)
        r = category_ceiling_status("PEACETIME", cfg)
        self.assertEqual(r["total"], M100)
        self.assertFalse(r["over"])

    def test_ceiling_over_trips_warning(self):
        cfg = SRPConfig.get()
        cfg.monthly_ceiling_peacetime = M50
        cfg.save()
        self._approved_peacetime(M100)  # 100M > 50M ceiling
        r = category_ceiling_status("PEACETIME", cfg)
        self.assertEqual(r["total"], M100)
        self.assertTrue(r["over"])


# ---------------------------------------------------------------------------
# A4 — CSV importer no longer silently zeroes tiers.
# ---------------------------------------------------------------------------
class SRPBulkImportTests(TestCase):
    """
    Following the app's own (previously wrong) instructions must not overwrite
    payout tiers with 0: header matching is case-insensitive/trimmed, a MISSING
    column leaves that tier unchanged, and an explicit 0 still sets 0.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="boss", password="x", email="boss@example.com"
        )
        self.client.force_login(self.admin)

    def _job(self, csv_text):
        from .models import PayoutImportJob

        return PayoutImportJob.objects.create(
            created_by=self.admin, csv_text=csv_text, original_filename="t.csv"
        )

    def _apply(self, job):
        return self.client.post(
            "/srp/admin/payouts/bulk/apply/", {"job_id": job.id}
        )

    def test_missing_tier_column_leaves_tier_unchanged(self):
        ship = ShipPayout.objects.create(
            ship_name="Rifter",
            strategic=M100,
            peacetime=M100,
            shitstack=M100,
            tnt_special=M100,
        )
        # Lowercase headers; only Strategic present (Peacetime/Shitstack/TNT omitted).
        self._apply(self._job("ship name,strategic\nRifter,200000000\n"))
        ship.refresh_from_db()
        self.assertEqual(ship.strategic, M200)  # case-insensitive header matched
        self.assertEqual(ship.peacetime, M100)  # missing column -> unchanged
        self.assertEqual(ship.shitstack, M100)  # missing column -> unchanged
        self.assertEqual(ship.tnt_special, M100)  # missing column -> unchanged

    def test_explicit_zero_sets_zero(self):
        ship = ShipPayout.objects.create(ship_name="Rifter", strategic=M100)
        self._apply(self._job("Ship Name,Strategic\nRifter,0\n"))
        ship.refresh_from_db()
        self.assertEqual(ship.strategic, Decimal("0"))  # deliberate zeroing works

    def test_blank_cell_leaves_unchanged(self):
        ship = ShipPayout.objects.create(ship_name="Rifter", strategic=M100)
        self._apply(self._job("Ship Name,Strategic\nRifter,\n"))
        ship.refresh_from_db()
        self.assertEqual(ship.strategic, M100)  # blank cell -> unchanged, not 0

    def test_tnt_special_column_applied_case_insensitive(self):
        ship = ShipPayout.objects.create(ship_name="Rifter", tnt_special=M100)
        self._apply(self._job("Ship Name,tnt special\nRifter,200000000\n"))
        ship.refresh_from_db()
        self.assertEqual(ship.tnt_special, M200)

    def test_create_absent_tiers_default_zero(self):
        self._apply(self._job("Ship Name,Strategic\nNewHull,300000000\n"))
        s = ShipPayout.objects.get(ship_name="NewHull")
        self.assertEqual(s.strategic, Decimal("300000000"))
        self.assertEqual(s.peacetime, Decimal("0"))
        self.assertEqual(s.tnt_special, Decimal("0"))

    def test_preview_marks_unchanged_and_changed(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        ShipPayout.objects.create(ship_name="Rifter", strategic=M100, tnt_special=M100)
        f = SimpleUploadedFile(
            "p.csv", b"Ship Name,Strategic\nRifter,200000000\n", content_type="text/csv"
        )
        r = self.client.post("/srp/admin/payouts/bulk/", {"file": f})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("unchanged", html)  # tnt_special/peacetime left alone
        self.assertIn("changed", html)  # strategic changed


# ---------------------------------------------------------------------------
# B0 — Reviewer edits are status-guarded (P2-4). The edit path recomputes via
# calculate_payout() (which since A3 applies default_multiplier), so allowing it
# on a frozen APPROVED/PAID claim would RE-SCALE money already committed. Edits
# are allowed ONLY while PENDING; a POST on a processed claim is rejected
# server-side and writes NO audit row.
# ---------------------------------------------------------------------------
class SRPReviewerEditGuardTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Permission

        self.reviewer = User.objects.create_user(username="rev", password="x")
        perm = Permission.objects.get(
            content_type__app_label="srp", codename="can_review_srp"
        )
        self.reviewer.user_permissions.add(perm)
        self.client.force_login(self.reviewer)

        cfg = SRPConfig.get()
        cfg.auto_calculate_payouts = True
        cfg.default_multiplier = Decimal("1")
        cfg.save()

    def _ship(self, name="Rifter", **cols):
        defaults = dict(strategic=0, peacetime=0, shitstack=0, tnt_special=0)
        defaults.update(cols)
        return ShipPayout.objects.create(ship_name=name, **defaults)

    def _claim(self, ship=None, category="STRATEGIC", status="PENDING", **extra):
        return SRPClaim.objects.create(
            submitter=self.reviewer,
            character_name="Pilot",
            category=category,
            ship=ship,
            status=status,
            broadcast_text="op",
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            **extra,
        )

    def _url(self, claim):
        return f"/srp/claim/{claim.id}/"

    def test_edit_rejected_on_paid_claim_no_rescale_no_audit(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        claim.set_status("APPROVED", reviewer=self.reviewer)
        claim.save()
        claim.set_status("PAID", reviewer=self.reviewer)
        claim.paid_at = timezone.now()
        claim.save()
        self.assertEqual(claim.payout_amount, M100)

        # Multiplier bumps under the frozen claim; an edit POST must NOT re-scale.
        cfg = SRPConfig.get()
        cfg.default_multiplier = Decimal("3")
        cfg.save()

        resp = self.client.post(
            self._url(claim),
            {"edit_claim": "1", "category": "STRATEGIC", "payout_amount": ""},
        )
        self.assertEqual(resp.status_code, 302)  # rejected + redirect, no 500
        claim.refresh_from_db()
        self.assertEqual(claim.status, "PAID")
        self.assertEqual(claim.payout_amount, M100)  # frozen, NOT 300M
        self.assertFalse(
            ClaimReview.objects.filter(claim=claim, action="Edited").exists()
        )

    def test_edit_form_not_offered_on_approved(self):
        ship = self._ship(strategic=M100)
        claim = self._claim(ship=ship)
        claim.set_status("APPROVED", reviewer=self.reviewer)
        claim.save()

        resp = self.client.get(self._url(claim))
        self.assertIsNone(resp.context["edit_form"])
        self.assertTrue(resp.context["edit_locked"])

    def test_edit_applies_and_audits_on_pending(self):
        # Strategic 100M provisional; edit to Shitstack (50M) — no broadcast/
        # reviewer model-clean traps on that category — recompute + audit row.
        ship = self._ship(strategic=M100, shitstack=M50)
        claim = self._claim(ship=ship, category="STRATEGIC")
        self.assertEqual(claim.payout_amount, M100)

        resp = self.client.post(
            self._url(claim),
            {"edit_claim": "1", "category": "SHITSTACK", "payout_amount": ""},
        )
        self.assertEqual(resp.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.category, "SHITSTACK")
        self.assertEqual(claim.payout_amount, M50)  # recomputed on the edit
        self.assertTrue(
            ClaimReview.objects.filter(claim=claim, action="Edited").exists()
        )


# ---------------------------------------------------------------------------
# B1 — Batch actions + default-to-Pending (Cluster B). Batch approve must run
# the same per-claim safeguards as single approve: money-warning claims are
# SKIPPED (not approved), illegal transitions are skipped (never 500), every
# processed claim writes its own audit row, and the queue defaults to PENDING.
# ---------------------------------------------------------------------------
class SRPBatchActionTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Permission

        self.reviewer = User.objects.create_user(username="rev", password="x")
        perm = Permission.objects.get(
            content_type__app_label="srp", codename="can_review_srp"
        )
        self.reviewer.user_permissions.add(perm)
        self.client.force_login(self.reviewer)

        cfg = SRPConfig.get()
        cfg.auto_calculate_payouts = True
        cfg.default_multiplier = Decimal("1")
        cfg.monthly_ceiling_strategic = Decimal("0")
        cfg.monthly_ceiling_peacetime = Decimal("0")
        cfg.save()

    def _ship(self, name, **cols):
        d = dict(strategic=0, peacetime=0, shitstack=0, tnt_special=0)
        d.update(cols)
        return ShipPayout.objects.create(ship_name=name, **d)

    def _claim(self, ship=None, category="STRATEGIC", status="PENDING", **extra):
        return SRPClaim.objects.create(
            submitter=self.reviewer,
            character_name="Pilot",
            category=category,
            ship=ship,
            status=status,
            broadcast_text="op",
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            **extra,
        )

    def test_batch_approve_skips_unfunded_and_audits_rest(self):
        funded = self._claim(ship=self._ship("Funded", strategic=M100))
        unfunded = self._claim(ship=self._ship("Unfunded", strategic=0))
        resp = self.client.post(
            "/srp/queue/batch/",
            {
                "batch_action": "approve",
                "claim_ids": [funded.id, unfunded.id],
                "comment": "batch pass",
            },
        )
        self.assertEqual(resp.status_code, 302)
        funded.refresh_from_db()
        unfunded.refresh_from_db()
        self.assertEqual(funded.status, "APPROVED")
        self.assertEqual(funded.payout_amount, M100)  # frozen snapshot at approval
        self.assertEqual(unfunded.status, "PENDING")  # skipped, NOT approved
        self.assertTrue(
            ClaimReview.objects.filter(claim=funded, action="Approved").exists()
        )
        self.assertFalse(
            ClaimReview.objects.filter(claim=unfunded, action="Approved").exists()
        )

    def test_batch_approve_skips_over_ceiling(self):
        cfg = SRPConfig.get()
        cfg.monthly_ceiling_strategic = M50
        cfg.save()
        big = self._claim(ship=self._ship("Big", strategic=M100))  # 100M > 50M
        resp = self.client.post(
            "/srp/queue/batch/",
            {"batch_action": "approve", "claim_ids": [big.id]},
        )
        self.assertEqual(resp.status_code, 302)
        big.refresh_from_db()
        self.assertEqual(big.status, "PENDING")  # skipped: over ceiling
        self.assertFalse(
            ClaimReview.objects.filter(claim=big, action="Approved").exists()
        )

    def test_batch_approve_skips_illegal_transition_no_500(self):
        paid = self._claim(ship=self._ship("Paid", strategic=M100))
        paid.set_status("APPROVED", reviewer=self.reviewer)
        paid.save()
        paid.set_status("PAID", reviewer=self.reviewer)
        paid.paid_at = timezone.now()
        paid.save()
        resp = self.client.post(
            "/srp/queue/batch/",
            {"batch_action": "approve", "claim_ids": [paid.id]},
        )
        self.assertEqual(resp.status_code, 302)  # redirect, never 500
        paid.refresh_from_db()
        self.assertEqual(paid.status, "PAID")  # unchanged

    def test_batch_deny_shared_note_audits_each(self):
        c1 = self._claim(ship=self._ship("A", strategic=M100))
        c2 = self._claim(ship=self._ship("B", strategic=M100))
        resp = self.client.post(
            "/srp/queue/batch/",
            {
                "batch_action": "deny",
                "claim_ids": [c1.id, c2.id],
                "comment": "spy loss",
            },
        )
        self.assertEqual(resp.status_code, 302)
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c1.status, "DENIED")
        self.assertEqual(c2.status, "DENIED")
        self.assertTrue(
            ClaimReview.objects.filter(
                claim=c1, action="Denied", comment="spy loss"
            ).exists()
        )
        self.assertTrue(
            ClaimReview.objects.filter(
                claim=c2, action="Denied", comment="spy loss"
            ).exists()
        )

    def test_queue_defaults_to_pending(self):
        pending = self._claim(ship=self._ship("P", strategic=M100))
        approved = self._claim(ship=self._ship("Q", strategic=M100))
        approved.set_status("APPROVED", reviewer=self.reviewer)
        approved.save()

        resp = self.client.get("/srp/queue/")
        self.assertEqual(resp.context["status"], "PENDING")
        ids = [c.id for c in resp.context["claims"]]
        self.assertIn(pending.id, ids)
        self.assertNotIn(approved.id, ids)  # non-pending hidden by default

    def test_queue_status_all_shows_everything(self):
        pending = self._claim(ship=self._ship("P", strategic=M100))
        approved = self._claim(ship=self._ship("Q", strategic=M100))
        approved.set_status("APPROVED", reviewer=self.reviewer)
        approved.save()

        resp = self.client.get("/srp/queue/?status=all")
        ids = [c.id for c in resp.context["claims"]]
        self.assertIn(pending.id, ids)
        self.assertIn(approved.id, ids)


# ---------------------------------------------------------------------------
# B2 — Fit check precomputed at submission so the queue badge is populated
# before a reviewer opens the claim. When ESI/killmail data is absent the
# stored status is an HONEST sentinel (NO_KILLMAIL), never blank/false-clean.
# No live ESI: uses stored killmail_raw / factory data only.
# ---------------------------------------------------------------------------
class SRPFitcheckPrecomputeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="x")

    def _claim(self, **extra):
        return SRPClaim.objects.create(
            submitter=self.user,
            character_name="Pilot",
            category="STRATEGIC",
            status="PENDING",
            broadcast_text="op",
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            **extra,
        )

    def test_no_killmail_stores_honest_sentinel(self):
        from .fitcheck import FITCHECK_NO_KILLMAIL, precompute_fitcheck_on_submit

        claim = self._claim(killmail_raw=None)
        self.assertEqual(claim.fitcheck_status, "")  # nothing yet
        precompute_fitcheck_on_submit(claim)
        claim.refresh_from_db()
        # Honest "no data" sentinel, NOT a blank badge and NOT a false-clean.
        self.assertEqual(claim.fitcheck_status, FITCHECK_NO_KILLMAIL)
        self.assertIsNotNone(claim.fitcheck_updated_at)

    def test_empty_esi_body_stores_sentinel(self):
        from .fitcheck import FITCHECK_NO_KILLMAIL, precompute_fitcheck_on_submit

        # populate_claim_from_esi returns True on an empty fetch (P2-3), storing
        # killmail_raw={} — falsy. The badge must still be honest, not blank.
        claim = self._claim(killmail_raw={})
        precompute_fitcheck_on_submit(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.fitcheck_status, FITCHECK_NO_KILLMAIL)

    def test_badge_populated_from_stored_killmail_no_live_esi(self):
        from .fitcheck import precompute_fitcheck_on_submit
        from .models import DoctrineFit, DoctrineFitItem

        ship_type_id = 587  # Rifter
        fit = DoctrineFit.objects.create(
            ship_type_id=ship_type_id,
            ship_name="Rifter",
            name="Doctrine",
            eft_text="x",
            active=True,
        )
        # Expected: one high-slot module (type 111 x1).
        DoctrineFitItem.objects.create(
            doctrine_fit=fit, slot_group="HIGH", type_id=111, qty=1
        )
        km = {
            "victim": {
                "ship_type_id": ship_type_id,
                "items": [
                    # flag 27 -> High Slots (see slots.slot_group_from_flag)
                    {"item_type_id": 111, "flag": 27, "quantity_destroyed": 1},
                ],
            },
            "attackers": [],
        }
        claim = self._claim(killmail_raw=km, ship_type_id=ship_type_id)
        precompute_fitcheck_on_submit(claim)
        claim.refresh_from_db()

        # Badge is a real verdict at submission time (no reviewer opened it yet,
        # no live ESI touched).
        self.assertTrue(claim.fitcheck_status)
        self.assertNotEqual(claim.fitcheck_status, "NO_KILLMAIL")
        self.assertIn(
            claim.fitcheck_status, {"FIT_OK", "FIT_CLOSE", "FIT_MISMATCH"}
        )
        self.assertEqual(claim.fitcheck_best_fit_id, fit.id)
        self.assertIsNotNone(claim.fitcheck_updated_at)


# ---------------------------------------------------------------------------
# P1-4 — Ownership check on claim submission + review-time signal.
#
# No live ESI: populate_claim_from_esi is patched to set the resolved victim on
# the claim, exactly as the real ESI pull would. Covers the four required cases:
# member files own loss (accepted), member files someone else's (rejected),
# reviewer files on behalf (accepted + INFO), ESI-unknown victim (accepted + NA).
# ---------------------------------------------------------------------------
from unittest import mock  # noqa: E402


def _fake_esi(victim_id=None, victim_name=None, ok=True):
    """A populate_claim_from_esi stand-in: sets the victim the way ESI would."""

    def _inner(claim):
        claim.killmail_id = 999001
        claim.killmail_hash = "deadbeef"
        victim_blob = {}
        if victim_id is not None:
            victim_blob["character_id"] = victim_id
        claim.killmail_raw = {"victim": victim_blob, "attackers": []}
        claim.victim_character_id = victim_id
        claim.victim_character_name = victim_name
        return ok

    return _inner


class SRPOwnershipCheckHelperTests(TestCase):
    """Unit-level: ownership_check tri-state + the alt corp false-positive fix."""

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="x")

    def _char(self, user, character_id, corp_id=None):
        from eve_sso.models import EveCharacter

        return EveCharacter.objects.create(
            user=user,
            character_id=character_id,
            character_name=f"Char{character_id}",
            corporation_id=corp_id,
        )

    def _reload(self):
        return SRPClaim.objects.select_related(
            "submitter", "submitter__main_character"
        )

    def _claim(self, victim_id=None, victim_name=None, corp_id=None):
        km = {"victim": {}, "attackers": []}
        if corp_id is not None:
            km["victim"]["corporation_id"] = corp_id
        return SRPClaim.objects.create(
            submitter=self.user,
            character_name="Pilot",
            category="STRATEGIC",
            status="PENDING",
            broadcast_text="op",
            esi_link="https://esi.evetech.net/latest/killmails/1/abc/",
            killmail_raw=km,
            victim_character_id=victim_id,
            victim_character_name=victim_name,
        )

    def test_unknown_victim_is_na_sentinel(self):
        claim = self._claim(victim_id=None)
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.NA)
        self.assertEqual(res.badge_class, "secondary")  # neutral, NOT green
        self.assertIn("not verifiable", res.label.lower())

    def test_victim_is_own_main_is_clean(self):
        self._char(self.user, character_id=1001)
        self.user.main_character = self.user.eve_characters.first()
        self.user.save()
        claim = self._claim(victim_id=1001)
        claim = self._reload().get(id=claim.id)
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.CLEAN)

    def test_victim_is_own_alt_is_clean(self):
        self._char(self.user, character_id=1001, corp_id=100)  # main
        self.user.main_character = self.user.eve_characters.first()
        self.user.save()
        self._char(self.user, character_id=1002, corp_id=200)  # alt in another corp
        claim = self._claim(victim_id=1002)
        claim = self._reload().get(id=claim.id)
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.CLEAN)

    def test_member_files_others_loss_is_warn(self):
        self._char(self.user, character_id=1001)
        claim = self._claim(victim_id=2002, victim_name="SomeoneElse")
        claim = self._reload().get(id=claim.id)
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.WARN)
        self.assertEqual(res.badge_class, "danger")
        self.assertIn("SomeoneElse", res.label)

    def test_reviewer_files_others_loss_is_info_on_behalf(self):
        from django.contrib.auth.models import Permission

        perm = Permission.objects.get(
            content_type__app_label="srp", codename="can_review_srp"
        )
        self.user.user_permissions.add(perm)
        self.user = User.objects.get(id=self.user.id)  # drop the perm cache
        self._char(self.user, character_id=1001)
        claim = self._claim(victim_id=2002, victim_name="FleetMate")
        claim = self._reload().get(id=claim.id)
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.INFO)
        self.assertTrue(res.is_info)
        self.assertIn("behalf", res.label.lower())

    def test_submitter_character_ids_unions_main_and_alts(self):
        self._char(self.user, character_id=1001)
        self.user.main_character = self.user.eve_characters.first()
        self.user.save()
        self._char(self.user, character_id=1002)
        self.assertEqual(submitter_character_ids(self.user), {1001, 1002})

    # -- corp-mismatch no longer false-positives on a legit alt ----------
    def test_corp_check_own_alt_is_clean_not_mismatch(self):
        self._char(self.user, character_id=1001, corp_id=100)  # main in corp 100
        self.user.main_character = self.user.eve_characters.first()
        self.user.save()
        self._char(self.user, character_id=1002, corp_id=200)  # alt in corp 200
        # Victim is the alt (corp 200) while the main is corp 100 — pre-fix this
        # read as a corp mismatch WARN; now it's a clean own-character loss.
        claim = self._claim(victim_id=1002, corp_id=200)
        claim = self._reload().get(id=claim.id)
        res = corp_mismatch_check(claim)
        self.assertEqual(res.state, CheckResult.CLEAN)

    def test_corp_check_still_warns_on_genuine_cross_corp(self):
        # Victim is NOT the submitter's character and is in a different corp.
        self._char(self.user, character_id=1001, corp_id=100)
        self.user.main_character = self.user.eve_characters.first()
        self.user.save()
        claim = self._claim(victim_id=2002, corp_id=200)
        claim = self._reload().get(id=claim.id)
        res = corp_mismatch_check(claim)
        self.assertEqual(res.state, CheckResult.WARN)


class SRPOwnershipSubmitTests(TestCase):
    """
    End-to-end submit gate (P1-4). populate_claim_from_esi is patched — no live
    ESI. The gate must block a member filing another pilot's loss (form error,
    no row) while accepting own losses, reviewer-on-behalf, and unresolved
    victims (graceful degradation).
    """

    def setUp(self):
        cfg = SRPConfig.get()
        cfg.auto_calculate_payouts = True
        cfg.save()
        self.user = User.objects.create_user(username="pilot", password="x")

    def _char(self, user, character_id):
        from eve_sso.models import EveCharacter

        ch = EveCharacter.objects.create(
            user=user,
            character_id=character_id,
            character_name=f"Char{character_id}",
        )
        user.main_character = ch
        user.save()
        return ch

    def _post(self, category="SHITSTACK"):
        return self.client.post(
            "/srp/submit/",
            {
                "esi_link": (
                    "https://esi.evetech.net/latest/killmails/999001/deadbeef/"
                    "?datasource=tranquility"
                ),
                "category": category,
                "broadcast_text": "",
            },
        )

    def test_member_submits_own_loss_accepted(self):
        self._char(self.user, character_id=1001)
        self.client.force_login(self.user)
        with mock.patch(
            "srp.views.populate_claim_from_esi", _fake_esi(victim_id=1001)
        ):
            resp = self._post()
        self.assertEqual(resp.status_code, 302)  # redirect to my_claims
        self.assertEqual(SRPClaim.objects.count(), 1)
        claim = SRPClaim.objects.get()
        self.assertEqual(claim.submitter_id, self.user.id)
        self.assertEqual(ownership_check(claim).state, CheckResult.CLEAN)

    def test_member_submits_others_loss_rejected(self):
        self._char(self.user, character_id=1001)
        self.client.force_login(self.user)
        with mock.patch(
            "srp.views.populate_claim_from_esi",
            _fake_esi(victim_id=2002, victim_name="VictimPilot"),
        ):
            resp = self._post()
        # Re-render with a form error, NOT a redirect, and NOTHING persisted.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SRPClaim.objects.count(), 0)
        self.assertTrue(resp.context["form"].errors)
        self.assertContains(resp, "VictimPilot")

    def test_reviewer_files_on_behalf_accepted_and_info(self):
        from django.contrib.auth.models import Permission

        perm = Permission.objects.get(
            content_type__app_label="srp", codename="can_review_srp"
        )
        self.user.user_permissions.add(perm)
        self._char(self.user, character_id=1001)
        self.client.force_login(self.user)
        with mock.patch(
            "srp.views.populate_claim_from_esi",
            _fake_esi(victim_id=2002, victim_name="FleetMate"),
        ):
            resp = self._post()
        self.assertEqual(resp.status_code, 302)  # accepted
        self.assertEqual(SRPClaim.objects.count(), 1)
        claim = SRPClaim.objects.select_related(
            "submitter", "submitter__main_character"
        ).get()
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.INFO)
        self.assertIn("behalf", res.label.lower())

    def test_esi_unknown_victim_accepted_with_na_sentinel(self):
        self._char(self.user, character_id=1001)
        self.client.force_login(self.user)
        with mock.patch(
            "srp.views.populate_claim_from_esi",
            _fake_esi(victim_id=None, ok=True),
        ):
            resp = self._post()
        self.assertEqual(resp.status_code, 302)  # not blocked — graceful
        self.assertEqual(SRPClaim.objects.count(), 1)
        claim = SRPClaim.objects.get()
        res = ownership_check(claim)
        self.assertEqual(res.state, CheckResult.NA)
