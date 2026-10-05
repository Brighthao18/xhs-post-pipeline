"""Behavioral checks for lost themes, missing methods and stale review evidence."""
import copy
import unittest
from pipeline.v2.fidelity import fidelity_issues
from pipeline.v2.quality import verify_review
from pipeline.v2.store import StateError
from pipeline.v2.tests import test_runtime as runtime_fixtures


SOURCE = "混元卧是一种睡前练习。仰卧，双手十指交叉举过头。屈膝，双腿向外打开，两脚心相对。睡觉前放下手臂，慢慢伸直双腿。不要整晚保持动作。坚持一个月就能治疗失眠。"
BODY = "混元卧的要点是先看懂动作。仰卧，双手十指交叉举过头。屈膝，双腿向外打开，两脚心相对。睡觉前放下手臂，慢慢伸直双腿，不要整晚保持动作。本文不承诺治疗失眠。"


def outline(job):
    definitions = [
        ("identity", "identity", True, "混元卧是一种睡前练习。", [["混元卧"]]),
        ("upper", "method", True, "仰卧，双手十指交叉举过头。", [["十指交叉"], ["举过头"]]),
        ("lower", "method", True, "屈膝，双腿向外打开，两脚心相对。", [["屈膝"], ["向外打开"], ["脚心相对"]]),
        ("boundary", "condition", True, "不要整晚保持动作。", [["不要整晚"]]),
        ("claim", "claim", False, "坚持一个月就能治疗失眠。", []),
    ]
    return {"schema_version": 1, "source_hash": job["source_hash"], "subject": "混元卧",
            "subject_aliases": [], "content_type": "动作介绍", "reader_question": "动作和使用边界是什么？",
            "units": [{"id": uid, "kind": kind, "required": required, "summary": excerpt,
                       "source_excerpt": excerpt, "key_terms": terms}
                      for uid, kind, required, excerpt, terms in definitions]}


def comparison(job):
    return {"source_outline_hash": job["source_outline_hash"],
            "fidelity": {"independent_comparison": True, "additions_checked": True,
                         "units": [{"unit_id": unit["id"], "decision": "retain" if unit["required"] else "omit",
                                    "reason": "保留动作与边界；去掉无依据疗效承诺。", "semantic_ok": True,
                                    "targets": [{"field": "body", "excerpt": job["draft"]["body"]}] if unit["required"] else []}
                                   for unit in job["source_outline"]["units"]], "additions": []}}


class FidelityRuntimeTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeTests.setUp
    import_job = runtime_fixtures.RuntimeTests.import_job
    ingest = runtime_fixtures.RuntimeTests.ingest
    content = runtime_fixtures.RuntimeTests.content
    review_payload = runtime_fixtures.RuntimeTests.review_payload

    def acquired(self):
        self.runtime.config["editorial"] = {"require_source_fidelity": True}
        return self.ingest(self.import_job(), text=SOURCE, title="混元卧介绍")

    def drafted(self, body=BODY):
        job = self.acquired()
        job = self.runtime.source_outline(job["id"], outline(job))["job"]
        return self.runtime.draft(job["id"], self.content(job, title="混元卧动作介绍", body=body,
                                  quotes=["混元卧先看懂动作，再理解使用时的边界。",
                                          "手指交叉举过头，避免强拉到不适的角度。",
                                          "脚心相对与屈膝外展，共同构成下半部动作。",
                                          "睡觉前放下手脚，不要把练习动作保持整晚。"]))["job"]

    def reviewed(self, job):
        payload = self.review_payload(job)
        payload["checks"]["source_fidelity"] = True
        payload.update(comparison(job))
        return payload

    def test_next_requires_outline_and_drafting_cannot_skip_it(self):
        job = self.acquired()
        self.assertEqual(self.runtime.next()["work"][0]["action"], "source_outline")
        with self.assertRaisesRegex(ValueError, "outline"):
            self.runtime.draft(job["id"], self.content(job))

    def test_outline_rejects_invented_source_and_obsolete_hash(self):
        job = self.acquired()
        raw = outline(job)
        raw["units"][1]["source_excerpt"] = "原文从未出现的动作。"
        with self.assertRaisesRegex(ValueError, "excerpt"):
            self.runtime.source_outline(job["id"], raw)
        raw = outline(job); raw["source_hash"] = "old"
        with self.assertRaisesRegex(ValueError, "source_hash"):
            self.runtime.source_outline(job["id"], raw)

    def test_title_information_can_be_traced_separately(self):
        job = self.acquired()
        raw = outline(job)
        raw["units"].append({"id": "source_title", "kind": "context", "required": False,
                             "summary": "原文标题", "source_field": "title",
                             "source_excerpt": "混元卧介绍", "key_terms": []})
        saved = self.runtime.source_outline(job["id"], raw)["job"]
        self.assertEqual(saved["source_outline"]["units"][-1]["source_field"], "title")

    def test_keywords_cannot_override_a_failed_semantic_comparison(self):
        job = self.drafted(); payload = self.reviewed(job)
        payload["fidelity"]["units"][2]["semantic_ok"] = False
        self.assertFalse(verify_review(job, payload))

    def test_missing_theme_in_published_style_copy_is_rejected(self):
        job = self.acquired()
        self.runtime.source_outline(job["id"], outline(job))
        with self.assertRaisesRegex(ValueError, "subject"):
            self.runtime.draft(job["id"], self.content(job, tags=["混元卧"]))

    def test_theme_name_without_method_fails_review_and_render(self):
        job = self.drafted("混元卧让人想到睡前放松。选一个舒服姿势，安静下来就好。")
        payload = self.reviewed(job)
        updated = self.runtime.review(job["id"], payload)
        self.assertIn("specific detail", updated["reason"])
        self.assertFalse(verify_review(updated, payload))
        with self.assertRaisesRegex(StateError, "Semantic"):
            self.runtime.render(job["id"])

    def test_supplementary_sleep_advice_cannot_replace_original_method(self):
        job = self.drafted("混元卧与睡前放松有关。保持卧室安静，规律作息，少喝咖啡，及时求医。")
        self.assertTrue(fidelity_issues(job, self.reviewed(job)))

    def test_boolean_checks_alone_do_not_pass(self):
        job = self.drafted()
        payload = self.review_payload(job)
        payload["checks"]["source_fidelity"] = True
        self.assertFalse(verify_review(job, payload))

    def test_methods_survive_while_unsubstantiated_effect_is_omitted(self):
        job = self.drafted()
        payload = self.reviewed(job)
        self.assertEqual(fidelity_issues(job, payload), [])
        self.assertTrue(verify_review(job, payload))

    def test_required_method_cannot_be_omitted_with_a_safety_excuse(self):
        job = self.drafted(); payload = self.reviewed(job)
        record = payload["fidelity"]["units"][1]
        record.update(decision="omit", reason="缺少治疗效果证据所以删去整个动作。", targets=[])
        self.assertFalse(verify_review(job, payload))

    def test_theme_absent_from_cards_is_rejected(self):
        job = self.drafted()
        content = copy.deepcopy(job["draft"])
        content["quotes"][0] = "给睡前留一点安静空间，舒服比标准重要。"
        with self.assertRaisesRegex(ValueError, "cards"):
            self.runtime.draft(job["id"], content)

    def test_new_fact_requires_evidence(self):
        job = self.drafted(); payload = self.reviewed(job)
        payload["fidelity"]["additions"] = [{"target": {"field": "body", "excerpt": "本文不承诺治疗失眠。"},
                                              "reason": "新增解释。", "evidence_urls": []}]
        self.assertFalse(verify_review(job, payload))

    def test_only_updating_review_hash_leaves_stale_output_mapping(self):
        job = self.drafted(); payload = self.reviewed(job)
        content = copy.deepcopy(job["draft"])
        content["body"] = BODY.replace("十指交叉举过头", "双手放在胸前")
        changed = self.runtime.draft(job["id"], content)["job"]
        payload["content_hash"] = changed["content_hash"]
        self.assertFalse(verify_review(changed, payload))

    def test_source_change_clears_outline_and_requires_new_extraction(self):
        job = self.drafted()
        self.ingest(job, text=SOURCE + "新增原文内容。", title="混元卧介绍")
        changed = self.runtime.store.get_job(job["id"])
        self.assertIsNone(changed["source_outline"])
        self.assertIsNone(changed["draft"])
        self.assertEqual(self.runtime.next()["work"][0]["action"], "source_outline")

    def test_outline_change_invalidates_old_copy_and_review(self):
        job = self.drafted()
        self.runtime.review(job["id"], self.reviewed(job))
        raw = copy.deepcopy(job["source_outline"])
        raw["reader_question"] = "上下动作如何对应？"
        changed = self.runtime.source_outline(job["id"], raw)["job"]
        self.assertIsNone(changed["draft"])
        self.assertIsNone(changed["review"])
        self.assertNotEqual(changed["source_outline_hash"], job["source_outline_hash"])


if __name__ == "__main__":
    unittest.main()
