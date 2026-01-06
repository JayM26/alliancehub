from django.conf import settings  # pyright: ignore[reportMissingModuleSource]
from django.core.validators import (  # pyright: ignore[reportMissingModuleSource]
    MinValueValidator,
)  # pyright: ignore[reportMissingModuleSource]
from django.db import models  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

User = settings.AUTH_USER_MODEL

# ---- constants
SRP_CATEGORIES = [
    ("STRATEGIC", "Strategic"),
    ("PEACETIME", "Peacetime"),
    ("SHITSTACK", "Shitstack"),
    ("TNT_SPECIAL", "TNT Special"),
]
STATUS_CHOICES = [
    ("PENDING", "Pending"),
    ("APPROVED", "Approved"),
    ("DENIED", "Denied"),
    ("PAID", "Paid"),
]


class ShipPayout(models.Model):
    """Master payout table per ship, by category."""

    ship_name = models.CharField(max_length=100, unique=True)

    strategic = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    peacetime = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    shitstack = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    tnt_special = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    hull_contract = models.BooleanField(default=False)  # if True: give hull vs ISK
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ship_name"]
        permissions = [
            ("can_manage_srp_payouts", "Can manage SRP payouts"),
        ]

    def __str__(self):
        return self.ship_name

    def payout_for_category(self, category: str):
        mapping = {
            "STRATEGIC": self.strategic,
            "PEACETIME": self.peacetime,
            "SHITSTACK": self.shitstack,
            "TNT_SPECIAL": self.tnt_special,
        }
        return mapping.get(category, 0)


class SRPConfig(models.Model):
    """One-row configuration for ceilings and behavior."""

    monthly_ceiling_peacetime = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    monthly_ceiling_strategic = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    auto_calculate_payouts = models.BooleanField(default=True)
    default_multiplier = models.DecimalField(max_digits=6, decimal_places=2, default=1)

    def __str__(self):
        return "SRP Configuration"

    @classmethod
    def get(cls):
        return cls.objects.first() or cls.objects.create()


class SRPClaim(models.Model):
    """User-submitted SRP claim (loss)."""

    # -------------------------
    # Submitter / ownership
    # -------------------------
    submitter = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="srp_claims"
    )
    character_name = models.CharField(
        max_length=100,
        help_text="Character name reported by submitter (may differ from victim)",
    )

    # -------------------------
    # Ship / payout
    # -------------------------
    ship = models.ForeignKey(
        ShipPayout,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claims",
        help_text="Resolved ShipPayout (manual or ESI)",
    )

    category = models.CharField(max_length=20, choices=SRP_CATEGORIES)

    isk_loss = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    payout_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        blank=True,
        null=True,
    )

    # -------------------------
    # Killmail / ESI input
    # -------------------------
    esi_link = models.URLField(help_text="Killmail / ESI link")

    killmail_id = models.BigIntegerField(null=True, blank=True)
    killmail_hash = models.CharField(max_length=255, null=True, blank=True)
    killmail_raw = models.JSONField(null=True, blank=True)

    # -------------------------
    # Victim (from ESI)
    # -------------------------
    victim_character_id = models.BigIntegerField(null=True, blank=True)
    victim_character_name = models.CharField(max_length=255, null=True, blank=True)

    ship_type_id = models.BigIntegerField(null=True, blank=True)
    ship_name = models.CharField(max_length=255, null=True, blank=True)

    solar_system_id = models.BigIntegerField(null=True, blank=True)
    solar_system_name = models.CharField(max_length=255, null=True, blank=True)

    # -------------------------
    # Location (legacy/manual)
    # -------------------------
    system = models.CharField(max_length=100, blank=True, null=True)
    region = models.CharField(max_length=100, blank=True, null=True)

    # -------------------------
    # Additional claim data
    # -------------------------
    broadcast_text = models.TextField(
        blank=True,
        help_text="Fleet broadcast text (required for some categories)",
    )

    fit_data = models.JSONField(
        blank=True,
        null=True,
        help_text="Optional ship fitting data (future use)",
    )

    # -------------------------
    # Review / processing
    # -------------------------
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING",
    )

    reviewer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_srp_claims",
    )

    note = models.TextField(
        blank=True,
        help_text="Latest reviewer note (full history in ClaimReview)",
    )

    submitted_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def calculate_payout(self):
        """
        Computes payout from ShipPayout + claim category.
        If ship isn't set yet (Option B / ESI), payout is 0.
        """
        if not self.ship:
            return 0
        base = self.ship.payout_for_category(self.category)
        return base or 0

    def set_status(self, new_status: str, reviewer=None, note: str = ""):
        self.status = new_status
        if reviewer:
            self.reviewer = reviewer
        if new_status in {"APPROVED", "DENIED", "PAID"}:
            self.processed_at = timezone.now()
        if note:
            self.note = (self.note + "\n" if self.note else "") + note

    def save(self, *args, **kwargs):
        # Only auto-calc when payout is missing (lets you override manually later if desired)
        if SRPConfig.get().auto_calculate_payouts and self.payout_amount is None:
            self.payout_amount = self.calculate_payout()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"SRPClaim #{self.id} - {self.character_name}"

    class Meta:
        indexes = [
            models.Index(fields=["status", "category"]),
            models.Index(fields=["submitted_at"]),
            models.Index(fields=["character_name"]),
        ]
        ordering = ["-submitted_at"]
        permissions = [
            ("can_review_srp", "Can review SRP claims"),
            ("can_view_srp_reports", "Can view SRP reports"),
        ]

    # ---- business logic
    def clean(self):
        from django.core.exceptions import (  # pyright: ignore[reportMissingModuleSource]
            ValidationError,
        )  # pyright: ignore[reportMissingModuleSource]

        if (
            self.category in {"STRATEGIC", "PEACETIME"}
            and not self.broadcast_text.strip()
        ):
            raise ValidationError(
                "Broadcast/Op Post is required for Strategic or Peacetime claims."
            )


class ClaimReview(models.Model):
    """Optional: keep a trail of actions."""

    claim = models.ForeignKey(
        SRPClaim, on_delete=models.CASCADE, related_name="reviews"
    )
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE)
    action = models.CharField(max_length=50)  # Approved / Denied / Paid / Edited
    comment = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.claim_id} - {self.action} by {self.reviewer}"
