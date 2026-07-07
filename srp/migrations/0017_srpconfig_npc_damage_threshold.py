from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('srp', '0016_alter_srpclaim_payout_amount_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='srpconfig',
            name='npc_damage_threshold',
            field=models.PositiveIntegerField(
                default=50,
                help_text=(
                    'NPC damage share (percent, 0-100) at or above which a '
                    'claim is FLAGGED as an NPC/ratting loss. Below this, NPC '
                    'involvement is shown as neutral info, not a warning. '
                    'NPC-only kills are always flagged regardless of this '
                    'value. Default 50.'
                ),
                validators=[MinValueValidator(0)],
            ),
        ),
    ]
