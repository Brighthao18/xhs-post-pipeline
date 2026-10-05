"""Codex performs reasoning; this module enforces durable execution boundaries."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid
from .config import TZ, digest, load_config, now_iso, read_json, write_json
from .store import Store, StateError
from .quality import BASE_CHECKS, normalize_draft, verify_review, verify_artifacts
from .backend_runtime import BackendWorkflow
from .image_jobs import ImageWorkflow


class Runtime(ImageWorkflow, BackendWorkflow):
    def __init__(self, config_path, lease_token=None):
        self.config = load_config(config_path)
        self.store = Store(Path(self.config["state_dir"]) / "state.sqlite")
        self.lease_token = lease_token

    def check_run(self, required=False):
        self.store.check_run(self.lease_token, required=required)

    def _verify_materials(self, job):
        if job["draft"].get("visual"):
            self._image_api()  # Load the configured asset contract in fresh sessions.
        return verify_artifacts(job["artifacts"], job["draft"],
                                require_five=self.config.get("image_policy", {}).get("require_five_images", False))

    def source(self, source_id):
        for source in self.config["sources"]:
            if source["id"] == source_id:
                return source
        raise StateError("Source is outside the configured allowlist")

    def doctor(self):
        from .quality import generator_module
        generator = generator_module(self.config["skill_dir"])
        fonts = generator.resolve_fonts(**self.config.get("fonts", {}))
        blockers = []
        for source in self.config["sources"]:
            if source.get("enabled") and source.get("type") == "unconfigured":
                blockers.append({"source_id": source["id"], "reason": "Discovery feed is not configured"})
            elif source.get("enabled"):
                state = self.store.source_state(source['id'])
                if state.get('health') != 'healthy':
                    blockers.append({'source_id': source['id'], 'reason': state.get('error') or 'No successful live source listing has been verified'})
        if not self.config.get("account", {}).get("account_id"):
            blockers.append({"reason": "Target account stable ID has not been bound"})
        if self.config.get('publication_backend', {}).get('type') == 'xiaohongshu_mcp' and not self.config.get('account', {}).get('user_id'):
            blockers.append({'reason': 'Independent backend user_id has not been observed and bound'})
        if self.config.get("policy", {}).get("submit_backend_ready") is False:
            blockers.append({"reason": self.config.get("publication_backend", {}).get("reason", "The submission backend has not been verified")})
        from . import __version__
        return {"version": __version__, "engine": "codex", "fonts": fonts,
                "database": str(self.store.path), "blockers": blockers,
                "publish_enabled": self.config.get("policy", {}).get("allow_publish") is True,
                "dependencies": "Python standard library and existing Pillow; no model API required"}

    def poll(self):
        self.check_run()
        from .sources import discover, SourceError
        if self.store.paused():
            return {"status": "paused", "sources": []}
        results = []
        for source in self.config["sources"]:
            if not source.get("enabled", False):
                continue
            previous = self.store.source_state(source["id"])
            try:
                result = discover(source, json.loads(previous["cursor"]) if previous.get("cursor") else None)
                baseline = not previous.get("last_success") and source.get("new_only", True)
                saved = self.store.discover(source, result, baseline=baseline)
                results.append({"source_id": source["id"], "ok": True, **saved,
                                "capabilities": result.get("capabilities", {})})
            except (SourceError, ValueError, OSError) as error:
                reason = str(error)
                self.store.source_failure(source["id"], reason)
                diagnostics = getattr(error, "diagnostics", None)
                if diagnostics:
                    self.store.event("source_diagnostics", source_id=source["id"], diagnostics=diagnostics)
                results.append({"source_id": source["id"], "ok": False, "error_code": getattr(error, "code", "source_error"), "reason": reason, "diagnostics": diagnostics})
        return {"status": "polled", "sources": results}

    def import_url(self, source_id, url):
        self.check_run()
        from .sources import article_identity, normalize_url
        source = self.source(source_id)
        article = {"source_id": source_id, "article_id": article_identity(url),
                   "url": normalize_url(url), "title": "", "coverage": "partial"}
        job_id, added = self.store.import_article(source_id, article)
        return {"job": self.store.get_job(job_id), "added": [job_id] if added else []}

    def acquire(self, job_id):
        self.check_run()
        from .sources import acquire
        job = self.store.get_job(job_id)
        source = self.source(job["source_id"])
        try:
            article = acquire(job["article"], source)
            self._save_source(job_id, article)
        except Exception as error:
            retry_at = (datetime.now(TZ) + timedelta(minutes=30)).isoformat(timespec="seconds")
            self.store.update(job_id, status="FETCH_RETRY", reason=str(error), retry_at=retry_at)
        return self.store.get_job(job_id)

    def _save_source(self, job_id, article):
        job = self.store.get_job(job_id)
        from .sources import normalize_url
        if not article.get("url"):
            raise StateError("Acquisition URL missing")
        full = article.get("coverage") == "full" and bool(article.get("content"))
        previous = dict(job["article"])
        previous.update(article)
        previous["original_url"] = job["article"].get("original_url", job["article"]["url"])
        source_hash = digest({key: previous.get(key) for key in ("url", "title", "author", "published_at", "content", "coverage")})
        if job["source_hash"] == source_hash:
            if job["status"] in ("SOURCE_RECHECK", "FETCH_RETRY"):
                if job["artifacts"]:
                    health = self.source(job["source_id"]).get("domain") == "health"
                    status = "READY" if job["review"] and verify_review(job, job["review"], health=health, final=True) else "MATERIALS_GENERATED"
                else:
                    status = "DRAFTED" if job["draft"] else "FETCHED"
                self.store.update(job_id, status=status, reason=None, retry_at=None)
                self.store.event("source_rechecked_unchanged", job_id)
            return
        self.store.update(job_id, article=previous, source_hash=source_hash,
                          status="FETCHED" if full else "REVIEW_REQUIRED", reason=None if full else "Source incomplete",
                          draft=None, content_hash=None, review=None, artifacts=None, retry_at=None)

    def ingest_source(self, job_id, data):
        self.check_run()
        from .sources import extract_html
        job = self.store.get_job(job_id)
        if data.get("url") not in (job["article"]["url"], job["article"].get("original_url")):
            raise StateError("Source evidence URL mismatch")
        path = Path(data.get("evidence_path", ""))
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Source evidence must be an actual local HTML file")
        raw = path.read_text(encoding="utf-8-sig")
        extracted = extract_html(raw, data["url"])
        extracted.update({"source_id": job["source_id"], "url": data["url"],
                          "retrieval_method": data.get("retrieval_method", "codex_html"),
                          "fetched_at": data.get("fetched_at", now_iso()), "evidence_path": str(path)})
        for key in ("title", "author", "published_at"):
            if data.get(key):
                extracted[key] = data[key]
        self._save_source(job_id, extracted)
        return self.store.get_job(job_id)

    def draft(self, job_id, data):
        self.check_run()
        job = self.store.get_job(job_id)
        if job["article"].get("coverage") != "full" or not job["source_hash"]:
            raise StateError("Complete source acquisition is required before drafting")
        if job["revision"] >= self.config["policy"].get("max_revision_rounds", 2) + 1:
            raise StateError("Revision limit reached; resolve the job explicitly")
        if isinstance(data, dict) and "fonts" not in data and self.config.get("fonts"):
            data = {**data, "fonts": self.config["fonts"]}
        draft, warnings = normalize_draft(data, self.config["skill_dir"], self.config.get("author", "示例作者"))
        if self.config.get("image_policy", {}).get("require_five_images") and not draft.get("visual"):
            raise StateError("Current content policy requires visual and one cover plus four native Image cards")
        self.check_public_copy(draft)
        if not any(s.get("url") in (job["article"]["url"], job["article"].get("original_url")) for s in draft["sources"]):
            raise StateError("Draft sources do not include its queued article")
        self.store.update(job_id, draft=draft, content_hash=digest(draft), review=None,
                          artifacts=None, status="DRAFTED", revision=job["revision"] + 1, reason=None)
        return {"job": self.store.get_job(job_id), "warnings": warnings}

    def check_public_copy(self, draft):
        cover_text = tuple(draft.get("visual", {}).get("cover", {}).get(name, "") for name in ("headline", "subheadline", "label"))
        for name in self.config.get("editorial", {}).get("excluded_source_names", []):
            if name and any(name in text for text in (draft["title"], *draft.get("candidate_titles", []), draft["body"], *draft["tags"], *draft["quotes"], *cover_text)):
                raise StateError("Public copy contains an excluded source name; keep attribution in the private source review")

    def review(self, job_id, data):
        self.check_run()
        job = self.store.get_job(job_id)
        if not job["draft"]:
            raise StateError("Draft must exist before review")
        self.check_public_copy(job["draft"])
        health = self.source(job["source_id"]).get("domain") == "health"
        final = bool(job["artifacts"])
        passed = verify_review(job, data, health=health, final=final)
        if final and passed:
            facts = self._verify_materials(job)
            if data.get("manifest_hash") != facts["manifest_hash"]:
                raise StateError("Visual review is not bound to current image files")
        self.store.update(job_id, expected_content_hash=job["content_hash"], review=data, status="READY" if final and passed else "DRAFTED",
                          reason=None if passed else "Review incomplete or unresolved issues")
        return self.store.get_job(job_id)

    def render(self, job_id):
        self.check_run()
        job = self.store.get_job(job_id)
        if job["artifacts"]:
            facts = self._verify_materials(job)
            return {"job": job, **facts}
        health = self.source(job["source_id"]).get("domain") == "health"
        if not job["review"] or not verify_review(job, job["review"], health=health, final=False):
            raise StateError("Semantic/rights/domain checks must pass before rendering")
        work = Path(self.config["work_dir"]) / job_id
        input_path = work / "draft.json"
        write_json(input_path, job["draft"])
        target = Path(self.config["output_dir"]) / ("auto_" + job_id + "_v" + str(job["revision"]))
        command = [sys.executable, "-X", "utf8", str(Path(self.config["skill_dir"]) / "scripts/generate.py"),
                   "--input", str(input_path), "--output-dir", str(target)]
        if job["draft"].get("visual"):
            results = self._image_results(job_id)
            results_path = work / "image-results.json"
            write_json(results_path, results)
            target = target.with_name(target.name + "_i" + digest([r["normalized_sha256"] for r in results])[:12])
            command[-1] = str(target)
            command += ["--image-results", str(results_path)]
        if target.exists():
            # Recover packaging completed just before an interruption; verify every file first.
            bundle = {"output_dir": str(target)}
        else:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=180,
                                    cwd=self.config["workspace"])
            if result.returncode:
                raise StateError("Material generation failed: " + result.stderr[-2000:])
            bundle = json.loads(result.stdout)
        facts = verify_artifacts(bundle, job["draft"], require_five=self.config.get("image_policy", {}).get("require_five_images", False))
        bundle_hash = digest({"content": job["draft"], "ordered_images": [r["sha256"] for r in facts["publish_images"]]})
        self.check_run()
        self.store.update(job_id, expected_content_hash=job["content_hash"], artifacts=bundle,
                          content_hash=bundle_hash, review=None, status="MATERIALS_GENERATED")
        return {"job": self.store.get_job(job_id), **facts}

    def prepare_publish(self, job_id, account_id, evidence, expected_content_hash=None):
        self.check_run()
        policy = self.config["policy"]
        if policy.get("submit_backend_ready") is False:
            raise StateError("The actual submission backend is unavailable; retain prepared materials without creating an intent")
        expected = self.config.get("account", {}).get("account_id")
        if policy.get("allow_publish") is not True or not expected:
            raise StateError("Publishing disabled or actual target account has not been bound")
        if not account_id or expected != account_id:
            raise StateError("Observed account does not match the bound target")
        evidence_path = Path(evidence)
        if not evidence_path.is_absolute() or not evidence_path.is_file():
            raise StateError("Fresh account evidence file required")
        proof = read_json(evidence_path)
        if proof.get("account_id") != expected or proof.get("nickname") != self.config["account"]["nickname"] or proof.get("authenticated") is not True:
            raise StateError("Account evidence does not identify the authenticated target")
        observed = datetime.fromisoformat(proof["observed_at"])
        now = datetime.now(TZ)
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 900:
            raise StateError("Account evidence must be timezone-aware and within 15 minutes")
        reference = Path(proof.get("evidence_ref", ""))
        if not reference.is_absolute() or not reference.is_file():
            raise StateError("Actual account screenshot/record evidence is missing")
        start, end = policy.get("publish_hours", [9, 21])
        if not start <= now.hour <= end:
            raise StateError("Outside configured publication hours")
        job = self.store.get_job(job_id)
        if expected_content_hash is not None and job['content_hash'] != expected_content_hash:
            raise StateError('Reviewed backend content changed before submission')
        self.check_public_copy(job["draft"])
        if job["article"].get("is_fixture") is True or self.source(job["source_id"]).get("type") == "fixture":
            raise StateError("Fixture sources can never authorize live publication")
        health = self.source(job["source_id"]).get("domain") == "health"
        if not job["review"] or not verify_review(job, job["review"], health=health, final=True):
            raise StateError("Final review missing")
        facts = self._verify_materials(job)
        if job["review"].get("manifest_hash") != facts["manifest_hash"]:
            raise StateError("Materials changed since visual review")
        attempt = self.store.prepare_attempt(job_id, expected, self.config["policy_hash"], str(evidence_path),
                                            policy["max_posts_per_day"], now.date().isoformat(),
                                            expected_content_hash=job['content_hash'])
        return {"attempt": attempt, "post": job["draft"], "images": [r["path"] for r in facts["publish_images"]],
                "instruction": "Check the actual editor, click at most once, record-submit, then reconcile. Existing intents require read-only reconciliation."}

    def next(self):
        pending = []
        manual_parents = self.store.manual_retry_parents()
        for row in self.store.db.execute("SELECT attempt_id FROM attempts WHERE status NOT IN ('PUBLISHED','REJECTED') ORDER BY intent_at"):
            if row[0] in manual_parents:
                continue
            pending.append({"action": "reconcile", "attempt": self.store.get_attempt(row[0])})
        if self.store.paused():
            return {"paused": True, "work": pending}
        for job in self.store.jobs(("DISCOVERED", "FETCH_RETRY", "SOURCE_RECHECK", "FETCHED", "DRAFTED", "MATERIALS_GENERATED", "READY", "REVIEW_REQUIRED")):
            if job["status"] == "FETCH_RETRY" and job["retry_at"] and job["retry_at"] > now_iso():
                continue
            if job["status"] in ("DISCOVERED", "FETCH_RETRY", "SOURCE_RECHECK"):
                action = "acquire"
            elif not job["draft"]:
                action = "draft" if job["article"].get("coverage") == "full" else "source_review"
            elif not job["review"]:
                action = "visual_review" if job["artifacts"] else "semantic_review"
            elif not job["artifacts"]:
                health = self.source(job["source_id"]).get("domain") == "health"
                if not verify_review(job, job["review"], health=health, final=False):
                    action = "revise"
                elif self.config.get("image_policy", {}).get("require_five_images") and not job["draft"].get("visual"):
                    action = "revise"
                elif job["draft"].get("visual"):
                    image_work = self._image_action(job)
                    pending.append({**image_work, "job": job})
                    continue
                else:
                    action = "render"
            else:
                action = "publish" if job["status"] == "READY" else "visual_review"
                if action == "publish" and self.config.get("publication_backend", {}).get("type") == "xiaohongshu_mcp":
                    try:
                        session = self._prepared_session(job["id"])
                        action = "backend_submit" if session["review"] else "backend_visual_review"
                    except (StateError, KeyError, ValueError):
                        action = "backend_preflight"
            pending.append({"action": action, "job": job})
        return {"paused": False, "work": pending}

    def run_once(self):
        self.check_run(required=True)
        recovered = self.store.recover_intents()
        if self.store.paused():
            return {"recovered_intents": recovered, "discovery": {"status": "paused", "sources": []}, "acquired": [], **self.next()}
        discovered = self.poll()
        jobs = [job for job in self.store.jobs(("DISCOVERED", "FETCH_RETRY", "SOURCE_RECHECK")) if not job["retry_at"] or job["retry_at"] <= now_iso()]
        acquired = []
        for job in jobs[:self.config["policy"].get("max_acquire_per_run", 3)]:
            acquired.append({"job_id": job["id"], "status": self.acquire(job["id"])["status"]})
        return {"recovered_intents": recovered, "discovery": discovered, "acquired": acquired, **self.next()}

    def reconcile(self, attempt_id, observation):
        self.check_run()
        from .publication import validate_observation
        attempt = self.store.get_attempt(attempt_id)
        result = validate_observation(attempt, observation)
        if result.get("confirmed"):
            from urllib.parse import urlsplit
            references = [observation.get("evidence_ref", "")]
            references += list((observation.get("match_evidence") or {}).values())
            for reference in references:
                if isinstance(reference, str) and urlsplit(reference).scheme not in ("https",):
                    if not Path(reference).is_file():
                        result = dict(result, status="submit_unknown", confirmed=False, published=False,
                                      reason="Captured local publication evidence does not exist", error_code="missing_evidence")
                        break
        saved = self.store.reconcile(attempt_id, result, observation)
        return {"result": result, "attempt": saved}
