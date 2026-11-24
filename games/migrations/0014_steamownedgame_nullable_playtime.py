from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("games", "0013_libraryentry_added_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="steamownedgame",
            name="playtime_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
