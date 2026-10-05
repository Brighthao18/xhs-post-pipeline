"""Offline protocol fixtures; never evidence of Image calls, OCR or upload."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from pipeline.v2.config import BUNDLED_SKILL_DIR, digest, write_json
from pipeline.v2.runtime import Runtime
from pipeline.v2.quality import BASE_CHECKS, generator_module, verify_artifacts
from pipeline.v2.store import StateError

SKILL = BUNDLED_SKILL_DIR


class ImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-images-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config = {"schema_version": 2, "workspace": str(self.root), "skill_dir": str(SKILL),
                       "sources": [{"id": "fixture", "type": "fixture", "enabled": False, "domain": "general"}],
                       "account": {"nickname": "示例作者", "account_id": "offline", "user_id": "offline"},
                       "policy": {"allow_publish": False, "max_posts_per_day": 1},
                       "image_policy": {"default_mode": "imagegen_native", "require_five_images": True, "max_repairs_per_image": 2},
                       "editorial": {"excluded_source_names": ["禁止公开来源"]}}
        write_json(self.config_path, self.config)
        self.runtime = Runtime(self.config_path)
        self.addCleanup(self.runtime.store.close)
        self.lease = self.runtime.store.begin_run(self.id())
        self.runtime.lease_token = self.lease["lease_token"]
        self.addCleanup(self.runtime.store.end_run, self.lease["lease_token"])
        article = {"article_id": "isolated", "url": "https://example.com/offline-fixture", "content": "离线夹具正文", "coverage": "full", "is_fixture": True}
        self.job_id, _ = self.runtime.store.import_article("fixture", article)
        self.runtime._save_source(self.job_id, article)
        self.raw = {"title": "给睡前留一点安静", "body": "这是一段用于测试字段和版本绑定的离线夹具，不代表真实内容。",
                    "quotes": ["给睡前留一点安静空间，不把睡眠变成打卡任务", "规律作息慢慢调整，不用一晚上完成所有改变", "放松不用追求标准动作，先照顾身体真实感受", "持续失眠影响生活时，寻求专业评估与合适治疗"],
                    "tags": ["离线测试"], "sources": [{"url": article["url"], "coverage": "full"}],
                    "visual": {"version": 1, "mode": "imagegen_native", "style_id": "quiet_living",
                               "cover": {"headline": "睡前松一点", "subheadline": "给夜晚留一点安静"},
                               "cards": [{"quote_index": i, "purpose": "离线职责" + str(i), "scene": "卧室和书", "text_region": "上方留白"} for i in range(4)]}}
        self.job = self.runtime.draft(self.job_id, self.raw)["job"]
        self.precheck()

    def precheck(self):
        job = self.runtime.store.get_job(self.job_id)
        checks = {name: True for name in BASE_CHECKS}
        checks["visual"] = False
        self.runtime.review(self.job_id, {"job_id": self.job_id, "content_hash": job["content_hash"], "source_hash": job["source_hash"],
                                          "checks": checks, "passed": False, "issues": [], "evidence_urls": []})

    def plan(self):
        return self.runtime.plan_images(self.job_id, {"references": []})["images"]

    def returned(self, row, repair=None):
        payload = {"image_job_id": row["id"]}
        if repair:
            payload["repair_prompt"] = repair
        intent = self.runtime.image_intent(self.job_id, payload)["intent"]
        path = self.root / (intent["intent_id"] + ".png")
        Image.new("RGB", (768, 1024), (100 + row["image_order"] * 20, 140, 190)).save(path)
        data = {"image_job_id": row["id"], "intent_id": intent["intent_id"], "tool": "image_gen.imagegen", "original_path": str(path),
                "returned_metadata": {"is_fixture": True, "label": "OFFLINE_PROTOCOL_FIXTURE_NO_IMAGE_CALL"}}
        return self.runtime.image_result(self.job_id, data)["result"], data

    def inspection(self, row, result, passed=True):
        api = self.runtime._image_api()
        return {"image_job_id": row["id"], "intent_id": result["intent_id"], "creative_hash": result["creative_hash"],
                "result_hash": result["normalized_sha256"], "original_hash": result["original_sha256"],
                "thumbnail_hash": result["thumbnail_sha256"], "passed": passed,
                "checks": {key: passed for key in api.INSPECTION_CHECKS}, "issues": [] if passed else ["离线模拟文字错误"],
                "observed_text": api.expected_text(self.runtime.store.get_job(self.job_id)["draft"], row["role"]),
                "original_observation": "OFFLINE fixture inspection schema only", "mobile_observation": "OFFLINE fixture schema only"}

    def passed(self, row):
        result, data = self.returned(row)
        self.runtime.image_inspect(self.job_id, self.inspection(row, result))
        return result, data

    def bundle(self):
        for row in self.plan():
            self.passed(row)
        return self.runtime.render(self.job_id)

    def ready(self):
        rendered = self.bundle()
        job = rendered["job"]
        self.runtime.review(self.job_id, {"job_id": self.job_id, "content_hash": job["content_hash"], "source_hash": job["source_hash"],
                                          "manifest_hash": rendered["manifest_hash"], "passed": True, "issues": [], "evidence_urls": [],
                                          "checks": {**{k: True for k in BASE_CHECKS}, "visual": True}})
        return rendered

    def test_strict_visual_quote_indices_and_mode(self):
        generator = generator_module(SKILL)
        for change in ("duplicate", "boolean", "mode", "unknown"):
            raw = copy.deepcopy(self.raw)
            if change == "duplicate": raw["visual"]["cards"][1]["quote_index"] = 0
            if change == "boolean": raw["visual"]["cards"][0]["quote_index"] = False
            if change == "mode": raw["visual"]["mode"] = "template_fallback"
            if change == "unknown": raw["visual"]["secret_flag"] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                generator.normalize_content(raw)

    def test_default_five_policy_and_cover_public_copy(self):
        raw = copy.deepcopy(self.raw); raw.pop("visual")
        with self.assertRaises(StateError): self.runtime.draft(self.job_id, raw)
        raw = copy.deepcopy(self.raw); raw["visual"]["cover"]["label"] = "禁止公开来源"
        with self.assertRaises(StateError): self.runtime.draft(self.job_id, raw)

    def test_plan_recovery_and_call_intent_are_idempotent(self):
        row = self.plan()[0]
        self.assertEqual(self.plan()[0]["id"], row["id"])
        intent = self.runtime.image_intent(self.job_id, {"image_job_id": row["id"]})
        recovered = self.runtime.image_intent(self.job_id, {"image_job_id": row["id"]})
        self.assertEqual(recovered["intent"]["intent_id"], intent["intent"]["intent_id"])
        self.assertEqual(recovered["action"], "recover_image")
        self.assertEqual(self.runtime.image_status(self.job_id)["images"][0]["attempt_count"], 1)

    def test_resume_keeps_passed_cover_and_requests_only_next_card(self):
        rows = self.plan(); result, _ = self.passed(rows[0])
        self.assertEqual(self.runtime.next()["work"][0]["image_job"]["role"], "card_0")
        other = Runtime(self.config_path, lease_token=self.lease["lease_token"])
        try:
            self.assertEqual(other.plan_images(self.job_id, {"references": []})["images"][0]["result"]["original_sha256"], result["original_sha256"])
            self.assertEqual(other.image_intent(self.job_id, {"image_job_id": rows[0]["id"]})["action"], "reuse_image")
        finally: other.store.close()

    def test_uninspected_cover_blocks_card_and_packaging(self):
        rows = self.plan(); self.returned(rows[0])
        with self.assertRaises(StateError): self.runtime.image_intent(self.job_id, {"image_job_id": rows[1]["id"]})
        with self.assertRaises(StateError): self.runtime.render(self.job_id)

    def test_result_requires_current_intent_and_native_receipt(self):
        row = self.plan()[0]
        result, data = self.returned(row)
        self.assertTrue(self.runtime.image_result(self.job_id, data)["reused"])
        for field, value in (("intent_id", "wrong"), ("tool", "lovart"), ("returned_metadata", {})):
            bad = dict(data, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): self.runtime.image_result(self.job_id, bad)

    def test_inspection_requires_original_mobile_hashes_and_actual_text(self):
        row = self.plan()[0]; result, _ = self.returned(row)
        for name, value in (("thumbnail_hash", "old"), ("observed_text", ["wrong"]), ("mobile_observation", "")):
            data = self.inspection(row, result); data[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError): self.runtime.image_inspect(self.job_id, data)

    def test_tool_unavailable_retains_work_without_ready_or_fallback(self):
        rows = self.plan(); cover, _ = self.passed(rows[0])
        self.runtime.image_intent(self.job_id, {"image_job_id": rows[1]["id"]})
        status = self.runtime.image_block(self.job_id, {"image_job_id": rows[1]["id"], "reason": "OFFLINE simulated unavailable native tool"})
        self.assertEqual(status["next_image"]["action"], "wait_image_tool")
        self.assertEqual(status["images"][0]["result"]["original_sha256"], cover["original_sha256"])
        with self.assertRaises(StateError): self.runtime.render(self.job_id)

    def test_repair_limit_separate_from_text_revision(self):
        row = self.plan()[0]; result, _ = self.returned(row)
        self.runtime.image_inspect(self.job_id, self.inspection(row, result, False))
        for _ in range(2):
            result, _ = self.returned(self.runtime.image_status(self.job_id)["images"][0], "仅修正上方错字，正确文字来自原金句，其他区域保持。")
            self.runtime.image_inspect(self.job_id, self.inspection(row, result, False))
        self.assertEqual(self.runtime.image_status(self.job_id)["images"][0]["state"], "FAILED")
        with self.assertRaises(StateError): self.runtime.image_intent(self.job_id, {"image_job_id": row["id"], "repair_prompt": "again"})
        self.assertEqual(self.runtime.store.get_job(self.job_id)["revision"], 1)

    def test_five_ordered_images_reach_backend_payload_and_hash(self):
        rendered = self.ready(); job = self.runtime.store.get_job(self.job_id)
        self.assertEqual([r["role"] for r in rendered["publish_images"]], ["cover", "card_0", "card_1", "card_2", "card_3"])
        payload = self.runtime._backend_payload(job)
        self.assertEqual(payload["images"], [r["path"] for r in rendered["publish_images"]])
        self.assertEqual(payload["image_hashes"], [r["sha256"] for r in rendered["publish_images"]])
        self.assertEqual(job["content_hash"], digest({"content": job["draft"], "ordered_images": payload["image_hashes"]}))

    def test_missing_cover_or_changed_image_blocks_old_final_review(self):
        rendered = self.ready(); job = self.runtime.store.get_job(self.job_id)
        path = Path(rendered["publish_images"][0]["path"]); original = path.read_bytes()
        path.unlink()
        with self.assertRaises((ValueError, OSError)): self.runtime._backend_payload(job)
        path.write_bytes(original + b"changed")
        with self.assertRaises(ValueError): self.runtime._backend_payload(job)

    def test_wrong_order_or_missing_cover_manifest_is_rejected(self):
        rendered = self.bundle(); root = Path(rendered["job"]["artifacts"]["output_dir"])
        import json
        meta = json.loads((root / "_meta.json").read_text(encoding="utf-8"))
        meta["publish_images"][0], meta["publish_images"][1] = meta["publish_images"][1], meta["publish_images"][0]
        write_json(root / "_meta.json", meta)
        with self.assertRaises(ValueError): verify_artifacts(rendered["job"]["artifacts"], rendered["job"]["draft"])

    def test_cover_copy_or_visual_scene_change_invalidates_old_images(self):
        self.ready(); old_rows = self.runtime.image_status(self.job_id)["images"]
        raw = copy.deepcopy(self.raw); raw["visual"]["cards"][0]["scene"] = "新场景和新主体位置"
        self.runtime.draft(self.job_id, raw)
        self.assertIsNone(self.runtime.store.get_job(self.job_id)["review"])
        with self.assertRaises(StateError): self.runtime.image_intent(self.job_id, {"image_job_id": old_rows[0]["id"]})
        self.precheck(); new_rows = self.plan()
        self.assertNotEqual(old_rows[0]["id"], new_rows[0]["id"])
        self.assertTrue(all(row["state"] == "PLANNED" for row in new_rows))

    def test_new_result_invalidates_bundle_final_review_and_editor_session(self):
        self.ready(); rows = self.runtime.image_status(self.job_id)["images"]
        row = rows[1]; result = row["result"]
        self.runtime._backend_tables()
        self.runtime.store.db.execute("INSERT INTO backend_sessions VALUES(?,?,?,?,?,?)", (self.job_id, "old", "offline", "old", "{}", "{}"))
        self.runtime.image_inspect(self.job_id, self.inspection(row, result, False))
        job = self.runtime.store.get_job(self.job_id)
        self.assertIsNone(job["artifacts"]); self.assertIsNone(job["review"])
        self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)

    def test_lease_and_publication_intent_freeze_images(self):
        row = self.plan()[0]
        other = Runtime(self.config_path)
        try:
            with self.assertRaises(StateError): other.image_intent(self.job_id, {"image_job_id": row["id"]})
        finally: other.store.close()
        self.runtime.store.db.execute("INSERT INTO attempts(attempt_id,job_id,account_id,title,content_hash,policy_hash,status,intent_at,account_evidence) VALUES('offline',?,'offline','offline','offline','offline','SUBMIT_UNKNOWN','2026-10-04','offline')", (self.job_id,))
        with self.assertRaises(StateError): self.runtime.image_intent(self.job_id, {"image_job_id": row["id"]})

    def test_changed_reference_requires_replan(self):
        path = self.root / "reference.png"; Image.new("RGB", (120, 120), "white").save(path)
        refs = {"references": [{"path": str(path), "role": "historical_style", "purpose": "offline fixture", "viewed": True}]}
        old = self.runtime.plan_images(self.job_id, refs)["images"][0]
        Image.new("RGB", (120, 120), "blue").save(path)
        self.assertEqual(self.runtime.next()["work"][0]["action"], "plan_images")
        with self.assertRaises(StateError): self.runtime.image_intent(self.job_id, {"image_job_id": old["id"]})
        self.assertNotEqual(self.runtime.plan_images(self.job_id, refs)["images"][0]["id"], old["id"])


if __name__ == "__main__":
    unittest.main()
