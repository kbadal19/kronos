import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Corrections and speaking profiles become per user, so the pipeline
    can serve more than one person without one's vocabulary leaking into
    another's transcripts.

    CorrectionPair was empty when this was written; the placeholder default
    below exists only to satisfy AddField and is never applied to a row.
    """

    dependencies = [
        ("pipeline", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="correctionpair",
            name="unique_correction_pair",
        ),
        migrations.AddField(
            model_name="correctionpair",
            name="user",
            field=models.ForeignKey(
                default=1,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="correction_pairs",
                to=settings.AUTH_USER_MODEL,
            ),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="correctionpair",
            constraint=models.UniqueConstraint(
                fields=("user", "wrong", "right"), name="unique_correction_pair_per_user"
            ),
        ),
        migrations.CreateModel(
            name="TranscriptionProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("recognizer_language", models.CharField(blank=True, max_length=20)),
                ("languages", models.CharField(default="Hindi and English", max_length=200)),
                ("script", models.CharField(
                    choices=[("roman", "Roman (e.g. Hinglish)"), ("native", "Each language in its own script")],
                    default="roman", max_length=10,
                )),
                ("speaker_notes", models.TextField(blank=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="transcription_profile",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]
