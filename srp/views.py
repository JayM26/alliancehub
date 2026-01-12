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

from .esi import populate_claim_from_esi, fetch_type_name, get_type_names_cached
from .forms import SRPClaimForm
from .models import ClaimReview, SRPClaim, ShipPayout, SRPConfig


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
    for name in ["High Slots", "Mid Slots", "Low Slots", "Rigs", "Cargo", "Drone Bay", "Other"]:
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
    blue_corp_ids = set(
        int(x) for x in (cfg.blue_corp_ids or []) if str(x).isdigit()
    )

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
    npc_damage_pct = round((npc_damage / total_damage) * 100, 1) if total_damage else 0.0
    player_damage_pct = round((player_damage / total_damage) * 100, 1) if total_damage else 0.0

    npc_only = player_count == 0 and npc_count > 0
    npc_present = npc_count > 0

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
        },
    )


