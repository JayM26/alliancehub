from django.contrib import messages  # pyright: ignore[reportMissingModuleSource]
from django.contrib.auth.decorators import (  # pyright: ignore[reportMissingModuleSource]
    login_required,
    permission_required,
)
from django.db.models import Q  # pyright: ignore[reportMissingModuleSource]
from django.shortcuts import (  # pyright: ignore[reportMissingModuleSource]
    get_object_or_404,
    redirect,
    render,
)
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

from .esi import populate_claim_from_esi, fetch_type_name
from .forms import SRPClaimForm
from .models import ClaimReview, SRPClaim, ShipPayout


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

    claim.set_status(
        "APPROVED",
        reviewer=request.user,
        note=comment or "Approved via reviewer queue.",
    )
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

    claim.set_status(
        "DENIED", reviewer=request.user, note=comment or "Denied via reviewer queue."
    )
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

    claim.set_status(
        "PAID", reviewer=request.user, note=comment or "Paid via reviewer queue."
    )
    claim.paid_at = claim.paid_at or timezone.now()
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
    - Includes MVP killmail summary (grouped fittings + top attackers)
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

    # Sort attackers by damage_done desc (top first)
    attackers_sorted = sorted(
        attackers,
        key=lambda a: a.get("damage_done") or 0,
        reverse=True,
    )
    attackers_top = attackers_sorted[:25]

    final_blow = next((a for a in attackers_sorted if a.get("final_blow")), None)

    # ---- Group fittings by slot (based on ESI "flag")
    def _slot_group(flag: int) -> str:
        # Common EVE inventory flags:
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

    # ---- MVP name resolution (capped)
    type_names: dict[int, str] = {}
    char_names: dict[int, str] = {}

    try:
        from .esi import fetch_type_name, fetch_character_name
    except Exception:
        fetch_type_name = None
        fetch_character_name = None

    # Collect unique type_ids from items + attacker ships/weapons + victim ship
    type_ids: set[int] = set()
    for it in items:
        tid = it.get("item_type_id")
        if tid:
            type_ids.add(int(tid))

    for a in attackers_top:
        for key in ("ship_type_id", "weapon_type_id"):
            tid = a.get(key)
            if tid:
                type_ids.add(int(tid))

    if claim.ship_type_id:
        type_ids.add(int(claim.ship_type_id))

    # Cap to avoid slow pages
    type_ids_list = list(type_ids)[:40]

    if fetch_type_name:
        for tid in type_ids_list:
            try:
                name = fetch_type_name(int(tid))
                if name:
                    type_names[int(tid)] = name
            except Exception:
                pass

    # Collect unique character_ids (top attackers + victim) and resolve (cap)
    char_ids: set[int] = set()
    for a in attackers_top:
        cid = a.get("character_id")
        if cid:
            char_ids.add(int(cid))

    if claim.victim_character_id:
        char_ids.add(int(claim.victim_character_id))

    char_ids_list = list(char_ids)[:15]

    if fetch_character_name:
        for cid in char_ids_list:
            try:
                name = fetch_character_name(int(cid))
                if name:
                    char_names[int(cid)] = name
            except Exception:
                pass

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
            "attackers": attackers_top,
            "final_blow": final_blow,
            "type_names": type_names,
            "char_names": char_names,
        },
    )
