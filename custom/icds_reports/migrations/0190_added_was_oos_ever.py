# -*- coding: utf-8 -*-
# Generated by Django 1.11.28 on 2020-05-13 15:47
from __future__ import unicode_literals

from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('icds_reports', '0189_new_fields_to_bihar_demogrpahics'),
    ]

    operations = [
        migrations.AddField(
            model_name='biharapidemographics',
            name='was_oos_ever',
            field=models.SmallIntegerField(null=True),
        ),
    ]