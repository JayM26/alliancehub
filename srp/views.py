from django.contrib import messages  # pyright: ignore[reportMissingModuleSource]
from django.contrib.auth.decorators import (  # pyright: ignore[reportMissingModuleSource]
    login_required,
    permission_required,
)
from django.db.models import (  # pyright: ignore[reportMissingModuleSource]
    Q,
    Count,
    Sum,
    Min,
    Max,
)
from django.shortcuts import (  # pyright: ignore[reportMissingModuleSource]
    get_object_or_404,
    redirect,
    render,
)
from django.db.models.functions import (  # pyright: ignore[reportMissingModuleSource]
    Coalesce,
)
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

from .esi import populate_claim_from_esi, fetch_type_name, get_type_names_cached
from .forms import SRPClaimForm, ShipPayoutForm, SRPClaimReviewerEditForm
from .models import ClaimReview, SRPClaim, ShipPayout, SRPConfig, PayoutImportJob
from datetime import date, datetime, timedelta
from decimal import Decimal
import csv
import io
import re
from django.db import transaction  # pyright: ignore[reportMissingModuleSource]


@login_required
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

            # Try to populate from ESI link (Option B)
            try:
                ok = populate_claim_from_esi(claim)

                # If we have a ship_type_id but ship_name didn't resolve, try once more here
                if claim.ship_type_id and not claim.ship_name:
                    try:
                        claim.ship_name = fetch_type_name(int(claim.ship_type_id))
                    except Exception:
                        pass

                # If we got a ship name from ESI and no ShipPayout selected, match/create one
                if not claim.ship and claim.ship_name:
                    sp = ShipPayout.objects.filter(
                        ship_name__iexact=claim.ship_name
                    ).first()
                    if not sp:
                        sp = ShipPayout.objects.create(ship_name=claim.ship_name)
                    claim.ship = sp
                    claim.payout_amount = claim.calculate_payout()

                # Optional: backfill legacy system field for display/search
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


