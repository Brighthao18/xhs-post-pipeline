"""Resumable native Image handoffs. Only Codex can invoke the desktop tool."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from .config import digest, now_iso, write_json
from .quality import generator_module
from .store import StateError


class ImageWorkflow:
    def _image_api(self):
        generator_module(self.config["skill_dir"])
        import image_assets
        return image_assets

    def _image_context(self, job_id, semantic=False):
        job = self.store.get_job(job_id)
        if self.store.db.execute("SELECT 1 FROM attempts WHERE job_id=?", (job_id,)).fetchone():
            raise StateError("Publication intent freezes images; reconcile instead")
        if not job["draft"] or not job["draft"].get("visual"):
            raise StateError("Image workflow requires reviewed visual content")
        self.check_public_copy(job["draft"])
        if semantic:
            health = self.source(job["source_id"]).get("domain") == "health"
            if not job["review"] or not self._review_valid(job, job["review"], health=health, final=False):
                raise StateError("Semantic/rights/domain checks must pass before native image calls")
        return job, digest(job["draft"])

    def _active_image_plan(self, job_id):
        row = self.store.db.execute("SELECT * FROM image_plans WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def _image_rows(self, job_id):
        plan = self._active_image_plan(job_id)
        if not plan:
            return []
        rows = []
        for row in self.store.db.execute("SELECT * FROM image_jobs WHERE job_id=? AND plan_hash=? ORDER BY image_order",
                                         (job_id, plan["plan_hash"])):
            record = dict(row)
            for name in ("payload", "result", "inspection"):
                record[name] = json.loads(record[name]) if record[name] else None
            rows.append(record)
        return rows

    def _image_row(self, job_id, image_id):
        for row in self._image_rows(job_id):
            if row["id"] == image_id:
                return row
        raise StateError("Image job is missing or belongs to an obsolete plan")

    def _check_image_version(self, job_id, row):
        job, creative_hash = self._image_context(job_id)
        plan = self._active_image_plan(job_id)
        if (row["creative_hash"] != creative_hash or not plan
                or plan["source_hash"] != job["source_hash"]
                or json.loads(plan["payload"]).get("source_outline_hash") != job.get("source_outline_hash")):
            raise StateError("Image task belongs to an obsolete content/source/visual version")
        for reference in json.loads(plan["payload"])["references"]:
            if self._image_api().file_hash(reference["path"]) != reference["sha256"]:
                raise StateError("Reference image changed; create a new plan and review")
        return job

    def _invalidate_image_materials(self, job):
        if job["artifacts"]:
            self.store._update(job["id"], artifacts=None, review=None, content_hash=digest(job["draft"]), status="DRAFTED")
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='backend_sessions'").fetchone():
                self.store.db.execute("DELETE FROM backend_sessions WHERE job_id=?", (job["id"],))

    def plan_images(self, job_id, data):
        self.check_run(required=True)
        job, creative_hash = self._image_context(job_id, semantic=True)
        if not isinstance(data, dict) or set(data) - {"references"}:
            raise ValueError("Image plan accepts references only; scenes/text come from the reviewed visual object")
        references = []
        if not isinstance(data.get("references", []), list):
            raise ValueError("references must be an array")
        for ref in data.get("references", []):
            if (not isinstance(ref, dict) or set(ref) - {"path", "role", "purpose", "viewed"}
                    or ref.get("viewed") is not True or not ref.get("purpose")
                    or ref.get("role") not in ("historical_style", "object_reference")):
                raise ValueError("Each reference requires an actual viewing attestation, purpose and explicit role")
            path = Path(ref.get("path", ""))
            if not path.is_absolute() or not path.is_file():
                raise ValueError("Reference must be an actual absolute image path")
            from PIL import Image
            with Image.open(path) as image:
                image.load()
            references.append(dict(ref, path=str(path.resolve()), sha256=self._image_api().file_hash(path)))
        payload = {"references": references, "source_hash": job["source_hash"],
                   "creative_hash": creative_hash, "requested_model_family": "gpt-image-2.5"}
        if job.get("source_outline_hash"):
            payload["source_outline_hash"] = job["source_outline_hash"]
        plan_hash = digest(payload)
        with self.store.transaction():
            self._image_context(job_id, semantic=True)
            if self.store.get_job(job_id)["content_hash"] != job["content_hash"]:
                raise StateError("Content changed while planning images")
            old = self._active_image_plan(job_id)
            if old and old["plan_hash"] != plan_hash:
                self._invalidate_image_materials(job)
            self.store.db.execute("INSERT OR REPLACE INTO image_plans VALUES(?,?,?,?,?)",
                                  (job_id, creative_hash, job["source_hash"], plan_hash, json.dumps(payload, ensure_ascii=False)))
            for order, role in enumerate(self._image_api().ROLES):
                prompt = self._image_api().build_prompt(job["draft"], role)
                task = dict(payload, role=role, prompt=prompt,
                            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            anchor_asset_id=None if role == "cover" else "cover")
                image_id = digest({"job_id": job_id, "plan_hash": plan_hash, "role": role})
                at = now_iso()
                self.store.db.execute("INSERT OR IGNORE INTO image_jobs(id,job_id,creative_hash,plan_hash,role,image_order,state,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,'PLANNED',?,?,?)",
                                      (image_id, job_id, creative_hash, plan_hash, role, order, json.dumps(task, ensure_ascii=False), at, at))
            self.store.event("image_plan_saved", job_id, plan_hash=plan_hash, creative_hash=creative_hash)
        return self.image_status(job_id)

    def image_status(self, job_id):
        job = self.store.get_job(job_id)
        return {"job_id": job_id, "creative_hash": digest(job["draft"]) if job["draft"] else None,
                "plan": self._active_image_plan(job_id), "images": self._image_rows(job_id),
                "next_image": self._image_action(job) if job["draft"] and job["draft"].get("visual") else None}

    def _image_action(self, job):
        rows = self._image_rows(job["id"])
        if len(rows) != 5:
            return {"action": "plan_images"}
        for row in rows:
            try:
                self._check_image_version(job["id"], row)
                if row["state"] == "PASSED":
                    self._image_api().verify_result(dict(row["result"], inspection=row["inspection"]), job["draft"])
                    continue
            except (ValueError, OSError) as error:
                return {"action": "plan_images", "reason": str(error), "image_job": row}
            action = {"PLANNED": "generate_image", "CALLING": "recover_image", "RETURNED": "inspect_image",
                      "NEEDS_REPAIR": "repair_image", "WAIT_IMAGE_TOOL": "wait_image_tool", "FAILED": "image_failed"}.get(row["state"])
            return {"action": action or "image_failed", "image_job": row}
        return {"action": "render"}

    def image_intent(self, job_id, data):
        self.check_run(required=True)
        job, _ = self._image_context(job_id, semantic=True)
        row = self._image_row(job_id, data.get("image_job_id"))
        self._check_image_version(job_id, row)
        if row["state"] == "CALLING":
            intent = self.store.db.execute("SELECT payload FROM image_attempts WHERE image_id=? ORDER BY attempt_number DESC LIMIT 1", (row["id"],)).fetchone()
            return {"intent": json.loads(intent[0]), "action": "recover_image", "instruction": "Match this intent with the actual tool receipt/result before another call; do not regenerate blindly"}
        if row["state"] == "PASSED":
            self._image_api().verify_result(dict(row["result"], inspection=row["inspection"]), job["draft"])
            return {"action": "reuse_image", "result": row["result"]}
        if row["state"] not in ("PLANNED", "NEEDS_REPAIR", "WAIT_IMAGE_TOOL"):
            raise StateError("Inspect the returned image or resolve its failure before a new call")
        repair = row["state"] == "NEEDS_REPAIR" or (row["state"] == "WAIT_IMAGE_TOOL" and row["result"] is not None)
        limit = self.config.get("image_policy", {}).get("max_repairs_per_image", 2)
        if repair and row["repair_count"] >= limit:
            raise StateError("Image repair limit reached; retain result and actual failure")
        task = copy.deepcopy(row["payload"])
        references = task["references"]
        if row["role"] != "cover":
            cover = self._image_rows(job_id)[0]
            if cover["state"] != "PASSED":
                raise StateError("Inspect and pass the fixed cover anchor before generating cards")
            self._image_api().verify_result(dict(cover["result"], inspection=cover["inspection"]), job["draft"])
            references.append({"path": cover["result"]["original_path"], "sha256": cover["result"]["original_sha256"],
                               "role": "series_anchor", "purpose": "固定本篇封面的配色、材质和光线，不复制字稿"})
        if repair:
            correction = data.get("repair_prompt")
            if not isinstance(correction, str) or not correction.strip():
                raise ValueError("Repair needs a targeted region/problem/correct text/preserve prompt")
            task["prompt"] = correction.strip()
            references.append({"path": row["result"]["original_path"], "sha256": row["result"]["original_sha256"],
                               "role": "edit_target", "purpose": "仅修正登记的问题，保留其他画面与文字"})
        task["prompt_hash"] = hashlib.sha256(task["prompt"].encode("utf-8")).hexdigest()
        intent = dict(task, intent_id=uuid.uuid4().hex, image_job_id=row["id"], job_id=job_id,
                      attempt_number=row["attempt_count"] + 1, kind="repair" if repair else "generate", called_at=now_iso(),
                      cache_key=digest({"content": job["draft"], "source_hash": job["source_hash"],
                                        "prompt": task["prompt"], "references": references, "mode": "imagegen_native"}))
        with self.store.transaction():
            self._check_image_version(job_id, row)
            current = self._image_row(job_id, row["id"])
            if current["state"] != row["state"] or current["attempt_count"] != row["attempt_count"]:
                raise StateError("Image state changed while saving intent")
            self._invalidate_image_materials(job)
            self.store.db.execute("INSERT INTO image_attempts VALUES(?,?,?,?,?,NULL)",
                                  (intent["intent_id"], row["id"], intent["attempt_number"], "CALLING", json.dumps(intent, ensure_ascii=False)))
            self.store.db.execute("UPDATE image_jobs SET state='CALLING',attempt_count=attempt_count+1,repair_count=repair_count+?,inspection=NULL,reason=NULL,updated_at=? WHERE id=?",
                                  (int(repair), now_iso(), row["id"]))
            self.store.event("image_call_intent", job_id, intent_id=intent["intent_id"], image_job_id=row["id"], call_kind=intent["kind"])
        return {"intent": intent, "action": "call_native_image", "tool": "image_gen.imagegen",
                "tool_arguments": {"prompt": intent["prompt"], "transparent_background": False,
                                   **({"referenced_image_paths": [r["path"] for r in references]} if references else {})}}

    def image_result(self, job_id, data):
        self.check_run(required=True)
        row = self._image_row(job_id, data.get("image_job_id"))
        job = self._check_image_version(job_id, row)
        attempt = self.store.db.execute("SELECT * FROM image_attempts WHERE id=? AND image_id=?", (data.get("intent_id"), row["id"])).fetchone()
        if not attempt or attempt["attempt_number"] != row["attempt_count"]:
            raise StateError("Result receipt must identify the current saved generation intent")
        intent = json.loads(attempt["payload"])
        path = Path(data.get("original_path", ""))
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Tool result must be the actual returned local image, not the newest guessed file")
        if data.get("tool") != "image_gen.imagegen" or not isinstance(data.get("returned_metadata"), dict) or not data["returned_metadata"]:
            raise ValueError("Actual native tool name and returned metadata are required")
        raw_hash = self._image_api().file_hash(path)
        if row["state"] in ("RETURNED", "PASSED"):
            if row["result"]["intent_id"] == intent["intent_id"] and row["result"]["original_sha256"] == raw_hash:
                return {"result": dict(row["result"], inspection=row["inspection"]), "reused": True}
            raise StateError("An intent has already returned a different image; create a new call intent")
        if row["state"] != "CALLING":
            raise StateError("No pending generation intent")
        for reference in intent["references"]:
            if self._image_api().file_hash(reference["path"]) != reference["sha256"]:
                raise StateError("Generation reference changed during the call")
        work = Path(self.config["work_dir"]) / job_id / "images" / row["id"] / intent["intent_id"]
        work.mkdir(parents=True, exist_ok=True)
        original = work / ("original" + path.suffix.lower())
        if original.exists() and self._image_api().file_hash(original) != raw_hash:
            raise StateError("Refusing to replace a previously registered original")
        if not original.exists():
            shutil.copy2(path, original)
        normalized, thumbnail = work / "publish.jpg", work / "mobile.jpg"
        adaptation = self._image_api().adapt_image(original, normalized, thumbnail)
        receipt = {"tool": data["tool"], "status": "returned", "intent_id": intent["intent_id"],
                   "original_sha256": raw_hash, "source_path": str(path), "called_at": intent["called_at"],
                   "returned_at": now_iso(), "timestamp_source": "local_registration_clock",
                   "requested_model_family": "gpt-image-2.5", "observed_model": data.get("observed_model"),
                   "model_exposure": "not_exposed" if not data.get("observed_model") else "tool_returned",
                   "returned_metadata": data["returned_metadata"]}
        result = {"role": row["role"], "creative_hash": row["creative_hash"], "plan_hash": row["plan_hash"],
                  "intent_id": intent["intent_id"], "cache_key": intent["cache_key"], "prompt": intent["prompt"],
                  "prompt_hash": intent["prompt_hash"], "references": intent["references"], "receipt": receipt,
                  "original_path": str(original), "original_sha256": raw_hash,
                  "normalized_path": str(normalized), "normalized_sha256": self._image_api().file_hash(normalized),
                  "thumbnail_path": str(thumbnail), "thumbnail_sha256": self._image_api().file_hash(thumbnail),
                  "adaptation": adaptation}
        self._image_api().verify_result(result, job["draft"], require_inspection=False)
        write_json(work / "result.json", result)
        with self.store.transaction():
            self._check_image_version(job_id, row)
            current = self._image_row(job_id, row["id"])
            if current["state"] != "CALLING" or current["attempt_count"] != intent["attempt_number"]:
                raise StateError("Image changed during result registration")
            self._invalidate_image_materials(job)
            self.store.db.execute("UPDATE image_attempts SET state='RETURNED',result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), intent["intent_id"]))
            self.store.db.execute("UPDATE image_jobs SET state='RETURNED',result=?,inspection=NULL,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), now_iso(), row["id"]))
            if row["role"] == "cover":
                self.store.db.execute("UPDATE image_jobs SET state='PLANNED',result=NULL,inspection=NULL,repair_count=0,reason='Cover anchor changed',updated_at=? WHERE job_id=? AND plan_hash=? AND role!='cover'",
                                      (now_iso(), job_id, row["plan_hash"]))
            self.store.event("image_result_registered", job_id, intent_id=intent["intent_id"], original_sha256=raw_hash)
        return {"result": result, "action": "inspect_image", "instruction": "Actually view original, normalized result and 270x360 thumbnail, then register exact text and observations"}

    def image_inspect(self, job_id, data):
        self.check_run(required=True)
        row = self._image_row(job_id, data.get("image_job_id"))
        job = self._check_image_version(job_id, row)
        if row["state"] not in ("RETURNED", "PASSED", "NEEDS_REPAIR") or not row["result"]:
            raise StateError("An actual returned image is required before inspection")
        result = row["result"]
        self._image_api().verify_result(result, job["draft"], require_inspection=False)
        if (data.get("intent_id") != result["intent_id"] or data.get("creative_hash") != row["creative_hash"]
                or data.get("result_hash") != result["normalized_sha256"]
                or data.get("original_hash") != result["original_sha256"]
                or data.get("thumbnail_hash") != result["thumbnail_sha256"]):
            raise StateError("Inspection must bind current intent/content/original/result/mobile hashes")
        if type(data.get("passed")) is not bool or not isinstance(data.get("issues"), list):
            raise ValueError("Inspection needs explicit passed boolean and issues array")
        inspection = dict(data, expected_text=self._image_api().expected_text(job["draft"], row["role"]), inspected_at=now_iso())
        if inspection["passed"]:
            self._image_api().verify_inspection(dict(result, inspection=inspection), job["draft"])
        state = "PASSED" if inspection["passed"] else ("FAILED" if row["repair_count"] >= self.config.get("image_policy", {}).get("max_repairs_per_image", 2) else "NEEDS_REPAIR")
        with self.store.transaction():
            self._check_image_version(job_id, row)
            if self._image_row(job_id, row["id"])["result"]["intent_id"] != result["intent_id"]:
                raise StateError("Result changed while inspecting")
            if row["state"] == "PASSED" and not inspection["passed"]:
                self._invalidate_image_materials(job)
            self.store.db.execute("UPDATE image_jobs SET state=?,inspection=?,reason=?,updated_at=? WHERE id=?",
                                  (state, json.dumps(inspection, ensure_ascii=False), None if inspection["passed"] else json.dumps(inspection["issues"], ensure_ascii=False), now_iso(), row["id"]))
            self.store.event("image_inspected", job_id, image_job_id=row["id"], intent_id=result["intent_id"], passed=inspection["passed"])
        return self.image_status(job_id)

    def image_block(self, job_id, data):
        self.check_run(required=True)
        row = self._image_row(job_id, data.get("image_job_id"))
        job = self._check_image_version(job_id, row)
        if row["state"] != "CALLING" or not isinstance(data.get("reason"), str) or not data["reason"].strip():
            raise StateError("Block only a pending call with an actual tool failure reason")
        with self.store.transaction():
            self._invalidate_image_materials(job)
            self.store.db.execute("UPDATE image_jobs SET state='WAIT_IMAGE_TOOL',reason=?,updated_at=? WHERE id=?", (data["reason"], now_iso(), row["id"]))
            self.store.db.execute("UPDATE image_attempts SET state='WAIT_IMAGE_TOOL' WHERE image_id=? AND attempt_number=?", (row["id"], row["attempt_count"]))
            self.store.event("WAIT_IMAGE_TOOL", job_id, image_job_id=row["id"], reason=data["reason"])
        return self.image_status(job_id)

    def _image_results(self, job_id):
        job, _ = self._image_context(job_id)
        rows = self._image_rows(job_id)
        if len(rows) != 5 or any(row["state"] != "PASSED" for row in rows):
            raise StateError("Five actual native Image results and individual inspections are required before packaging")
        for row in rows:
            self._check_image_version(job_id, row)
        results = [dict(row["result"], inspection=row["inspection"]) for row in rows]
        self._image_api().verify_results(results, job["draft"])
        return results
