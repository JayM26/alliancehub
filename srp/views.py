"""
SRP views.

End-state principles:
- Views own HTTP concerns (request parsing, permissions, rendering, redirects).
- Business logic is kept in model methods or dedicated helpers (fitcheck, ESI helpers, importer).
- Avoid duplicated “magic” constants and slot grouping logic; use shared helpers consistently.
- Keep templates simple by attaching clearly-named computed attributes to claim objects
  (e.g., flag_* and fitting preview fields) only when needed for that view.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib import messages  # pyright: ignore[reportMissingModuleSource]
from django.contrib.auth.decorators import (  # pyright: ignore[reportMissingModuleSource]
    login_required,
    permission_required,
)
from django.db import transaction  # pyright: ignore[reportMissingModuleSource]
from django.db.models import (  # pyright: ignore[reportMissingModuleSource]
    Count,
    Max,
    Q,
    Sum,
)
from django.db.models.functions import (  # pyright: ignore[reportMissingModuleSource]
    Coalesce,
)
from django.shortcuts import (  # pyright: ignore[reportMissingModuleSource]
    get_object_or_404,
    redirect,
    render,
)
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]
from django.views.decorators.http import (  # pyright: ignore[reportMissingModuleSource]
    require_POST,
)

from .checks import (
    approve_block_reason,
    category_ceiling_status,
    claim_auto_checks,
)
from .esi import fetch_type_name, get_type_names_cached, populate_claim_from_esi
from .fit_importer import import_eft_fit
from .fitcheck import ensure_fitcheck_cached
from .forms import (
    DoctrineFitEditForm,
    DoctrineFitImportForm,
    ShipPayoutForm,
    SRPClaimForm,
    SRPClaimReviewerEditForm,
)
from .models import (
    ClaimReview,
    DoctrineFit,
    PayoutImportJob,
    ShipPayout,
    SRPClaim,
    SRPConfig,
)
from .slots import slot_group_from_flag


# Shared display order for slot-grouped fitting output.
SLOT_GROUP_ORDER = (
    "High Slots",
    "Mid Slots",
    "Low Slots",
    "Rigs",
    "Cargo",
    "Drone Bay",
    "Other",
)


# ---------------------------------------------------------------------------
# User views
# ---------------------------------------------------------------------------


def payout_table(request):
    """View showing all ships and their payout values."""
    ships = ShipPayout.objects.all()
    return render(request, "srp/payout_table.html", {"ships": ships})


@login_required
def submit_claim(request):
    """Allow a logged-in user to submit a new SRP claim."""
    if request.method == "POST":
        form = SRPClaimForm(request.POST)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.submitter = request.user
            claim.character_name = request.user.username
            claim.save()

            # Best-effort ESI enrichment (never block submission).
            try:
                ok = populate_claim_from_esi(claim)

                # If we have a ship_type_id but ship_name didn't resolve, try once more.
                if claim.ship_type_id and not claim.ship_name:
                    try:
                        claim.ship_name = fetch_type_name(int(claim.ship_type_id))
                    except Exception:
                        pass

                # If we got a ship name from ESI and no ShipPayout selected, match/create one.
                if not claim.ship and claim.ship_name:
                    sp = ShipPayout.objects.filter(
                        ship_name__iexact=claim.ship_name
                    ).first()
                    if not sp:
                        sp = ShipPayout.objects.create(ship_name=claim.ship_name)
                    claim.ship = sp
                    claim.payout_amount = claim.calculate_payout()

                # Optional: backfill legacy system field for display/search.
                if claim.solar_system_name and not claim.system:
                    claim.system = claim.solar_system_name

                claim.save()

                if ok:
                    messages.success(
                        request,
                        f"Your SRP claim has been submitted. ESI pull OK: {claim.ship_name or 'Unknown ship'}"
                        f"{' in ' + claim.solar_system_name if claim.solar_system_name else ''}.",
                    )
                else:
                    messages.warning(
                        request,
                        "Your SRP claim has been submitted, but the link didn't look like an ESI killmail URL (missing /killmails/<id>/<hash>/).",
                    )
            except Exception as e:
                messages.warning(
                    request,
                    f"Your SRP claim has been submitted, but ESI pull failed: {e}",
                )

            return redirect("srp:my_claims")

        messages.error(request, "Please correct the errors below.")
    else:
        form = SRPClaimForm()

    return render(request, "srp/submit_claim.html", {"form": form})


@login_required
def my_claims(request):
    """List of claims submitted by the logged-in user."""
    claims = SRPClaim.objects.filter(submitter=request.user).order_by("-submitted_at")
    return render(request, "srp/my_claims.html", {"claims": claims})


# ---------------------------------------------------------------------------
# Reviewer queue + actions
# ---------------------------------------------------------------------------


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def review_queue(request):
    """
    Reviewer queue with simple filters:
    - status (default: PENDING — the queue's real job; ?status=all shows everything)
    - category
    - search (character/ship/system/link)

    For template convenience, this view attaches computed attributes to each claim:
    - flag_npc: bool (any NPC attacker present)
    - check_blue / check_corp / check_non_tnt: CheckResult tri-states
      (WARN / CLEAN / neutral-when-unconfigured) — see srp.checks.
    - fitting_item_count: int
    - fitting_groups_preview: list[tuple[group_name, list[str]]]
    """
    # Default to PENDING (the reviewer's actual working set) when no explicit
    # status param is given; an explicit ?status=all still shows everything.
    status = (request.GET.get("status") or "PENDING").strip().upper()
    category = (request.GET.get("category") or "").strip().upper()
    search = (request.GET.get("q", "") or "").strip()

    qs = SRPClaim.objects.select_related(
        "ship", "submitter", "submitter__main_character", "reviewer"
    ).all()

    if status != "ALL":
        qs = qs.filter(status=status)

    if category:
        qs = qs.filter(category=category)

    if search:
        qs = qs.filter(
            Q(character_name__icontains=search)
            | Q(ship__ship_name__icontains=search)
            | Q(system__icontains=search)
            | Q(region__icontains=search)
            | Q(esi_link__icontains=search)
        )

    claims = qs.select_related("fitcheck_best_fit").order_by("submitted_at")

    cfg = SRPConfig.get()

    from collections import defaultdict

    for c in claims:
        km = c.killmail_raw or {}
        attackers = km.get("attackers") or []
        victim_blob = km.get("victim") or {}

        # Tri-state auto-checks (blue / corp mismatch / non-TNT). Each is a
        # CheckResult so the template can render an unconfigured/uncomputable
        # check as a NEUTRAL badge instead of a false-green or a silent pass.
        checks = claim_auto_checks(c, cfg)
        c.check_blue = checks["blue"]
        c.check_corp = checks["corp"]
        c.check_non_tnt = checks["non_tnt"]
        # NPC involvement, gated by SRPConfig.npc_damage_threshold. WARN only
        # when NPC-only or NPC damage >= threshold; below-threshold involvement
        # renders as neutral info (carries the %), never a bare binary alarm.
        c.check_npc = checks["npc"]

        # Fitting preview (grouped, bounded).
        items = (victim_blob.get("items") or []) if victim_blob else []
        c.fitting_item_count = len(items)
        c.fitting_groups_preview = []  # list[tuple[str, list[str]]]

        if not items:
            continue

        # Collect type IDs (bounded, de-duped in order).
        item_type_ids: list[int] = []
        seen: set[int] = set()
        for it in items:
            tid = it.get("item_type_id")
            if not tid:
                continue
            tid_i = int(tid)
            if tid_i in seen:
                continue
            seen.add(tid_i)
            item_type_ids.append(tid_i)
            if len(item_type_ids) >= 60:
                break

        type_names = get_type_names_cached(item_type_ids, fetch_cap=60)

        grouped: dict[str, list[str]] = defaultdict(list)

        # Build lines like: "Warp Disruptor II ×1"
        for it in items:
            tid = it.get("item_type_id")
            if not tid:
                continue

            flag = int(it.get("flag") or 0)
            group = slot_group_from_flag(flag, extended=True)

            name = type_names.get(int(tid)) or str(tid)
            qd = int(it.get("quantity_destroyed") or 0)
            qp = int(it.get("quantity_dropped") or 0)
            qty = qd + qp

            grouped[group].append(f"{name} ×{qty}" if qty else name)

        for g in SLOT_GROUP_ORDER:
            if grouped.get(g):
                c.fitting_groups_preview.append((g, grouped[g]))

    status_choices = ["PENDING", "APPROVED", "DENIED", "PAID"]

    context = {
        "claims": claims,
        "status": status,
        "status_choices": ["ALL"] + status_choices,
        "category": category,
        "q": search,
    }
    return render(request, "srp/review_queue.html", context)


def _add_review_record(claim: SRPClaim, reviewer, action: str, comment: str = ""):
    ClaimReview.objects.create(
        claim=claim,
        reviewer=reviewer,
        action=action,
        comment=comment or "",
    )


def _get_comment(request) -> str:
    return (request.POST.get("comment") or "").strip()


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def approve_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    comment = _get_comment(request)

    if claim.status == "APPROVED":
        claim.set_status(
            "PENDING", reviewer=request.user, note=comment or "Approval removed."
        )
        claim.save()
        _add_review_record(claim, request.user, "Unapproved", comment)
        messages.success(request, f"Unapproved claim #{claim.id} (back to Pending).")
    else:
        claim.set_status("APPROVED", reviewer=request.user, note=comment or "Approved.")
        claim.save()
        _add_review_record(claim, request.user, "Approved", comment)
        messages.success(request, f"Approved claim #{claim.id}.")
        if claim.needs_manual_payout:
            messages.warning(
                request,
                f"⚠ Claim #{claim.id} has no configured payout (0 ISK) — set a "
                f"Manual amount or configure the ship payout before paying.",
            )
        # Soft monthly-ceiling warning (never a hard block).
        ceiling = category_ceiling_status(claim.category)
        if ceiling and ceiling["over"]:
            messages.warning(
                request,
                f"⚠ {SRPClaim.category_label(claim.category)} approvals this "
                f"month now total {ceiling['total']:,.0f} ISK, over the "
                f"{ceiling['ceiling']:,.0f} ISK monthly ceiling.",
            )

    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def deny_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    comment = _get_comment(request)

    if claim.status == "DENIED":
        claim.set_status(
            "PENDING", reviewer=request.user, note=comment or "Denial removed."
        )
        claim.save()
        _add_review_record(claim, request.user, "Undenied", comment)
        messages.success(
            request, f"Removed denial on claim #{claim.id} (back to Pending)."
        )
    else:
        claim.set_status("DENIED", reviewer=request.user, note=comment or "Denied.")
        claim.save()
        _add_review_record(claim, request.user, "Denied", comment)
        messages.success(request, f"Denied claim #{claim.id}.")

    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def pay_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    comment = _get_comment(request)

    if claim.status == "PAID":
        claim.set_status(
            "APPROVED", reviewer=request.user, note=comment or "Payment mark removed."
        )
        claim.paid_at = None
        claim.save()
        _add_review_record(claim, request.user, "Unpaid", comment)
        messages.success(request, f"Unpaid claim #{claim.id} (back to Approved).")
    else:
        if claim.needs_manual_payout:
            messages.warning(
                request,
                f"⚠ Claim #{claim.id} has no configured payout (0 ISK) — set a "
                f"Manual amount or configure the ship payout before paying.",
            )
        claim.set_status("PAID", reviewer=request.user, note=comment or "Paid.")
        claim.paid_at = timezone.now()
        claim.save()
        _add_review_record(claim, request.user, "Paid", comment)
        messages.success(request, f"Marked claim #{claim.id} as Paid.")

    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
@require_POST
def batch_action(request):
    """
    Apply Approve or Deny to a set of selected claims in one request (B1).

    CRITICAL: batch approve must NOT bypass the per-claim safeguards P0 +
    Cluster A built. For each selected claim it runs the same checks as single
    approve; a claim that would trigger a money warning (unfunded, over the soft
    monthly ceiling) or isn't a legal transition is SKIPPED — never approved —
    and the result message reports exactly what happened, so the reviewer can
    handle the skipped ones singly (where they'll see the full warning). Every
    approved/denied claim writes its own ClaimReview audit row, same as the
    single actions. Illegal transitions are skipped and reported, never 500.
    """
    fallback = request.META.get("HTTP_REFERER") or "srp:review_queue"

    action = (request.POST.get("batch_action") or "").strip().lower()
    if action not in {"approve", "deny"}:
        messages.error(request, "Unknown batch action.")
        return redirect(fallback)

    comment = _get_comment(request)
    claim_ids = [
        int(x) for x in request.POST.getlist("claim_ids") if str(x).isdigit()
    ]
    if not claim_ids:
        messages.warning(request, "No claims were selected.")
        return redirect(fallback)

    cfg = SRPConfig.get()
    claims = SRPClaim.objects.filter(id__in=claim_ids)

    done = 0
    skipped: list[str] = []

    for claim in claims:
        if action == "approve":
            # Same safeguards as single-approve: only PENDING is a legal source,
            # and money-warning claims are skipped (not silently approved).
            if claim.status != SRPClaim.Status.PENDING:
                skipped.append(
                    f"#{claim.id} not pending ({claim.get_status_display()})"
                )
                continue
            reason = approve_block_reason(claim, cfg)
            if reason:
                skipped.append(f"#{claim.id} {reason}")
                continue
            try:
                claim.set_status(
                    "APPROVED",
                    reviewer=request.user,
                    note=comment or "Approved (batch).",
                )
                claim.save()
            except ValueError:
                skipped.append(f"#{claim.id} illegal transition")
                continue
            _add_review_record(claim, request.user, "Approved", comment)
            done += 1
        else:  # deny — legal from PENDING or APPROVED (not DENIED/PAID)
            if claim.status not in {
                SRPClaim.Status.PENDING,
                SRPClaim.Status.APPROVED,
            }:
                skipped.append(
                    f"#{claim.id} not deniable ({claim.get_status_display()})"
                )
                continue
            try:
                claim.set_status(
                    "DENIED",
                    reviewer=request.user,
                    note=comment or "Denied (batch).",
                )
                claim.save()
            except ValueError:
                skipped.append(f"#{claim.id} illegal transition")
                continue
            _add_review_record(claim, request.user, "Denied", comment)
            done += 1

    verb = "approved" if action == "approve" else "denied"
    parts = [f"{done} {verb}"]
    if skipped:
        parts.append(f"{len(skipped)} skipped — " + ", ".join(skipped))
    summary = "; ".join(parts)

    if skipped:
        messages.warning(request, summary)
    else:
        messages.success(request, summary)

    return redirect(fallback)


def _require_reviewer(user) -> bool:
    return user.has_perm("srp.can_review_srp")


# ---------------------------------------------------------------------------
# Doctrine fits (reviewer tools)
# ---------------------------------------------------------------------------


@login_required
def doctrine_fit_list(request):
    if not _require_reviewer(request.user):
        return redirect("srp:admin_overview")

    q = (request.GET.get("q") or "").strip()
    active = (request.GET.get("active") or "").strip()  # "", "1", "0"

    fits = DoctrineFit.objects.annotate(item_count=Count("items"))

    if q:
        fits = fits.filter(Q(ship_name__icontains=q) | Q(name__icontains=q))

    if active == "1":
        fits = fits.filter(active=True)
    elif active == "0":
        fits = fits.filter(active=False)

    fits = fits.order_by("ship_name", "name")

    return render(
        request,
        "srp/admin/doctrine_fits_list.html",
        {"fits": fits, "q": q, "active": active},
    )


@login_required
def doctrine_fit_import(request):
    if not _require_reviewer(request.user):
        return redirect("srp:overview")

    if request.method == "POST":
        form = DoctrineFitImportForm(request.POST)
        if form.is_valid():
            eft_text = form.cleaned_data["eft_text"]
            try:
                fit = import_eft_fit(eft_text=eft_text, updated_by=request.user)
            except Exception as e:
                messages.error(request, f"Import failed: {e}")
            else:
                messages.success(request, f"Imported fit: {fit.ship_name} — {fit.name}")
                return redirect("srp:doctrine_fit_detail", fit_id=fit.id)
    else:
        form = DoctrineFitImportForm()

    return render(request, "srp/admin/doctrine_fit_import.html", {"form": form})


@login_required
def doctrine_fit_detail(request, fit_id: int):
    if not _require_reviewer(request.user):
        return redirect("srp:overview")

    fit = get_object_or_404(DoctrineFit.objects.prefetch_related("items"), id=fit_id)

    if request.method == "POST":
        # Two actions:
        # 1) Overwrite items by re-importing EFT text
        # 2) Edit name/active
        if request.POST.get("overwrite") == "1":
            eft_text = (request.POST.get("eft_text") or "").strip()
            if not eft_text:
                messages.error(request, "Paste EFT text to overwrite this fit.")
            else:
                try:
                    import_eft_fit(
                        eft_text=eft_text,
                        updated_by=request.user,
                        overwrite_fit_id=fit.id,
                    )
                except Exception as e:
                    messages.error(request, f"Overwrite failed: {e}")
                else:
                    messages.success(request, "Fit overwritten from EFT.")
                    return redirect("srp:doctrine_fit_detail", fit_id=fit.id)
        else:
            form = DoctrineFitEditForm(request.POST, instance=fit)
            if form.is_valid():
                updated = form.save(commit=False)
                updated.updated_by = request.user
                updated.save()
                messages.success(request, "Fit updated.")
                return redirect("srp:doctrine_fit_detail", fit_id=fit.id)

    form = DoctrineFitEditForm(instance=fit)

    return render(
        request,
        "srp/admin/doctrine_fit_detail.html",
        {"fit": fit, "form": form, "items": fit.items.all()},
    )


@login_required
@require_POST
def fitcheck_rerun(request, claim_id: int):
    if not request.user.has_perm("srp.can_review_srp"):
        return redirect("srp:my_claims")

    claim = get_object_or_404(
        SRPClaim.objects.select_related("fitcheck_best_fit", "fitcheck_selected_fit"),
        id=claim_id,
    )

    from .fitcheck import compute_fitcheck  # local import avoids circular imports

    result = compute_fitcheck(claim)

    claim.fitcheck_status = result.get("status") or ""
    claim.fitcheck_best_fit_id = result.get("best_fit_id")
    claim.fitcheck_data = result
    claim.no_rigs_flag = bool(result.get("no_rigs"))
    claim.fitcheck_updated_at = timezone.now()

    claim.save(
        update_fields=[
            "fitcheck_status",
            "fitcheck_best_fit",
            "fitcheck_data",
            "no_rigs_flag",
            "fitcheck_updated_at",
        ]
    )

    messages.success(request, "Fit check re-ran.")
    return redirect("srp:claim_detail", claim_id=claim.id)


@login_required
@require_POST
def doctrine_fit_deactivate(request, fit_id: int):
    if not _require_reviewer(request.user):
        return redirect("srp:admin_overview")

    fit = get_object_or_404(DoctrineFit, id=fit_id)
    fit.active = False
    fit.updated_by = request.user
    fit.save(update_fields=["active", "updated_by", "updated_at"])
    messages.success(request, "Fit deactivated.")
    return redirect("srp:doctrine_fit_list")


@login_required
@require_POST
def doctrine_fit_delete(request, fit_id: int):
    if not _require_reviewer(request.user):
        return redirect("srp:admin_overview")

    fit = get_object_or_404(DoctrineFit, id=fit_id)
    fit.delete()
    messages.success(request, "Fit deleted.")
    return redirect("srp:doctrine_fit_list")


# ---------------------------------------------------------------------------
# Claim detail
# ---------------------------------------------------------------------------


@login_required
def claim_detail(request, claim_id: int):
    """
    Claim detail page:
    - Reviewers (srp.can_review_srp) can view any claim
    - Regular users can view only their own claims
    - Auto checks: NPC-only / NPC-present / Blue-involved
    - Fit check (cached, lazy)
    - Submitter/Victim corp + alliance display (best-effort)
    """
    claim = get_object_or_404(
        SRPClaim.objects.select_related(
            "ship",
            "submitter",
            "reviewer",
            "fitcheck_best_fit",
            "fitcheck_selected_fit",
        ),
        id=claim_id,
    )

    is_reviewer = request.user.has_perm("srp.can_review_srp")
    if not is_reviewer and claim.submitter_id != request.user.id:
        return redirect("srp:my_claims")

    # Fit check (lazy, cached)
    ensure_fitcheck_cached(claim)

    # Review history
    reviews = (
        ClaimReview.objects.select_related("reviewer")
        .filter(claim=claim)
        .order_by("-timestamp")
    )

    # Killmail basics
    km = claim.killmail_raw or {}
    victim_blob = km.get("victim") or {}
    items = victim_blob.get("items") or []
    attackers = km.get("attackers") or []

    # ------------------------------------------------------------------
    # Submitter / Victim corp + alliance (display only)
    # ------------------------------------------------------------------
    from .esi import get_entity_names_cached  # local import avoids cyclic surprises

    submitter_char = None
    submitter_corp = None
    submitter_alliance = None
    submitter_corp_id = None

    mc = getattr(claim.submitter, "main_character", None)
    if mc:
        submitter_char = mc.character_name
        submitter_corp = getattr(mc, "corporation_name", None)
        submitter_alliance = getattr(mc, "alliance_name", None)
        submitter_corp_id = getattr(mc, "corporation_id", None)

    victim_char = claim.victim_character_name
    victim_corp = None
    victim_alliance = None

    victim_corp_id = victim_blob.get("corporation_id")
    victim_alliance_id = victim_blob.get("alliance_id")

    if victim_corp_id:
        victim_corp = get_entity_names_cached(
            "corp", [victim_corp_id], fetch_cap=1
        ).get(int(victim_corp_id))

    if victim_alliance_id:
        victim_alliance = get_entity_names_cached(
            "alliance", [victim_alliance_id], fetch_cap=1
        ).get(int(victim_alliance_id))

    # ------------------------------------------------------------------
    # Auto-checks (tri-state: WARN / CLEAN / neutral-when-unconfigured).
    # Computed centrally so an empty SRPConfig renders a neutral
    # "not configured" badge, never a false-green "all clear".
    # ------------------------------------------------------------------
    cfg = SRPConfig.get()
    checks = claim_auto_checks(claim, cfg)
    check_blue = checks["blue"]
    check_corp = checks["corp"]
    check_non_tnt = checks["non_tnt"]
    check_npc = checks["npc"]

    # ------------------------------------------------------------------
    # Fitting grouping (slots, ammo filtered)
    # ------------------------------------------------------------------
    from collections import defaultdict

    fittings_map: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for it in items:
        flag = int(it.get("flag") or 0)

        destroyed = int(it.get("quantity_destroyed") or 0)
        dropped = int(it.get("quantity_dropped") or 0)
        qty_total = destroyed + dropped

        singleton = int(it.get("singleton") or 0)

        # Filter ammo / charges / scripts (multi-quantity non-singleton items).
        if qty_total > 1 and singleton == 0:
            continue

        it["qty_total"] = qty_total
        fittings_map[slot_group_from_flag(flag, extended=True)].append(it)

    fitting_groups: list[tuple[str, list[dict[str, Any]]]] = []
    for name in SLOT_GROUP_ORDER:
        if fittings_map.get(name):
            fitting_groups.append((name, fittings_map[name]))

    # ------------------------------------------------------------------
    # Type name resolution (items + fitcheck diff)
    # ------------------------------------------------------------------
    item_type_ids: list[int] = []
    seen_types: set[int] = set()

    for it in items:
        tid = it.get("item_type_id")
        if not tid:
            continue
        tid_i = int(tid)
        if tid_i in seen_types:
            continue
        seen_types.add(tid_i)
        item_type_ids.append(tid_i)

    diff_type_ids: list[int] = []
    fc = claim.fitcheck_data or {}
    diff = (fc.get("diff") or {}) if isinstance(fc, dict) else {}
    for bucket in ("missing", "extra"):
        groups = diff.get(bucket) or {}
        for rows in groups.values():
            for r in rows or []:
                tid = r.get("type_id")
                if tid:
                    tid_i = int(tid)
                    if tid_i not in seen_types:
                        seen_types.add(tid_i)
                        diff_type_ids.append(tid_i)

    # Hard cap to keep requests bounded.
    all_type_ids = (item_type_ids + diff_type_ids)[:120]
    type_names = get_type_names_cached(all_type_ids, fetch_cap=120)

    # ------------------------------------------------------------------
    # NPC / Blue flags
    # ------------------------------------------------------------------
    # Blue-on-blue and NPC involvement are computed centrally (tri-state) above
    # via check_blue / check_npc. Pull the NPC damage numbers off the result.
    npc_count = check_npc.npc_count
    player_count = check_npc.player_count
    npc_damage = check_npc.npc_damage
    player_damage = check_npc.player_damage
    npc_damage_pct = check_npc.npc_damage_pct
    player_damage_pct = check_npc.player_damage_pct
    npc_only = check_npc.npc_only
    npc_present = check_npc.npc_present

    # ------------------------------------------------------------------
    # Reviewer edit form — allowed ONLY while the claim is PENDING (B0 / P2-4).
    #
    # Once a claim is APPROVED/PAID (or DENIED) its payout_amount is FROZEN
    # (P0-1). The edit path recomputes non-Manual payouts via calculate_payout(),
    # which since A3 applies SRPConfig.default_multiplier — so editing a frozen
    # claim would RE-SCALE money that's already been approved/paid, and a Manual
    # edit would overwrite the frozen hand-entered amount. Guard it server-side
    # (not just in the template): the form is only offered while PENDING, and an
    # edit POST on a non-PENDING claim is rejected with a clear message. To
    # change a processed claim the reviewer must un-approve/un-pay it back to
    # Pending first (those reversal transitions exist since P0-3).
    # ------------------------------------------------------------------
    edit_form = None
    edit_locked = is_reviewer and claim.status != SRPClaim.Status.PENDING
    if is_reviewer:
        is_edit_post = (
            request.method == "POST" and request.POST.get("edit_claim") == "1"
        )

        if is_edit_post and claim.status != SRPClaim.Status.PENDING:
            messages.error(
                request,
                f"Claim #{claim.id} is {claim.get_status_display()} — its payout is "
                f"frozen and can't be edited. Un-approve or un-pay it back to "
                f"Pending first, then edit the category or amount.",
            )
            return redirect("srp:claim_detail", claim_id=claim.id)

        if claim.status == SRPClaim.Status.PENDING:
            if is_edit_post:
                old_category = claim.category
                old_payout = claim.payout_amount

                edit_form = SRPClaimReviewerEditForm(request.POST, instance=claim)
                if edit_form.is_valid():
                    updated = edit_form.save(commit=False)
                    new_category = (
                        (edit_form.cleaned_data.get("category") or "").strip().upper()
                    )
                    new_payout = edit_form.cleaned_data.get("payout_amount")

                    updated.category = new_category
                    updated.payout_amount = (
                        new_payout
                        if new_category == SRPClaim.Category.MANUAL
                        else updated.calculate_payout()
                    )

                    updated.reviewer = request.user
                    updated.edited_at = timezone.now()
                    updated.save()

                    changes: list[str] = []
                    if old_category != updated.category:
                        changes.append(
                            f"category: {SRPClaim.category_label(old_category)} → {SRPClaim.category_label(updated.category)}"
                        )
                    if old_payout != updated.payout_amount:
                        changes.append(
                            f"payout: {old_payout} → {updated.payout_amount}"
                        )

                    _add_review_record(
                        updated,
                        request.user,
                        "Edited",
                        "; ".join(changes) if changes else "Edited claim.",
                    )

                    messages.success(request, "Claim updated.")
                    return redirect("srp:claim_detail", claim_id=updated.id)
            else:
                edit_form = SRPClaimReviewerEditForm(instance=claim)

    return render(
        request,
        "srp/claim_detail.html",
        {
            "claim": claim,
            "reviews": reviews,
            "is_reviewer": is_reviewer,
            "needs_manual_payout": claim.needs_manual_payout,
            # Parties
            "submitter_char": submitter_char,
            "submitter_corp": submitter_corp,
            "submitter_alliance": submitter_alliance,
            "victim_char": victim_char,
            "victim_corp": victim_corp,
            "victim_alliance": victim_alliance,
            # Fitting
            "fitting_groups": fitting_groups,
            "type_names": type_names,
            # Flags
            "npc_only": npc_only,
            "npc_present": npc_present,
            # Tri-state auto-checks (WARN / CLEAN / neutral-when-unconfigured)
            "check_blue": check_blue,
            "check_corp": check_corp,
            "check_non_tnt": check_non_tnt,
            "check_npc": check_npc,
            # Back-compat booleans (summary card warning badges)
            "blue_involved": check_blue.is_warn,
            "corp_mismatch": check_corp.is_warn,
            "victim_non_tnt": check_non_tnt.is_warn,
            # Damage
            "npc_count": npc_count,
            "player_count": player_count,
            "npc_damage": npc_damage,
            "player_damage": player_damage,
            "npc_damage_pct": npc_damage_pct,
            "player_damage_pct": player_damage_pct,
            # Fit check
            "fitcheck_status": claim.fitcheck_status,
            "fitcheck_data": claim.fitcheck_data,
            "fitcheck_best_fit": claim.fitcheck_best_fit,
            "fitcheck_selected_fit": claim.fitcheck_selected_fit,
            "no_rigs_flag": claim.no_rigs_flag,
            # Forms
            "edit_form": edit_form,
            "edit_locked": edit_locked,
        },
    )


# ---------------------------------------------------------------------------
# Admin overview + payouts bulk import
# ---------------------------------------------------------------------------


def _range_from_preset(preset: str):
    """
    Returns (start_dt, end_dt_exclusive, label).
    Start is inclusive; end is exclusive.
    """
    tz = timezone.get_current_timezone()
    today = timezone.localdate()

    preset = (preset or "this_week").lower()

    if preset == "today":
        start_d = today
        end_d = today + timedelta(days=1)
        label = "today"
    elif preset == "this_week":
        start_d = today - timedelta(days=today.weekday())  # Monday
        end_d = start_d + timedelta(days=7)
        label = "this week"
    elif preset == "this_month":
        start_d = today.replace(day=1)
        if start_d.month == 12:
            end_d = date(start_d.year + 1, 1, 1)
        else:
            end_d = date(start_d.year, start_d.month + 1, 1)
        label = "this month"
    elif preset == "last_month":
        this_month_start = today.replace(day=1)
        if this_month_start.month == 1:
            start_d = date(this_month_start.year - 1, 12, 1)
        else:
            start_d = date(this_month_start.year, this_month_start.month - 1, 1)
        end_d = this_month_start
        label = "last month"
    elif preset == "this_year":
        start_d = date(today.year, 1, 1)
        end_d = date(today.year + 1, 1, 1)
        label = "this year"
    elif preset == "last_year":
        start_d = date(today.year - 1, 1, 1)
        end_d = date(today.year, 1, 1)
        label = "last year"
    else:
        start_d = today - timedelta(days=6)
        end_d = today + timedelta(days=1)
        label = "last 7 days"

    start_dt = timezone.make_aware(datetime.combine(start_d, datetime.min.time()), tz)
    end_dt = timezone.make_aware(datetime.combine(end_d, datetime.min.time()), tz)
    return start_dt, end_dt, label


def _range_from_custom(start_str: str | None, end_str: str | None):
    """
    Custom range from GET params start/end in YYYY-MM-DD.
    End is inclusive in UI; we convert to end-exclusive internally.
    Returns (start_dt, end_dt_exclusive, label) or None if invalid/missing.
    """
    if not start_str or not end_str:
        return None

    try:
        start_d = date.fromisoformat(start_str)
        end_d_inclusive = date.fromisoformat(end_str)
    except ValueError:
        return None

    if end_d_inclusive < start_d:
        return None

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(start_d, datetime.min.time()), tz)
    end_dt = timezone.make_aware(
        datetime.combine(end_d_inclusive + timedelta(days=1), datetime.min.time()), tz
    )
    label = f"{start_d.isoformat()} → {end_d_inclusive.isoformat()}"
    return start_dt, end_dt, label


@login_required
@permission_required("srp.can_view_srp_reports", raise_exception=True)
def admin_overview(request):
    """
    SRP Admin Overview (read-only):
    - Status summary (Paid/Approved/Pending/Denied order)
    - Queue health (pending aging)
    - Paid breakdown toggles (category / submitter corp / reviewer)
    - Time presets + custom start/end (inclusive end date)
    """
    preset = request.GET.get("t", "this_week")
    start_str = request.GET.get("start")
    end_str = request.GET.get("end")

    custom = _range_from_custom(start_str, end_str)
    if custom:
        start_dt, end_dt, time_label = custom
        using_custom = True
    else:
        start_dt, end_dt, time_label = _range_from_preset(preset)
        using_custom = False

    paid_by = (request.GET.get("paid_by") or "category").lower()
    if paid_by not in {"category", "corp", "reviewer"}:
        paid_by = "category"

    qs = SRPClaim.objects.select_related(
        "ship", "submitter", "submitter__main_character", "reviewer"
    )

    recent = qs.filter(submitted_at__gte=start_dt, submitted_at__lt=end_dt)

    status_summary_qs = recent.values("status").annotate(
        count=Count("id"),
        isk=Coalesce(Sum("payout_amount"), Decimal("0")),
    )

    status_order = {"PAID": 0, "APPROVED": 1, "PENDING": 2, "DENIED": 3}
    status_summary = sorted(
        list(status_summary_qs),
        key=lambda r: status_order.get(r["status"], 99),
    )

    pending_qs = qs.filter(status="PENDING")
    oldest_pending = pending_qs.order_by("submitted_at").first()

    now = timezone.now()
    pending_7d = pending_qs.filter(submitted_at__lt=now - timedelta(days=7)).count()
    pending_14d = pending_qs.filter(submitted_at__lt=now - timedelta(days=14)).count()

    oldest_pending_list = pending_qs.order_by("submitted_at")[:10]

    reviewer_activity = (
        ClaimReview.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt)
        .values("reviewer__username")
        .annotate(actions=Count("id"), last_action=Max("timestamp"))
        .order_by("-actions", "-last_action")
    )

    paid_qs = qs.filter(status="PAID", paid_at__gte=start_dt, paid_at__lt=end_dt)

    paid_title = "Paid breakdown"
    paid_breakdown_rows: list[dict[str, Any]] = []

    if paid_by == "category":
        paid_title = "Paid by SRP Category"
        paid_breakdown_rows = list(
            paid_qs.values("category")
            .annotate(
                count=Count("id"),
                isk=Coalesce(Sum("payout_amount"), Decimal("0")),
            )
            .order_by("-isk", "-count")
        )
        for r in paid_breakdown_rows:
            r["label"] = r.get("category") or "Unknown"

    elif paid_by == "reviewer":
        paid_title = "Paid by Reviewer"
        paid_breakdown_rows = list(
            paid_qs.values("reviewer__username")
            .annotate(
                count=Count("id"),
                isk=Coalesce(Sum("payout_amount"), Decimal("0")),
            )
            .order_by("-isk", "-count")
        )
        for r in paid_breakdown_rows:
            r["label"] = r.get("reviewer__username") or "Unknown"

    elif paid_by == "corp":
        paid_title = "Paid by Submitter Corp"
        agg: dict[str, dict[str, Any]] = {}
        for c in paid_qs.select_related(
            "submitter", "submitter__main_character"
        ).iterator():
            submitter = c.submitter
            corp = submitter.get_corp_name() if submitter else "Unknown"
            corp = corp or "Unknown"

            if corp not in agg:
                agg[corp] = {"count": 0, "isk": Decimal("0")}
            agg[corp]["count"] += 1
            agg[corp]["isk"] += c.payout_amount or Decimal("0")

        paid_breakdown_rows = [
            {"label": corp, "count": data["count"], "isk": data["isk"]}
            for corp, data in agg.items()
        ]
        paid_breakdown_rows.sort(key=lambda r: (r["isk"], r["count"]), reverse=True)

    context = {
        "preset": preset,
        "using_custom": using_custom,
        "start": start_str or "",
        "end": end_str or "",
        "time_label": time_label,
        "status_summary": status_summary,
        "oldest_pending": oldest_pending,
        "oldest_pending_list": oldest_pending_list,
        "pending_7d": pending_7d,
        "pending_14d": pending_14d,
        "reviewer_activity": reviewer_activity,
        "paid_by": paid_by,
        "paid_title": paid_title,
        "paid_breakdown": paid_breakdown_rows,
    }
    return render(request, "srp/admin/overview.html", context)


@login_required
@permission_required("srp.can_manage_srp_payouts", raise_exception=True)
def admin_payouts(request):
    q = (request.GET.get("q") or "").strip()

    ships = ShipPayout.objects.all()
    if q:
        ships = ships.filter(ship_name__icontains=q)

    ships = ships.order_by("ship_name")[:500]
    return render(request, "srp/admin/payouts_list.html", {"ships": ships, "q": q})


@login_required
@permission_required("srp.can_manage_srp_payouts", raise_exception=True)
def admin_payout_new(request):
    if request.method == "POST":
        form = ShipPayoutForm(request.POST)
        if form.is_valid():
            ship = form.save()
            messages.success(request, f"Created payout record for {ship.ship_name}.")
            return redirect("srp:admin_payouts")
        messages.error(request, "Please correct the errors below.")
    else:
        form = ShipPayoutForm()

    return render(request, "srp/admin/payout_edit.html", {"form": form, "is_new": True})


@login_required
@permission_required("srp.can_manage_srp_payouts", raise_exception=True)
def admin_payout_edit(request, ship_id: int):
    ship = get_object_or_404(ShipPayout, id=ship_id)

    if request.method == "POST":
        form = ShipPayoutForm(request.POST, instance=ship)
        if form.is_valid():
            form.save()
            messages.success(request, f"Updated payouts for {ship.ship_name}.")
            return redirect("srp:admin_payouts")
        messages.error(request, "Please correct the errors below.")
    else:
        form = ShipPayoutForm(instance=ship)

    return render(
        request,
        "srp/admin/payout_edit.html",
        {"form": form, "is_new": False, "ship": ship},
    )


def _parse_bool(value) -> bool:
    """
    Robust boolean parser for CSV inputs.

    Accepts:
      - 1 / "1" / "1 (anything...)"
      - 0 / "0" / "0 (anything...)"
      - "true", "yes", "y", "t"
      - "" / None
    """
    if value is None:
        return False

    s = str(value).strip().replace("\xa0", " ")  # handle NBSP from Excel
    if not s:
        return False

    m = re.search(r"\b([01])\b", s)
    if m:
        return m.group(1) == "1"

    return s.lower() in {"true", "yes", "y", "t"}


def _parse_isk(value) -> Decimal:
    """
    ISK parser for CSV inputs.

    Accepts:
      - 200000000
      - "200,000,000"
      - "200,000,000 (325,787,715)"
      - "" / None

    Uses the first number chunk and ignores anything after (like parentheses).
    """
    if value is None:
        return Decimal("0")

    s = str(value).strip().replace("\xa0", " ")
    if not s:
        return Decimal("0")

    m = re.search(r"[\d,]+", s)
    if not m:
        return Decimal("0")

    return Decimal(m.group(0).replace(",", ""))


def _get_cell(row: dict, key: str):
    """Fetch a cell from a CSV DictReader row with header normalization fallback."""
    if key in row:
        return row.get(key)

    for k, v in row.items():
        if (k or "").strip().lower() == key.strip().lower():
            return v
    return None


# Payout tier -> its documented CSV header. Header matching is case-insensitive
# and whitespace-trimmed (see _present_headers), so "strategic", " Strategic "
# and "STRATEGIC" all map here.
TIER_COLUMNS = [
    ("strategic", "Strategic"),
    ("peacetime", "Peacetime"),
    ("shitstack", "Shit Stack"),
    ("tnt_special", "TNT Special"),
]


def _present_headers(reader: csv.DictReader) -> set[str]:
    """Normalized (trimmed, lowercased) set of headers actually in the CSV."""
    return {(h or "").strip().lower() for h in (reader.fieldnames or [])}


def _parse_isk_optional(value) -> Decimal | None:
    """
    Like _parse_isk, but returns None for "no value provided" so a missing or
    blank cell means "leave this tier UNCHANGED", never silently zero it. An
    explicit "0" in a provided column parses to Decimal(0) (deliberate zeroing
    stays possible).
    """
    if value is None:
        return None
    s = str(value).strip().replace("\xa0", " ")
    if not s:
        return None
    m = re.search(r"[\d,]+", s)
    if not m:
        return None
    return Decimal(m.group(0).replace(",", ""))


def _parse_hull_optional(row: dict, present: set[str]) -> bool | None:
    """
    hull_contract from the Capital/HullContract columns. Returns None when
    NEITHER column is present, so an import that omits them leaves the existing
    flag untouched instead of forcing it False.
    """
    if "capital" not in present and "hullcontract" not in present:
        return None
    return _parse_bool(_get_cell(row, "Capital")) or _parse_bool(
        _get_cell(row, "HullContract")
    )


def _parse_payout_row(row: dict, present: set[str]):
    """
    Parse one CSV row into (ship_name, tiers, hull).

    ``tiers`` maps model field -> Decimal or None, where None means "column
    absent or blank -> leave unchanged". ``hull`` is bool or None (unchanged).
    Shared by the preview and apply steps so they can't diverge.
    """
    ship_name = (
        _get_cell(row, "Ship Name") or _get_cell(row, "ship_name") or ""
    ).strip()

    tiers: dict[str, Decimal | None] = {}
    for field, header in TIER_COLUMNS:
        if header.strip().lower() in present:
            tiers[field] = _parse_isk_optional(_get_cell(row, header))
        else:
            tiers[field] = None  # column not in the file -> unchanged

    hull = _parse_hull_optional(row, present)
    return ship_name, tiers, hull


@login_required
@permission_required("srp.can_manage_srp_payouts", raise_exception=True)
def admin_payouts_bulk(request):
    if request.method != "POST":
        return render(request, "srp/admin/payouts_bulk_upload.html")

    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Please choose a CSV file to upload.")
        return render(request, "srp/admin/payouts_bulk_upload.html")

    try:
        raw = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raw = f.read().decode("latin-1")

    job = PayoutImportJob.objects.create(
        created_by=request.user,
        csv_text=raw,
        original_filename=getattr(f, "name", "") or "",
    )

    reader = csv.DictReader(io.StringIO(job.csv_text))
    present = _present_headers(reader)
    preview_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for i, row in enumerate(reader):
        ship_name, tiers, hull = _parse_payout_row(row, present)
        if not ship_name:
            errors.append(f"Row {i+2}: missing Ship Name")
            continue

        existing = ShipPayout.objects.filter(ship_name__iexact=ship_name).first()
        creating = existing is None

        # Per-tier breakdown so the reviewer sees exactly what changes vs. what
        # is deliberately left alone. A missing/blank column shows as
        # "unchanged", never a silent overwrite to 0.
        tier_rows: list[dict[str, Any]] = []
        any_change = False
        for field, header in TIER_COLUMNS:
            val = tiers[field]
            old = getattr(existing, field) if existing else None
            provided = val is not None
            if not provided:
                # Column absent/blank -> keep existing (or model default on create).
                new = old if existing else Decimal("0")
                unchanged = True
            else:
                new = val
                unchanged = (not creating) and (old == val)
                if not unchanged:
                    any_change = True
            tier_rows.append(
                {
                    "field": header,
                    "old": old,
                    "new": new,
                    "provided": provided,
                    "unchanged": unchanged,
                }
            )

        # hull_contract row.
        hull_old = existing.hull_contract if existing else None
        hull_provided = hull is not None
        if not hull_provided:
            hull_new = hull_old if existing else False
            hull_unchanged = True
        else:
            hull_new = hull
            hull_unchanged = (not creating) and (hull_old == hull)
            if not hull_unchanged:
                any_change = True

        if creating:
            action = "CREATE"
        elif any_change:
            action = "UPDATE"
        else:
            action = "NO_CHANGE"

        preview_rows.append(
            {
                "ship_name": ship_name,
                "action": action,
                "tiers": tier_rows,
                "hull": {
                    "old": hull_old,
                    "new": hull_new,
                    "provided": hull_provided,
                    "unchanged": hull_unchanged,
                },
            }
        )

    creates = sum(1 for r in preview_rows if r["action"] == "CREATE")
    updates = sum(1 for r in preview_rows if r["action"] == "UPDATE")
    nochange = sum(1 for r in preview_rows if r["action"] == "NO_CHANGE")

    preview_rows = [r for r in preview_rows if r["action"] != "NO_CHANGE"]

    return render(
        request,
        "srp/admin/payouts_bulk_preview.html",
        {
            "job": job,
            "preview_rows": preview_rows,
            "errors": errors,
            "creates": creates,
            "updates": updates,
            "nochange": nochange,
        },
    )


@login_required
@permission_required("srp.can_manage_srp_payouts", raise_exception=True)
def admin_payouts_bulk_apply(request):
    if request.method != "POST":
        return redirect("srp:admin_payouts_bulk")

    job_id = request.POST.get("job_id")
    job = get_object_or_404(PayoutImportJob, id=job_id, created_by=request.user)

    excluded = {
        x.strip().lower()
        for x in (request.POST.getlist("exclude_ship") or [])
        if x and x.strip()
    }

    reader = csv.DictReader(io.StringIO(job.csv_text))
    present = _present_headers(reader)

    created = updated = skipped = errors = 0

    with transaction.atomic():
        for row in reader:
            ship_name, tiers, hull = _parse_payout_row(row, present)
            if not ship_name:
                errors += 1
                continue

            if ship_name.lower() in excluded:
                skipped += 1
                continue

            obj = ShipPayout.objects.filter(ship_name__iexact=ship_name).first()
            creating = obj is None
            if creating:
                obj = ShipPayout(ship_name=ship_name)

            # Only write tiers that were actually provided; None means "leave
            # unchanged" (existing value on update, model default on create).
            for field, _header in TIER_COLUMNS:
                val = tiers[field]
                if val is not None:
                    setattr(obj, field, val)
            if hull is not None:
                obj.hull_contract = hull

            obj.save()
            if creating:
                created += 1
            else:
                updated += 1

    job.delete()

    messages.success(
        request,
        f"Bulk payout import complete. Created: {created}, Updated: {updated}, Skipped: {skipped}, Errors: {errors}.",
    )
    return redirect("srp:admin_payouts")