# ---------------------------
# Reviewer queue + actions
# ---------------------------


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def review_queue(request):
    """
    Reviewer queue with simple filters:
    - status (default: ALL)
    - category
    - search (character/ship/system/link)
    """
    status = (request.GET.get("status") or "ALL").upper()
    category = request.GET.get("category", "")
    search = (request.GET.get("q", "") or "").strip()

    qs = SRPClaim.objects.select_related("ship", "submitter", "reviewer").all()

    # Only filter by status if it's not ALL
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

    claims = qs.order_by("-submitted_at")[:500]

    # --- Flag-only checks for queue (Blue + NPC present)
    cfg = SRPConfig.get()
    blue_alliance_ids = set(
        int(x) for x in (cfg.blue_alliance_ids or []) if str(x).isdigit()
    )
    blue_corp_ids = set(int(x) for x in (cfg.blue_corp_ids or []) if str(x).isdigit())

    for c in claims:
        km = c.killmail_raw or {}
        attackers = km.get("attackers") or []

        npc_present = False
        blue_involved = False

        for a in attackers:
            char_id = a.get("character_id")
            if not char_id:
                npc_present = True
            else:
                alliance_id = a.get("alliance_id")
                corp_id = a.get("corporation_id")
                if (alliance_id and int(alliance_id) in blue_alliance_ids) or (
                    corp_id and int(corp_id) in blue_corp_ids
                ):
                    blue_involved = True

            # tiny early exit
            if npc_present and blue_involved:
                break

        # attach flags to the object for the template
        c.flag_npc = npc_present
        c.flag_blue = blue_involved

        # --- tiny fitting preview (per-claim, first N items)
        # --- fitting grouped preview (per-claim)
        victim = km.get("victim") or {}
        items = victim.get("items") or []
        c.fitting_item_count = len(items)

        def _slot_group(flag: int) -> str:
            # High: 27-34, Mid: 19-26, Low: 11-18, Rigs: 92-94, Cargo: 5, Drone Bay: 87
            if 27 <= flag <= 34:
                return "High Slots"
            if 19 <= flag <= 26:
                return "Mid Slots"
            if 11 <= flag <= 18:
                return "Low Slots"
            if 92 <= flag <= 94:
                return "Rigs"
            if flag == 5:
                return "Cargo"
            if flag == 87:
                return "Drone Bay"
            return "Other"

        c.fitting_groups_preview = []  # list[(group_name, list[str])]

        if items:
            # resolve names (bounded)
            item_type_ids = []
            for it in items:
                tid = it.get("item_type_id")
                if tid:
                    item_type_ids.append(int(tid))

            type_names = get_type_names_cached(item_type_ids[:60], fetch_cap=60)

            from collections import defaultdict

            grouped = defaultdict(list)

            # build lines like: "Warp Disruptor II ×1"
            for it in items:
                tid = it.get("item_type_id")
                if not tid:
                    continue

                flag = int(it.get("flag") or 0)
                group = _slot_group(flag)

                name = type_names.get(int(tid)) or str(tid)
                qd = int(it.get("quantity_destroyed") or 0)
                qp = int(it.get("quantity_dropped") or 0)
                qty = qd + qp

                grouped[group].append(f"{name} ×{qty}" if qty else name)

            # keep ordering consistent
            order = [
                "High Slots",
                "Mid Slots",
                "Low Slots",
                "Rigs",
                "Cargo",
                "Drone Bay",
                "Other",
            ]
            for g in order:
                if grouped.get(g):
                    c.fitting_groups_preview.append((g, grouped[g]))

    # Build dropdown options (ALL + whatever your model defines)
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
        # Toggle off -> back to PENDING
        claim.set_status(
            "PENDING", reviewer=request.user, note=comment or "Approval removed."
        )
        claim.processed_at = timezone.now()
        claim.save()
        _add_review_record(claim, request.user, "Unapproved", comment)
        messages.success(request, f"Unapproved claim #{claim.id} (back to Pending).")
    else:
        # Normal approve only from PENDING (but allow if someone wants to correct a DENIED)
        claim.set_status("APPROVED", reviewer=request.user, note=comment or "Approved.")
        claim.processed_at = timezone.now()
        claim.save()
        _add_review_record(claim, request.user, "Approved", comment)
        messages.success(request, f"Approved claim #{claim.id}.")

    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def deny_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    comment = _get_comment(request)

    if claim.status == "DENIED":
        # Toggle off -> back to PENDING
        claim.set_status(
            "PENDING", reviewer=request.user, note=comment or "Denial removed."
        )
        claim.processed_at = timezone.now()
        claim.save()
        _add_review_record(claim, request.user, "Undenied", comment)
        messages.success(
            request, f"Removed denial on claim #{claim.id} (back to Pending)."
        )
    else:
        claim.set_status("DENIED", reviewer=request.user, note=comment or "Denied.")
        claim.processed_at = timezone.now()
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
        # Toggle off -> back to APPROVED
        claim.set_status(
            "APPROVED", reviewer=request.user, note=comment or "Payment mark removed."
        )
        claim.paid_at = None
        claim.processed_at = timezone.now()
        claim.save()
        _add_review_record(claim, request.user, "Unpaid", comment)
        messages.success(request, f"Unpaid claim #{claim.id} (back to Approved).")
    else:
        # Mark paid (only makes sense from APPROVED, but allow as correction)
        claim.set_status("PAID", reviewer=request.user, note=comment or "Paid.")
        claim.paid_at = timezone.now()
        claim.processed_at = timezone.now()
        claim.save()
        _add_review_record(claim, request.user, "Paid", comment)
        messages.success(request, f"Marked claim #{claim.id} as Paid.")

    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
