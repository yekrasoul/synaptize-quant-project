import unittest

from btc_dca_bridge.schemas import load_schema, validate_all_schemas


class SchemaTest(unittest.TestCase):
    def test_all_eight_schemas_parse_and_validate_as_draft_2020_12(self):
        names = validate_all_schemas()
        self.assertEqual(len(names), 12)
        for name in names:
            self.assertEqual(
                load_schema(name)["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )


if __name__ == "__main__":
    unittest.main()
