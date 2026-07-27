"""Offline contract tests for the branch-only integration review export."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("integration_preview.py")
SPEC = importlib.util.spec_from_file_location("bzzoiro_integration_preview", MODULE_PATH)
assert SPEC and SPEC.loader
preview = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preview
SPEC.loader.exec_module(preview)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle).__next__())


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class IntegrationPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="bzzoiro-preview-test-")
        cls.output = Path(cls.temporary.name) / "review_export"
        canonical_files = [
            context.source_dir / filename
            for context in preview.CONTEXTS
            for filename in ("matches.csv", "fixtures.csv", "playermatchstats.csv")
        ]
        cls.canonical_before = {path: file_hash(path) for path in canonical_files}
        preview.build_review_export(cls.output)
        cls.canonical_after = {path: file_hash(path) for path in canonical_files}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def export_dir(self, context: preview.Context) -> Path:
        return self.output / "git_export" / context.export_rel

    def test_canonical_data_is_not_modified(self) -> None:
        self.assertEqual(self.canonical_before, self.canonical_after)

    def test_canonical_headers_and_fixture_bytes_are_exact(self) -> None:
        for context in preview.CONTEXTS:
            export = self.export_dir(context)
            self.assertEqual(
                csv_header(export / "matches.csv"),
                csv_header(context.source_dir / "matches.csv"),
            )
            self.assertEqual(
                csv_header(export / "playermatchstats.csv"),
                csv_header(context.source_dir / "playermatchstats.csv"),
            )
            self.assertEqual(
                (export / "matches.csv").read_bytes(),
                (export / "fixtures.csv").read_bytes(),
            )

    def test_all_canonical_friendlies_are_present(self) -> None:
        friendlies = preview.CONTEXTS[1]
        source_rows = csv_rows(friendlies.source_dir / "matches.csv")
        review_rows = csv_rows(self.export_dir(friendlies) / "matches.csv")
        self.assertEqual(91, len(source_rows))
        self.assertEqual(
            [row["match_id"] for row in source_rows],
            [row["match_id"] for row in review_rows],
        )

    def test_no_private_provenance_columns_are_emitted(self) -> None:
        for path in self.output.rglob("*.csv"):
            header = set(csv_header(path))
            self.assertFalse(
                header & preview.PROHIBITED_PUBLIC_COLUMNS,
                f"{path} leaked private identity columns",
            )

    def test_only_exact_friendly_identities_are_accepted(self) -> None:
        captured = preview._load_json(
            preview.SAMPLE_DIR / "friendlies_sample.json", {}
        )
        expected = {
            str(row["repo_match_id"]) for row in captured.get("exact_matches", [])
        }
        accepted = {
            row["canonical_id"]
            for row in csv_rows(self.output / "audit" / "accepted_identities.csv")
            if row["entity"] == "event" and row["season"] == preview.CURRENT_SEASON
        }
        self.assertEqual(expected, accepted)

    def test_player_identities_are_high_confidence_and_one_to_one(self) -> None:
        rows = [
            row
            for row in csv_rows(self.output / "audit" / "accepted_identities.csv")
            if row["entity"] == "player"
        ]
        self.assertTrue(rows)
        self.assertEqual({"high"}, {row["confidence"] for row in rows})
        keys = [(row["season"], row["canonical_id"]) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_player_enrichment_keys_are_unique(self) -> None:
        for context in preview.CONTEXTS:
            rows = csv_rows(
                self.export_dir(context) / "bzzoiro_player_match_enrichment.csv"
            )
            keys = [(row["player_id"], row["match_id"]) for row in rows]
            self.assertEqual(len(keys), len(set(keys)))

    def test_unresolved_friendly_candidates_are_quarantined(self) -> None:
        rows = csv_rows(self.output / "audit" / "rejection_summary.csv")
        reasons = {
            (row["entity"], row["reason"]): int(row["row_count"]) for row in rows
        }
        self.assertGreater(
            reasons.get(("event_identity", "loose_or_partial_event_identity"), 0),
            0,
        )
        self.assertGreater(
            reasons.get(("event_identity", "api_event_without_exact_identity"), 0),
            0,
        )

    def test_sparse_team_stats_are_not_complete_even_when_defaults_are_zero(self) -> None:
        sparse = {"ball_possession": 69, "shots_on_target": 0, "fouls": 0}
        complete = {
            **sparse,
            "total_shots": 0,
            "passes": 0,
            "accurate_passes": 0,
        }
        self.assertFalse(preview.PreviewBuilder._team_stats_complete(sparse))
        self.assertTrue(preview.PreviewBuilder._team_stats_complete(complete))

    def test_blocked_attacking_shots_never_map_to_defender_blocks(self) -> None:
        self.assertNotIn("blocked_scoring_attempt", preview.PMS_DIRECT_SAFE)
        self.assertNotIn("penalty_miss", preview.PMS_DIRECT_SAFE)
        self.assertNotIn("duel_won", preview.PMS_DIRECT_SAFE)
        self.assertNotIn("aerial_won", preview.PMS_DIRECT_SAFE)
        self.assertIn(
            "attacking_shots_blocked", preview.PLAYER_ENRICHMENT_COLUMNS
        )
        for context in preview.CONTEXTS:
            self.assertIn(
                "blocks",
                csv_header(self.export_dir(context) / "playermatchstats.csv"),
            )

    def test_provider_availability_is_separate_from_fpl_availability(self) -> None:
        path = (
            self.output
            / "git_export"
            / "supplemental"
            / "bzzoiro_player_availability.csv"
        )
        header = csv_header(path)
        self.assertIn("bzzoiro_availability", header)
        self.assertNotIn("status", header)
        self.assertNotIn("news", header)
        self.assertFalse(any(self.output.rglob("players.csv")))
        players = {
            row["player_id"]: row
            for row in csv_rows(
                preview.REPO_ROOT
                / "data"
                / preview.CURRENT_SEASON
                / "players.csv"
            )
        }
        for row in csv_rows(path):
            self.assertEqual(preview.CURRENT_SEASON, row["season"])
            self.assertIn(row["player_id"], players)
            canonical = players[row["player_id"]]
            self.assertIn(
                row["player_name"],
                {
                    canonical["web_name"],
                    canonical["second_name"],
                    f"{canonical['first_name']} {canonical['second_name']}".strip(),
                },
            )

    def test_betting_rows_require_a_canonical_match(self) -> None:
        canonical_match_ids = {
            row["match_id"]
            for context in preview.CONTEXTS
            for row in csv_rows(self.export_dir(context) / "matches.csv")
        }
        supplemental = self.output / "git_export" / "supplemental"
        for filename in ("bzzoiro_odds.csv", "bzzoiro_predictions.csv"):
            for row in csv_rows(supplemental / filename):
                self.assertIn(row["match_id"], canonical_match_ids)

    def test_swapped_betting_orientation_is_canonicalised(self) -> None:
        self.assertEqual(
            ("Away", "Home"),
            preview._canonical_sides("Home", "Away", True),
        )
        self.assertEqual("A", preview._canonical_outcome("H", True))
        self.assertEqual("H", preview._canonical_outcome("A", True))
        self.assertEqual("D", preview._canonical_outcome("D", True))

    def test_zero_is_not_dropped_by_fallback_selection(self) -> None:
        self.assertEqual(0, preview._first_present(0, 1))

    def test_manifest_matches_generated_files(self) -> None:
        manifest = json.loads(
            (self.output / "MANIFEST.json").read_text(encoding="utf-8")
        )
        for item in manifest["files"]:
            path = self.output / item["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(item["sha256"], file_hash(path))

    def test_refuses_unowned_or_out_of_scope_output_directories(self) -> None:
        with self.assertRaises(ValueError):
            preview.build_review_export(preview.REPO_ROOT / "data")
        with tempfile.TemporaryDirectory(prefix="bzzoiro-preview-unowned-") as temp:
            target = Path(temp) / "review_export"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("user-owned\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                preview.build_review_export(target)
            self.assertEqual("user-owned\n", sentinel.read_text(encoding="utf-8"))

    def test_generation_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bzzoiro-preview-repeat-") as temp:
            second = Path(temp) / "review_export"
            preview.build_review_export(second)
            self.assertEqual(
                preview._tree_hashes(self.output),
                preview._tree_hashes(second),
            )

    def test_committed_review_export_is_current(self) -> None:
        ok, changes = preview.check_review_export(preview.DEFAULT_OUTPUT)
        self.assertTrue(ok, "\n".join(changes))


if __name__ == "__main__":
    unittest.main()
