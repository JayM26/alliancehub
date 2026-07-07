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
)
from .models import ShipPayout, SRPClaim, SRPConfig

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
