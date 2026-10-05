"""Isolated persistence invariants; no network, browser, or real account."""

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline.v2.config import digest, now_iso
from pipeline.v2.store import Store, StateError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state.sqlite")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.source = {"id": "test-source", "enabled": True, "new_only": True}
        self.article = {
            "article_id": "test-article", "url": "https://mp.weixin.qq.com/s/test-article",
            "title": "测试文章", "content": "订阅摘要", "coverage": "partial",
        }

    def discover(self, article=None, baseline=False):
        return self.store.discover(self.source, {"articles": [copy.deepcopy(article or self.article)], "cursor": {"poll": 1}}, baseline=baseline)

    def ready_job(self, key="test-article"):
        article = {**self.article, "article_id": key, "url": "https://mp.weixin.qq.com/s/" + key}
        job_id = self.discover(article)["added"][0]
        full = {**article, "content": "完整正文包含全部证据", "coverage": "full"}
        self.store.update(job_id, article=full, source_hash=digest(full), status="READY",
                          draft={"title": "测试文案", "body": "独立创作"}, content_hash="a" * 64,
                          review={"checks": {"visual": True}}, artifacts={"output_dir": "/test/materials"})
        return job_id

    def attempt(self, job_id):
        return self.store.prepare_attempt(job_id, "test-account", "policy", "test-evidence", 10, now_iso()[:10])

    def test_repeat_discovery_deduplicates_stable_article(self):
        first = self.discover()
        second = self.discover()
        self.assertEqual(len(first["added"]), 1)
        self.assertEqual(second["added"], [])
        self.assertEqual(second["changed"], [])
        self.assertEqual(len(self.store.jobs()), 1)

    def test_human_retry_retains_unknown_parent_and_requires_bound_proof(self):
        parent_job = self.ready_job()
        parent = self.attempt(parent_job)
        self.store.recover_intents()
        self.assertEqual(self.store.manual_retry_parents(), set())
        child_job = self.ready_job('explicit-human-retry')
        # Simulate the explicitly authorized atomic transfer used by the
        # one-off adapter; ordinary prepare_attempt still rejects the lock.
        with self.assertRaises(StateError):
            self.attempt(child_job)
        self.store.db.execute('DELETE FROM account_locks WHERE account_id=?', ('test-account',))
        child = self.attempt(child_job)
        proof = Path(self.directory.name) / 'authorization.json'
        proof.write_text('{"human_authorized":true,"extra_submissions":1}', encoding='utf-8')
        self.store.event('human_authorized_retry_intent', child_job,
                         parent_attempt_id=parent['attempt_id'], child_attempt_id=child['attempt_id'],
                         human_authorized=True, max_extra_submissions=1, content_hash='a' * 64,
                         account_id='test-account', authorization_ref=str(proof),
                         authorization_sha256=hashlib.sha256(proof.read_bytes()).hexdigest())
        self.assertEqual(self.store.manual_retry_parents(), {parent['attempt_id']})
        self.assertEqual(self.store.get_attempt(parent['attempt_id'])['status'], 'SUBMIT_UNKNOWN')
        self.assertEqual(self.store.get_job(parent_job)['draft']['body'], '独立创作')
        proof.write_text('changed authorization', encoding='utf-8')
        self.assertEqual(self.store.manual_retry_parents(), set())

    def test_partial_feed_summary_cannot_erase_acquired_full_text_or_draft(self):
        job_id = self.ready_job()
        before = self.store.get_job(job_id)
        self.discover()
        after = self.store.get_job(job_id)
        self.assertEqual(after["article"], before["article"])
        self.assertEqual(after["draft"], before["draft"])
        self.assertEqual(after["review"], before["review"])
        self.assertEqual(after["status"], "READY")

    def test_feed_summary_change_cannot_activate_a_historical_baseline(self):
        job_id = self.discover(baseline=True)["added"][0]
        self.discover({**self.article, "content": "订阅服务补充了更长摘要"})
        self.assertEqual(self.store.get_job(job_id)["status"], "BASELINE")

    def test_frozen_content_cannot_change_after_submission_intent(self):
        job_id = self.ready_job()
        self.attempt(job_id)
        with self.assertRaises(StateError):
            self.store.update(job_id, draft={"title": "另一版本"})
        self.assertEqual(self.store.get_job(job_id)["draft"]["title"], "测试文案")

    def test_discovery_revision_after_intent_preserves_frozen_content(self):
        job_id = self.ready_job()
        self.attempt(job_id)
        before = self.store.get_job(job_id)
        self.discover({**self.article, "content": "发布后来源变化"})
        after = self.store.get_job(job_id)
        self.assertEqual(after["article"], before["article"])
        self.assertEqual(after["draft"], before["draft"])
        self.assertEqual(after["status"], "SUBMIT_INTENT")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='source_changed_after_intent' AND job_id=?", (job_id,)).fetchone()[0], 1)

    def test_changed_feed_summary_requests_recheck_without_erasing_full_evidence(self):
        job_id = self.ready_job()
        before = self.store.get_job(job_id)
        result = self.discover({**self.article, "content": "已更新的订阅摘要"})
        after = self.store.get_job(job_id)
        self.assertEqual(result["changed"], [job_id])
        self.assertEqual(after["status"], "SOURCE_RECHECK")
        for field in ("article", "draft", "review", "artifacts", "source_hash", "content_hash"):
            self.assertEqual(after[field], before[field])

    def test_title_or_modified_changes_trigger_recheck_even_when_feed_body_unchanged(self):
        for change in ({"title": "来源更正了标题"}, {"modified_at": "2026-10-04T12:00:00+08:00"}):
            with self.subTest(change=change):
                key = "case-title" if "title" in change else "case-modified"
                job_id = self.ready_job(key)
                article = {**self.article, "article_id": key, "url": "https://mp.weixin.qq.com/s/" + key, **change}
                self.discover(article)
                self.assertEqual(self.store.get_job(job_id)["status"], "SOURCE_RECHECK")

    def test_fetch_timestamps_do_not_trigger_rechecks(self):
        job_id = self.ready_job()
        result = self.discover({**self.article, "fetched_at": "2026-10-04T13:00:00+08:00", "retrieval_method": "changed_transport"})
        self.assertEqual(result["changed"], [])
        self.assertEqual(self.store.get_job(job_id)["status"], "READY")

    def test_feed_change_does_not_reactivate_canonical_duplicate(self):
        job_id = self.discover()["added"][0]
        self.store.update(job_id, status="DUPLICATE", reason="Canonical article already tracked")
        changed = self.discover({**self.article, "content": "更新的摘要"})
        self.assertEqual(changed["changed"], [])
        self.assertEqual(self.store.get_job(job_id)["status"], "DUPLICATE")

    def test_first_snapshot_for_legacy_job_does_not_infer_source_revision(self):
        job_id = self.ready_job()
        self.store.db.execute("DELETE FROM feed_snapshots")
        first = self.discover({**self.article, "title": "首次建立订阅基线", "content": "与旧正文不同的摘要"})
        self.assertEqual(first["changed"], [])
        self.assertEqual(self.store.get_job(job_id)["status"], "READY")
        second = self.discover({**self.article, "title": "首次建立订阅基线", "content": "随后确实更新的摘要"})
        self.assertEqual(second["changed"], [job_id])

    def test_repeated_changed_feed_does_not_duplicate_revision_events(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        self.store.reconcile(attempt["attempt_id"], {"status": "published", "confirmed": True}, {"trusted": "record"})
        before = self.store.get_job(job_id)
        changed_feed = {**self.article, "content": "来源已更新"}
        first = self.discover(changed_feed)
        second = self.discover(changed_feed)
        self.assertEqual(first["changed"], [job_id])
        self.assertEqual(second["changed"], [])
        self.assertEqual(self.store.get_job(job_id)["article"], before["article"])
        self.assertEqual(self.store.get_job(job_id)["status"], "PUBLISHED")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='source_changed_after_intent' AND job_id=?", (job_id,)).fetchone()[0], 1)

    def test_explicit_import_activates_baseline_without_changing_poll_cursor(self):
        job_id = self.discover(baseline=True)["added"][0]
        before_source = self.store.source_state(self.source["id"])
        imported, added = self.store.import_article(self.source["id"], copy.deepcopy(self.article))
        self.assertEqual(imported, job_id)
        self.assertFalse(added)
        self.assertEqual(self.store.get_job(job_id)["status"], "DISCOVERED")
        self.assertEqual(self.store.source_state(self.source["id"]), before_source)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='baseline_activated_by_import' AND job_id=?", (job_id,)).fetchone()[0], 1)

    def test_explicit_import_canonical_alias_activates_same_baseline_row(self):
        job_id = self.discover(baseline=True)["added"][0]
        article = self.store.get_job(job_id)["article"]
        self.store.update(job_id, article={**article, "canonical_article_id": "canonical-id"})
        imported, added = self.store.import_article(self.source["id"], {**self.article, "article_id": "canonical-id"})
        self.assertEqual(imported, job_id)
        self.assertFalse(added)
        self.assertEqual(self.store.get_job(job_id)["status"], "DISCOVERED")
        self.assertEqual(len(self.store.jobs()), 1)

    def test_import_of_existing_nonbaseline_is_idempotent(self):
        job_id = self.ready_job()
        before = self.store.get_job(job_id)
        imported, added = self.store.import_article(self.source["id"], copy.deepcopy(self.article))
        self.assertEqual(imported, job_id)
        self.assertFalse(added)
        self.assertEqual(self.store.get_job(job_id), before)

    def test_interrupted_submission_blocks_resubmission_and_account(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        self.assertEqual(self.store.recover_intents(), 1)
        self.assertEqual(self.store.get_attempt(attempt["attempt_id"])["status"], "SUBMIT_UNKNOWN")
        with self.assertRaises(StateError):
            self.attempt(job_id)
        second_job = self.ready_job("second-article")
        with self.assertRaises(StateError):
            self.attempt(second_job)

    def test_unknown_reconciliation_does_not_release_account_lock(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        self.store.reconcile(attempt["attempt_id"], {"status": "submit_unknown", "confirmed": False}, {"account_id": "wrong-account"})
        self.assertEqual(len(self.store.status()["account_locks"]), 1)

    def test_confirmed_pending_review_is_recorded_without_becoming_published(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        result = self.store.reconcile(attempt["attempt_id"], {"status": "pending_review", "confirmed": True}, {})
        self.assertEqual(result["status"], "PENDING_REVIEW")
        self.assertEqual(self.store.get_job(job_id)["status"], "PENDING_REVIEW")
        self.assertEqual(len(self.store.status()["account_locks"]), 0)

    def test_unconfirmed_followup_preserves_pending_review_and_its_evidence(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        trusted = {"evidence_ref": "/captures/trusted-pending.png", "state": "pending_review"}
        self.store.reconcile(attempt["attempt_id"], {"status": "pending_review", "confirmed": True}, trusted)
        failure = {"status": "submit_unknown", "confirmed": False, "error_code": "wrong_account", "reason": "Wrong account in follow-up"}
        failed_observation = {"account_id": "wrong-account"}
        saved = self.store.reconcile(attempt["attempt_id"], failure, failed_observation)
        self.assertEqual(saved["status"], "PENDING_REVIEW")
        self.assertEqual(saved["observation"], trusted)
        self.assertEqual(self.store.get_job(job_id)["status"], "PENDING_REVIEW")
        event = self.store.db.execute("SELECT details FROM events WHERE kind='publication_recheck_failed' AND job_id=?", (job_id,)).fetchone()
        details = json.loads(event[0])
        self.assertEqual(details["last_trusted_status"], "PENDING_REVIEW")
        self.assertEqual(details["observation"], failed_observation)

    def test_unconfirmed_followup_preserves_schedule_then_real_publication_can_confirm(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        trusted = {"evidence_ref": "/captures/trusted-scheduled.png", "state": "scheduled"}
        self.store.reconcile(attempt["attempt_id"], {"status": "scheduled", "confirmed": True}, trusted)
        saved = self.store.reconcile(attempt["attempt_id"], {"status": "submit_unknown", "confirmed": False}, {"available": False})
        self.assertEqual(saved["status"], "SCHEDULED")
        self.assertEqual(saved["observation"], trusted)
        later = self.store.reconcile(attempt["attempt_id"], {"status": "published", "confirmed": True}, {"state": "published"})
        self.assertEqual(later["status"], "PUBLISHED")

    def test_pause_blocks_new_submission_intent(self):
        job_id = self.ready_job()
        self.store.set_paused(True)
        with self.assertRaises(StateError):
            self.attempt(job_id)
        self.assertEqual(self.store.status()["attempts"], [])

    def test_paused_state_still_allows_readonly_reconciliation(self):
        job_id = self.ready_job()
        attempt = self.attempt(job_id)
        self.store.set_paused(True)
        result = self.store.reconcile(attempt["attempt_id"], {"status": "published", "confirmed": True}, {})
        self.assertEqual(result["status"], "PUBLISHED")

    def test_backup_is_readable_and_releases_its_windows_file_handle(self):
        job_id = self.ready_job()
        backup = Path(self.directory.name) / "backup.sqlite"
        self.store.backup(backup)
        moved = backup.with_name("moved-backup.sqlite")
        backup.rename(moved)
        connection = sqlite3.connect(str(moved))
        try:
            self.assertEqual(connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "READY")
        finally:
            connection.close()
        moved.unlink()

    def test_status_reports_run_owner_without_exposing_lease_token(self):
        lease = self.store.begin_run("test-execution-owner")
        reported = self.store.status()["run_leases"]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["owner"], "test-execution-owner")
        self.assertEqual(set(reported[0]), {"name", "owner", "expires_at"})
        self.assertNotIn(lease["lease_token"], json.dumps(self.store.status()))


if __name__ == "__main__":
    unittest.main()