def claim_detail(request, claim_id: int):
    """
    Claim detail page:
    - Reviewers (srp.can_review_srp) can view any claim
    - Regular users can view only their own claims
    - Flag-only auto checks: NPC-only / NPC-present / Blue-involved
    - Uses DB cache for type names (modules/items) to avoid repeated ESI calls
    - Reviewer can Approve/Unapprove, Deny/Undeny, Paid/Unpay from this page
    """
    claim = get_object_or_404(
        SRPClaim.objects.select_related("ship", "submitter", "reviewer"),
        id=claim_id,
    )

    is_reviewer = request.user.has_perm("srp.can_review_srp")
    if not is_reviewer and claim.submitter_id != request.user.id:
        return redirect("srp:my_claims")

    reviews = (
        ClaimReview.objects.select_related("reviewer")
        .filter(claim=claim)
        .order_by("-timestamp")
    )

    km = claim.killmail_raw or {}
    victim = km.get("victim") or {}
    items = victim.get("items") or []
    attackers = km.get("attackers") or []

    # ---- Group fittings by slot (based on ESI "flag")
    def _slot_group(flag: int) -> str:
        # High: 27-34, Mid: 19-26, Low: 11-18, Rigs: 92-94, Cargo: 5, Drone Bay: 87
        if 27 <= flag <= 34:
            return "High Slots"
        if 19 <= flag <= 26:
            return "Mid Slots"
        if 11 <= flag <= 18:
            return "Low Slots"
        if 92 <= flag <= 94:
            return "Rigs"
        if flag == 5:
            return "Cargo"
        if flag == 87:
            return "Drone Bay"
        return "Other"

    from collections import defaultdict

    fittings_map = defaultdict(list)
    for it in items:
        flag = int(it.get("flag") or 0)
        fittings_map[_slot_group(flag)].append(it)

    fitting_groups = []
    for name in [
        "High Slots",
        "Mid Slots",
        "Low Slots",
        "Rigs",
        "Cargo",
        "Drone Bay",
        "Other",
    ]:
        if fittings_map.get(name):
            fitting_groups.append((name, fittings_map[name]))

    # ---- Cached type name lookups (bounded, warms over time)
    from .esi import get_type_names_cached  # local import avoids circular surprises

    item_type_ids = []
    for it in items:
        tid = it.get("item_type_id")
        if tid:
            item_type_ids.append(int(tid))

    type_names = get_type_names_cached(item_type_ids, fetch_cap=40)

    # ---- Flag-only checks (no name resolution)
    npc_count = 0
    player_count = 0
    npc_damage = 0
    player_damage = 0

    cfg = SRPConfig.get()
    blue_alliance_ids = set(
        int(x) for x in (cfg.blue_alliance_ids or []) if str(x).isdigit()
    )
    blue_corp_ids = set(int(x) for x in (cfg.blue_corp_ids or []) if str(x).isdigit())

    blue_involved = False

    for a in attackers:
        dmg = int(a.get("damage_done") or 0)
        char_id = a.get("character_id")

        if char_id:
            player_count += 1
            player_damage += dmg

            alliance_id = a.get("alliance_id")
            corp_id = a.get("corporation_id")
            if (alliance_id and int(alliance_id) in blue_alliance_ids) or (
                corp_id and int(corp_id) in blue_corp_ids
            ):
                blue_involved = True
        else:
            npc_count += 1
            npc_damage += dmg

    total_damage = npc_damage + player_damage
    npc_damage_pct = (
        round((npc_damage / total_damage) * 100, 1) if total_damage else 0.0
    )
    player_damage_pct = (
        round((player_damage / total_damage) * 100, 1) if total_damage else 0.0
    )

    npc_only = player_count == 0 and npc_count > 0
    npc_present = npc_count > 0

    edit_form = None
    if is_reviewer:
        if request.method == "POST" and request.POST.get("edit_claim") == "1":
            old_category = claim.category
            old_payout = claim.payout_amount

            edit_form = SRPClaimReviewerEditForm(request.POST, instance=claim)
            if edit_form.is_valid():
                updated = edit_form.save(commit=False)

                # --- Canonicalize category so we never compare against "Manual"
                updated.category = (updated.category or "").strip().upper()

                # --- Apply payout logic based on canonical category
                if updated.category == "MANUAL":
                    updated.payout_amount = edit_form.cleaned_data.get("payout_amount")
                else:
                    updated.payout_amount = updated.calculate_payout()

                updated.reviewer = request.user
                updated.processed_at = timezone.now()
                updated.save()

                # Audit log (use the saved instance)
                changes = []
                if old_category != updated.category:
                    changes.append(f"category: {old_category} -> {updated.category}")
                if old_payout != updated.payout_amount:
                    changes.append(f"payout: {old_payout} -> {updated.payout_amount}")

                _add_review_record(
                    updated,
                    request.user,
                    "Edited",
                    "; ".join(changes) or "Edited claim.",
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
            "km": km,
            "victim": victim,
            "items": items,
            "fitting_groups": fitting_groups,
            "type_names": type_names,
            "npc_only": npc_only,
            "npc_present": npc_present,
            "blue_involved": blue_involved,
            "npc_count": npc_count,
            "player_count": player_count,
            "npc_damage": npc_damage,
            "player_damage": player_damage,
            "npc_damage_pct": npc_damage_pct,
            "player_damage_pct": player_damage_pct,
            "edit_form": edit_form,
        },
    )


