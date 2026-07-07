from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('srp', '0017_srpconfig_npc_damage_threshold'),
    ]

    operations = [
        migrations.AlterField(
            model_name='srpconfig',
            name='monthly_ceiling_peacetime',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text=(
                    "Soft monthly ISK ceiling for Peacetime approvals. When "
                    "approving a claim would push this month's approved+paid "
                    "Peacetime total over this value, the reviewer sees a "
                    "WARNING (never a hard block). Leave 0/blank to disable the "
                    "check."
                ),
                max_digits=20,
                validators=[MinValueValidator(0)],
            ),
        ),
        migrations.AlterField(
            model_name='srpconfig',
            name='monthly_ceiling_strategic',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text=(
                    "Soft monthly ISK ceiling for Strategic approvals. When "
                    "approving a claim would push this month's approved+paid "
                    "Strategic total over this value, the reviewer sees a "
                    "WARNING (never a hard block). Leave 0/blank to disable the "
                    "check."
                ),
                max_digits=20,
                validators=[MinValueValidator(0)],
            ),
        ),
        migrations.AlterField(
            model_name='srpconfig',
            name='default_multiplier',
            field=models.DecimalField(
                decimal_places=2,
                default=1,
                help_text=(
                    "Global multiplier applied to every auto-calculated payout "
                    "(base ShipPayout value x this). 1.00 = no change. Only "
                    "affects recomputed (PENDING) claims; APPROVED/PAID amounts "
                    "stay frozen and MANUAL payouts are never scaled."
                ),
                max_digits=6,
            ),
        ),
    ]
