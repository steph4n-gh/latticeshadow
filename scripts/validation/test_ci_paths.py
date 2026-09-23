"""Representative file changes must keep required checks and save Mac minutes."""

import unittest

from ci_paths import ALL, BUNDLE, MAC, MODEL, NONE, SWIFT, Checks, select


class PathSelectionTests(unittest.TestCase):
    def test_docs_and_linux_tests_skip_macos(self):
        self.assertEqual(select(["README.md", "docs/ARCHITECTURE.md", "packages/db/tests/test_store.py"]), NONE)
        self.assertEqual(select([
            "docs/USER_MANUAL.md", "docs/USER_MANUAL.html", "docs/user-manual.css",
            "scripts/render_user_manual.sh",
        ]), NONE)

    def test_cli_test_runs_core_only(self):
        self.assertEqual(select(["packages/cli/tests/test_menu.py"]), MAC)
        self.assertEqual(select(["packages/cli/scripts/validate_lifecycle.py"]), MAC)
        self.assertEqual(select(["packages/cli/scripts/validate_mcp_client.py"]), MAC)

    def test_recall_evaluator_needs_model_but_not_bundle(self):
        self.assertEqual(select(["packages/cli/scripts/evaluate_recall.py"]), MODEL)

    def test_memory_or_model_change_runs_model(self):
        self.assertEqual(select(["packages/cli/latticeshadow/timeline.py"]), MODEL)
        self.assertEqual(select(["packages/db/latticeshadow_db/latticedb/store.py"]), MODEL)

    def test_native_and_recipe_changes(self):
        self.assertEqual(select(["packages/cli/native/Package.swift"]), SWIFT)
        self.assertEqual(select(["packages/cli/packaging/setup_app.py"]), BUNDLE)
        self.assertEqual(select(["packages/cli/latticeshadow/menu.py"]), BUNDLE)
        self.assertEqual(select(["packages/cli/latticeshadow/shadowd.py"]), BUNDLE)

    def test_mixed_changes_union(self):
        self.assertEqual(select(["packages/cli/native/Package.swift", "packages/cli/latticeshadow/vaults.py"]),
                         Checks(macos=True, model=True, swift=True))

    def test_unknown_and_candidate_are_conservative(self):
        self.assertEqual(select([".github/workflows/ci.yml"]), ALL)
        self.assertEqual(select(["docs/README.md"], candidate=True), ALL)
        self.assertEqual(select([]), ALL)


if __name__ == "__main__":
    unittest.main()
