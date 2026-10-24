from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1] / "src" / "btc_dca_bridge"


class ArchitectureV2Tests(unittest.TestCase):
    def test_core_and_risk_do_not_depend_on_venue_or_cli(self):
        for directory in (ROOT / "core", ROOT / "risk"):
            for path in directory.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = "\n".join(ast.unparse(node) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)))
                self.assertNotIn("ccxt", imports, path)
                self.assertNotIn("bybit", imports.lower(), path)
                self.assertNotIn("cli", imports.lower(), path)

    def test_strategy_has_no_venue_or_execution_transport_import(self):
        for path in (ROOT / "strategies").rglob("*.py"):
            source = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("venues.", source, path)
            self.assertNotIn("submit_once", source, path)

    def test_ccxt_is_contained_to_venue_adapter(self):
        for path in ROOT.rglob("*.py"):
            if path == ROOT / "venues" / "ccxt" / "adapter.py":
                continue
            self.assertIsNone(re.search(r"^\\s*(?:from\\s+ccxt|import\\s+ccxt)\\b", path.read_text(encoding="utf-8"), re.MULTILINE), path)


if __name__ == "__main__":
    unittest.main()
