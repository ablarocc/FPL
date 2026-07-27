"""Offline unit tests for the read-only Bzzoiro evaluator."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import evaluator
import mapping


class ResponseShapeTests(unittest.TestCase):
    def test_response_rows_accepts_bare_list(self):
        self.assertEqual(evaluator.response_rows([{"id": 1}]), [{"id": 1}])

    def test_response_rows_accepts_documented_and_live_envelopes(self):
        for key in ("results", "seasons", "player_stats", "players", "fixtures"):
            with self.subTest(key=key):
                self.assertEqual(
                    evaluator.response_rows({key: [{"id": 1}]}, key),
                    [{"id": 1}],
                )

    def test_response_rows_rejects_non_list_payload(self):
        self.assertEqual(evaluator.response_rows({"results": {"id": 1}}), [])
        self.assertEqual(evaluator.response_rows(None), [])


class IdentityTests(unittest.TestCase):
    def test_id_key_normalises_csv_float_ids(self):
        self.assertEqual(evaluator.id_key("36.0"), "36")
        self.assertEqual(evaluator.id_key(36), "36")
        self.assertEqual(evaluator.id_key("provider-x"), "provider-x")

    def test_competition_selection_uses_region(self):
        leagues = [
            {"id": 29, "name": "CAF Champions League", "country": "Africa"},
            {"id": 7, "name": "Champions League", "country": "Europe"},
        ]
        selected = evaluator.select_competition(
            leagues, r"^(uefa )?champions league$", "Europe"
        )
        self.assertEqual(selected["id"], 7)

    def test_team_aliases_reject_truncated_and_variant_clubs(self):
        self.assertIsNone(mapping.resolve_team_name("Manchester"))
        self.assertIsNone(mapping.resolve_team_name("Manchester United Women"))
        self.assertIsNone(mapping.resolve_team_name("Tottenham Hotspur U21"))
        self.assertIsNone(mapping.resolve_team_name("Ipswich Town XI"))
        self.assertEqual(
            mapping.resolve_team_name("Manchester United FC"), "Man Utd"
        )


class MappingTests(unittest.TestCase):
    def test_schema_coverage_accounts_for_identity_columns(self):
        coverage = mapping.coverage_summary()
        self.assertEqual(coverage["identity_columns"], 2)
        self.assertEqual(coverage["stat_columns"], 62)
        self.assertEqual(
            coverage["covered"] + coverage["unavailable"],
            coverage["stat_columns"],
        )
        self.assertEqual(coverage["candidate_comparable"], 35)
        self.assertEqual(coverage["merge_safe"], 23)

    def test_blocked_attempt_is_not_a_validated_defensive_block(self):
        self.assertNotIn("blocked_scoring_attempt", mapping.PMS_DIRECT)
        self.assertIn("blocks", mapping.PMS_UNAVAILABLE)

    def test_percentage_derivations_use_percent_scale(self):
        row = {"won_contest": 2, "total_contest": 4}
        self.assertEqual(
            evaluator.derived_provider_value(
                row, "successful_dribbles_percent"
            ),
            50,
        )

    def test_duel_derivations_use_won_plus_lost_denominator(self):
        row = {"duel_won": 3, "duel_lost": 1}
        self.assertEqual(
            evaluator.derived_provider_value(row, "ground_duels_won_percent"),
            75,
        )

    def test_zero_denominator_is_zero(self):
        row = {"accurate_pass": 0, "total_pass": 0}
        self.assertEqual(
            evaluator.derived_provider_value(row, "accurate_passes_percent"),
            0,
        )


class FriendlyIdentityTests(unittest.TestCase):
    def test_match_id_recovers_both_opponents(self):
        self.assertEqual(
            evaluator.parse_friendly_match_id(
                "26-27-friendly-western-sydney-wanderers-fc-vs-chelsea-2026-07-28"
            ),
            ("western sydney wanderers fc", "chelsea"),
        )

    def test_team_key_normalises_provider_suffixes(self):
        self.assertEqual(
            evaluator.fixture_team_key("Western Sydney Wanderers FC"),
            evaluator.fixture_team_key("western-sydney-wanderers"),
        )

    def test_timestamp_parser_normalises_naive_values_to_utc(self):
        self.assertEqual(
            evaluator.parse_utc("2026-07-28T09:45:00"),
            evaluator.parse_utc("2026-07-28T09:45:00Z"),
        )

    def test_fixture_orientation_accepts_neutral_site_reversal(self):
        self.assertEqual(
            evaluator.fixture_orientation("chelsea", "sydney", "sydney", "chelsea"),
            "swapped",
        )
        self.assertEqual(
            evaluator.fixture_orientation("chelsea", "sydney", "chelsea", "sydney"),
            "same",
        )
        self.assertIsNone(
            evaluator.fixture_orientation("chelsea", "sydney", "chelsea", "perth")
        )


class CaptureContractTests(unittest.TestCase):
    def test_friendly_betting_targets_require_future_exact_mapping(self):
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        exact = [
            {"event_id": 1, "repo_match_id": "future-match", "orientation": "same"},
            {"event_id": 2, "repo_match_id": "past-match", "orientation": "swapped"},
        ]
        events = {
            1: {
                "id": 1,
                "event_date": "2026-07-02T12:00:00Z",
                "status": "scheduled",
            },
            2: {
                "id": 2,
                "event_date": "2026-06-30T12:00:00Z",
                "status": "finished",
            },
        }
        targets = evaluator.mapped_friendly_betting_targets(exact, events, now=now)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["event"]["id"], 1)
        self.assertEqual(targets[0]["canonical_match_id"], "future-match")

    def test_betting_selection_prefers_mapped_friendly(self):
        mapped = [
            {
                "event": {"id": 10, "home_team": "Arsenal"},
                "canonical_match_id": "friendly-arsenal",
                "orientation": "same",
            }
        ]
        selected = evaluator.choose_betting_target(
            mapped,
            [{"event_id": 999, "market": "1x2"}],
            {"id": 50},
        )
        self.assertEqual(selected["event"]["id"], 10)
        self.assertEqual(selected["canonical_match_id"], "friendly-arsenal")
        self.assertEqual(selected["selection"], "next_mapped_upcoming_friendly")

    def test_betting_selection_uses_mapped_target_with_global_odds(self):
        mapped = [
            {"event": {"id": 10}, "canonical_match_id": "first"},
            {"event": {"id": 11}, "canonical_match_id": "with-odds"},
        ]
        selected = evaluator.choose_betting_target(
            mapped,
            [{"event_id": 11, "market": "1x2"}],
            None,
        )
        self.assertEqual(selected["event"]["id"], 11)
        self.assertEqual(selected["canonical_match_id"], "with-odds")
        self.assertEqual(
            selected["selection"], "mapped_upcoming_friendly_with_global_odds"
        )

    def test_friendly_player_identity_is_scoped_to_canonical_team(self):
        class FakeApi:
            def get(self, path):
                self.path = path
                return {"players": [{"id": 700, "name": "Alex Smith"}]}

        fpl_players = [
            {
                "player_id": 1,
                "first_name": "Alex",
                "second_name": "Smith",
                "web_name": "Smith",
                "team_code": 3,
            },
            {
                "player_id": 2,
                "first_name": "Alex",
                "second_name": "Smith",
                "web_name": "Smith",
                "team_code": 7,
            },
        ]
        teams = [{"id": 1, "code": 3, "name": "Arsenal"}]
        resolved = {"1": {"id": 101, "name": "Arsenal"}}
        with mock.patch.object(evaluator, "read_csv", return_value=fpl_players):
            identities, rejections = evaluator.build_friendly_player_identity_map(
                FakeApi(), teams, resolved, "2026-2027"
            )
        self.assertEqual(rejections, [])
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["fpl_player_id"], 1)
        self.assertEqual(identities[0]["fpl_team_code"], 3)

class ArtifactSafetyTests(unittest.TestCase):
    def test_output_inside_canonical_data_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluator.safe_output_path(
                str(evaluator.DATA_ROOT / "accidental-probe-output")
            )

    def test_repo_root_and_unapproved_repo_paths_are_rejected(self):
        for path in (
            evaluator.REPO_ROOT,
            evaluator.REPO_ROOT / "scripts" / "bzzoiro" / "accidental-output",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                evaluator.safe_output_path(str(path))

    def test_intentional_repo_output_paths_are_allowed(self):
        for path in (
            evaluator.REPO_ROOT / "bzzoiro_probe_out",
            evaluator.REPO_ROOT / "bzzoiro_probe_out" / "quick",
            evaluator.REPO_ROOT / "scripts" / "bzzoiro" / "sample_data",
        ):
            with self.subTest(path=path):
                self.assertEqual(evaluator.safe_output_path(str(path)), path.resolve())

    def test_json_artifacts_are_complete_and_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            payload = {"rows": [{"id": i, "text": "x" * 100} for i in range(100)]}
            evaluator.dump_json(out, "sample.json", payload)
            self.assertEqual(
                json.loads((out / "sample.json").read_text(encoding="utf-8")),
                payload,
            )

    def test_artifact_filenames_cannot_escape_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("../REPORT.md", "nested/file.json", "event:ads.json"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    evaluator.dump_json(out, name, {"unsafe": True})

    def test_nonempty_unowned_output_is_rejected_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            sentinel = out / "reviewer_notes.txt"
            sentinel.write_text("keep me\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                evaluator.ensure_owned_output_directory(out)
            self.assertEqual("keep me\n", sentinel.read_text(encoding="utf-8"))

    def test_empty_or_evaluator_owned_output_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            evaluator.ensure_owned_output_directory(out)
            (out / "RUN_MANIFEST.json").write_text(
                json.dumps({"probe_version": "2.0"}) + "\n",
                encoding="utf-8",
            )
            evaluator.ensure_owned_output_directory(out)

    def test_api_key_and_base_url_are_not_cli_options(self):
        for option in ("--key", "--base-url"):
            with (
                self.subTest(option=option),
                mock.patch("sys.stderr"),
                self.assertRaises(SystemExit),
            ):
                evaluator.parse_args([option, "unsafe"])

    def test_refresh_removes_only_owned_stale_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            stale = [
                out / "REPORT.md",
                out / "event_99_detail.json",
                out / "friendly_player_stats_event_99.json",
                out / "friendly_event_99_stats.json",
                out / "player_identity_map.json",
                out / "mapped_betting_event.json",
            ]
            keep = out / "reviewer_notes.txt"
            for path in [*stale, keep]:
                path.write_text("content", encoding="utf-8")
            evaluator.clear_owned_artifacts(out)
            self.assertFalse(any(path.exists() for path in stale))
            self.assertTrue(keep.exists())

    def test_stale_secret_scan_markers_are_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            markers = [out / "SECRET_SCAN_OK", out / "SECRET_SCAN_FAILED.txt"]
            for marker in markers:
                marker.write_text("stale", encoding="utf-8")
            evaluator.clear_owned_artifacts(out)
            self.assertFalse(any(marker.exists() for marker in markers))

    def test_unreadable_artifact_blocks_secret_scan_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "unreadable.txt").write_text("content", encoding="utf-8")
            with mock.patch.object(Path, "read_text", side_effect=OSError("blocked")):
                self.assertEqual(
                    evaluator.scrub_secret_leaks(out, "secret"),
                    ["unreadable.txt [unreadable]"],
                )

    def test_secret_scan_scrubs_unsafe_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "safe.txt").write_text("no credentials", encoding="utf-8")
            self.assertEqual(evaluator.scrub_secret_leaks(out, "secret"), [])
            unsafe_path = out / "unsafe.txt"
            unsafe_path.write_text("prefix-secret-suffix", encoding="utf-8")
            self.assertEqual(
                evaluator.scrub_secret_leaks(out, "secret"), ["unsafe.txt"]
            )
            scrubbed = unsafe_path.read_text(encoding="utf-8")
            self.assertNotIn("secret", scrubbed)
            self.assertIn("scrubbed", scrubbed)


if __name__ == "__main__":
    unittest.main()
