"""Isolated backend bridge checks; transport and page evidence are fixtures.

Actual image/hash/manifest verification is retained without repeated renderer
runs. Passing these checks is not proof of live platform publication.
"""

import copy
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from pipeline.v2.backend import BackendError
from pipeline.v2.config import BUNDLED_SKILL_DIR, TZ, digest, now_iso, read_json, write_json
from pipeline.v2.quality import BASE_CHECKS, verify_artifacts
from pipeline.v2.runtime import Runtime
from pipeline.v2.store import StateError


class BackendWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        buffer = io.BytesIO()
        Image.new("RGB", (1080, 1440), (240, 230, 220)).save(buffer, "JPEG")
        cls.card_bytes = buffer.getvalue()
        capture = io.BytesIO()
        Image.new("RGB", (1200, 900), (245, 245, 245)).save(capture, "PNG")
        cls.evidence_bytes = capture.getvalue()
        cls.upload_capture_bytes = []
        for color in ((220, 90, 70), (90, 170, 120), (70, 120, 210), (210, 175, 70)):
            preview = io.BytesIO()
            Image.new("RGB", (360, 480), color).save(preview, "PNG")
            cls.upload_capture_bytes.append(preview.getvalue())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-backend-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "独立工作区"
        self.workspace.mkdir()
        self.backend_root = self.workspace / "private-backend"
        self.backend_root.mkdir()
        self.token_path = self.backend_root / "auth-token.json"
        write_json(self.token_path, {"auth_token": "offline-test-only-token"})
        self.evidence_dir = self.backend_root / "session" / "evidence"
        self.evidence_dir.mkdir(parents=True)
        self.config_path = self.workspace / "config.json"
        self.config = {
            "schema_version": 2, "workspace": str(self.workspace), "timezone": "Asia/Singapore",
            "engine": "codex", "author": "示例作者",
            "skill_dir": str(BUNDLED_SKILL_DIR),
            "account": {"nickname": "示例作者", "account_id": "unit-red", "user_id": "unit-user"},
            "sources": [{"id": "unit-source", "type": "rss", "enabled": True, "domain": "general", "url": "https://example.com/feed"}],
            "policy": {"allow_publish": True, "submit_backend_ready": True, "max_posts_per_day": 2, "max_revision_rounds": 2, "publish_hours": [0, 23]},
            "publication_backend": {"type": "xiaohongshu_mcp", "base_url": "http://127.0.0.1:18060", "auth_token_path": str(self.token_path)},
            "editorial": {"excluded_source_names": ["示例来源"]},
        }
        write_json(self.config_path, self.config)
        self.runtime = Runtime(self.config_path)
        self.addCleanup(self.runtime.store.close)
        self.lease = self.runtime.store.begin_run(self.id())
        self.runtime.lease_token = self.lease["lease_token"]
        self.addCleanup(self.runtime.store.end_run, self.runtime.lease_token)

        self.account_evidence = self.evidence_dir / "identity.png"
        self.account_evidence.write_bytes(self.evidence_bytes)
        self.editor_evidence = self.evidence_dir / "editor.png"
        self.editor_evidence.write_bytes(self.evidence_bytes)
        self.record_ref = self.evidence_dir / "editor-record.json"
        self.management_capture = self.evidence_dir / "management.png"
        self.management_capture.write_bytes(self.evidence_bytes)
        self.management_record = self.evidence_dir / "management-record.json"
        self.management_url = "https://creator.xiaohongshu.com/new/note-manager"
        self.image_evidence_paths = []
        for index, captured in enumerate(self.upload_capture_bytes):
            image_evidence = self.evidence_dir / ("upload-preview-" + str(index) + ".png")
            image_evidence.write_bytes(captured)
            self.image_evidence_paths.append(image_evidence)
        self.identity = {
            "authenticated": True, "user_id": "unit-user", "red_id": "unit-red",
            "nickname": "示例作者", "observed_at": now_iso(), "evidence_ref": str(self.account_evidence),
        }
        self.client = MagicMock()
        self.client.identity.side_effect = lambda: {"success": True, "data": copy.deepcopy(self.identity)}
        self.client.preflight.side_effect = self.preflight_reply
        self.client.submit.return_value = {"success": True, "data": {
            "phase": "SUBMIT_UNKNOWN", "click_attempted": True, "confirmed": False, "published": False, "retry_allowed": False,
        }}
        self.client.observations.return_value = {"success": True, "data": {
            "phase": "SUBMIT_UNKNOWN", "observations": [], "candidates": [], "unverified": True,
            "requires_management_evidence": True, "confirmed": False, "published": False,
        }}
        self.client.management_evidence.side_effect = self.management_reply
        self.backend_patch = patch.object(self.runtime, "_backend", return_value=self.client)
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)

    def ready_job(self, suffix="one", status="READY", tags=None):
        job = self.runtime.import_url("unit-source", "https://example.com/" + suffix)["job"]
        draft = {
            "title": "读懂日常知识", "candidate_titles": ["读懂日常知识"],
            "body": "先核对来源与适用条件，再独立组织成容易理解的小红书笔记。",
            "tags": list(tags) if tags is not None else ["知识分享"], "quotes": ["先核对条件再理解", "每张只讲一个重点", "保留必要限定", "用自己的结构表达"],
            "author": "示例作者", "style": "morandi", "claim_checks": [],
            "sources": [{"url": job["article"]["url"], "title": "内部来源", "coverage": "full"}],
        }
        output = self.workspace / "output" / job["id"]
        output.mkdir(parents=True)
        write_json(output / "content.json", draft)
        records = []
        for index in range(4):
            path = output / ("card_" + str(index) + ".jpg")
            path.write_bytes(self.card_bytes)
            records.append({"path": str(path), "sha256": hashlib.sha256(self.card_bytes).hexdigest()})
        content_path = output / "content.json"
        records.append({"path": str(content_path), "sha256": hashlib.sha256(content_path.read_bytes()).hexdigest()})
        write_json(output / "_meta.json", {"status": "materials_generated", "files": records})
        bundle = {"output_dir": str(output)}
        facts = verify_artifacts(bundle, draft)
        source_hash = digest({"unit": suffix, "body": "actual fixture source body"})
        content_hash = digest({"content": draft, "ordered_images": [record["sha256"] for record in records[:4]]})
        review = {
            "job_id": job["id"], "source_hash": source_hash, "content_hash": content_hash,
            "manifest_hash": facts["manifest_hash"], "passed": True,
            "checks": {**{key: True for key in BASE_CHECKS}, "visual": True}, "issues": [], "evidence_urls": [],
        }
        article = dict(job["article"], coverage="full", content="actual fixture source body", is_fixture=False)
        self.runtime.store.update(job["id"], article=article, draft=draft, source_hash=source_hash,
                                  content_hash=content_hash, review=review, artifacts=bundle, status=status)
        return self.runtime.store.get_job(job["id"])

    def preflight_reply(self, payload):
        session_id = "editor-" + payload["attempt_id"]
        editor_content = payload["content"] + "\n" + " ".join("#" + tag.lstrip("#") for tag in payload["tags"])
        captures = [str(path) for path in self.image_evidence_paths]
        capture_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.image_evidence_paths]
        write_json(self.record_ref, {
            "content_hash": payload["content_hash"], "editor_session_id": session_id,
            "image_hashes": copy.deepcopy(payload["image_hashes"]), "expected_paths": copy.deepcopy(payload["images"]),
            "image_evidence_refs": captures.copy(), "image_evidence_sha256": capture_hashes.copy(),
            "account": copy.deepcopy(self.identity),
            "editor": {"title": payload["title"], "content": editor_content,
                       "preview_srcs": ["blob:https://creator.xiaohongshu.com/fixture-preview-" + str(index) for index in range(len(payload["images"]))],
                       "ai_declared": True},
        })
        return {"success": True, "data": {
            "phase": "PREPARED", "prepared": True, "content_hash": payload["content_hash"],
            "editor_session_id": session_id,
            "expires_at": (datetime.now(TZ) + timedelta(minutes=15)).isoformat(timespec="seconds"),
            "account": copy.deepcopy(self.identity), "title": payload["title"], "content": editor_content, "tags": copy.deepcopy(payload["tags"]),
            "image_count": len(payload["images"]), "ai_declared": True,
            "image_match_verified": False, "images_order_verified": False, "upload_order_verified": True,
            "observed_at": now_iso(), "evidence_ref": str(self.editor_evidence), "record_ref": str(self.record_ref),
            "evidence_sha256": hashlib.sha256(self.editor_evidence.read_bytes()).hexdigest(),
            "image_evidence_refs": captures, "image_evidence_sha256": capture_hashes,
        }}

    def review_data(self, job, prepared):
        result = prepared["preflight"]
        return {
            "job_id": job["id"], "content_hash": job["content_hash"],
            "editor_session_id": result["editor_session_id"], "evidence_hash": prepared["evidence_hash"],
            "evidence_ref": result["evidence_ref"], "passed": True, "issues": [],
            "checks": {key: True for key in ("account", "title", "body", "images", "image_order", "ai_declaration")},
        }

    def management_reply(self, view="all"):
        label = {"all": "", "published": "已发布", "pending_review": "审核中", "rejected": "未通过"}[view]
        write_json(self.management_record, {
            "url": self.management_url, "account": copy.deepcopy(self.identity), "view": view, "filter_label": label,
            "visible_text": "笔记管理\n全部笔记\n实际页面文本，尚未验证任何发布结果。",
            "visible_links": [{"text": "笔记管理", "url": self.management_url}],
        })
        return {"success": True, "data": {
            "phase": "READ_ONLY", "unverified": True, "confirmed": False, "published": False, "view": view, "filter_label": label,
            "account": copy.deepcopy(self.identity), "observed_at": now_iso(), "url": self.management_url,
            "evidence_ref": str(self.management_capture), "record_ref": str(self.management_record),
            "evidence_sha256": hashlib.sha256(self.management_capture.read_bytes()).hexdigest(),
        }}

    def prepared_job(self, suffix="one", status="READY"):
        job = self.ready_job(suffix, status)
        prepared = self.runtime.backend_preflight(job["id"])
        review = self.review_data(job, prepared)
        self.runtime.backend_review(job["id"], review)
        return job, prepared, review

    def update_session_response(self, job_id, **fields):
        row = self.runtime.store.db.execute("SELECT response FROM backend_sessions WHERE job_id=?", (job_id,)).fetchone()
        response = json.loads(row["response"])
        response.update(fields)
        self.runtime.store.db.execute("UPDATE backend_sessions SET response=? WHERE job_id=?", (json.dumps(response), job_id))

    def assert_no_dispatch(self):
        self.client.submit.assert_not_called()
        self.assertEqual(self.runtime.store.status()["attempts"], [])
        self.assertEqual(self.runtime.store.status()["account_locks"], [])

    def test_identity_binding_saves_only_matching_actual_account(self):
        self.runtime.config["account"].pop("user_id")
        data = read_json(self.config_path)
        data["account"].pop("user_id")
        write_json(self.config_path, data)
        bound = self.runtime.backend_identity(bind=True)
        self.assertTrue(bound["bound"])
        self.assertEqual(read_json(self.config_path)["account"]["user_id"], "unit-user")
        self.assertEqual(self.runtime.config["account"]["backend_binding_evidence"], str(self.account_evidence))
        self.identity["red_id"] = "wrong-red"
        before = self.config_path.read_bytes()
        with self.assertRaises(StateError):
            self.runtime.backend_identity(bind=True)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assert_no_dispatch()

    def test_stale_future_unauthenticated_or_wrong_identity_is_rejected(self):
        job = self.ready_job()
        original = copy.deepcopy(self.identity)
        variants = [
            {"user_id": "wrong-user"}, {"red_id": "wrong-red"}, {"nickname": "wrong-name"}, {"authenticated": False},
            {"observed_at": (datetime.now(TZ) - timedelta(minutes=16)).isoformat()},
            {"observed_at": (datetime.now(TZ) + timedelta(minutes=1)).isoformat()},
            {"evidence_ref": str(self.workspace / "missing.png")},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.identity = dict(original, **variant)
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
        self.client.preflight.assert_not_called()
        self.assert_no_dispatch()

    def test_preflight_persists_session_then_visual_review_binds_exact_evidence(self):
        job = self.ready_job()
        prepared = self.runtime.backend_preflight(job["id"])
        session = self.runtime._prepared_session(job["id"])
        self.assertEqual(session["content_hash"], job["content_hash"])
        self.assertEqual(session["evidence_hash"], hashlib.sha256(self.editor_evidence.read_bytes()).hexdigest())
        self.assertEqual(session["response"]["local_record_hash"], hashlib.sha256(self.record_ref.read_bytes()).hexdigest())
        self.assertEqual(session["response"]["image_evidence_refs"], [str(path) for path in self.image_evidence_paths])
        self.assertEqual(session["response"]["image_evidence_sha256"], [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.image_evidence_paths])
        self.assertIsNone(session["review"])
        self.assertEqual(self.runtime.next()["work"][0]["action"], "backend_visual_review")
        self.runtime.backend_review(job["id"], self.review_data(job, prepared))
        self.assertIsNotNone(self.runtime._prepared_session(job["id"])["review"])
        self.assertEqual(self.runtime.next()["work"][0]["action"], "backend_submit")
        self.assert_no_dispatch()

    def test_preflight_accepts_same_editor_text_with_layout_and_zero_width_characters(self):
        job = self.ready_job(tags=["知识分享", "日常学习"])

        def layout_only(payload):
            reply = self.preflight_reply(payload)
            reply["data"]["title"] = "  " + payload["title"] + "\n"
            reply["data"]["content"] = "\u200b" + reply["data"]["content"].replace("先", "先 \u200d") + "\n\ufeff"
            record = read_json(self.record_ref)
            record["editor"]["title"] = "\t" + payload["title"] + " "
            record["editor"]["content"] = "\u200c" + record["editor"]["content"].replace("条件", "条\u200b件") + "\n"
            write_json(self.record_ref, record)
            return reply

        self.client.preflight.side_effect = layout_only
        prepared = self.runtime.backend_preflight(job["id"])
        self.assertFalse(prepared["preflight"]["image_match_verified"])
        self.assertFalse(prepared["preflight"]["images_order_verified"])
        self.assertIsNone(self.runtime._prepared_session(job["id"])["review"])
        self.assertEqual(prepared["preflight"]["local_record_hash"], hashlib.sha256(self.record_ref.read_bytes()).hexdigest())
        self.assert_no_dispatch()

    def test_preflight_missing_or_contradictory_machine_fields_never_create_session(self):
        job = self.ready_job(tags=["知识分享", "日常学习"])
        required = ("prepared", "title", "content", "tags", "image_count", "ai_declared",
                    "upload_order_verified", "evidence_sha256", "content_hash", "editor_session_id",
                    "phase", "expires_at", "evidence_ref", "record_ref", "account")
        variants = [(field, None, True) for field in required]
        variants += [(field, value, False) for field, value in (
            ("prepared", False), ("prepared", 1), ("title", "另一个标题"), ("title", None),
            ("title", {"text": job["draft"]["title"]}), ("content", job["draft"]["body"]),
            ("content", "矛盾的正文#知识分享#日常学习"), ("content", []),
            ("tags", ["日常学习", "知识分享"]), ("tags", []),
            ("image_count", 3), ("image_count", 5), ("image_count", 4.0), ("image_count", True),
            ("ai_declared", False), ("ai_declared", 1), ("upload_order_verified", False),
            ("upload_order_verified", 1), ("evidence_sha256", "0" * 64),
            ("expires_at", "no timestamp"), ("expires_at", None), ("account", []),
        )]
        self.runtime._backend_tables()
        for field, value, missing in variants:
            with self.subTest(field=field, value=value, missing=missing):
                self.runtime.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))
                def reply(payload):
                    response = self.preflight_reply(payload)
                    if missing:
                        response["data"].pop(field)
                    else:
                        response["data"][field] = value
                    return response

                self.client.preflight.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
                self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.assert_no_dispatch()

    def test_preflight_page_record_missing_or_contradictory_content_is_rejected(self):
        job = self.ready_job(tags=["知识分享", "日常学习"])
        variants = [(field, None, True) for field in ("content_hash", "editor_session_id", "image_hashes", "expected_paths", "account", "editor")]
        variants += [(field, value, False) for field, value in (
            ("content_hash", "0" * 64), ("editor_session_id", "wrong-editor"),
            ("image_hashes", ["a" * 64] * 4), ("expected_paths", []),
            ("account", dict(self.identity, user_id="wrong-user")),
            ("account", dict(self.identity, red_id="wrong-red")),
            ("account", dict(self.identity, authenticated=False)), ("account", []), ("editor", []),
        )]
        editor_variants = [(field, None, True) for field in ("title", "content", "preview_srcs", "ai_declared")]
        editor_variants += [(field, value, False) for field, value in (
            ("title", "矛盾标题"), ("title", None), ("content", job["draft"]["body"]),
            ("content", "另一正文#知识分享#日常学习"), ("content", []),
            ("preview_srcs", []), ("preview_srcs", ["blob:one"] * 3),
            ("preview_srcs", ["blob:one", "blob:two", "", "blob:four"]),
            ("preview_srcs", ["blob:one", "blob:two", None, "blob:four"]),
            ("preview_srcs", ["blob:one", "blob:two", "none", "blob:four"]),
            ("preview_srcs", ["blob:one", "blob:two", "   ", "blob:four"]),
            ("ai_declared", False), ("ai_declared", 1),
        )]
        self.runtime._backend_tables()
        for location, changes in (("record", variants), ("editor", editor_variants)):
            for field, value, missing in changes:
                with self.subTest(location=location, field=field, value=value, missing=missing):
                    self.runtime.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))
                    def reply(payload):
                        response = self.preflight_reply(payload)
                        record = read_json(self.record_ref)
                        target = record if location == "record" else record["editor"]
                        if missing:
                            target.pop(field)
                        else:
                            target[field] = value
                        write_json(self.record_ref, record)
                        return response

                    self.client.preflight.side_effect = reply
                    with self.assertRaises(StateError):
                        self.runtime.backend_preflight(job["id"])
                    self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.assert_no_dispatch()

    def test_preflight_record_must_be_parseable_json_object(self):
        job = self.ready_job()
        self.runtime._backend_tables()
        for invalid in ("{malformed JSON", "[]", '"unverified page text"', "null"):
            with self.subTest(record=invalid):
                self.runtime.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))

                def reply(payload):
                    response = self.preflight_reply(payload)
                    self.record_ref.write_text(invalid, encoding="utf-8")
                    return response

                self.client.preflight.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
                self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.assert_no_dispatch()

    def test_preflight_record_cannot_swap_ordered_expected_paths(self):
        job = self.ready_job()

        def reordered(payload):
            response = self.preflight_reply(payload)
            record = read_json(self.record_ref)
            record["expected_paths"][0], record["expected_paths"][1] = record["expected_paths"][1], record["expected_paths"][0]
            write_json(self.record_ref, record)
            return response

        self.client.preflight.side_effect = reordered
        with self.assertRaises(StateError):
            self.runtime.backend_preflight(job["id"])
        self.assert_no_dispatch()

    def test_preflight_requires_all_actual_captures_inside_private_directory(self):
        job = self.ready_job()
        outside_png = self.workspace / "editor.png"
        outside_png.write_bytes(self.evidence_bytes)
        outside_json = self.workspace / "editor.json"
        write_json(outside_json, {"fixture": True})
        sibling = self.evidence_dir.parent / "evidence-other"
        sibling.mkdir()
        sibling_png = sibling / "editor.png"
        sibling_png.write_bytes(self.evidence_bytes)
        wrong_png = self.evidence_dir / "editor.jpg"
        wrong_png.write_bytes(self.evidence_bytes)
        wrong_json = self.evidence_dir / "editor.txt"
        wrong_json.write_text("{}", encoding="utf-8")
        variants = (
            ("evidence_ref", str(outside_png)), ("record_ref", str(outside_json)),
            ("evidence_ref", str(sibling_png)), ("record_ref", str(self.token_path)),
            ("evidence_ref", str(self.evidence_dir / ".." / ".." / ".." / "editor.png")),
            ("evidence_ref", "session/evidence/editor.png"), ("record_ref", "session/evidence/editor-record.json"),
            ("evidence_ref", str(wrong_png)), ("record_ref", str(wrong_json)),
            ("evidence_ref", str(self.evidence_dir / "missing.png")),
            ("record_ref", str(self.evidence_dir / "missing.json")),
        )
        self.runtime._backend_tables()
        for field, value in variants:
            with self.subTest(field=field, value=value):
                def reply(payload):
                    response = self.preflight_reply(payload)
                    response["data"][field] = value
                    return response

                self.client.preflight.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
                self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.identity["evidence_ref"] = str(outside_png)
        with self.assertRaises(StateError):
            self.runtime.backend_identity()
        self.assert_no_dispatch()

    def test_page_record_change_invalidates_visual_review_and_submission(self):
        job = self.ready_job()
        prepared = self.runtime.backend_preflight(job["id"])
        original = self.record_ref.read_bytes()
        self.record_ref.write_bytes(original + b"\n")
        with self.assertRaises(StateError):
            self.runtime.backend_review(job["id"], self.review_data(job, prepared))
        self.record_ref.write_bytes(original)
        self.runtime.backend_review(job["id"], self.review_data(job, prepared))
        record = read_json(self.record_ref)
        record["editor"]["content"] = "JSON被改动后不再是本次审核的正文"
        write_json(self.record_ref, record)
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()

    def test_preflight_requires_complete_image_capture_arrays_and_actual_hashes(self):
        job = self.ready_job()
        paths = [str(path) for path in self.image_evidence_paths]
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.image_evidence_paths]
        variants = []
        for location in ("data", "record", "both"):
            for field in ("image_evidence_refs", "image_evidence_sha256"):
                variants.append((location, field, None, True))
        variants += [("both", field, value, False) for field, value in (
            ("image_evidence_refs", []), ("image_evidence_refs", paths[:3]),
            ("image_evidence_refs", paths + [str(self.account_evidence)]),
            ("image_evidence_refs", paths[0]), ("image_evidence_refs", [paths[0]] * 4),
            ("image_evidence_refs", [paths[0], paths[1], None, paths[3]]),
            ("image_evidence_sha256", []), ("image_evidence_sha256", hashes[:3]),
            ("image_evidence_sha256", hashes + [hashes[0]]),
            ("image_evidence_sha256", "0" * 64), ("image_evidence_sha256", ["0" * 64] * 4),
            ("image_evidence_sha256", [hashes[0], hashes[1], True, hashes[3]]),
        )]
        self.runtime._backend_tables()
        for location, field, value, missing in variants:
            with self.subTest(location=location, field=field, value=value, missing=missing):
                self.runtime.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))

                def reply(payload):
                    response = self.preflight_reply(payload)
                    record = read_json(self.record_ref)
                    targets = [response["data"], record] if location == "both" else [response["data"] if location == "data" else record]
                    for target in targets:
                        if missing:
                            target.pop(field)
                        else:
                            target[field] = copy.deepcopy(value)
                    write_json(self.record_ref, record)
                    return response

                self.client.preflight.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
                self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.assert_no_dispatch()

    def test_preflight_rejects_image_capture_path_outside_private_directory(self):
        job = self.ready_job()
        outside = self.workspace / "outside-upload.png"
        outside.write_bytes(self.upload_capture_bytes[0])
        disguised = self.evidence_dir / "upload-preview.jpg"
        disguised.write_bytes(self.upload_capture_bytes[0])
        variants = (str(outside), str(self.evidence_dir / ".." / ".." / ".." / "outside-upload.png"),
                    str(disguised), str(self.evidence_dir / "missing.png"), "session/evidence/upload-preview-0.png")
        self.runtime._backend_tables()
        for reference in variants:
            with self.subTest(reference=reference):
                self.runtime.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))

                def reply(payload):
                    response = self.preflight_reply(payload)
                    record = read_json(self.record_ref)
                    response["data"]["image_evidence_refs"][0] = reference
                    record["image_evidence_refs"][0] = reference
                    write_json(self.record_ref, record)
                    return response

                self.client.preflight.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
                self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.assert_no_dispatch()

    def test_preflight_record_cannot_reorder_supplementary_image_evidence(self):
        job = self.ready_job()
        self.runtime._backend_tables()
        for fields in (("image_evidence_refs",), ("image_evidence_sha256",), ("image_evidence_refs", "image_evidence_sha256")):
            with self.subTest(fields=fields):
                self.runtime.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))

                def reply(payload):
                    response = self.preflight_reply(payload)
                    record = read_json(self.record_ref)
                    for field in fields:
                        record[field][0], record[field][1] = record[field][1], record[field][0]
                    write_json(self.record_ref, record)
                    return response

                self.client.preflight.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
                self.assertEqual(self.runtime.store.db.execute("SELECT COUNT(*) FROM backend_sessions").fetchone()[0], 0)
        self.assert_no_dispatch()

    def test_each_image_capture_change_invalidates_editor_review_and_submission(self):
        job = self.ready_job()
        prepared = self.runtime.backend_preflight(job["id"])
        for index, image in enumerate(self.image_evidence_paths):
            with self.subTest(stage="review", index=index):
                original = image.read_bytes()
                try:
                    image.write_bytes(self.upload_capture_bytes[(index + 1) % 4])
                    with self.assertRaises(StateError):
                        self.runtime.backend_review(job["id"], self.review_data(job, prepared))
                finally:
                    image.write_bytes(original)
        self.runtime.backend_review(job["id"], self.review_data(job, prepared))
        for index, image in enumerate(self.image_evidence_paths):
            with self.subTest(stage="submit", index=index):
                original = image.read_bytes()
                try:
                    image.write_bytes(self.upload_capture_bytes[(index + 1) % 4])
                    with self.assertRaises(StateError):
                        self.runtime.backend_submit(job["id"])
                finally:
                    image.write_bytes(original)
        self.assert_no_dispatch()

    def test_submit_safe_diagnostic_is_persisted_without_releasing_unknown_lock(self):
        job, _, _ = self.prepared_job()
        self.client.submit.side_effect = BackendError(
            "backend_http_error", "private-response-message-must-not-be-saved", phase="NOT_SUBMITTED", http_status=409,
            backend_code="PREFLIGHT_FAILED", evidence_ref=str(self.editor_evidence), record_ref=str(self.record_ref),
        )
        result = self.runtime.backend_submit(job["id"])
        expected = {"backend_code": "PREFLIGHT_FAILED", "evidence_ref": str(self.editor_evidence), "record_ref": str(self.record_ref)}
        for field, value in expected.items():
            self.assertEqual(result["dispatch"][field], value)
        marker = self.runtime.store.db.execute("SELECT result FROM backend_calls WHERE attempt_id=?", (result["attempt"]["attempt_id"],)).fetchone()
        persisted = json.loads(marker["result"])
        for field, value in expected.items():
            self.assertEqual(persisted[field], value)
        self.assertNotIn("private-response-message-must-not-be-saved", marker["result"])
        self.assertEqual(result["attempt"]["status"], "SUBMIT_UNKNOWN")
        self.assertFalse(result["published"])
        self.assertEqual(len(self.runtime.store.status()["account_locks"]), 1)
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assertEqual(self.client.submit.call_count, 1)

    def test_preflight_and_review_reject_wrong_bundle_session_hash_or_account(self):
        job = self.ready_job()
        for field, value in (("content_hash", "0" * 64), ("phase", "SUBMITTED"), ("editor_session_id", "")):
            with self.subTest(field=field):
                self.client.preflight.side_effect = lambda payload, field=field, value=value: {"success": True, "data": dict(self.preflight_reply(payload)["data"], **{field: value})}
                with self.assertRaises(StateError):
                    self.runtime.backend_preflight(job["id"])
        self.client.preflight.side_effect = self.preflight_reply
        prepared = self.runtime.backend_preflight(job["id"])
        original = self.review_data(job, prepared)
        for field, value in (("content_hash", "0" * 64), ("editor_session_id", "different-session"), ("evidence_hash", "wrong-screenshot"), ("evidence_ref", str(self.account_evidence)), ("passed", False)):
            with self.subTest(field=field), self.assertRaises(StateError):
                self.runtime.backend_review(job["id"], dict(original, **{field: value}))
        for check in original["checks"]:
            with self.subTest(check=check), self.assertRaises(StateError):
                self.runtime.backend_review(job["id"], dict(original, checks=dict(original["checks"], **{check: False})))
        self.assertIsNone(self.runtime._prepared_session(job["id"])["review"])
        self.assert_no_dispatch()

    def test_expired_changed_screenshot_changed_content_or_policy_blocks_before_intent(self):
        job, _, _ = self.prepared_job()
        response = self.runtime._prepared_session(job["id"])["response"]
        self.update_session_response(job["id"], expires_at=(datetime.now(TZ) - timedelta(seconds=1)).isoformat())
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.update_session_response(job["id"], expires_at=response["expires_at"])
        self.editor_evidence.write_bytes(self.evidence_bytes + b"altered-evidence")
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.editor_evidence.write_bytes(self.evidence_bytes)
        self.runtime.store.update(job["id"], content_hash="f" * 64)
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.runtime.store.update(job["id"], content_hash=job["content_hash"])
        self.runtime.config["policy_hash"] = "different-policy"
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()

    def test_sample_can_prepare_but_cannot_submit(self):
        job, _, _ = self.prepared_job(status="SAMPLE_READY")
        with self.assertRaisesRegex(StateError, "sample history is excluded"):
            self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()
        self.assertEqual(self.runtime.store.get_job(job["id"])["status"], "SAMPLE_READY")

    def test_pause_or_unreviewed_editor_prevents_dispatch(self):
        job = self.ready_job()
        self.runtime.backend_preflight(job["id"])
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.runtime.backend_review(job["id"], self.review_data(job, self.runtime.backend_preflight(job["id"])))
        self.runtime.store.set_paused(True)
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()

    def test_dispatch_marker_and_intent_exist_before_single_transport_call(self):
        job, prepared, _ = self.prepared_job()
        transport_seen = []

        def submit(payload):
            attempt = self.runtime.store.get_attempt(payload["attempt_id"])
            marker = self.runtime.store.db.execute("SELECT * FROM backend_calls WHERE attempt_id=?", (payload["attempt_id"],)).fetchone()
            transport_seen.append(payload)
            self.assertEqual(attempt["status"], "SUBMIT_INTENT")
            self.assertEqual(attempt["content_hash"], job["content_hash"])
            self.assertEqual(marker["payload_hash"], digest(payload))
            self.assertIsNone(marker["result"])
            self.assertEqual(payload["editor_session_id"], prepared["preflight"]["editor_session_id"])
            self.assertTrue(payload["visual_reviewed"])
            self.assertEqual(payload["reviewed_evidence_ref"], str(self.editor_evidence))
            return {"success": True, "data": {"phase": "SUBMIT_UNKNOWN", "click_attempted": True, "published": False, "confirmed": False}}

        self.client.submit.side_effect = submit
        result = self.runtime.backend_submit(job["id"])
        self.assertEqual(len(transport_seen), 1)
        self.assertFalse(result["published"])
        self.assertEqual(result["attempt"]["status"], "SUBMIT_UNKNOWN")
        self.assertEqual(len(self.runtime.store.status()["account_locks"]), 1)
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assertEqual(len(transport_seen), 1)
        marker = self.runtime.store.db.execute("SELECT result FROM backend_calls WHERE attempt_id=?", (result["attempt"]["attempt_id"],)).fetchone()
        self.assertIsNotNone(marker["result"])

    def test_transport_failure_keeps_intent_lock_and_never_dispatches_again(self):
        for phase in ("SUBMIT_UNKNOWN", "NOT_SUBMITTED"):
            with self.subTest(phase=phase):
                job, _, _ = self.prepared_job(phase)
                self.client.submit.side_effect = BackendError("backend_timeout", "Safe transport failure", phase=phase)
                result = self.runtime.backend_submit(job["id"])
                self.assertEqual(result["attempt"]["status"], "SUBMIT_UNKNOWN")
                self.assertFalse(result["published"])
                self.assertFalse(result["dispatch"]["retry_allowed"])
                before = self.client.submit.call_count
                with self.assertRaises(StateError):
                    self.runtime.backend_submit(job["id"])
                self.assertEqual(self.client.submit.call_count, before)
                self.assertEqual(len(self.runtime.store.status()["account_locks"]), 1)
                # End this isolated account attempt through an explicit simulated
                # confirmed rejection before creating the next independent case.
                self.runtime.store.reconcile(result["attempt"]["attempt_id"], {"status": "rejected", "confirmed": True}, {"fixture": True})

    def test_backend_candidates_do_not_reconcile_or_release_lock(self):
        job, _, _ = self.prepared_job()
        result = self.runtime.backend_submit(job["id"])
        before = self.runtime.store.get_attempt(result["attempt"]["attempt_id"])
        observation = self.runtime.backend_observations(before["attempt_id"])
        self.assertTrue(observation["data"]["requires_management_evidence"])
        self.assertEqual(self.runtime.store.get_attempt(before["attempt_id"]), before)
        self.assertEqual(len(self.runtime.store.status()["account_locks"]), 1)

    def test_management_capture_is_read_only_without_creating_intent(self):
        job = self.ready_job()
        before_job = self.runtime.store.get_job(job["id"])
        before_status = self.runtime.store.status()
        result = self.runtime.backend_management()
        self.assertEqual(result["management"]["view"], "all")
        self.assertEqual(result["management"]["filter_label"], "")
        self.assertEqual(result["management"]["phase"], "READ_ONLY")
        self.assertTrue(result["management"]["unverified"])
        self.assertFalse(result["management"]["published"])
        self.assertFalse(result["management"]["confirmed"])
        self.assertEqual(result["management"]["evidence_ref"], str(self.management_capture))
        self.assertIn("reconcile", result["instruction"])
        self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        self.assertEqual(self.runtime.store.status(), before_status)
        self.assertEqual(self.client.management_evidence.call_count, 1)
        self.assert_no_dispatch()

    def test_management_explicit_published_view_is_consistent_and_still_read_only(self):
        job = self.ready_job()
        before_job = self.runtime.store.get_job(job["id"])
        before_status = self.runtime.store.status()
        result = self.runtime.backend_management("published")
        self.assertEqual(result["management"]["view"], "published")
        self.assertEqual(result["management"]["filter_label"], "已发布")
        self.assertEqual(read_json(self.management_record)["view"], "published")
        self.assertEqual(read_json(self.management_record)["filter_label"], "已发布")
        self.assertFalse(result["management"]["confirmed"])
        self.assertFalse(result["management"]["published"])
        self.assertTrue(result["management"]["unverified"])
        self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        self.assertEqual(self.runtime.store.status(), before_status)
        self.client.management_evidence.assert_called_once_with(view="published")
        self.assert_no_dispatch()

    def test_management_filter_view_and_label_must_match_data_and_record(self):
        job = self.ready_job()
        before_job = self.runtime.store.get_job(job["id"])
        before_status = self.runtime.store.status()
        variants = (("all", "data", "view", "published"), ("all", "data", "filter_label", "全部"),
                    ("published", "data", "view", "all"), ("published", "data", "filter_label", "审核中"),
                    ("published", "record", "view", "pending_review"),
                    ("published", "record", "filter_label", ""), ("published", "record", "filter_label", "未通过"))
        for requested, location, field, value in variants:
            with self.subTest(requested=requested, location=location, field=field, value=value):
                def reply(view="all"):
                    response = self.management_reply(view)
                    if location == "data":
                        response["data"][field] = value
                    else:
                        record = read_json(self.management_record)
                        record[field] = value
                        write_json(self.management_record, record)
                    return response

                self.client.management_evidence.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_management(requested)
                self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
                self.assertEqual(self.runtime.store.status(), before_status)
        self.assert_no_dispatch()

    def test_invalid_management_filter_never_calls_backend_or_changes_state(self):
        job = self.ready_job()
        before_job = self.runtime.store.get_job(job["id"])
        before_status = self.runtime.store.status()
        for view in (None, [], {}, "", "scheduled", "published&token=invalid", "PUBLISHED", True):
            with self.subTest(view=view):
                with self.assertRaises(StateError):
                    self.runtime.backend_management(view)
        self.client.management_evidence.assert_not_called()
        self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        self.assertEqual(self.runtime.store.status(), before_status)
        self.assert_no_dispatch()

    def test_management_capture_cannot_resolve_unknown_attempt_or_release_lock_while_paused(self):
        job, _, _ = self.prepared_job()
        submitted = self.runtime.backend_submit(job["id"])
        self.runtime.store.set_paused(True)
        before_job = self.runtime.store.get_job(job["id"])
        before_attempt = self.runtime.store.get_attempt(submitted["attempt"]["attempt_id"])
        before_status = self.runtime.store.status()
        result = self.runtime.backend_management()
        self.assertTrue(result["management"]["unverified"])
        self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        self.assertEqual(self.runtime.store.get_attempt(before_attempt["attempt_id"]), before_attempt)
        self.assertEqual(self.runtime.store.status(), before_status)
        self.assertEqual(before_attempt["status"], "SUBMIT_UNKNOWN")
        self.assertEqual(len(before_status["account_locks"]), 1)
        self.assertEqual(self.client.submit.call_count, 1)

    def test_management_requires_read_only_fresh_matching_account_and_capture(self):
        job = self.ready_job()
        before_job = self.runtime.store.get_job(job["id"])
        required = ("phase", "unverified", "confirmed", "published", "view", "filter_label", "account", "observed_at",
                    "url", "evidence_ref", "record_ref", "evidence_sha256")
        variants = [(field, None, True) for field in required]
        variants += [(field, value, False) for field, value in (
            ("phase", "PREPARED"), ("unverified", False), ("unverified", 1),
            ("confirmed", True), ("confirmed", "false"), ("published", True), ("published", "false"),
            ("account", dict(self.identity, user_id="wrong-user")),
            ("account", dict(self.identity, red_id="wrong-red")),
            ("account", dict(self.identity, nickname="other-account")),
            ("account", dict(self.identity, authenticated=False)), ("account", []),
            ("observed_at", (datetime.now(TZ) - timedelta(minutes=16)).isoformat()),
            ("observed_at", (datetime.now(TZ) + timedelta(minutes=1)).isoformat()),
            ("observed_at", "not time"), ("evidence_sha256", "0" * 64),
            ("url", "https://example.com/new/note-manager"),
            ("url", "https://www.xiaohongshu.com/explore/unit-note"),
            ("url", "http://creator.xiaohongshu.com/new/note-manager"),
            ("url", "https://user:offline-password" + chr(64) + "creator.xiaohongshu.com/new/note-manager"),
            ("url", "https://creator.xiaohongshu.com:444/new/note-manager"),
            ("url", self.management_url + "?token=must-not-be-used"),
            ("url", self.management_url + "#must-not-be-used"), ("url", []),
        )]
        for field, value, missing in variants:
            with self.subTest(field=field, value=value, missing=missing):
                def reply(view="all"):
                    response = self.management_reply(view)
                    if missing:
                        response["data"].pop(field)
                    else:
                        response["data"][field] = value
                    return response

                self.client.management_evidence.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_management()
                self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        for data in ([], None, "unverified"):
            with self.subTest(data=data):
                self.client.management_evidence.side_effect = None
                self.client.management_evidence.return_value = {"success": True, "data": data}
                with self.assertRaises(StateError):
                    self.runtime.backend_management()
        self.assert_no_dispatch()

    def test_management_requires_private_existing_png_and_json(self):
        job = self.ready_job()
        outside = self.workspace / "management.png"
        outside.write_bytes(self.evidence_bytes)
        outside_record = self.workspace / "management-record.json"
        write_json(outside_record, {"fixture": True})
        wrong_png = self.evidence_dir / "management.jpg"
        wrong_png.write_bytes(self.evidence_bytes)
        wrong_json = self.evidence_dir / "management.txt"
        wrong_json.write_text("{}", encoding="utf-8")
        variants = (("evidence_ref", str(outside)), ("record_ref", str(outside_record)),
                    ("evidence_ref", "session/evidence/management.png"), ("record_ref", str(self.token_path)),
                    ("evidence_ref", str(wrong_png)), ("record_ref", str(wrong_json)),
                    ("evidence_ref", str(self.evidence_dir / "missing.png")),
                    ("record_ref", str(self.evidence_dir / "missing.json")))
        before_job = self.runtime.store.get_job(job["id"])
        for field, value in variants:
            with self.subTest(field=field, value=value):
                def reply(view="all"):
                    response = self.management_reply(view)
                    response["data"][field] = value
                    return response

                self.client.management_evidence.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_management()
                self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        self.assert_no_dispatch()

    def test_management_page_record_must_agree_with_actual_url_and_account(self):
        job = self.ready_job()
        variants = [(field, None, True) for field in ("url", "account", "visible_text", "view", "filter_label")]
        variants += [(field, value, False) for field, value in (
            ("url", self.management_url + "/different-record"), ("url", "https://example.com/fake-page"),
            ("account", dict(self.identity, user_id="wrong-user")),
            ("account", dict(self.identity, red_id="wrong-red")),
            ("account", dict(self.identity, observed_at=(datetime.now(TZ) - timedelta(minutes=16)).isoformat())),
            ("account", []), ("visible_text", []), ("visible_text", "   "), ("visible_text", None),
        )]
        before_job = self.runtime.store.get_job(job["id"])
        for field, value, missing in variants:
            with self.subTest(field=field, value=value, missing=missing):
                def reply(view="all"):
                    response = self.management_reply(view)
                    record = read_json(self.management_record)
                    if missing:
                        record.pop(field)
                    else:
                        record[field] = value
                    write_json(self.management_record, record)
                    return response

                self.client.management_evidence.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_management()
                self.assertEqual(self.runtime.store.get_job(job["id"]), before_job)
        for invalid in ("{not JSON", "[]", "null", '"page text"'):
            with self.subTest(record=invalid):
                def reply(view="all"):
                    response = self.management_reply(view)
                    self.management_record.write_text(invalid, encoding="utf-8")
                    return response

                self.client.management_evidence.side_effect = reply
                with self.assertRaises(StateError):
                    self.runtime.backend_management()
        self.assert_no_dispatch()

    def test_interrupted_dispatch_retains_intent_and_cannot_repeat_after_restart(self):
        job, _, _ = self.prepared_job()
        self.client.submit.side_effect = KeyboardInterrupt("fixture crash after durable marker")
        with self.assertRaises(KeyboardInterrupt):
            self.runtime.backend_submit(job["id"])
        attempt = self.runtime.store.status()["attempts"][0]
        marker = self.runtime.store.db.execute("SELECT result FROM backend_calls WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        self.assertIsNone(marker["result"])
        self.assertEqual(attempt["status"], "SUBMIT_INTENT")
        reopened = Runtime(self.config_path, lease_token=self.runtime.lease_token)
        try:
            with patch.object(reopened, "_backend", return_value=self.client):
                with self.assertRaises(StateError):
                    reopened.backend_submit(job["id"])
            self.assertEqual(self.client.submit.call_count, 1)
            self.assertEqual(reopened.store.recover_intents(), 1)
            self.assertEqual(reopened.store.get_attempt(attempt["attempt_id"])["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(len(reopened.store.status()["account_locks"]), 1)
        finally:
            reopened.store.close()

    def test_content_change_during_identity_read_cannot_freeze_new_version_and_send_old(self):
        job, _, _ = self.prepared_job()
        self.client.identity.side_effect = None

        def identity():
            current = self.runtime.store.get_job(job["id"])
            review = dict(current["review"], content_hash="e" * 64)
            self.runtime.store.update(job["id"], content_hash="e" * 64, review=review)
            return {"success": True, "data": copy.deepcopy(self.identity)}

        self.client.identity.side_effect = identity
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()

    def test_editor_expiration_during_identity_read_cannot_create_intent(self):
        job, _, _ = self.prepared_job()

        def identity():
            self.update_session_response(job["id"], expires_at=(datetime.now(TZ) - timedelta(seconds=1)).isoformat())
            return {"success": True, "data": copy.deepcopy(self.identity)}

        self.client.identity.side_effect = identity
        with self.assertRaises(StateError):
            self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()

    def test_atomic_intent_guard_rejects_version_change_after_pre_submit_checks(self):
        job, _, _ = self.prepared_job()
        original_prepare = self.runtime.store.prepare_attempt

        def change_before_atomic_guard(*args, **kwargs):
            self.runtime.store.update(job["id"], content_hash="d" * 64)
            return original_prepare(*args, **kwargs)

        with patch.object(self.runtime.store, "prepare_attempt", side_effect=change_before_atomic_guard):
            with self.assertRaises(StateError):
                self.runtime.backend_submit(job["id"])
        self.assert_no_dispatch()

    def test_excluded_source_name_is_rejected_in_all_public_fields_before_preflight(self):
        job = self.ready_job()
        original = copy.deepcopy(job["draft"])
        for field in ("title", "body", "tags", "quotes"):
            altered = copy.deepcopy(original)
            if isinstance(altered[field], list):
                altered[field][0] = "示例来源"
            else:
                altered[field] = "示例来源"
            self.runtime.store.update(job["id"], draft=altered)
            with self.subTest(field=field), self.assertRaisesRegex(StateError, "excluded source name"):
                self.runtime.backend_preflight(job["id"])
        self.client.preflight.assert_not_called()
        self.assert_no_dispatch()


if __name__ == "__main__":
    unittest.main()
