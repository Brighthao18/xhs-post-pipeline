"""Source-to-copy evidence checks; semantic judgments remain Codex's responsibility."""
import copy
import re
from urllib.parse import urlsplit
from .config import digest


KINDS = {"identity", "fact", "method", "condition", "claim", "context"}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be non-empty text")
    return value.strip()


def normalize_outline(raw, article, source_hash):
    """Freeze atomic source units before drafting, including actual source excerpts."""
    fields = {"schema_version", "source_hash", "subject", "subject_aliases", "content_type", "reader_question", "units"}
    if not isinstance(raw, dict) or set(raw) - fields or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("Invalid source outline schema; expected version 1")
    if not source_hash or raw.get("source_hash") != source_hash:
        raise ValueError("Source outline must bind the current acquired source_hash")
    if article.get("coverage") != "full" or not article.get("content"):
        raise ValueError("Source outline requires the complete acquired article")
    result = copy.deepcopy(raw)
    for key in ("subject", "content_type", "reader_question"):
        result[key] = _text(raw.get(key), key)
    source = article["content"]
    if result["subject"] not in source and result["subject"] not in article.get("title", ""):
        raise ValueError("The primary subject must occur in the acquired source")
    aliases = raw.get("subject_aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(v, str) or not v.strip() for v in aliases):
        raise ValueError("subject_aliases must be explicit text alternatives")
    result["subject_aliases"] = list(dict.fromkeys(v.strip() for v in aliases))
    units = raw.get("units")
    if not isinstance(units, list) or not 1 <= len(units) <= 40:
        raise ValueError("Source outline needs between 1 and 40 atomic units")
    seen = set()
    identity = False
    for unit in result["units"]:
        allowed = {"id", "kind", "required", "summary", "source_field", "source_excerpt", "key_terms"}
        if not isinstance(unit, dict) or set(unit) - allowed:
            raise ValueError("Invalid source unit fields")
        uid = _text(unit.get("id"), "unit id")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", uid) or uid in seen:
            raise ValueError("Source unit IDs must be unique stable names")
        seen.add(uid)
        if unit.get("kind") not in KINDS or type(unit.get("required")) is not bool:
            raise ValueError("Every source unit needs a kind and explicit required flag")
        unit["summary"] = _text(unit.get("summary"), uid + " summary")
        unit["source_excerpt"] = _text(unit.get("source_excerpt"), uid + " source excerpt")
        unit["source_field"] = unit.get("source_field", "content")
        if unit["source_field"] not in ("content", "title"):
            raise ValueError("Source units can reference content or title only")
        if unit["source_excerpt"] not in article.get(unit["source_field"], ""):
            raise ValueError("Source excerpt is not present in the acquired article: " + uid)
        groups = unit.get("key_terms", [])
        if not isinstance(groups, list):
            raise ValueError("key_terms must be an array of alternative-term groups")
        for group in groups:
            if (not isinstance(group, list) or not group
                    or any(not isinstance(v, str) or not v.strip() for v in group)
                    or not any(v in unit["source_excerpt"] for v in group)):
                raise ValueError("Each key-term group needs a source-supported anchor: " + uid)
        if unit["required"] and not groups:
            raise ValueError("Required units need explicit key terms: " + uid)
        if unit["kind"] == "identity" and unit["required"]:
            identity = True
    if not identity:
        raise ValueError("At least one identity unit must be required")
    return result


def current_outline(job):
    outline = job.get("source_outline")
    if not outline or job.get("source_outline_hash") != digest(outline):
        raise ValueError("A saved source outline is required before drafting")
    return normalize_outline(outline, job["article"], job["source_hash"])


def public_text(content, field):
    if field in ("title", "body"):
        return content[field]
    match = re.fullmatch(r"quotes\[([0-9]+)\]", field)
    if match:
        return content["quotes"][int(match[1])]
    if field in ("visual.cover.headline", "visual.cover.subheadline", "visual.cover.label"):
        return content.get("visual", {}).get("cover", {}).get(field.rsplit(".", 1)[1], "")
    raise ValueError("Unsupported public-copy field: " + field)


def validate_subject(outline, content):
    """Reject a vanished subject before spending time or calls on images."""
    terms = [outline["subject"], *outline["subject_aliases"]]
    if not any(term in content["body"] for term in terms):
        raise ValueError("The source subject is missing from the body")
    heading = content["title"] + " " + public_text(content, "visual.cover.headline")
    if not any(term in heading for term in terms):
        raise ValueError("The source subject is missing from the title/cover")
    if not any(term in quote for term in terms for quote in content["quotes"]):
        raise ValueError("The source subject is missing from the cards")


def _target(content, target):
    if not isinstance(target, dict) or set(target) != {"field", "excerpt"}:
        raise ValueError("A fidelity target must identify its public field and actual excerpt")
    field = _text(target["field"], "target field")
    excerpt = _text(target["excerpt"], "target excerpt")
    if excerpt not in public_text(content, field):
        raise ValueError("A mapped output excerpt is not present in the current copy")
    return field, excerpt


def fidelity_issues(job, review):
    """Mechanical evidence checks complement an independently performed comparison."""
    issues = []
    try:
        outline = current_outline(job)
        validate_subject(outline, job["draft"])
        if review.get("source_outline_hash") != job["source_outline_hash"]:
            raise ValueError("Fidelity review belongs to a different source outline")
        report = review.get("fidelity")
        if not isinstance(report, dict) or set(report) != {"independent_comparison", "additions_checked", "units", "additions"}:
            raise ValueError("A detailed source-to-copy fidelity report is required")
        if report["independent_comparison"] is not True or report["additions_checked"] is not True:
            raise ValueError("Compare raw source with final copy and check additions independently")
        records = report["units"]
        if not isinstance(records, list) or any(not isinstance(v, dict) for v in records):
            raise ValueError("Fidelity units must be explicit mapping records")
        ids = [record.get("unit_id") for record in records]
        if (any(not isinstance(uid, str) for uid in ids) or len(set(ids)) != len(ids)
                or set(ids) != {unit["id"] for unit in outline["units"]}):
            raise ValueError("Every source unit must be accounted for exactly once")
        indexed = {record["unit_id"]: record for record in records}
        for unit in outline["units"]:
            record = indexed[unit["id"]]
            if set(record) != {"unit_id", "decision", "reason", "semantic_ok", "targets"}:
                raise ValueError("Invalid fidelity unit record: " + unit["id"])
            _text(record["reason"], "unit handling reason")
            if record["semantic_ok"] is not True:
                issues.append("Meaning not verified: " + unit["id"])
            decision = record["decision"]
            if decision not in ("retain", "qualify", "omit"):
                raise ValueError("Invalid source unit decision")
            if unit["required"] and decision == "omit":
                issues.append("Required subject/method/condition omitted: " + unit["id"])
            if not isinstance(record["targets"], list):
                raise ValueError("Unit targets must be an array")
            targets = [_target(job["draft"], target) for target in record["targets"]]
            if decision == "omit" and targets:
                raise ValueError("Omitted units must not claim retained output excerpts")
            if decision != "omit" and not targets:
                issues.append("No retained output location: " + unit["id"])
            if unit["required"] and decision != "omit":
                body = "\n".join(excerpt for field, excerpt in targets if field == "body")
                if not body:
                    issues.append("Required unit has no body mapping: " + unit["id"])
                for group in unit["key_terms"]:
                    if not any(term in body for term in group):
                        issues.append("Required unit lost a specific detail: " + unit["id"] + "/" + group[0])
        additions = report["additions"]
        if not isinstance(additions, list):
            raise ValueError("Added facts must be listed with checked evidence")
        for addition in additions:
            if not isinstance(addition, dict) or set(addition) != {"target", "reason", "evidence_urls"}:
                raise ValueError("Invalid added-fact evidence record")
            _target(job["draft"], addition["target"])
            _text(addition["reason"], "added-fact reason")
            urls = addition["evidence_urls"]
            if (not isinstance(urls, list) or not urls
                    or any(not isinstance(url, str) or urlsplit(url).scheme not in ("https", "http")
                           or not urlsplit(url).netloc for url in urls)):
                raise ValueError("Added facts require actual checked evidence URLs")
    except (ValueError, KeyError, IndexError, TypeError) as error:
        issues.append(str(error))
    return issues
