import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0004_question_conversation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='question',
            name='spoken',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='Teaching',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('transcript', models.TextField()),
                ('heard_by', models.CharField(blank=True, max_length=100)),
                ('proposed', models.JSONField(blank=True, default=list)),
                ('model', models.CharField(blank=True, max_length=100)),
                ('applied', models.JSONField(blank=True, default=list)),
                ('applied_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='teachings', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
