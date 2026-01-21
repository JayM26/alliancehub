from django.contrib.auth.decorators import (  # pyright: ignore[reportMissingModuleSource]
    login_required,
)
from django.shortcuts import (  # pyright: ignore[reportMissingModuleSource]
    render,
    redirect,
)

from accounts.utils import get_user_identity_bundle


def home(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "home.html")


@login_required
def dashboard(request):
    user = request.user

    ident = get_user_identity_bundle(user)

    context = {
        **ident,
        "is_reviewer": user.has_perm("srp.can_review_srp"),
        "can_view_reports": user.has_perm("srp.can_view_srp_reports"),
    }
    return render(request, "core/dashboard.html", context)


@login_required
def link_character(request):
    request.session["link_mode"] = "alt"
    return redirect("/sso/login/")
