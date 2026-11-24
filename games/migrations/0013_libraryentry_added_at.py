from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("games", "0012_steamaccount_steamownedgame"),
    ]

    operations = [
        # Leave existing entries undated before enabling dates for new entries.
        migrations.AddField(
            model_name="libraryentry",
            name="added_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name="libraryentry",
            name="added_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
    ]
