"""Deterministic checks bind Codex's reviews to exact source/content/assets."""
import hashlib
import importlib.util
from pathlib import Path
import sys
from .config import digest

BASE_CHECKS = ("facts", "title_body", "cards", "rights")


def generator_module(skill_dir):
    scripts = Path(skill_dir) / "scripts"
    if not (scripts / "generate.py").is_file():
        raise ValueError("Configured xhs-post generator is missing")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("xhs_material_generator", scripts / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_draft(raw, skill_dir, author):
    if isinstance(raw, dict) and "author" not in raw:
        raw = {**raw, "author": author}
    content, warnings, _ = generator_module(skill_dir).normalize_content(raw)
    if content["author"] != author:
        raise ValueError("Draft author does not match configured account style")
    if not content["sources"] or any(s.get("coverage") != "full" for s in content["sources"]):
        raise ValueError("Automatic workflow requires full source coverage")
    return content, warnings


def verify_review(job, review, health=False, final=False, fidelity_required=False):
    if review.get("job_id") != job["id"] or review.get("content_hash") != job["content_hash"] or review.get("source_hash") != job["source_hash"]:
        raise ValueError("Review hashes/job_id do not match the current source and content")
    checks = review.get("checks", {})
    if not isinstance(checks, dict) or any(type(v) is not bool for v in checks.values()):
        raise ValueError("Review checks must be explicit booleans")
    fidelity_required = fidelity_required or bool(job.get("source_outline"))
    required = BASE_CHECKS + (("source_fidelity",) if fidelity_required else ()) + (("domain_safety",) if health else ()) + (("visual",) if final else ())
    passed = all(checks.get(key) is True for key in required)
    if review.get("passed") is True and not all(checks.get(key) is True for key in required + ("visual",)):
        raise ValueError("passed=true requires every semantic and visual check")
    if not isinstance(review.get("issues", []), list):
        raise ValueError("Review issues must be an array")
    if review.get("issues"):
        passed = False
    if final and review.get("passed") is not True:
        passed = False
    evidence = review.get("evidence_urls", [])
    from urllib.parse import urlsplit
    if not isinstance(evidence, list) or any(not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc for url in evidence):
        raise ValueError("Evidence URLs must be actual HTTP(S) references")
    if health and passed and (not isinstance(evidence, list) or not evidence):
        raise ValueError("Health review needs actual checked evidence URLs")
    for claim in job["draft"].get("claim_checks", []):
        if claim["decision"] in ("retain", "qualify") and not claim.get("evidence_urls"):
            passed = False
    if fidelity_required:
        from .fidelity import fidelity_issues
        if fidelity_issues(job, review):
            passed = False
    return passed


def verify_artifacts(bundle, expected_content=None, require_five=False):
    from PIL import Image
    root = Path(bundle["output_dir"]).resolve()
    manifest = root / "_meta.json"
    from .config import read_json
    meta = read_json(manifest)
    if meta.get("status") != "materials_generated":
        raise ValueError("Material generation not completed")
    cards = []
    indexed = {}
    for record in meta["files"]:
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Artifact escaped its material directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Artifact changed after generation")
        if str(path) in indexed:
            raise ValueError("Duplicate artifact in file manifest")
        indexed[str(path)] = record
        if path.name.startswith("card_") and path.suffix.lower() == ".jpg":
            with Image.open(path) as picture:
                picture.load()
                if picture.size != (1080, 1440) or picture.mode != "RGB":
                    raise ValueError("Incorrect card dimensions")
            cards.append(record)
    if not cards:
        raise ValueError("No publishable cards")
    if expected_content is not None and digest(read_json(root / "content.json")) != digest(expected_content):
        raise ValueError("Rendered content does not match the queued draft")
    ordered = sorted(cards, key=lambda r: int(Path(r["path"]).stem.split("_")[-1]))
    content = expected_content if expected_content is not None else read_json(root / "content.json")
    native = bool(content.get("visual"))
    if require_five and not native:
        raise ValueError("Current image policy requires native five-image content; legacy four-card downgrade is forbidden")
    if native:
        import image_assets
        if meta.get("schema_version") != 2 or meta.get("protocol") != "imagegen_native_five" or meta.get("creative_hash") != digest(content):
            raise ValueError("Native image manifest/content protocol mismatch")
        publish_images = meta.get("publish_images")
        if not isinstance(publish_images, list) or [r.get("role") for r in publish_images] != list(image_assets.ROLES):
            raise ValueError("Cover and four cards must be present in exact publish order")
        if read_json(root / "images.json") != publish_images or meta.get("images") != [r["path"] for r in publish_images]:
            raise ValueError("Ordered image manifests disagree")
        for order, record in enumerate(publish_images):
            path = Path(record["path"]).resolve()
            if (record.get("order") != order or type(record.get("order")) is not int
                    or record.get("asset_id") != record["role"] or path != root / (record["role"] + ".jpg")
                    or record.get("width") != 1080 or record.get("height") != 1440
                    or indexed.get(str(path), {}).get("sha256") != record.get("sha256")):
                raise ValueError("Invalid ordered native image asset/path/hash")
            generation = record.get("generation") or {}
            if generation.get("normalized_sha256") != record["sha256"] or Path(generation.get("normalized_path", "")).resolve() != path:
                raise ValueError("Publish image is not its registered result")
            for name in ("original_path", "normalized_path", "thumbnail_path"):
                if not Path(generation.get(name, "")).resolve().is_relative_to(root):
                    raise ValueError("Native result evidence escaped its bundle")
            for reference in generation.get("references", []):
                if not Path(reference["path"]).resolve().is_relative_to(root):
                    raise ValueError("Reference evidence escaped its bundle")
        image_assets.verify_results([r["generation"] for r in publish_images], content)
    else:
        if meta.get("schema_version", 1) != 1 or meta.get("publish_images") or len(ordered) != len(content["quotes"]):
            raise ValueError("Legacy card protocol/count mismatch")
        publish_images = ordered
    return {"cards": ordered, "publish_images": publish_images, "protocol": "imagegen_native_five" if native else "legacy_cards",
            "manifest_hash": hashlib.sha256(manifest.read_bytes()).hexdigest()}
