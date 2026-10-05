"""Validate a post-submit platform observation without submitting or retrying.

The Codex browser bridge supplies an observation from one uniquely identified
management record or note detail. This module validates that contract; it does
not visit URLs or interpret screenshots. ``evidence_ref`` must point to the
actual captured platform evidence, never an invented file or link. A prepared
editor, URL transition, toast, or tool's ``success`` flag is not a platform
record.

The attempt's SHA-256 ``content_hash`` identifies the canonical bundle of title,
body, tags, and ordered image hashes. ``verified_match`` is an alternative for
platforms whose records cannot expose that bundle hash: Codex must read back
title, body, images, and image order, then attach body/image evidence references.
Every unknown result forbids another automatic submission of the same attempt.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from datetime import datetime
from enum import Enum
from urllib.parse import unquote, urlsplit


class FailureClass(str, Enum):
    """Machine-readable reasons for an unconfirmed or rejected submission."""

    INVALID_ATTEMPT = "invalid_attempt"
    INVALID_OBSERVATION = "invalid_observation"
    INVALID_TIME = "invalid_time"
    STALE_RECORD = "stale_record"
    WRONG_ACCOUNT = "wrong_account"
    AMBIGUOUS_RECORD = "ambiguous_record"
    INVALID_NOTE_IDENTITY = "invalid_note_identity"
    MISSING_EVIDENCE = "missing_evidence"
    CONTENT_MISMATCH = "content_mismatch"
    INSUFFICIENT_MATCH = "insufficient_match"
    PLATFORM_REJECTED = "platform_rejected"


PLATFORM_STATES = frozenset({"pending_review", "published", "rejected", "scheduled"})
EVIDENCE_TYPES = frozenset({"management_record", "note_detail"})
_PLATFORM_HOSTS = frozenset({
    "xiaohongshu.com", "www.xiaohongshu.com", "creator.xiaohongshu.com",
})
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_NOTE_ID = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
_PLACEHOLDERS = frozenset({
    "none", "null", "unknown", "pending", "success", "note_id", "placeholder",
    "example", "test", "undefined",
})


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _aware_time(value: object) -> datetime | None:
    """Only ISO timestamps carrying an explicit timezone are admissible."""
    value = _text(value)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _platform_url(value: object) -> str | None:
    value = _text(value)
    if value is None or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in _PLATFORM_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            return None
    except ValueError:
        return None
    return value


def _stable_note_id(value: object) -> str | None:
    value = _text(value)
    if value is None or value.lower() in _PLACEHOLDERS or not _NOTE_ID.fullmatch(value):
        return None
    return value


def _note_url_id(value: object) -> tuple[str | None, str | None]:
    url = _platform_url(value)
    if url is None:
        return None, None
    parsed = urlsplit(url)
    if parsed.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        return None, None
    path = unquote(parsed.path).rstrip("/")
    match = re.fullmatch(r"/(?:explore|discovery/item)/([^/]+)", path)
    if not match:
        return None, None
    note_id = _stable_note_id(match.group(1))
    return (url, note_id) if note_id else (None, None)


def _evidence_ref(value: object) -> bool:
    """Validate reference shape; capture/interpretation belongs to Codex."""
    value = _text(value)
    if value is None or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return False
    if _platform_url(value):
        return bool(urlsplit(value).path.rstrip("/"))
    # Windows drive/UNC or POSIX absolute captures are portable handoff forms.
    windows_absolute = ntpath.isabs(value) and bool(ntpath.splitdrive(value)[0])
    absolute = windows_absolute or posixpath.isabs(value)
    suffix = posixpath.splitext(value.replace("\\", "/"))[1].lower()
    return absolute and suffix in {".png", ".jpg", ".jpeg", ".webp", ".json", ".html", ".txt"}


def _unknown(attempt: object, failure: FailureClass, reason: str) -> dict:
    return {
        "status": "submit_unknown",
        "confirmed": False,
        "published": False,
        "retry_allowed": False,
        "error_code": failure.value,
        "reason": reason,
        "attempt_id": attempt.get("attempt_id") if isinstance(attempt, dict) else None,
        "note_id": None,
        "note_url": None,
    }


def validate_observation(attempt: dict, observation: dict) -> dict:
    """Return a verified platform state or ``submit_unknown`` with a reason.

    Required attempt fields: attempt_id, stable account_id, title, canonical
    SHA-256 content_hash, submitted_at (ISO timestamp with timezone).

    Required observation fields: account_id, title, observed_at (timezone),
    state, stable note_id or actual HTTPS note_url, evidence_type, evidence_ref,
    and either matching content_hash or the strong readback contract above.
    Optional ``match_count`` must be exactly one; ``candidates`` can hold exactly
    one record, whose fields must agree with the top-level observation. Optional
    record_created_at excludes an older record. Booleans are never counts.

    All return paths are JSON serializable and prohibit automatic resubmission.
    A verified rejected/pending/scheduled record is distinct from publication.
    """
    if not isinstance(attempt, dict):
        return _unknown(attempt, FailureClass.INVALID_ATTEMPT, "Attempt must be an object.")
    if any(_text(attempt.get(key)) is None for key in ("attempt_id", "account_id", "title")):
        return _unknown(attempt, FailureClass.INVALID_ATTEMPT, "Attempt identity, account, and title are required.")
    expected_hash = _text(attempt.get("content_hash"))
    if expected_hash is None or not _SHA256.fullmatch(expected_hash):
        return _unknown(attempt, FailureClass.INVALID_ATTEMPT, "Attempt must carry the canonical bundle SHA-256.")
    submitted_at = _aware_time(attempt.get("submitted_at"))
    if submitted_at is None:
        return _unknown(attempt, FailureClass.INVALID_TIME, "Submission time must include a timezone.")
    if not isinstance(observation, dict):
        return _unknown(attempt, FailureClass.INVALID_OBSERVATION, "Observation must be an object.")
    if "attempt_id" in observation and observation["attempt_id"] != attempt["attempt_id"]:
        return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Observation identifies a different submission attempt.")

    if "match_count" in observation:
        count = observation["match_count"]
        if type(count) is not int or count != 1:
            return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Exactly one matching platform record is required.")
    if "candidates" in observation:
        candidates = observation["candidates"]
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Candidate records are missing or ambiguous.")
        candidate = candidates[0]
        for key in ("account_id", "title", "state", "note_id", "note_url", "content_hash"):
            if key in candidate and candidate[key] != observation.get(key):
                return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Candidate identity disagrees with the observation.")

    if _text(observation.get("account_id")) != _text(attempt["account_id"]):
        return _unknown(attempt, FailureClass.WRONG_ACCOUNT, "Platform record does not identify the intended account.")
    observed_at = _aware_time(observation.get("observed_at"))
    if observed_at is None:
        return _unknown(attempt, FailureClass.INVALID_TIME, "Observation time must include a timezone.")
    if observed_at < submitted_at:
        return _unknown(attempt, FailureClass.STALE_RECORD, "Observation predates this submission attempt.")
    if "record_created_at" in observation:
        created_at = _aware_time(observation["record_created_at"])
        if created_at is None:
            return _unknown(attempt, FailureClass.INVALID_TIME, "Record creation time must include a timezone.")
        if created_at < submitted_at or created_at > observed_at:
            return _unknown(attempt, FailureClass.STALE_RECORD, "Platform record time does not belong to this attempt.")

    state = observation.get("state")
    if not isinstance(state, str) or state not in PLATFORM_STATES:
        return _unknown(attempt, FailureClass.INVALID_OBSERVATION, "An explicit supported platform state is required.")
    note_id = _stable_note_id(observation.get("note_id"))
    note_url, url_note_id = _note_url_id(observation.get("note_url"))
    if "note_id" in observation and not note_id:
        return _unknown(attempt, FailureClass.INVALID_NOTE_IDENTITY, "The platform note ID is invalid or a placeholder.")
    if "note_url" in observation and not note_url:
        return _unknown(attempt, FailureClass.INVALID_NOTE_IDENTITY, "A real HTTPS Xiaohongshu note URL is required.")
    if not note_id and not url_note_id:
        return _unknown(attempt, FailureClass.INVALID_NOTE_IDENTITY, "Stable platform note identity is required.")
    if note_id and url_note_id and note_id != url_note_id:
        return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Platform note ID and URL identify different records.")
    note_id = note_id or url_note_id
    evidence_type = observation.get("evidence_type")
    if not isinstance(evidence_type, str) or evidence_type not in EVIDENCE_TYPES or not _evidence_ref(observation.get("evidence_ref")):
        return _unknown(attempt, FailureClass.MISSING_EVIDENCE, "Captured management-record or note-detail evidence is required.")
    evidence_url, evidence_note_id = _note_url_id(observation.get("evidence_ref"))
    if evidence_url and evidence_note_id != note_id:
        return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Evidence refers to a different note.")
    if _text(observation.get("title")) != _text(attempt["title"]):
        return _unknown(attempt, FailureClass.CONTENT_MISMATCH, "Platform record title does not match the submitted title.")

    observed_hash = _text(observation.get("content_hash"))
    if observed_hash is not None:
        if not _SHA256.fullmatch(observed_hash) or observed_hash.lower() != expected_hash.lower():
            return _unknown(attempt, FailureClass.CONTENT_MISMATCH, "Platform record does not match the canonical content bundle.")
    else:
        verified_match = observation.get("verified_match")
        match_evidence = observation.get("match_evidence")
        if (not isinstance(verified_match, dict)
                or any(verified_match.get(key) is not True for key in ("title", "body", "images", "image_order"))
                or not isinstance(match_evidence, dict)
                or any(not _evidence_ref(match_evidence.get(key)) for key in ("body", "images"))):
            return _unknown(attempt, FailureClass.INSUFFICIENT_MATCH, "Verified title/body/ordered-image readback and evidence are required.")
        for key in ("body", "images"):
            match_url, match_note_id = _note_url_id(match_evidence[key])
            if match_url and match_note_id != note_id:
                return _unknown(attempt, FailureClass.AMBIGUOUS_RECORD, "Readback evidence refers to a different note.")

    rejected = state == "rejected"
    return {
        "status": state,
        "confirmed": True,
        "published": state == "published",
        "retry_allowed": False,
        "error_code": FailureClass.PLATFORM_REJECTED.value if rejected else None,
        "reason": "Platform record confirms rejection." if rejected else "A unique matching platform record confirms this state.",
        "attempt_id": attempt["attempt_id"],
        "note_id": note_id,
        "note_url": note_url,
        "observed_at": observation["observed_at"],
        "evidence_type": observation["evidence_type"],
        "evidence_ref": observation["evidence_ref"],
    }
