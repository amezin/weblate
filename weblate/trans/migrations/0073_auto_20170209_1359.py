# -*- coding: utf-8 -*-
# Generated by Django 1.11a1 on 2017-02-09 12:59
from __future__ import unicode_literals

from django.db import migrations
from django.db.models import Q

from weblate.utils.hash import calculate_hash


def calculate_id_hash(apps, schema_editor):
    Unit = apps.get_model('trans', 'Unit')
    Comment = apps.get_model('trans', 'Comment')
    Check = apps.get_model('trans', 'Check')
    Suggestion = apps.get_model('trans', 'Suggestion')
    Source = apps.get_model('trans', 'Source')

    content_models = (Comment, Check, Suggestion)

    units = Unit.objects.prefetch_related(
        'translation', 'translation__language',
        'translation__subproject',
        'translation__subproject__project',
    )

    for unit in units.iterator():
        unit.content_hash = calculate_hash(unit.source, unit.context)
        if unit.translation.subproject.template:
            unit.id_hash = calculate_hash(None, unit.context)
        else:
            unit.id_hash = unit.content_hash
        unit.save(update_fields=['content_hash', 'id_hash'])

        for model in content_models:
            model.objects.filter(
                project=unit.translation.subproject.project,
                contentsum=unit.contentsum
                Q(language=unit.translation.language) | Q(language=None),
            ).update(
                content_hash=unit.content_hash
            )
        Source.objects.filter(
            subproject=unit.translation.subproject,
            checksum=unit.checksum
        ).update(
            id_hash=unit.id_hash
        )

    # Remove stale instances
    for model in content_models:
        model.objects.filter(content_hash=0).delete()
    Source.objects.filter(id_hash=0).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('trans', '0072_auto_20170209_1234'),
    ]

    operations = [
        migrations.RunPython(calculate_id_hash),
    ]
