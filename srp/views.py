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
        form = SRPClaimForm(request.POST, user=request.user)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.submitter = request.user
            claim.character_name = request.user.username  # will later pull from EVE SSO
            # submitted_at is auto_now_add on the model, so we don't need this,
            # but leaving it here is harmless if your model doesn't set auto_now_add.
            claim.submitted_at = timezone.now()
            claim.save()
            messages.success(request, "Your SRP claim has been submitted.")
            return redirect("srp:my_claims")
        messages.error(request, "Please correct the errors below.")
    else:
        form = SRPClaimForm(user=request.user)

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
    - status (default: PENDING)
    - category
    - search (character/ship/system/link)
    """
    status = request.GET.get("status", "PENDING")
    category = request.GET.get("category", "")
    search = (request.GET.get("q", "") or "").strip()

    qs = SRPClaim.objects.select_related("ship", "submitter", "reviewer").all()

    if status:
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

    claims = qs.order_by("-submitted_at")[:500]  # safe cap for now

    context = {
        "claims": claims,
        "status": status,
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


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def approve_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    claim.set_status(
        "APPROVED", reviewer=request.user, note="Approved via reviewer queue."
    )
    claim.save()
    _add_review_record(claim, request.user, "Approved", "Approved via reviewer queue.")

    messages.success(request, f"Approved claim #{claim.id}.")
    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def deny_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    claim.set_status("DENIED", reviewer=request.user, note="Denied via reviewer queue.")
    claim.save()
    _add_review_record(claim, request.user, "Denied", "Denied via reviewer queue.")

    messages.success(request, f"Denied claim #{claim.id}.")
    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))


@login_required
@permission_required("srp.can_review_srp", raise_exception=True)
def pay_claim(request, claim_id: int):
    if request.method != "POST":
        return redirect("srp:review_queue")

    claim = get_object_or_404(SRPClaim, id=claim_id)
    claim.set_status("PAID", reviewer=request.user, note="Paid via reviewer queue.")
    claim.save()
    _add_review_record(claim, request.user, "Paid", "Paid via reviewer queue.")

    messages.success(request, f"Marked claim #{claim.id} as Paid.")
    return redirect(request.META.get("HTTP_REFERER", "srp:review_queue"))
