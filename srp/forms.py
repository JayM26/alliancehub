from django import forms  # pyright: ignore[reportMissingModuleSource]
from .models import SRPClaim


class SRPClaimForm(forms.ModelForm):
    class Meta:
        model = SRPClaim
        fields = ["esi_link", "ship", "category", "broadcast_text"]

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.fields["broadcast_text"].widget.attrs[
            "placeholder"
        ] = "Fleet broadcast or op post link"
        self.fields["esi_link"].widget.attrs[
            "placeholder"
        ] = "https://esi.evetech.net/v1/killmails/.../..."

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get("category")
        broadcast = cleaned.get("broadcast_text", "").strip()

        if category in ("STRATEGIC", "PEACETIME") and not broadcast:
            raise forms.ValidationError(
                "Broadcast/Op Post is required for Strategic or Peacetime claims."
            )
        return cleaned