def _range_from_preset(preset: str):
    """
    Returns (start_dt, end_dt_exclusive, label)
    start inclusive, end exclusive.
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
        # fallback: last 7 days
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

    # --- timeframe: custom overrides preset
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

    # what to group paid by
    paid_by = (request.GET.get("paid_by") or "category").lower()
    if paid_by not in {"category", "corp", "reviewer"}:
        paid_by = "category"

    qs = SRPClaim.objects.select_related("ship", "submitter", "reviewer")

    # --- STATUS SUMMARY (in window: submitted_at)
    recent = qs.filter(submitted_at__gte=start_dt, submitted_at__lt=end_dt)

    status_summary_qs = recent.values("status").annotate(
        count=Count("id"),
        isk=Coalesce(Sum("payout_amount"), Decimal("0")),
    )

    # order: Paid / Approved / Pending / Denied
    status_order = {"PAID": 0, "APPROVED": 1, "PENDING": 2, "DENIED": 3}
    status_summary = sorted(
        list(status_summary_qs),
        key=lambda r: status_order.get(r["status"], 99),
    )

    # --- QUEUE HEALTH (Pending overall)
    pending_qs = qs.filter(status="PENDING")
    oldest_pending = pending_qs.order_by("submitted_at").first()

    now = timezone.now()
    pending_7d = pending_qs.filter(submitted_at__lt=now - timedelta(days=7)).count()
    pending_14d = pending_qs.filter(submitted_at__lt=now - timedelta(days=14)).count()

    oldest_pending_list = pending_qs.order_by("submitted_at")[:10]

    # --- REVIEWER ACTIVITY (window: review timestamp)
    reviewer_activity = (
        ClaimReview.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt)
        .values("reviewer__username")
        .annotate(actions=Count("id"), last_action=Max("timestamp"))
        .order_by("-actions", "-last_action")
    )

    # --- PAID BREAKDOWN (window: paid_at)
    paid_qs = qs.filter(status="PAID", paid_at__gte=start_dt, paid_at__lt=end_dt)

    paid_title = "Paid breakdown"
    paid_breakdown_rows = []

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
        # Best-effort in Python, using accounts.User.get_corp_name()
        agg = {}  # corp -> {"count": int, "isk": Decimal}
        for c in paid_qs.select_related("submitter").iterator():
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
        # time controls
        "preset": preset,
        "using_custom": using_custom,
        "start": start_str or "",
        "end": end_str or "",
        "time_label": time_label,
        # status + queue
        "status_summary": status_summary,
        "oldest_pending": oldest_pending,
        "oldest_pending_list": oldest_pending_list,
        "pending_7d": pending_7d,
        "pending_14d": pending_14d,
        # reviewer + paid breakdown
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
    Accepts:
      1
      "1"
      "1 (anything...)"
      "0 (anything...)"
      "true", "yes", "y", "t"
      ""
    """
    if value is None:
        return False

    s = str(value).strip().replace("\xa0", " ")  # handle NBSP from Excel
    if not s:
        return False

    # If there’s a leading 0/1 anywhere, use that
    m = re.search(r"\b([01])\b", s)
    if m:
        return m.group(1) == "1"

    s_lower = s.lower()
    return s_lower in {"true", "yes", "y", "t"}


