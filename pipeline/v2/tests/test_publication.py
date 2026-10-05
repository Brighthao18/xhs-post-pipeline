"""Publication evidence regression checks for the public v2 protocol."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime

from pipeline.v2.publication import FailureClass, validate_observation


ATTEMPT = {
    "attempt_id": "attempt-001",
    "account_id": "offline-account",
    "title": "草木与季节",
    "content_hash": "a" * 64,
    "submitted_at": "2026-10-04T12:00:00+08:00",
}
OBSERVATION = {
    "account_id": "offline-account",
    "title": "草木与季节",
    "observed_at": "2026-10-04T12:00:10+08:00",
    "record_created_at": "2026-10-04T12:00:05+08:00",
    "state": "published",
    "note_id": "0123456789abcdef01234567",
    "note_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
    "content_hash": "a" * 64,
    "evidence_type": "management_record",
    "evidence_ref": r"D:\xhs-fixtures\record.png",
}


class ObservationTests(unittest.TestCase):
    def observe(self, **changes):
        observation = copy.deepcopy(OBSERVATION)
        observation.update(changes)
        return validate_observation(copy.deepcopy(ATTEMPT), observation)

    def assert_unknown(self, result, error=None):
        self.assertEqual(result["status"], "submit_unknown")
        self.assertIs(result["confirmed"], False)
        self.assertIs(result["published"], False)
        self.assertIs(result["retry_allowed"], False)
        if error is not None:
            self.assertEqual(result["error_code"], error.value)

    def test_matching_published_record(self):
        result = self.observe()
        self.assertEqual(result["status"], "published")
        self.assertIs(result["confirmed"], True)
        self.assertIs(result["published"], True)
        self.assertIs(result["retry_allowed"], False)
        self.assertEqual(result["note_id"], OBSERVATION["note_id"])

    def test_pending_review_is_not_publication(self):
        result = self.observe(state="pending_review")
        self.assertEqual(result["status"], "pending_review")
        self.assertTrue(result["confirmed"])
        self.assertFalse(result["published"])

    def test_scheduled_is_not_publication(self):
        result = self.observe(state="scheduled")
        self.assertEqual(result["status"], "scheduled")
        self.assertFalse(result["published"])

    def test_matching_rejection_is_explicit(self):
        result = self.observe(state="rejected")
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(result["confirmed"])
        self.assertFalse(result["published"])
        self.assertEqual(result["error_code"], FailureClass.PLATFORM_REJECTED.value)

    def test_wrong_or_missing_account_never_confirms(self):
        for account in ("other-account", "示例作者", "", None):
            with self.subTest(account=account):
                self.assert_unknown(self.observe(account_id=account), FailureClass.WRONG_ACCOUNT)

    def test_title_mismatch_does_not_confirm_even_matching_hash(self):
        self.assert_unknown(self.observe(title="同名旧笔记"), FailureClass.CONTENT_MISMATCH)

    def test_content_hash_mismatch_blocks_boolean_fallback(self):
        self.assert_unknown(self.observe(
            content_hash="b" * 64,
            verified_match={key: True for key in ("title", "body", "images", "image_order")},
        ), FailureClass.CONTENT_MISMATCH)

    def test_hash_without_platform_evidence_does_not_confirm(self):
        self.assert_unknown(self.observe(evidence_ref=None), FailureClass.MISSING_EVIDENCE)

    def test_toast_editor_and_url_transition_do_not_confirm(self):
        for evidence_type in ("toast", "url_transition", "record_editor", "editor", None):
            with self.subTest(evidence_type=evidence_type):
                self.assert_unknown(self.observe(
                    evidence_type=evidence_type, success=True,
                ), FailureClass.MISSING_EVIDENCE)

    def test_arbitrary_success_flag_is_insufficient(self):
        result = validate_observation(ATTEMPT, {"success": True, "url": "https://www.xiaohongshu.com"})
        self.assert_unknown(result)

    def test_conflicting_attempt_identity_is_rejected(self):
        self.assert_unknown(self.observe(attempt_id="other-attempt"), FailureClass.AMBIGUOUS_RECORD)

    def test_ambiguous_counts_including_boolean_are_rejected(self):
        for count in (0, 2, True, "1", None):
            with self.subTest(count=count):
                self.assert_unknown(self.observe(match_count=count), FailureClass.AMBIGUOUS_RECORD)

    def test_ambiguous_and_conflicting_candidates_are_rejected(self):
        for candidates in ([], [{}, {}], [{"account_id": "other"}], "record"):
            with self.subTest(candidates=candidates):
                self.assert_unknown(self.observe(candidates=candidates), FailureClass.AMBIGUOUS_RECORD)

    def test_one_consistent_candidate_is_valid(self):
        result = self.observe(match_count=1, candidates=[{"note_id": OBSERVATION["note_id"]}])
        self.assertTrue(result["published"])

    def test_unknown_platform_states_do_not_confirm(self):
        for state in ("success", "submitted", "", None, [], {}):
            with self.subTest(state=state):
                self.assert_unknown(self.observe(state=state), FailureClass.INVALID_OBSERVATION)

    def test_explicit_timezones_required(self):
        for value in ("2026-10-04T12:00:10", "2026-10-04", "yesterday", None):
            with self.subTest(value=value):
                self.assert_unknown(self.observe(observed_at=value), FailureClass.INVALID_TIME)

    def test_equivalent_utc_timestamp_is_accepted(self):
        self.assertTrue(self.observe(observed_at="2026-10-04T04:00:10Z")["published"])

    def test_old_or_future_record_is_not_this_attempt(self):
        for value in ("2026-10-04T11:59:59+08:00", "2026-10-04T12:01:00+08:00"):
            with self.subTest(value=value):
                self.assert_unknown(self.observe(record_created_at=value), FailureClass.STALE_RECORD)

    def test_observation_before_submit_is_rejected(self):
        self.assert_unknown(self.observe(observed_at="2026-10-04T11:00:00+08:00"), FailureClass.STALE_RECORD)

    def test_real_note_url_can_supply_identity(self):
        observation = copy.deepcopy(OBSERVATION)
        del observation["note_id"]
        result = validate_observation(ATTEMPT, observation)
        self.assertTrue(result["published"])
        self.assertEqual(result["note_id"], OBSERVATION["note_id"])

    def test_note_id_without_url_can_supply_identity(self):
        observation = copy.deepcopy(OBSERVATION)
        del observation["note_url"]
        self.assertTrue(validate_observation(ATTEMPT, observation)["published"])

    def test_bad_urls_and_placeholder_note_ids_are_rejected(self):
        for url in (
            "http://www.xiaohongshu.com/explore/0123456789abcdef01234567",
            "https://example.com/explore/0123456789abcdef01234567",
            "https://www.xiaohongshu.com/success",
            "https://www.xiaohongshu.com/explore/unknown",
            "https://user:secret@www.xiaohongshu.com/explore/0123456789abcdef01234567",
        ):
            with self.subTest(url=url):
                self.assert_unknown(self.observe(note_url=url), FailureClass.INVALID_NOTE_IDENTITY)
        self.assert_unknown(self.observe(note_id="unknown"), FailureClass.INVALID_NOTE_IDENTITY)

    def test_note_url_and_id_must_identify_same_record(self):
        self.assert_unknown(self.observe(note_id="another-note-123"), FailureClass.AMBIGUOUS_RECORD)

    def test_evidence_note_url_must_identify_same_record(self):
        self.assert_unknown(self.observe(
            evidence_ref="https://www.xiaohongshu.com/explore/another-note-123",
        ), FailureClass.AMBIGUOUS_RECORD)

    def test_strong_readback_without_hash_is_accepted(self):
        observation = copy.deepcopy(OBSERVATION)
        del observation["content_hash"]
        observation["verified_match"] = {key: True for key in ("title", "body", "images", "image_order")}
        observation["match_evidence"] = {
            "body": r"D:\xhs-fixtures\record-body.png",
            "images": r"D:\xhs-fixtures\record-images.png",
        }
        self.assertTrue(validate_observation(ATTEMPT, observation)["published"])

    def test_partial_image_matching_is_insufficient(self):
        observation = copy.deepcopy(OBSERVATION)
        del observation["content_hash"]
        observation["verified_match"] = {"title": True, "body": True, "images": True}
        observation["match_evidence"] = {"body": "/captures/body.png", "images": "/captures/images.png"}
        self.assert_unknown(validate_observation(ATTEMPT, observation), FailureClass.INSUFFICIENT_MATCH)

    def test_strong_readback_evidence_cannot_identify_different_note(self):
        observation = copy.deepcopy(OBSERVATION)
        del observation["content_hash"]
        observation["verified_match"] = {key: True for key in ("title", "body", "images", "image_order")}
        observation["match_evidence"] = {
            "body": "https://www.xiaohongshu.com/explore/another-note-123",
            "images": "/captures/images.png",
        }
        self.assert_unknown(validate_observation(ATTEMPT, observation), FailureClass.AMBIGUOUS_RECORD)

    def test_readback_flags_without_capture_references_are_insufficient(self):
        observation = copy.deepcopy(OBSERVATION)
        del observation["content_hash"]
        observation["verified_match"] = {key: True for key in ("title", "body", "images", "image_order")}
        self.assert_unknown(validate_observation(ATTEMPT, observation), FailureClass.INSUFFICIENT_MATCH)

    def test_relative_or_remote_nonplatform_evidence_is_insufficient(self):
        for ref in ("record.png", "https://example.com/record.png", "", r"D:\tools\evidence.exe"):
            with self.subTest(ref=ref):
                self.assert_unknown(self.observe(evidence_ref=ref), FailureClass.MISSING_EVIDENCE)

    def test_invalid_attempts_do_not_throw(self):
        for attempt in (None, {}, {**ATTEMPT, "content_hash": "success"}, {**ATTEMPT, "submitted_at": "today"}):
            with self.subTest(attempt=attempt):
                self.assert_unknown(validate_observation(attempt, OBSERVATION))

    def test_malformed_observation_fields_do_not_throw(self):
        for field in ("state", "evidence_type", "content_hash", "note_id", "observed_at"):
            with self.subTest(field=field):
                self.assert_unknown(self.observe(**{field: {"invalid": True}}))

    def test_validator_does_not_mutate_inputs(self):
        attempt = copy.deepcopy(ATTEMPT)
        observation = copy.deepcopy(OBSERVATION)
        validate_observation(attempt, observation)
        self.assertEqual(attempt, ATTEMPT)
        self.assertEqual(observation, OBSERVATION)


if __name__ == "__main__":
    unittest.main()
