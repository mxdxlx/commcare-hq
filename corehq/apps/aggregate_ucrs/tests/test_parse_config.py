from __future__ import absolute_import
from __future__ import unicode_literals
from django.test import SimpleTestCase

from corehq.apps.aggregate_ucrs.tests.base import AggregationBaseTestMixin


class ConfigParseTest(SimpleTestCase, AggregationBaseTestMixin):

    def test_parse_basic_definition(self):
        spec = self.get_config_spec()
        self.assertEqual('550c3cd432d931387e75e8506b5caf9e', spec.primary_table.data_source_id)
        self.assertEqual('doc_id', spec.primary_table.key_column)
        self.assertEqual(4, len(spec.primary_table.columns))
        self.assertEqual('month', spec.aggregation_config.unit)
        self.assertEqual('date_opened', spec.aggregation_config.start_column)
        self.assertEqual('date_closed', spec.aggregation_config.end_column)
        self.assertEqual(1, len(spec.secondary_tables))
        secondary_table = spec.secondary_tables[0]
        self.assertEqual('d824a4864ecb421fb3be8bf8173a05d7', secondary_table.data_source_id)
        self.assertEqual('form.case.@case_id', secondary_table.key_column)
        self.assertEqual('received_on', secondary_table.aggregation_column)
        self.assertEqual(1, len(secondary_table.columns))
