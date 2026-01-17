import re  # pyright: ignore[reportMissingModuleSource]
from django import forms  # pyright: ignore[reportMissingModuleSource]
from decimal import (
    Decimal,
    InvalidOperation,
)  # pyright: ignore[reportMissingModuleSource]
from .models import SRPClaim, ShipPayout  # pyright: ignore[reportMissingModuleSource]


class SRPClaimForm(forms.ModelForm):
    class Meta:
        model = SRPClaim
        fields = ["esi_link", "category", "broadcast_text"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Users should NOT be able to submit Manual claims (reviewer-only category).
        self.fields["category"].choices = [
            c
            for c in self.fields["category"].choices
            if c[0] != SRPClaim.Category.MANUAL
        ]

        self.fields["broadcast_text"].widget.attrs[
            "placeholder"
        ] = "Fleet broadcast or op post link"
        self.fields["esi_link"].widget.attrs[
            "placeholder"
        ] = "https://esi.evetech.net/latest/killmails/<killmail_id>/<hash>/?datasource=tranquility"

    def clean_esi_link(self):
        link = (self.cleaned_data.get("esi_link") or "").strip()
        if not re.search(r"/killmails/\d+/[0-9a-fA-F]+", link):
            raise forms.ValidationError(
                "Please paste an ESI killmail link that includes both the killmail ID and hash."
            )
        return link

    def clean(self):
        cleaned = super().clean()
        category = (cleaned.get("category") or "").strip().upper()
        broadcast = (cleaned.get("broadcast_text") or "").strip()

        if (
            category in (SRPClaim.Category.STRATEGIC, SRPClaim.Category.PEACETIME)
            and not broadcast
        ):
            raise forms.ValidationError(
                "Broadcast/Op Post is required for Strategic or Peacetime claims."
            )
        return cleaned


class ShipPayoutForm(forms.ModelForm):
    class Meta:
        model = ShipPayout
        fields = [
            "ship_name",
            "strategic",
            "peacetime",
            "shitstack",
            "tnt_special",
            "hull_contract",
        ]
        widgets = {
            "ship_name": forms.TextInput(attrs={"class": "form-control"}),
            "strategic": forms.NumberInput(attrs={"class": "form-control"}),
            "peacetime": forms.NumberInput(attrs={"class": "form-control"}),
            "shitstack": forms.NumberInput(attrs={"class": "form-control"}),
            "tnt_special": forms.NumberInput(attrs={"class": "form-control"}),
        }


class SRPClaimReviewerEditForm(forms.ModelForm):
    # Use text so "50,000,000" works (browsers hate commas in type=number)
    payout_amount = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "e.g. 50000000 or 50,000,000",
            }
        ),
        help_text="Only required for Manual.",
    )

    class Meta:
        model = SRPClaim
        fields = ["category", "payout_amount"]
        widgets = {
            "category": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_payout_amount(self):
        raw = (self.cleaned_data.get("payout_amount") or "").strip()
        if raw == "":
            return None

        normalized = raw.replace(",", "").replace("_", "").replace(" ", "")

        try:
            value = Decimal(normalized)
        except (InvalidOperation, ValueError):
            raise forms.ValidationError("Enter a valid ISK amount (numbers only).")

        if value < 0:
            raise forms.ValidationError("Payout cannot be negative.")

        return value

    def clean(self):
        cleaned = super().clean()
        category = (cleaned.get("category") or "").strip().upper()
        payout = cleaned.get("payout_amount")

        if category == SRPClaim.Category.MANUAL and payout is None:
            raise forms.ValidationError("Manual category requires a payout amount.")

        return cleaned
