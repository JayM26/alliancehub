# srp/fitcheck.py
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

from .models import DoctrineFit, DoctrineFitItem, SRPClaim
from .slots import slot_group_from_flag  # ✅ shared helper


SLOT_GROUPS = ("High Slots", "Mid Slots", "Low Slots", "Rigs")


@dataclass(frozen=True)
class FitScore:
    fit: DoctrineFit
    match_pct: float
    expected_total: int
    matched: int
    missing: int
    extra: int
    score: float


def extract_actual_hmlr(killmail_raw: dict[str, Any]) -> dict[str, Counter[int]]:
    km = killmail_raw or {}
    victim = km.get("victim") or {}
    items = victim.get("items") or []

    actual: dict[str, Counter[int]] = {g: Counter() for g in SLOT_GROUPS}

    for it in items:
        flag = int(it.get("flag") or 0)
        group = slot_group_from_flag(flag)
        if not group:
            continue

        type_id = it.get("item_type_id")
        if not type_id:
            continue

        qty = int(it.get("quantity_destroyed") or 0) + int(
            it.get("quantity_dropped") or 0
        )
        if qty <= 0:
            qty = 1  # paranoia fallback

        actual[group][int(type_id)] += qty

    return actual


def build_expected_hmlr(fit: DoctrineFit) -> dict[str, Counter[int]]:
    expected: dict[str, Counter[int]] = {g: Counter() for g in SLOT_GROUPS}
    items = fit.items.all()

    slot_map = {
        DoctrineFitItem.SlotGroup.HIGH: "High Slots",
        DoctrineFitItem.SlotGroup.MID: "Mid Slots",
        DoctrineFitItem.SlotGroup.LOW: "Low Slots",
        DoctrineFitItem.SlotGroup.RIG: "Rigs",
    }

    for it in items:
        group = slot_map.get(it.slot_group)
        if not group:
            continue
        expected[group][int(it.type_id)] += int(it.qty)

    return expected


def score_fit(
    actual: dict[str, Counter[int]], expected: dict[str, Counter[int]], fit: DoctrineFit
) -> FitScore:
    expected_total = sum(sum(c.values()) for c in expected.values())
    if expected_total <= 0:
        return FitScore(
            fit=fit,
            match_pct=0.0,
            expected_total=0,
            matched=0,
            missing=0,
            extra=0,
            score=-1.0,
        )

    matched = 0
    extra = 0

    for group in SLOT_GROUPS:
        a = actual[group]
        e = expected[group]
        for tid, eqty in e.items():
            matched += min(a.get(tid, 0), eqty)
        for tid, aqty in a.items():
            extra += max(aqty - e.get(tid, 0), 0)

    missing = expected_total - matched
    match_pct = matched / expected_total

    # light penalty for extras
    penalty = (extra / expected_total) if expected_total else 0.0
    score = match_pct - 0.15 * penalty

    return FitScore(
        fit=fit,
        match_pct=round(match_pct, 4),
        expected_total=expected_total,
        matched=matched,
        missing=missing,
        extra=extra,
        score=round(score, 4),
    )


def diff_expected_vs_actual(
    expected: dict[str, Counter[int]], actual: dict[str, Counter[int]]
) -> dict[str, Any]:
    missing: dict[str, list[dict[str, int]]] = {}
    extra: dict[str, list[dict[str, int]]] = {}

    for group in SLOT_GROUPS:
        e = expected[group]
        a = actual[group]

        # Missing
        mlist: list[dict[str, int]] = []
        for tid, eqty in e.items():
            aqty = a.get(tid, 0)
            if aqty < eqty:
                mlist.append({"type_id": int(tid), "qty": int(eqty - aqty)})
        if mlist:
            missing[group] = mlist

        # Extra
        elist: list[dict[str, int]] = []
        for tid, aqty in a.items():
            eqty = e.get(tid, 0)
            if aqty > eqty:
                # Ignore non-module noise (ammo, scripts, etc.)
                if aqty > 5:
                    continue
                elist.append({"type_id": int(tid), "qty": int(aqty - eqty)})
        if elist:
            extra[group] = elist

    return {"missing": missing, "extra": extra}


def classify(match_pct: float, missing: int) -> str:
    # Tunable thresholds
    if missing == 0 and match_pct >= 0.95:
        return "FIT_OK"
    if match_pct >= 0.75:
        return "FIT_CLOSE"
    return "FIT_MISMATCH"


def compute_fitcheck(claim: SRPClaim) -> dict[str, Any]:
    # Always compute no-rigs flag
    actual = extract_actual_hmlr(claim.killmail_raw or {})
    no_rigs = sum(actual["Rigs"].values()) == 0

    if not claim.ship_type_id:
        return {
            "status": "",
            "best_fit_id": None,
            "best_fit_name": None,
            "match_pct": None,
            "no_rigs": no_rigs,
            "diff": None,
        }

    fits = list(
        DoctrineFit.objects.filter(
            ship_type_id=claim.ship_type_id, active=True
        ).prefetch_related("items")
    )

    if not fits:
        return {
            "status": "NO_DOCTRINE_FIT",
            "best_fit_id": None,
            "best_fit_name": None,
            "match_pct": None,
            "no_rigs": no_rigs,
            "diff": None,
        }

    scored: list[FitScore] = []
    for fit in fits:
        expected = build_expected_hmlr(fit)
        scored.append(score_fit(actual, expected, fit))

    scored.sort(key=lambda s: s.score, reverse=True)
    best = scored[0]

    expected_best = build_expected_hmlr(best.fit)
    diff = diff_expected_vs_actual(expected_best, actual)

    status = classify(best.match_pct, best.missing)

    return {
        "status": status,
        "best_fit_id": best.fit.id,
        "best_fit_name": best.fit.name,
        "match_pct": best.match_pct,
        "no_rigs": no_rigs,
        "diff": diff,
    }


def ensure_fitcheck_cached(claim: SRPClaim) -> None:
    """
    Lazy cache updater. Safe to call from claim_detail().
    """

    # If we have no ship_type_id or no killmail, nothing to do.
    if not claim.killmail_raw:
        return

    # TEMP: force recompute during development
    # needs = True

    # Simple invalidation rule for MVP:
    # If cache missing, compute. If any fit updated after cache time, compute.
    needs = claim.fitcheck_updated_at is None or not claim.fitcheck_status

    if not needs and claim.ship_type_id:
        newest = (
            DoctrineFit.objects.filter(ship_type_id=claim.ship_type_id, active=True)
            .order_by("-updated_at")
            .values_list("updated_at", flat=True)
            .first()
        )
        if newest and claim.fitcheck_updated_at and newest > claim.fitcheck_updated_at:
            needs = True

    if not needs:
        return

    result = compute_fitcheck(claim)

    claim.no_rigs_flag = bool(result.get("no_rigs"))
    claim.fitcheck_status = result.get("status") or ""
    best_fit_id = result.get("best_fit_id")
    claim.fitcheck_best_fit_id = best_fit_id
    # Don't auto-change selected fit
    claim.fitcheck_data = result
    claim.fitcheck_updated_at = timezone.now()
    claim.save(
        update_fields=[
            "fitcheck_status",
            "fitcheck_best_fit",
            "fitcheck_data",
            "fitcheck_updated_at",
            "no_rigs_flag",
        ]
    )
