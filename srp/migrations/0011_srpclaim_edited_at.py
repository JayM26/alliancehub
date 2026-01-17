from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("srp", "0010_alter_srpclaim_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="srpclaim",
            name="edited_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
