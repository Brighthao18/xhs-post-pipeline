"""Bounded loopback client for the verified Xiaohongshu submission backend.

The Runtime and backend journal enforce durable attempt identity. This client
does not create attempts, retry a submission, or verify platform publication.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_SAFE_BACKEND_CODES = frozenset({
    "NOT_AUTHENTICATED", "ACCOUNT_ID_UNREADABLE", "ACCOUNT_RED_ID_UNREADABLE",
    "PROFILE_ACCOUNT_MISMATCH", "PROFILE_NICKNAME_MISMATCH", "LOGIN_NETWORK_RESTRICTED",
    "LOGIN_STATUS_TIMEOUT", "IDENTITY_FAILED", "PREFLIGHT_FAILED", "MANAGEMENT_READBACK_FAILED",
})


class BackendError(ValueError):
    """Safe machine-readable failure without credentials or response content."""

    def __init__(self, code, message, *, phase=None, http_status=None,
                 backend_code=None, evidence_ref=None, record_ref=None):
        super().__init__(message)
        self.code = code
        self.phase = phase if phase in {"NOT_SUBMITTED", "SUBMIT_UNKNOWN"} else None
        self.http_status = http_status
        self.retry_allowed = False
        self.backend_code = backend_code if isinstance(backend_code, str) and backend_code in _SAFE_BACKEND_CODES else None
        # References come only from BackendClient's controlled-file check.
        self.evidence_ref = evidence_ref
        self.record_ref = record_ref


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        fp.close()
        raise BackendError("redirect_rejected", "The authenticated backend request must not redirect.")


class BackendClient:
    """Return backend envelopes, keeping all submission errors non-retryable."""

    def __init__(self, config):
        if not isinstance(config, dict) or config.get("type") != "xiaohongshu_mcp":
            raise BackendError("invalid_backend_config", "A supported submission backend configuration is required.")
        self.base_url = self._base_url(config.get("base_url"))
        timeout = config.get("timeout_seconds", 45)
        maximum = config.get("max_bytes", 2 * 1024 * 1024)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0.05 <= timeout <= 180):
            raise BackendError("invalid_backend_config", "Backend timeout must be finite and bounded.")
        if type(maximum) is not int or not 1024 <= maximum <= 16 * 1024 * 1024:
            raise BackendError("invalid_backend_config", "Backend response size limit is invalid.")
        self.timeout = float(timeout)
        self.max_bytes = maximum
        self._token = self._load_token(config.get("auth_token_path"))
        self._evidence_root = Path(config["auth_token_path"]).parent.resolve() / "session" / "evidence"
        # Environment proxy settings must never receive a local bearer token.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._submitted_attempts = set()
        self._submission_lock = threading.Lock()

    @staticmethod
    def _base_url(value):
        if not isinstance(value, str) or any(ord(char) < 33 for char in value):
            raise BackendError("invalid_backend_url", "Backend URL must identify a loopback HTTP service.")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            host = parsed.hostname
            if (parsed.scheme not in {"http", "https"} or not host
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError("URL")
            # Avoid resolving localhost twice through mutable DNS/proxy settings.
            address = ipaddress.ip_address("127.0.0.1" if host.lower() == "localhost" else host)
            if not address.is_loopback or "%" in host:
                raise ValueError("loopback")
            canonical_host = "[" + str(address) + "]" if address.version == 6 else str(address)
            authority = canonical_host + (":" + str(port) if port is not None else "")
            return urlunsplit((parsed.scheme, authority, "", "", ""))
        except (ValueError, TypeError):
            raise BackendError("invalid_backend_url", "Backend URL must identify a loopback HTTP service.") from None

    @staticmethod
    def _load_token(value):
        try:
            path = Path(value)
            if not path.is_absolute() or not path.is_file():
                raise ValueError("path")
            with path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("size")
            text = raw.decode("utf-8-sig").strip()
            if text.startswith(("{", "[")):
                data = json.loads(text)
                token = data.get("auth_token") if isinstance(data, dict) else None
            else:
                token = text
            if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                    or any(not 33 <= ord(char) <= 126 for char in token)):
                raise ValueError("token")
            return token
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise BackendError("credentials_unavailable", "The private backend authentication token could not be loaded.") from None

    @staticmethod
    def _attempt_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
            raise BackendError("invalid_attempt_id", "A valid backend attempt identity is required.", phase="NOT_SUBMITTED")
        return value

    @staticmethod
    def _phase(value):
        data = value.get("data") if isinstance(value, dict) else None
        phase = data.get("phase") if isinstance(data, dict) else None
        return phase if phase in {"NOT_SUBMITTED", "SUBMIT_UNKNOWN"} else None

    def _controlled_evidence(self, value, suffix):
        if (not isinstance(value, str) or len(value) > 32768 or self._token in value
                or any(ord(char) < 32 for char in value)):
            return None
        try:
            path = Path(value)
            if not path.is_absolute() or path.suffix.lower() != suffix:
                return None
            # Reject external shares and paths before touching their filesystem.
            if not path.is_relative_to(self._evidence_root) or any(":" in part for part in path.parts[1:]):
                return None
            root = self._evidence_root.resolve(strict=True)
            # The evidence directory itself cannot redirect into another tree.
            if root != self._evidence_root:
                return None
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix.lower() != suffix:
                return None
            return str(resolved)
        except (OSError, ValueError, RuntimeError):
            return None

    def _safe_diagnostic(self, value):
        if not isinstance(value, dict):
            return {}
        data = value.get("data")
        data = data if isinstance(data, dict) else {}
        containers = [item for item in (data.get("diagnostic"), data.get("details")) if isinstance(item, dict)]
        codes = [item.get("backend_code", item.get("code")) for item in containers] + [value.get("error")]
        code = next((item for item in codes if isinstance(item, str) and item in _SAFE_BACKEND_CODES), None)
        if code is None:
            return {}
        result = {"backend_code": code}
        for field, suffix in (("evidence_ref", ".png"), ("record_ref", ".json")):
            for container in containers:
                reference = self._controlled_evidence(container.get(field), suffix)
                if reference is not None:
                    result[field] = reference
                    break
        return result

    def _error_details(self, response, default):
        try:
            raw = response.read(min(self.max_bytes, 65536) + 1)
            if len(raw) <= min(self.max_bytes, 65536):
                value = json.loads(raw.decode("utf-8-sig"))
                return {"phase": self._phase(value) or default, **self._safe_diagnostic(value)}
        except (OSError, ValueError, UnicodeError, http.client.HTTPException, RecursionError):
            pass
        return {"phase": default}

    @staticmethod
    def _claims_publication(value):
        if not isinstance(value, dict):
            return False
        for container in (value, value.get("data")):
            if not isinstance(container, dict):
                continue
            if container.get("published") is True or container.get("confirmed") is True:
                return True
            if any(isinstance(container.get(key), str) and container[key].lower() == "published" for key in ("state", "status", "phase")):
                return True
        return False

    def _request(self, method, endpoint, payload=None, *, submitting=False, reject_publication=False):
        phase = "SUBMIT_UNKNOWN" if submitting else None
        body = None
        if payload is not None:
            if not isinstance(payload, dict):
                raise BackendError("invalid_payload", "Backend payload must be a JSON object.", phase="NOT_SUBMITTED")
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(body) > self.max_bytes:
                    raise ValueError("size")
            except (ValueError, TypeError, RecursionError):
                raise BackendError("invalid_payload", "Backend payload cannot be serialized within its size limit.", phase="NOT_SUBMITTED") from None
        request = Request(self.base_url + endpoint, data=body, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                if status != 200:
                    raise BackendError("unexpected_http_status", "The backend did not return a completed JSON response.", phase=phase, http_status=status)
                if (response.headers.get("Content-Encoding") or "identity").lower() != "identity":
                    raise BackendError("unsupported_encoding", "The backend returned an unsupported response encoding.", phase=phase)
                media_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if media_type != "application/json" and not media_type.endswith("+json"):
                    raise BackendError("invalid_response_type", "The backend returned a non-JSON response.", phase=phase)
                declared = response.headers.get("Content-Length")
                try:
                    expected = int(declared) if declared is not None else None
                    if expected is not None and expected < 0:
                        raise ValueError("size")
                except ValueError:
                    raise BackendError("invalid_response_size", "The backend returned an invalid response length.", phase=phase) from None
                if expected is not None and expected > self.max_bytes:
                    raise BackendError("response_too_large", "The backend response exceeds its size limit.", phase=phase)
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise BackendError("response_too_large", "The backend response exceeds its size limit.", phase=phase)
                if expected is not None and len(raw) != expected:
                    raise BackendError("truncated_response", "The backend response was truncated.", phase=phase)
                try:
                    value = json.loads(raw.decode("utf-8-sig"))
                except (ValueError, UnicodeError, RecursionError):
                    raise BackendError("invalid_json_response", "The backend returned invalid JSON.", phase=phase) from None
                if not isinstance(value, dict):
                    raise BackendError("invalid_json_response", "The backend response must be a JSON object.", phase=phase)
                if value.get("success") is not True or not isinstance(value.get("data"), dict):
                    raise BackendError("backend_rejected", "The backend did not confirm completion of this request.",
                                       phase=self._phase(value) or phase, **self._safe_diagnostic(value))
                if (submitting or reject_publication) and self._claims_publication(value):
                    raise BackendError("unverified_publication_state", "A submit response cannot confirm platform publication.", phase=phase)
                return value
        except BackendError as error:
            if submitting and error.phase is None:
                error.phase = "SUBMIT_UNKNOWN"
            raise
        except HTTPError as error:
            status = error.code
            details = self._error_details(error, phase)
            error.close()
            raise BackendError("backend_http_error", "The backend rejected the HTTP request.", http_status=status, **details) from None
        except (TimeoutError, socket.timeout):
            raise BackendError("backend_timeout", "The backend request timed out.", phase=phase) from None
        except URLError as error:
            code = "backend_timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "backend_unavailable"
            raise BackendError(code, "The backend request failed or timed out.", phase=phase) from None
        except http.client.IncompleteRead:
            raise BackendError("truncated_response", "The backend response was truncated.", phase=phase) from None
        except (OSError, http.client.HTTPException):
            raise BackendError("backend_unavailable", "The backend connection failed.", phase=phase) from None

    def health(self):
        return self._request("GET", "/xhs-integration/health")

    def identity(self):
        return self._request("GET", "/xhs-integration/identity")

    def preflight(self, payload):
        return self._request("POST", "/xhs-integration/preflight", payload)

    def submit(self, payload):
        if not isinstance(payload, dict):
            raise BackendError("invalid_payload", "Backend payload must be a JSON object.", phase="NOT_SUBMITTED")
        attempt_id = self._attempt_id(payload.get("attempt_id"))
        with self._submission_lock:
            if attempt_id in self._submitted_attempts:
                raise BackendError("duplicate_submit", "This client has already invoked submission for this attempt.", phase="SUBMIT_UNKNOWN")
            # Mark before network I/O; timeout or malformed data can follow a click.
            self._submitted_attempts.add(attempt_id)
        return self._request("POST", "/xhs-integration/submit", payload, submitting=True)

    def observations(self, attempt_id):
        checked = self._attempt_id(attempt_id)
        return self._request("GET", "/xhs-integration/attempts/" + checked + "/observations", reject_publication=True)

    def management_evidence(self, view="all"):
        if not isinstance(view, str) or view not in {"all", "published", "pending_review", "rejected"}:
            raise BackendError("invalid_management_view", "A supported management view is required.")
        endpoint = "/xhs-integration/management-evidence" + ("?view=" + view if view != "all" else "")
        return self._request("GET", endpoint, reject_publication=True)
