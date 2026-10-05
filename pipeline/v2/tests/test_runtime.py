"""Runtime integration checks use isolated databases and real local rendering.

Network responses are fixtures; no user state, account session or publication is
touched. These checks do not prove source coverage or live platform access.
"""

from datetime import datetime, timedelta
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pipeline.v2.config import BUNDLED_SKILL_DIR, TZ, now_iso, write_json
from pipeline.v2.quality import BASE_CHECKS
from pipeline.v2.runtime import Runtime
from pipeline.v2.store import StateError


SKILL_DIR = BUNDLED_SKILL_DIR
TEXT = "这是完整的文章正文，解释一个日常知识点，并说明适用条件和信息来源。"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "独立工作区"
        self.workspace.mkdir()
        self.feed_path = self.workspace / "feed.json"
        write_json(self.feed_path, {"articles": []})
        self.config_path = self.workspace / "config.json"
        self.config = {
            "schema_version": 2,
            "workspace": str(self.workspace),
            "timezone": "Asia/Singapore",
            "engine": "codex",
            "skill_dir": str(SKILL_DIR),
            "author": "示例作者",
            "account": {"nickname": "示例作者", "account_id": "unit-account"},
            "sources": [{
                "id": "unit-source", "type": "fixture", "enabled": True,
                "path": str(self.feed_path), "domain": "general", "new_only": True,
            }],
            "policy": {
                "allow_publish": False,
                "max_posts_per_day": 1,
                "max_revision_rounds": 2,
                "max_acquire_per_run": 3,
                "publish_hours": [0, 23],
            },
        }
        write_json(self.config_path, self.config)
        self.runtime = Runtime(self.config_path)
        self.addCleanup(self.runtime.store.close)
        self.lease = self.runtime.store.begin_run(self.id())
        self.runtime.lease_token = self.lease["lease_token"]
        self.addCleanup(self.runtime.store.end_run, self.runtime.lease_token)

    def import_job(self, suffix="one"):
        return self.runtime.import_url("unit-source", "https://example.com/" + suffix)["job"]

    def ingest(self, job, text=TEXT, title="文章标题"):
        evidence = self.workspace / (job["id"] + ".html")
        evidence.write_text(
            '<html><head><title>' + title + '</title></head><body>'
            '<h1 id="activity-name">' + title + '</h1>'
            '<span id="js_name">测试来源</span><div id="js_content">'
            '<p>' + text * 12 + '</p><p>正文到这里完整结束。</p>'
            '</div></body></html>',
            encoding="utf-8",
        )
        return self.runtime.ingest_source(job["id"], {
            "url": job["article"]["url"],
            "evidence_path": str(evidence),
            "retrieval_method": "unit_fixture",
            "fetched_at": now_iso(),
        })

    def content(self, job, **overrides):
        content = {
            "title": "读懂日常习惯",
            "candidate_titles": ["读懂日常习惯", "从一个问题开始", "把知识讲清楚"],
            "body": ("从一个常见问题开始，先核对来源，再理解适用条件。\n\n"
                     "把事实、解释和建议分开，记录真实依据，再用自己的结构表达。" * 6),
            "tags": ["知识分享", "日常学习", "阅读笔记", "信息整理", "学习方法", "独立思考"],
            "quotes": [
                "先核对来源与条件，再理解一句话的真实含义。",
                "把事实和建议分开，让知识更容易被检查。",
                "卡片只表达一个重点，也保留必要的限定。",
                "用自己的结构组织表达，始终保留真实依据。",
            ],
            "style": "morandi",
            "author": "示例作者",
            "sources": [{
                "url": job["article"]["url"],
                "title": job["article"].get("title", "文章标题"),
                "author": "测试来源", "coverage": "full",
                "retrieval_method": "unit_fixture", "fetched_at": now_iso(),
            }],
            "editorial_notes": ["仅用于隔离的运行检查，未验证真实网络来源。"],
            "claim_checks": [],
        }
        content.update(overrides)
        return content

    def draft(self, suffix="one"):
        job = self.ingest(self.import_job(suffix))
        return self.runtime.draft(job["id"], self.content(job))["job"]

    def review_payload(self, job, visual=False, **overrides):
        checks = {key: True for key in BASE_CHECKS}
        checks["visual"] = visual
        payload = {
            "job_id": job["id"], "source_hash": job["source_hash"],
            "content_hash": job["content_hash"], "passed": visual,
            "checks": checks, "issues": [], "evidence_urls": [],
        }
        payload.update(overrides)
        return payload

    def render(self, job):
        self.runtime.review(job["id"], self.review_payload(job))
        result = self.runtime.render(job["id"])
        return result["job"], result

    def feed_job(self, suffix):
        self.runtime.config["sources"][0]["new_only"] = False
        item = {
            "id": "feed-" + suffix, "url": "https://example.com/" + suffix,
            "title": "订阅中的文章标题", "content": "订阅摘要初版",
        }
        write_json(self.feed_path, {"articles": [item]})
        result = self.runtime.poll()
        job_id = result["sources"][0]["added"][0]
        return self.ingest(self.runtime.store.get_job(job_id)), item

    def signal_feed_change(self, job, item):
        write_json(self.feed_path, {"articles": [dict(item, content="订阅摘要出现新版本")]})
        result = self.runtime.poll()
        self.assertEqual(result["sources"][0]["changed"], [job["id"]])
        pending = self.runtime.store.get_job(job["id"])
        self.assertEqual(pending["status"], "SOURCE_RECHECK")
        action = next(entry["action"] for entry in self.runtime.next()["work"] if entry.get("job", {}).get("id") == job["id"])
        self.assertEqual(action, "acquire")
        return pending

    def test_duplicate_import_and_restart_keep_one_job_and_progress(self):
        first = self.import_job()
        duplicate = self.import_job()
        self.assertEqual(first["id"], duplicate["id"])
        full = self.ingest(first)
        second_runtime = Runtime(self.config_path)
        try:
            self.assertEqual(len(second_runtime.store.jobs()), 1)
            reopened = second_runtime.store.get_job(first["id"])
            self.assertEqual(reopened["status"], "FETCHED")
            self.assertEqual(reopened["source_hash"], full["source_hash"])
            state = second_runtime.store.source_state("unit-source")
            self.assertIsNone(state.get("last_success"))
            self.assertNotEqual(state.get("health"), "healthy")
        finally:
            second_runtime.store.close()

    def test_source_outside_allowlist_is_rejected_without_new_job(self):
        with self.assertRaises(StateError):
            self.runtime.import_url("different-source", "https://example.com/article")
        self.assertEqual(self.runtime.store.jobs(), [])

    def test_partial_source_cannot_advance_to_draft(self):
        job = self.import_job()
        with self.assertRaises(StateError):
            self.runtime.draft(job["id"], self.content(job))
        self.assertIsNone(self.runtime.store.get_job(job["id"])["draft"])

    def test_ingest_requires_real_file_matching_job_url(self):
        job = self.import_job()
        data = {"url": "https://example.com/different", "evidence_path": str(self.workspace / "no.html")}
        with self.assertRaises(StateError):
            self.runtime.ingest_source(job["id"], data)
        data["url"] = job["article"]["url"]
        with self.assertRaises(ValueError):
            self.runtime.ingest_source(job["id"], data)
        self.assertEqual(self.runtime.store.get_job(job["id"])["status"], "DISCOVERED")

    def test_pasted_full_coverage_cannot_override_incomplete_html(self):
        job = self.import_job()
        evidence = self.workspace / "incomplete.html"
        evidence.write_text("<html><body>请完成下方验证</body></html>", encoding="utf-8")
        result = self.runtime.ingest_source(job["id"], {
            "url": job["article"]["url"], "evidence_path": str(evidence),
            "coverage": "full", "content": TEXT * 30,
            "retrieval_method": "unit_fixture", "fetched_at": now_iso(),
        })
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["article"]["coverage"], "partial")
        with self.assertRaises(StateError):
            self.runtime.draft(job["id"], self.content(result))

    def test_unchanged_partial_source_recheck_still_requires_source_review(self):
        job = self.import_job()
        evidence = self.workspace / "incomplete.html"
        evidence.write_text("<html><body>请完成下方验证</body></html>", encoding="utf-8")
        data = {"url": job["article"]["url"], "evidence_path": str(evidence),
                "retrieval_method": "unit_fixture", "fetched_at": now_iso()}
        partial = self.runtime.ingest_source(job["id"], data)
        self.assertEqual((partial["status"], partial["reason"]), ("REVIEW_REQUIRED", "Source incomplete"))
        for pending in ("SOURCE_RECHECK", "FETCH_RETRY"):
            with self.subTest(pending=pending):
                self.runtime.store.update(job["id"], status=pending, reason="Recheck requested", retry_at=now_iso())
                rechecked = self.runtime.ingest_source(job["id"], data)
                self.assertEqual(rechecked["source_hash"], partial["source_hash"])
                self.assertEqual((rechecked["status"], rechecked["reason"], rechecked["retry_at"]),
                                 ("REVIEW_REQUIRED", "Source incomplete", None))
                action = next(entry["action"] for entry in self.runtime.next()["work"] if entry.get("job", {}).get("id") == job["id"])
                self.assertEqual(action, "source_review")

    def test_baseline_poll_does_not_publish_history_but_new_item_is_work(self):
        item = {"id": "history", "url": "https://example.com/history", "title": "已有文章"}
        write_json(self.feed_path, {"articles": [item]})
        first = self.runtime.poll()
        self.assertTrue(first["sources"][0]["baseline"])
        self.assertEqual(self.runtime.store.jobs()[0]["status"], "BASELINE")
        self.assertEqual(self.runtime.next()["work"], [])
        write_json(self.feed_path, {"articles": [item, {"id": "new", "url": "https://example.com/new", "title": "新文章"}]})
        second = self.runtime.poll()
        self.assertFalse(second["sources"][0]["baseline"])
        self.assertEqual(len(second["sources"][0]["added"]), 1)
        self.assertEqual(len(self.runtime.poll()["sources"][0]["added"]), 0)
        self.assertEqual(self.runtime.next()["work"][0]["action"], "acquire")

    def test_undecodable_feed_is_recorded_without_stopping_other_sources(self):
        self.runtime.config["sources"].insert(0, {"id": "legacy-feed", "type": "rss", "enabled": True,
                                                   "url": "https://example.com/feed.xml", "new_only": True})
        feed = b'<?xml version="1.0" encoding="unknown-charset"?><rss version="2.0"><channel><title>Feed</title></channel></rss>'
        with patch("pipeline.v2.sources._fetch", return_value=(feed, "application/rss+xml", "https://example.com/feed.xml")):
            result = self.runtime.poll()
        self.assertEqual([(entry["source_id"], entry["ok"]) for entry in result["sources"]],
                         [("legacy-feed", False), ("unit-source", True)])
        self.assertEqual(result["sources"][0]["error_code"], "decode_failed")
        self.assertEqual(self.runtime.store.source_state("legacy-feed")["health"], "error")

    def test_wrong_article_draft_fails_without_consuming_revision(self):
        job = self.ingest(self.import_job())
        content = self.content(job)
        content["sources"][0]["url"] = "https://example.com/not-this-job"
        with self.assertRaises(StateError):
            self.runtime.draft(job["id"], content)
        unchanged = self.runtime.store.get_job(job["id"])
        self.assertIsNone(unchanged["draft"])
        self.assertEqual(unchanged["revision"], 0)

    def test_stale_review_or_non_boolean_check_cannot_advance(self):
        job = self.draft()
        stale = self.review_payload(job, content_hash="old-content-version")
        with self.assertRaises(ValueError):
            self.runtime.review(job["id"], stale)
        invalid = self.review_payload(job)
        invalid["checks"]["facts"] = "true"
        with self.assertRaises(ValueError):
            self.runtime.review(job["id"], invalid)
        self.assertIsNone(self.runtime.store.get_job(job["id"])["review"])
        with self.assertRaises(StateError):
            self.runtime.render(job["id"])

    def test_semantic_review_real_render_and_manifest_bound_ready(self):
        job = self.draft()
        precheck = self.runtime.review(job["id"], self.review_payload(job))
        self.assertEqual(precheck["status"], "DRAFTED")
        self.assertEqual(self.runtime.next()["work"][0]["action"], "render")
        rendered = self.runtime.render(job["id"])
        self.assertEqual(rendered["job"]["status"], "MATERIALS_GENERATED")
        self.assertEqual(len(rendered["cards"]), 4)
        self.assertEqual(self.runtime.next()["work"][0]["action"], "visual_review")
        final = self.review_payload(rendered["job"], visual=True, manifest_hash=rendered["manifest_hash"])
        ready = self.runtime.review(job["id"], final)
        self.assertEqual(ready["status"], "READY")
        self.assertEqual(self.runtime.next()["work"][0]["action"], "publish")
        self.assertEqual(len(self.runtime.store.status()["attempts"]), 0)

    def test_final_visual_review_rejects_stale_manifest(self):
        job, _ = self.render(self.draft())
        data = self.review_payload(job, visual=True, manifest_hash="unrelated-images")
        with self.assertRaises(StateError):
            self.runtime.review(job["id"], data)
        self.assertNotEqual(self.runtime.store.get_job(job["id"])["status"], "READY")

    def test_explicit_failed_final_review_does_not_make_job_ready(self):
        job, rendered = self.render(self.draft())
        data = self.review_payload(job, visual=True, passed=False, manifest_hash=rendered["manifest_hash"])
        try:
            result = self.runtime.review(job["id"], data)
        except (ValueError, StateError):
            result = self.runtime.store.get_job(job["id"])
        self.assertNotEqual(result["status"], "READY", "passed=false is an explicit failed review")

    def test_source_revision_invalidates_draft_review_and_old_hash(self):
        job, rendered = self.render(self.draft())
        old_review = self.review_payload(job, visual=True, manifest_hash=rendered["manifest_hash"])
        self.runtime.review(job["id"], old_review)
        updated = self.ingest(job, text=TEXT + "原文已经修订，旧信息须重新核对。")
        self.assertNotEqual(updated["source_hash"], job["source_hash"])
        self.assertEqual(updated["status"], "FETCHED")
        for field in ("draft", "content_hash", "review", "artifacts"):
            self.assertIsNone(updated[field])
        redrafted = self.runtime.draft(job["id"], self.content(updated))["job"]
        with self.assertRaises(ValueError):
            self.runtime.review(redrafted["id"], old_review)

    def test_unchanged_source_does_not_erase_existing_draft(self):
        job = self.draft()
        self.runtime.review(job["id"], self.review_payload(job))
        refreshed = self.ingest(job)
        self.assertEqual(refreshed["content_hash"], job["content_hash"])
        self.assertIsNotNone(refreshed["review"])
        self.assertEqual(refreshed["revision"], 1)

    def test_source_recheck_unchanged_restores_all_stages_and_retry(self):
        for expected in ("FETCHED", "DRAFTED", "MATERIALS_GENERATED", "READY"):
            with self.subTest(restored_stage=expected):
                job, item = self.feed_job(expected.lower())
                if expected != "FETCHED":
                    job = self.runtime.draft(job["id"], self.content(job))["job"]
                if expected in ("MATERIALS_GENERATED", "READY"):
                    job, rendered = self.render(job)
                    if expected == "READY":
                        job = self.runtime.review(job["id"], self.review_payload(job, visual=True, manifest_hash=rendered["manifest_hash"]))
                before = copy.deepcopy(job)
                pending = self.signal_feed_change(job, item)
                for key in ("source_hash", "content_hash", "draft", "review", "artifacts", "revision"):
                    self.assertEqual(pending[key], before[key], key)

                with patch("pipeline.v2.sources.acquire", return_value=copy.deepcopy(before["article"])) as acquire:
                    result = self.runtime.run_once()
                acquire.assert_called_once()
                self.assertIn({"job_id": job["id"], "status": expected}, result["acquired"])
                restored = self.runtime.store.get_job(job["id"])
                for key in ("source_hash", "content_hash", "draft", "review", "artifacts", "revision"):
                    self.assertEqual(restored[key], before[key], key)
                self.assertIsNone(restored["reason"])
                self.assertIsNone(restored["retry_at"])

                self.runtime.store.update(job["id"], status="FETCH_RETRY", reason="Temporary source failure", retry_at=now_iso())
                with patch("pipeline.v2.sources.acquire", return_value=copy.deepcopy(before["article"])):
                    retry_result = self.runtime.run_once()
                self.assertIn({"job_id": job["id"], "status": expected}, retry_result["acquired"])
                retried = self.runtime.store.get_job(job["id"])
                self.assertEqual(retried["status"], expected)
                for key in ("source_hash", "content_hash", "draft", "review", "artifacts", "revision"):
                    self.assertEqual(retried[key], before[key], key)

    def test_source_recheck_changed_body_invalidates_ready_bundle(self):
        job, item = self.feed_job("changed-body")
        job = self.runtime.draft(job["id"], self.content(job))["job"]
        job, rendered = self.render(job)
        job = self.runtime.review(job["id"], self.review_payload(job, visual=True, manifest_hash=rendered["manifest_hash"]))
        pending = self.signal_feed_change(job, item)
        self.assertEqual(pending["content_hash"], job["content_hash"])
        actual_revision = dict(job["article"], content=job["article"]["content"] + "实际正文新增了一条需要核对的解释。")
        with patch("pipeline.v2.sources.acquire", return_value=actual_revision) as acquire:
            result = self.runtime.run_once()
        acquire.assert_called_once()
        self.assertEqual(result["acquired"], [{"job_id": job["id"], "status": "FETCHED"}])
        updated = self.runtime.store.get_job(job["id"])
        self.assertNotEqual(updated["source_hash"], job["source_hash"])
        for key in ("draft", "content_hash", "review", "artifacts"):
            self.assertIsNone(updated[key], key)
        self.assertEqual(next(entry["action"] for entry in self.runtime.next()["work"] if entry.get("job", {}).get("id") == job["id"]), "draft")

    def test_unavailable_submission_backend_is_technical_block_without_intent(self):
        self.runtime.config["policy"].update(allow_publish=True, submit_backend_ready=False)
        self.runtime.config["publication_backend"] = {"reason": "Unit submission backend cannot click the platform submit control"}
        diagnosis = self.runtime.doctor()
        self.assertTrue(diagnosis["publish_enabled"], "Technical readiness does not revoke user authorization")
        self.assertIn({"reason": self.runtime.config["publication_backend"]["reason"]}, diagnosis["blockers"])
        job, rendered = self.render(self.draft())
        job = self.runtime.review(job["id"], self.review_payload(job, visual=True, manifest_hash=rendered["manifest_hash"]))
        before = copy.deepcopy(job)
        with self.assertRaisesRegex(StateError, "submission backend is unavailable"):
            self.runtime.prepare_publish(job["id"], "unit-account", str(self.workspace / "account-evidence-not-needed-for-block.json"))
        self.assertEqual(self.runtime.store.status()["attempts"], [])
        self.assertEqual(self.runtime.store.status()["account_locks"], [])
        self.assertEqual(self.runtime.store.get_job(job["id"]), before)
        self.assertTrue(self.runtime.config["policy"]["allow_publish"])

    def test_revision_limit_is_finite_and_preserves_last_good_draft(self):
        job = self.draft()
        for revision in range(2):
            job = self.runtime.draft(job["id"], self.content(job, body="修改版本" + str(revision) + TEXT * 12))["job"]
        self.assertEqual(job["revision"], 3)
        with self.assertRaises(StateError):
            self.runtime.draft(job["id"], self.content(job, body="超出修订次数" + TEXT * 12))
        self.assertEqual(self.runtime.store.get_job(job["id"])["content_hash"], job["content_hash"])

    def test_health_precheck_requires_evidence_and_domain_review(self):
        self.runtime.config["sources"][0]["domain"] = "health"
        job = self.draft()
        data = self.review_payload(job)
        data["checks"]["domain_safety"] = True
        with self.assertRaises(ValueError):
            self.runtime.review(job["id"], data)
        data["evidence_urls"] = ["https://example.com/unit-evidence"]
        self.runtime.review(job["id"], data)
        self.assertEqual(self.runtime.next()["work"][0]["action"], "render")

    def test_health_failed_domain_review_routes_to_revision_not_render(self):
        self.runtime.config["sources"][0]["domain"] = "health"
        job = self.draft()
        data = self.review_payload(job)
        data["checks"]["domain_safety"] = False
        self.runtime.review(job["id"], data)
        self.assertEqual(self.runtime.next()["work"][0]["action"], "revise")
        with self.assertRaises(StateError):
            self.runtime.render(job["id"])

    def test_pause_blocks_queued_acquisition_on_run_once(self):
        job = self.import_job()
        self.runtime.store.set_paused(True)
        with patch("pipeline.v2.sources.acquire") as acquire:
            result = self.runtime.run_once()
        acquire.assert_not_called()
        self.assertTrue(result["paused"])
        self.assertEqual(result["acquired"], [])
        self.assertEqual(self.runtime.store.get_job(job["id"])["status"], "DISCOVERED")

    def test_other_run_cannot_claim_lease_or_mutate_queue(self):
        other = Runtime(self.config_path)
        try:
            with self.assertRaises(StateError):
                other.store.begin_run("concurrent-run")
            with self.assertRaises(StateError):
                other.import_url("unit-source", "https://example.com/concurrent")
            self.assertEqual(self.runtime.store.jobs(), [])
        finally:
            other.store.close()

    def test_expired_lease_blocks_new_work_and_preserves_draft(self):
        job = self.draft()
        past = (datetime.now(TZ) - timedelta(minutes=1)).isoformat(timespec="seconds")
        self.runtime.store.db.execute("UPDATE run_leases SET expires_at=?", (past,))
        try:
            with self.assertRaises(StateError):
                self.runtime.draft(job["id"], self.content(job, body="不应被写入的新版" + TEXT * 12))
            self.assertEqual(self.runtime.store.get_job(job["id"])["content_hash"], job["content_hash"])
        finally:
            self.runtime.store.db.execute("UPDATE run_leases SET expires_at=?", (self.lease["expires_at"],))

    def test_future_retries_do_not_starve_later_due_jobs(self):
        for index in range(4):
            self.import_job(str(index))
        ordered = self.runtime.store.jobs()
        future = (datetime.now(TZ) + timedelta(hours=1)).isoformat(timespec="seconds")
        for index, job in enumerate(ordered):
            self.runtime.store.db.execute("UPDATE jobs SET created_at=? WHERE id=?", ("2020-01-01T00:00:0" + str(index) + "+08:00", job["id"]))
            if index < 3:
                self.runtime.store.update(job["id"], status="FETCH_RETRY", retry_at=future)
        due_id = ordered[-1]["id"]
        response = {
            "url": ordered[-1]["article"]["url"], "title": "到期文章", "coverage": "full", "content": TEXT * 12,
        }
        with patch.object(self.runtime, "poll", return_value={"status": "polled", "sources": []}), patch("pipeline.v2.sources.acquire", return_value=response) as acquire:
            result = self.runtime.run_once()
        acquire.assert_called_once()
        self.assertEqual(result["acquired"], [{"job_id": due_id, "status": "FETCHED"}])

    def test_backup_reads_persistent_jobs_and_refuses_overwrite(self):
        job = self.ingest(self.import_job())
        self.runtime.store.set_paused(True)
        backup_path = self.workspace / "backup" / "state.sqlite"
        self.runtime.store.backup(backup_path)
        db = sqlite3.connect(backup_path)
        try:
            self.assertEqual(db.execute("SELECT source_hash FROM jobs WHERE id=?", (job["id"],)).fetchone()[0], job["source_hash"])
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0], "true")
        finally:
            db.close()
        with self.assertRaises(StateError):
            self.runtime.store.backup(backup_path)


if __name__ == "__main__":
    unittest.main()
