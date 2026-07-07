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