def _parse_isk(value) -> Decimal:
    """
    Accepts:
      200000000
      "200,000,000"
      "200,000,000 (325,787,715)"
      "" / None
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


def _dec_to_str(d: Decimal | None) -> str:
    # keep it simple for hidden fields
    return str(d if d is not None else Decimal("0"))


def _get_cell(row: dict, key: str):
    # exact match first
    if key in row:
        return row.get(key)
    # fallback: strip whitespace from headers
    for k, v in row.items():
        if (k or "").strip().lower() == key.strip().lower():
            return v
    return None


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

    # Store server-side so we don't POST it back
    job = PayoutImportJob.objects.create(
        created_by=request.user,
        csv_text=raw,
        original_filename=getattr(f, "name", "") or "",
    )

    # Build preview using the same parsing logic, but from job.csv_text
    reader = csv.DictReader(io.StringIO(job.csv_text))
    preview_rows = []
    errors = []

    for i, row in enumerate(reader):
        ship_name = (row.get("Ship Name") or row.get("ship_name") or "").strip()
        if not ship_name:
            errors.append(f"Row {i+2}: missing Ship Name")
            continue

        strategic = _parse_isk(row.get("Strategic"))
        peacetime = _parse_isk(row.get("Peacetime"))
        shitstack = _parse_isk(row.get("Shit Stack"))
        tnt_special = _parse_isk(row.get("TNT Special"))
        capital_flag = _parse_bool(_get_cell(row, "Capital"))
        hull_contract = capital_flag or _parse_bool(row.get("HullContract"))

        existing = ShipPayout.objects.filter(ship_name__iexact=ship_name).first()
        if not existing:
            preview_rows.append(
                {
                    "ship_name": ship_name,
                    "action": "CREATE",
                    "new": {
                        "strategic": strategic,
                        "peacetime": peacetime,
                        "shitstack": shitstack,
                        "tnt_special": tnt_special,
                        "hull_contract": hull_contract,
                    },
                    "diffs": [],
                }
            )
            continue

        diffs = []

        def _diff(field, old, new):
            if old != new:
                diffs.append({"field": field, "old": old, "new": new})

        _diff("strategic", existing.strategic, strategic)
        _diff("peacetime", existing.peacetime, peacetime)
        _diff("shitstack", existing.shitstack, shitstack)
        _diff("tnt_special", existing.tnt_special, tnt_special)
        _diff("hull_contract", existing.hull_contract, hull_contract)

        preview_rows.append(
            {
                "ship_name": ship_name,
                "action": "UPDATE" if diffs else "NO_CHANGE",
                "new": {
                    "strategic": strategic,
                    "peacetime": peacetime,
                    "shitstack": shitstack,
                    "tnt_special": tnt_special,
                    "hull_contract": hull_contract,
                },
                "diffs": diffs,
            }
        )

    # Hide NO_CHANGE rows (since you asked not to display them),
    # but keep counts so the page can explain "why nothing is showing".
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

    created = updated = skipped = errors = 0

    with transaction.atomic():
        for row in reader:
            ship_name = (row.get("Ship Name") or row.get("ship_name") or "").strip()
            if not ship_name:
                errors += 1
                continue

            if ship_name.lower() in excluded:
                skipped += 1
                continue

            capital_flag = _parse_bool(_get_cell(row, "Capital"))
            hull_contract = capital_flag or _parse_bool(_get_cell(row, "HullContract"))

            defaults = {
                "strategic": _parse_isk(row.get("Strategic")),
                "peacetime": _parse_isk(row.get("Peacetime")),
                "shitstack": _parse_isk(row.get("Shit Stack")),
                "tnt_special": _parse_isk(row.get("TNT Special")),
                "hull_contract": hull_contract,
            }

            _, was_created = ShipPayout.objects.update_or_create(
                ship_name=ship_name,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

    # Optional: clean up old jobs (or keep for audit)
    job.delete()

    messages.success(
        request,
        f"Bulk payout import complete. Created: {created}, Updated: {updated}, Skipped: {skipped}, Errors: {errors}.",
    )
    return redirect("srp:admin_payouts")
