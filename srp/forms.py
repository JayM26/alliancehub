import re  # pyright: ignore[reportMissingModuleSource]
from django import forms  # pyright: ignore[reportMissingModuleSource]
from .models import SRPClaim  # pyright: ignore[reportMissingModuleSource]


class SRPClaimForm(forms.ModelForm):
    class Meta:
        model = SRPClaim
        fields = ["esi_link", "category", "broadcast_text"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        category = cleaned.get("category")
        broadcast = (cleaned.get("broadcast_text") or "").strip()

        if category in ("STRATEGIC", "PEACETIME") and not broadcast:
            raise forms.ValidationError(
                "Broadcast/Op Post is required for Strategic or Peacetime claims."
            )
        return cleaned
