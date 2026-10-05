"""Read-only article discovery and acquisition using the Python standard library.

Successful discovery is the only operation which returns a new cursor. A caller
must persist that cursor in the same transaction as the discovered records.
Feed bodies are preliminary evidence; only ``acquire`` evaluates an actual HTML
article body. Fixture sources are explicitly labelled and never count as live
source verification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET


DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
JSON_FEED_VERSIONS = {
    "https://jsonfeed.org/version/1",
    "https://jsonfeed.org/version/1.1",
}
TRACKING_PARAMETERS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "spm", "from", "isappinstalled",
    "wxfrom", "scene", "subscene", "ascene", "srcid", "sessionid",
    "clicktime", "enterid", "exportkey", "pass_ticket", "devicetype",
    "version", "fontScale", "nettype", "abtest_cookie", "ct", "winzoom",
}
ARTICLE_PARAMETERS = {"__biz", "mid", "appmsgid", "idx", "sn", "chksm"}
VERIFICATION_TEXT = (
    "环境异常", "完成验证", "访问过于频繁", "请完成下方验证",
    "该内容已被发布者删除", "此内容因违规无法查看", "该公众号已迁移",
    "verify you are human", "access denied", "captcha verification",
)


class SourceError(RuntimeError):
    """An actionable error with no URLs, credentials, or server response body."""

    def __init__(self, code: str, message: str, diagnostics: dict | None = None):
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """Normalize article URLs while retaining WeChat identity/access parameters."""
    if not isinstance(url, str) or not url.strip():
        raise SourceError("invalid_url", "A non-empty HTTP or HTTPS URL is required.")
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("scheme")
        if parts.username is not None or parts.password is not None:
            raise ValueError("credentials")
        port = parts.port
        host = parts.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        scheme = parts.scheme.lower()
        if port and not (scheme == "http" and port == 80 or scheme == "https" and port == 443):
            host += ":" + str(port)
        parameters = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key in ARTICLE_PARAMETERS or (
                not key.lower().startswith("utm_") and key not in TRACKING_PARAMETERS
            ):
                parameters.append((key, value))
        parameters.sort()
        return urlunsplit((scheme, host, parts.path or "/", urlencode(parameters), ""))
    except (ValueError, TypeError):
        raise SourceError("invalid_url", "The URL must use HTTP(S) without embedded credentials.") from None


def article_identity(url: str, stable_id: str | None = None) -> str:
    """Prefer WeChat biz/message/index identity, then a feed-provided stable ID."""
    canonical = normalize_url(url)
    parts = urlsplit(canonical)
    query = dict(parse_qsl(parts.query))
    if parts.hostname == "mp.weixin.qq.com":
        biz = query.get("__biz")
        message_id = query.get("mid") or query.get("appmsgid")
        index = query.get("idx")
        if biz and message_id and index:
            return "wechat:" + ":".join((biz, message_id, index))
    identity = stable_id.strip() if isinstance(stable_id, str) and stable_id.strip() else canonical
    prefix = "feed:" if stable_id else "url:"
    return prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's final redirect still needs the same HTTP(S) restriction.
        normalize_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError("werss_redirect_rejected", "A credential-bearing WeRSS request must not redirect.")


def _safe_werss_diagnostics(detail: dict) -> dict:
    """Accept only typed response metadata, excluding untrusted text and secrets."""
    supplied = detail.get("diagnostics")
    if not isinstance(supplied, dict):
        supplied = {}
    safe = {}
    identifier = supplied.get("refresh_id")
    if isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier):
        safe["refresh_id"] = identifier
    count = supplied.get("upstream_request_count")
    if type(count) is int and 0 <= count <= 100:
        safe["upstream_request_count"] = count
    retry = detail.get("retry_not_before_epoch")
    if type(retry) is int and 0 <= retry <= 4102444800:
        safe["retry_not_before_epoch"] = retry
    requests = supplied.get("upstream_requests")
    if isinstance(requests, list):
        observations = []
        allowed_paths = {"/cgi-bin/appmsgpublish", "/cgi-bin/freepublish", "/cgi-bin/appmsg", "/api/mp/cover", "/web/mp/content"}
        allowed_errors = {"upstream_tls_error", "upstream_timeout", "upstream_http_error", "malformed_upstream_response"}
        numeric_ranges = {"request_index": (1, 100), "started_at_epoch": (0, 4102444800),
                          "elapsed_ms": (0, 3600000), "http_status": (100, 599),
                          "base_resp_ret": (-(2**31), 2**31 - 1), "business_error_code": (-(2**31), 2**31 - 1)}
        for observation in requests[:5]:
            if (not isinstance(observation, dict) or not isinstance(observation.get("path"), str)
                    or observation["path"] not in allowed_paths):
                continue
            record = {"path": observation["path"]}
            for key, (low, high) in numeric_ranges.items():
                value = observation.get(key)
                if type(value) is int and low <= value <= high:
                    record[key] = value
            error = observation.get("transport_error")
            if isinstance(error, str) and error in allowed_errors:
                record["transport_error"] = error
            observations.append(record)
        safe["upstream_requests"] = observations
    return safe


def _werss_json(url: str, source: dict, *, token: str | None = None, form: dict | None = None, post: bool = False) -> dict:
    normalize_url(url)
    timeout, maximum = _limits(source)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urlencode(form).encode("utf-8")
    elif post:
        data = b""
    try:
        with build_opener(ProxyHandler({}), _NoCredentialRedirect()).open(Request(url, headers=headers, data=data), timeout=timeout) as response:
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise SourceError("response_too_large", "The WeRSS response exceeds its size limit.")
            result = json.loads(_decode(body, response.headers.get("Content-Type", "")))
            if not isinstance(result, dict):
                raise SourceError("invalid_werss_response", "WeRSS returned an invalid API response.")
            return result
    except SourceError:
        raise
    except HTTPError as error:
        diagnostics = {}
        try:
            failure = json.loads(error.read(min(maximum, 65536)))
            detail = failure.get("detail") if isinstance(failure, dict) else None
            reason = detail.get("code") if isinstance(detail, dict) else None
            if isinstance(detail, dict):
                diagnostics = _safe_werss_diagnostics(detail)
        except (ValueError, OSError, AttributeError):
            reason = None
        status = error.code
        error.close()
        allowed_reasons = {
            "source_auth_required", "source_not_configured_or_disabled", "source_refresh_too_soon",
            "refresh_busy", "upstream_listing_unverified", "upstream_article_census_mismatch",
            "upstream_listing_failed", "unverified_acquisition_mode", "invalid_page_window",
            "invalid_refresh_continuation",
            "source_rate_limited", "source_refresh_cooldown", "background_collection_disabled",
            "source_auth_expired", "source_invalid_arguments", "upstream_tls_error",
            "upstream_timeout", "upstream_http_error", "upstream_access_or_http_error",
            "unverified_upstream_list_structure", "unverified_upstream_batch_structure",
            "malformed_upstream_response", "uncontrolled_collection_disabled",
            "source_identity_mismatch", "upstream_content_unverified", "cookie_refresh_config_invalid",
            "cookie_refresh_browser_unavailable", "cookie_refresh_page_unavailable",
        }
        code = reason if isinstance(reason, str) and reason in allowed_reasons else "werss_access_denied" if status in {401, 403} else "werss_http_error"
        messages = {
            "source_auth_expired": "The upstream source rejected the saved authorization; sign in again.",
            "source_invalid_arguments": "WeChat rejected the article-list request arguments.",
            "source_rate_limited": "WeChat rate-limited the article-list request.",
            "source_refresh_cooldown": "The local collection cooldown has not elapsed; no upstream request was made.",
            "upstream_tls_error": "The upstream HTTPS certificate could not be verified.",
            "upstream_timeout": "The upstream article-list request timed out.",
            "source_identity_mismatch": "The upstream article does not match the configured source.",
            "upstream_content_unverified": "The upstream article body could not be verified; it was not saved as complete.",
            "cookie_refresh_config_invalid": "The managed Cookie refresh configuration is invalid.",
            "cookie_refresh_browser_unavailable": "The dedicated Cookie refresh browser could not start.",
            "cookie_refresh_page_unavailable": "The dedicated Cookie refresh page could not load.",
        }
        raise SourceError(code, messages.get(code, "WeRSS refresh could not verify an authorized upstream article listing."), diagnostics) from None
    except (ValueError, json.JSONDecodeError, RecursionError):
        raise SourceError("invalid_werss_response", "WeRSS returned malformed JSON.") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise SourceError("werss_unavailable", "The WeRSS request failed or timed out.") from None


def _safe_weread_metadata(result: dict) -> dict:
    cookie = result.get("cookie_refresh")
    cookie = cookie if isinstance(cookie, dict) else {}
    renewal = {}
    status = cookie.get("status")
    if isinstance(status, str) and status in {"disabled", "not_due", "refreshed"}:
        renewal["status"] = status
    for key, bounds in (("last_success_epoch", (0, 4102444800)), ("cooldown_hours", (1, 168))):
        value = cookie.get(key)
        if type(value) is int and bounds[0] <= value <= bounds[1]:
            renewal[key] = value
    body = result.get("body_validation")
    body = body if isinstance(body, dict) else {}
    validation = {}
    if body.get("selector") == "#js_content":
        validation["selector"] = "#js_content"
    for key in ("readable_characters", "image_count"):
        value = body.get(key)
        if type(value) is int and 0 <= value <= DEFAULT_MAX_BYTES:
            validation[key] = value
    if type(body.get("cached")) is bool:
        validation["cached"] = body["cached"]
    value = body.get("html_sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        validation["html_sha256"] = value
    return {"cookie_refresh": renewal, "body_validation": validation}


def refresh_werss(source: dict) -> dict:
    """Refresh upstream before reading RSS, accepting only observed list proof.

    Configuration: ``refresh: {type: werss, base_url: http://127.0.0.1:8001,
    mp_id: MP_WXS_..., credentials_path: <private JSON>, start_page: 0,
    end_page: 1}``. Requires the strict local bootstrap endpoint; upstream's
    stock success/zero response is deliberately insufficient.
    """
    config = source.get("refresh") if isinstance(source, dict) else None
    if not isinstance(config, dict) or config.get("type") != "werss":
        raise SourceError("invalid_werss_config", "An explicit WeRSS refresh configuration is required.")
    mode = config.get("acquisition_mode", "web")
    if mode not in {"web", "weread_mp"}:
        raise SourceError("invalid_werss_config", "The configured WeRSS acquisition mode is unsupported.")
    base = normalize_url(config.get("base_url", "")).rstrip("/")
    if urlsplit(base).query or urlsplit(base).path not in {"", "/"}:
        raise SourceError("invalid_werss_config", "The WeRSS base URL must identify the service root.")
    mp_id = config.get("mp_id")
    if not isinstance(mp_id, str) or not re.fullmatch(r"[A-Za-z0-9_+=-]{1,200}", mp_id):
        raise SourceError("invalid_werss_config", "A stable WeRSS公众号 ID is required.")
    try:
        start = int(config.get("start_page", 0))
        end = int(config.get("end_page", 1))
        if not 0 <= start < end <= 1001 or end - start > 5:
            raise ValueError("window")
    except (TypeError, ValueError, OverflowError):
        raise SourceError("invalid_werss_config", "The WeRSS page window is invalid.") from None
    if mode == "weread_mp" and (start != 0 or end != 1 or config.get("continuation_token") is not None):
        raise SourceError("invalid_werss_config", "Latest-only WeRead collection cannot request historical pages.")
    prefix = config.get("api_prefix", "/api/v1/wx")
    if not isinstance(prefix, str) or not re.fullmatch(r"/[A-Za-z0-9_/-]+", prefix):
        raise SourceError("invalid_werss_config", "The WeRSS API prefix is invalid.")
    try:
        with Path(config.get("credentials_path", "")).open("r", encoding="utf-8") as handle:
            credentials = json.loads(handle.read(16385))
        if not isinstance(credentials, dict) or any(not isinstance(credentials.get(key), str) or not credentials[key] for key in ("username", "password")):
            raise ValueError("credentials")
    except (OSError, ValueError, TypeError):
        raise SourceError("werss_credentials_unavailable", "The private WeRSS administrator credentials could not be loaded.") from None
    transport_source = {**source, "timeout_seconds": config.get("timeout_seconds", 60)}
    token_response = _werss_json(base + prefix.rstrip("/") + "/auth/token", transport_source, form={key: credentials[key] for key in ("username", "password")})
    credentials.clear()
    token = token_response.get("access_token")
    if not isinstance(token, str) or not token:
        raise SourceError("werss_access_denied", "The WeRSS administrator account could not authenticate.")
    token_response.clear()
    query = {"start_page": start, "end_page": end}
    if config.get("continuation_token") is not None:
        continuation = config["continuation_token"]
        if not isinstance(continuation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", continuation):
            raise SourceError("invalid_werss_config", "The WeRSS pagination continuation is invalid.")
        query["continuation_token"] = continuation
    result = _werss_json(base + "/xhs-integration/refresh/" + mp_id + "?" + urlencode(query), transport_source, token=token, post=True)
    token = None
    records = result.get("articles")
    metadata = _safe_weread_metadata(result)
    body_proof = metadata["body_validation"]
    proof = (result.get("verified_upstream_listing") is True if mode == "web" else
             result.get("verified_latest_article") is True and result.get("verified_upstream_listing") is False
             and result.get("coverage_mode") == "latest_one_only"
             and result.get("supports_backfill") is False and result.get("supports_multi_article") is False
             and result.get("end_of_list") is False and result.get("continuation_token") is None
             and result.get("observed_articles") == 1
             and body_proof.get("selector") == "#js_content" and body_proof.get("readable_characters", 0) >= 80
             and "html_sha256" in body_proof)
    if (result.get("ok") is not True or not proof
            or result.get("source_id") != mp_id or result.get("acquisition_mode") != mode
            or type(result.get("valid_pages")) is not int or result["valid_pages"] != end - start
            or not isinstance(records, list) or type(result.get("observed_articles")) is not int
            or result["observed_articles"] != len(records)):
        raise SourceError("upstream_listing_unverified", "WeRSS did not provide a verified upstream listing census.")
    for item in records:
        if not isinstance(item, dict):
            raise SourceError("invalid_werss_response", "WeRSS returned an invalid article record.")
        published = item.get("published_at")
        if isinstance(published, (int, float)) or isinstance(published, str) and published.isdigit():
            try:
                item["published_at"] = datetime.fromtimestamp(float(published), timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                raise SourceError("invalid_werss_response", "WeRSS returned an invalid article publication time.") from None
    return {
        "verified_upstream_listing": mode == "web",
        "verified_latest_article": mode == "weread_mp",
        "source_id": mp_id,
        "valid_pages": result["valid_pages"],
        "observed_articles": len(records),
        "acquisition_mode": mode,
        "coverage_mode": "latest_one_only" if mode == "weread_mp" else "upstream_listing",
        "articles": records,
        "end_of_list": result.get("end_of_list") is True,
        "continuation_token": result.get("continuation_token"),
        "refreshed_at": _utc_now(),
        "diagnostics": _safe_werss_diagnostics(result),
        **metadata,
    }


def _refresh_with_coverage(source: dict, seen: dict) -> dict:
    """Scan real upstream pages until a saved ID or explicit list end is seen."""
    config = source["refresh"]
    if config.get("acquisition_mode", "web") == "weread_mp":
        result = refresh_werss(source)
        return {**result, "distinct_observed_articles": 1, "pages_scanned": 1,
                "coverage_probe": "latest_cover_only", "historical_interval_verified": False}
    try:
        budget = int(config.get("max_pages_per_poll", 5))
        start = int(config.get("start_page", 0))
        initial_end = int(config.get("end_page", 1))
        window = initial_end - start
        if not 1 <= budget <= 5 or not 1 <= window <= budget or (seen and start != 0):
            raise ValueError("coverage")
    except (TypeError, ValueError, OverflowError):
        raise SourceError("invalid_werss_config", "The upstream coverage window or page budget is invalid.") from None
    current_source = {**source, "refresh": dict(config)}
    merged: dict[str, dict] = {}
    pages_scanned = 0
    observed_count = 0
    while True:
        try:
            result = refresh_werss(current_source)
        except SourceError as error:
            if pages_scanned:
                raise SourceError("source_coverage_gap", "Upstream pagination stopped before the prior discovery cursor was reached.", {
                    "source_id": source["id"], "pages_scanned": pages_scanned, "page_budget": budget,
                    "observed_article_ids": list(merged), "upstream_error_code": error.code,
                }) from None
            raise
        pages_scanned += result["valid_pages"]
        observed_count += result["observed_articles"]
        overlap = False
        for record in result["articles"]:
            item = _article(source, record, source["url"])
            identifier = item["article_id"]
            merged[identifier] = record
            if identifier in seen:
                overlap = True
        coverage_probe = "initial_baseline_window" if not seen else "prior_cursor_overlap" if overlap else "explicit_list_end" if result.get("end_of_list") else None
        if coverage_probe:
            return {
                **result, "articles": list(merged.values()), "observed_articles": observed_count,
                "valid_pages": pages_scanned,
                "distinct_observed_articles": len(merged), "pages_scanned": pages_scanned,
                "coverage_probe": coverage_probe, "continuation_token": None,
            }
        continuation = result.get("continuation_token")
        if pages_scanned >= budget or not continuation:
            raise SourceError("source_coverage_gap", "The verified upstream page window did not reach the prior discovery cursor or list end.", {
                "source_id": source["id"], "pages_scanned": pages_scanned, "page_budget": budget,
                "observed_article_ids": list(merged), "continuation_available": bool(continuation),
            })
        next_start = int(current_source["refresh"].get("end_page", initial_end))
        next_end = next_start + min(window, budget - pages_scanned)
        current_source = {
            **source,
            "refresh": {**config, "start_page": next_start, "end_page": next_end,
                        "continuation_token": continuation},
        }


def _limits(source: dict) -> tuple[float, int]:
    try:
        timeout = float(source.get("timeout_seconds", DEFAULT_TIMEOUT))
        maximum = int(source.get("max_bytes", DEFAULT_MAX_BYTES))
        if not 0.1 <= timeout <= 60 or not 1024 <= maximum <= 16 * 1024 * 1024:
            raise ValueError("limits")
        return timeout, maximum
    except (ValueError, TypeError, OverflowError):
        raise SourceError("invalid_limits", "Source timeout or response size limit is invalid.") from None


def _fetch(url: str, source: dict) -> tuple[bytes, str, str]:
    """Fetch bounded HTTP(S) data, allowing configured local WeRSS endpoints.

    No Cookie header, cookie jar, account session, or authentication is used.
    URLs from article data cannot request local files or non-HTTP protocols.
    """
    checked = normalize_url(url)
    timeout, maximum = _limits(source)
    # Fetch the original query: a tracking field can still be needed for access.
    request_url = urlunsplit(urlsplit(url.strip())._replace(fragment=""))
    request = Request(request_url, headers={
        "User-Agent": "xhs-post-source-reader/2.0",
        "Accept": "application/atom+xml, application/rss+xml, application/feed+json, text/html, */*;q=0.5",
        "Accept-Encoding": "identity",
    })
    try:
        with build_opener(_SafeRedirect()).open(request, timeout=timeout) as response:
            final_url = normalize_url(response.geturl())
            encoding = (response.headers.get("Content-Encoding") or "identity").lower()
            if encoding not in {"identity", ""}:
                raise SourceError("unsupported_encoding", "The source sent an unsupported compressed response.")
            declared_size = response.headers.get("Content-Length")
            if declared_size:
                try:
                    if int(declared_size) > maximum:
                        raise SourceError("response_too_large", "The source response exceeds the configured size limit.")
                except ValueError:
                    pass
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise SourceError("response_too_large", "The source response exceeds the configured size limit.")
            if not body.strip():
                raise SourceError("empty_response", "The source returned an empty response.")
            return body, response.headers.get("Content-Type", ""), final_url or checked
    except SourceError:
        raise
    except HTTPError as error:
        status = error.code
        error.close()
        if status in {401, 403}:
            code = "source_access_denied"
        elif status == 429:
            code = "source_rate_limited"
        else:
            code = "source_http_error"
        raise SourceError(code, "The source HTTP request failed (status " + str(status) + ").") from None
    except (URLError, TimeoutError, socket.timeout, OSError, ValueError):
        raise SourceError("source_unavailable", "The source request failed or timed out.") from None


def _read_fixture(path: str | Path, source: dict) -> bytes:
    _, maximum = _limits(source)
    try:
        with Path(path).open("rb") as handle:
            body = handle.read(maximum + 1)
    except (OSError, ValueError, TypeError):
        raise SourceError("fixture_unavailable", "The configured fixture could not be read.") from None
    if len(body) > maximum:
        raise SourceError("response_too_large", "The fixture exceeds the configured size limit.")
    if not body.strip():
        raise SourceError("empty_response", "The configured fixture is empty.")
    return body


def _decode(body: bytes, content_type: str = "") -> str:
    charset = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_.-]+)", content_type, re.I)
    if not charset:
        charset = re.search(r"<meta[^>]+charset\s*=\s*[\"']?([a-zA-Z0-9_.-]+)", body[:8192].decode("ascii", "ignore"), re.I)
    codec = charset.group(1) if charset else "utf-8-sig"
    try:
        return body.decode(codec)
    except (UnicodeError, LookupError):
        raise SourceError("decode_failed", "The source text encoding could not be decoded.") from None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _text(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _inner_content(element: ET.Element | None) -> str:
    if element is None:
        return ""
    if len(element):
        return (element.text or "") + "".join(ET.tostring(child, encoding="unicode") for child in element)
    return element.text or ""


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
        if not self.skip and tag in {"p", "div", "section", "br", "li", "tr", "h1", "h2", "h3"}:
            self.fragments.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1
        if not self.skip and tag in {"p", "div", "section", "li", "tr", "h1", "h2", "h3"}:
            self.fragments.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.fragments.append(data)


def _plain_text(markup: str) -> str:
    parser = _PlainText()
    parser.feed(markup or "")
    return _normalize_text("".join(parser.fragments))


def _normalize_text(text: str) -> str:
    """Normalize extracted text without interpreting literal '<...>' as HTML."""
    lines = [re.sub(r"[\t\r \u00a0]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _article(source: dict, data: dict, base_url: str) -> dict:
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise SourceError("invalid_feed_item", "A feed item has no usable article URL.")
    normalized = normalize_url(urljoin(base_url, url.strip()))
    title = data.get("title", "")
    if not isinstance(title, str) or not title.strip():
        raise SourceError("invalid_feed_item", "A feed item has no usable article title.")
    stable_id = data.get("id") or data.get("article_id")
    if stable_id is not None and not isinstance(stable_id, str):
        raise SourceError("invalid_feed_item", "A feed item identifier has an invalid type.")
    content = data.get("content", "")
    if not isinstance(content, str):
        raise SourceError("invalid_feed_item", "A feed item body has an invalid type.")
    result = {
        "source_id": source["id"],
        "article_id": article_identity(normalized, stable_id),
        "url": normalized,
        "title": _plain_text(title),
        "author": str(data.get("author") or "").strip(),
        "published_at": str(data.get("published_at") or "").strip(),
        "content": _plain_text(content),
        "coverage": "partial",
    }
    if source.get("type") == "fixture":
        result["is_fixture"] = True
        for key in ("html", "html_path"):
            if isinstance(data.get(key), str):
                result[key] = data[key]
    return result


def _parse_xml(body: bytes, source: dict, base_url: str) -> list[dict]:
    # Avoid entity/DTD expansion, including declarations in UTF-16 feeds.
    probe = body.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise SourceError("unsafe_xml", "XML entity or document type declarations are not accepted.")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise SourceError("invalid_feed", "The source returned malformed XML.") from None
    kind = _local_name(root.tag)
    items: list[dict] = []
    if kind == "rss" and source["type"] == "rss":
        channel = _child(root, "channel")
        if channel is None or not _text(_child(channel, "title")):
            raise SourceError("invalid_feed", "The source is missing a valid RSS channel.")
        for item in channel:
            if _local_name(item.tag) != "item":
                continue
            content = _child(item, "encoded")
            if content is None:
                content = _child(item, "description")
            guid = _text(_child(item, "guid"))
            link = _text(_child(item, "link"))
            if not link and guid.lower().startswith(("https://", "http://")):
                link = guid
            items.append(_article(source, {
                "id": guid,
                "url": link,
                "title": _text(_child(item, "title")),
                "author": _text(_child(item, "creator")) or _text(_child(item, "author")),
                "published_at": _text(_child(item, "pubDate")),
                "content": _inner_content(content),
            }, base_url))
    elif root.tag == "{" + ATOM_NAMESPACE + "}feed" and source["type"] == "atom":
        if not _text(_child(root, "title")):
            raise SourceError("invalid_feed", "The source is missing a valid Atom feed title.")
        for entry in root:
            if _local_name(entry.tag) != "entry":
                continue
            links = [child for child in entry if _local_name(child.tag) == "link"]
            alternate = next((link for link in links if link.get("rel", "alternate") == "alternate" and link.get("type", "text/html") in {"text/html", "application/xhtml+xml"}), None)
            author_element = _child(entry, "author")
            if author_element is None:
                author_element = _child(root, "author")
            content = _child(entry, "content")
            if content is None:
                content = _child(entry, "summary")
            entry_base = entry.get("{http://www.w3.org/XML/1998/namespace}base", base_url)
            items.append(_article(source, {
                "id": _text(_child(entry, "id")),
                "url": alternate.get("href", "") if alternate is not None else "",
                "title": _text(_child(entry, "title")),
                "author": _text(_child(author_element, "name")) if author_element is not None else "",
                "published_at": _text(_child(entry, "published")) or _text(_child(entry, "updated")),
                "content": _inner_content(content),
            }, urljoin(base_url, entry_base)))
    else:
        raise SourceError("invalid_feed", "The document does not match the configured RSS or Atom feed type.")
    return items


def _parse_json(body: bytes, source: dict, base_url: str, content_type: str) -> list[dict]:
    try:
        data = json.loads(_decode(body, content_type))
    except (json.JSONDecodeError, RecursionError):
        raise SourceError("invalid_feed", "The source returned malformed JSON.") from None
    fixture = source["type"] == "fixture"
    if not isinstance(data, dict):
        raise SourceError("invalid_feed", "The source must contain a JSON object.")
    if not fixture and (data.get("version") not in JSON_FEED_VERSIONS or not isinstance(data.get("title"), str) or not data["title"].strip()):
        raise SourceError("invalid_feed", "The source is not a supported JSON Feed.")
    records = data.get("articles") if fixture else data.get("items")
    if not isinstance(records, list):
        raise SourceError("invalid_feed", "The source is missing an article list.")
    result = []
    for item in records:
        if not isinstance(item, dict):
            raise SourceError("invalid_feed_item", "A feed item must be an object.")
        if fixture:
            result.append(_article(source, item, base_url))
            continue
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise SourceError("invalid_feed_item", "A JSON Feed item has no stable identifier.")
        authors = item.get("authors")
        if authors is None and isinstance(item.get("author"), dict):
            authors = [item["author"]]
        author = ", ".join(person.get("name", "") for person in (authors or []) if isinstance(person, dict) and isinstance(person.get("name"), str))
        result.append(_article(source, {
            "id": item["id"],
            "url": item.get("url") or item.get("external_url"),
            "title": item.get("title"),
            "author": author,
            "published_at": item.get("date_published") or item.get("date_modified"),
            "content": item.get("content_html") or item.get("content_text") or item.get("summary", ""),
        }, base_url))
    return result


def _source_config(source: dict) -> None:
    if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"].strip():
        raise SourceError("invalid_source", "A configured source ID is required.")
    if source.get("type") not in {"rss", "atom", "json_feed", "fixture"}:
        raise SourceError("invalid_source", "The configured source type is unsupported.")
    caps = source.get("capabilities", {})
    if not isinstance(caps, dict):
        raise SourceError("invalid_source", "Source capabilities must be an object.")
    for key in ("supports_listing", "supports_backfill", "supports_multi_article", "backfill", "multi_article"):
        if key in caps and not isinstance(caps[key], bool):
            raise SourceError("invalid_source", "Source capability flags must be booleans.")


def discover(source: dict, cursor: dict | None = None) -> dict:
    """Return new/revised articles, a new cursor, and declared source capability.

    Every overlapping poll parses the complete returned feed. Missing articles
    are not considered deleted, and advertised backfill is not claimed as
    verified. Malformed items fail the poll rather than silently advancing it.
    """
    _source_config(source)
    fixture = source["type"] == "fixture"
    if cursor is not None and not isinstance(cursor, dict):
        raise SourceError("invalid_cursor", "The discovery cursor must be an object.")
    next_cursor = copy.deepcopy(cursor or {})
    seen = next_cursor.get("seen", {})
    if not isinstance(seen, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in seen.items()):
        raise SourceError("invalid_cursor", "The cursor's seen entries are invalid.")
    refresh_result = None
    if source.get("refresh"):
        if fixture:
            raise SourceError("invalid_source", "Fixture discovery cannot trigger live upstream refresh.")
        refresh_result = _refresh_with_coverage(source, seen)
    if fixture:
        if not source.get("path"):
            raise SourceError("invalid_source", "A fixture source requires an explicit local path.")
        body = _read_fixture(source["path"], source)
        content_type = "application/json; charset=utf-8"
        base_url = source.get("url") or "https://fixture.invalid/"
    else:
        if source.get("path"):
            raise SourceError("invalid_source", "Local paths are accepted only for explicit fixture sources.")
        body, content_type, base_url = _fetch(source.get("url", ""), source)
    items = _parse_json(body, source, base_url, content_type) if source["type"] in {"json_feed", "fixture"} else _parse_xml(body, source, base_url)
    if refresh_result is not None:
        # RSS item limits must not discard items just observed in a larger batch.
        identifiers = {item["article_id"] for item in items}
        urls = {item["url"] for item in items}
        for record in refresh_result["articles"]:
            refreshed_article = _article(source, record, base_url)
            if refreshed_article["article_id"] not in identifiers and refreshed_article["url"] not in urls:
                items.append(refreshed_article)
                identifiers.add(refreshed_article["article_id"])
                urls.add(refreshed_article["url"])
            else:
                # Stock RSS may omit author or carry stale cover metadata.
                # Retain its body while using fields actually observed upstream.
                matched = next(item for item in items if item["article_id"] == refreshed_article["article_id"] or item["url"] == refreshed_article["url"])
                for key in ("title", "author", "published_at"):
                    if refreshed_article.get(key):
                        matched[key] = refreshed_article[key]
    new_items = []
    poll_ids: dict[str, str] = {}
    for item in items:
        digest = hashlib.sha256(json.dumps({key: item[key] for key in ("title", "author", "published_at", "content", "url")}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        identifier = item["article_id"]
        if identifier in poll_ids:
            if poll_ids[identifier] != digest:
                raise SourceError("conflicting_feed_items", "The feed contains conflicting records for the same article.")
            continue
        poll_ids[identifier] = digest
        item["source_fingerprint"] = digest
        if seen.get(identifier) != digest:
            new_items.append(item)
        seen[identifier] = digest
    next_cursor["seen"] = seen
    next_cursor["last_success_at"] = _utc_now()
    next_cursor["last_item_count"] = len(poll_ids)
    configured = source.get("capabilities", {})
    capabilities = {
        "supports_listing": configured.get("supports_listing", True),
        "supports_backfill": configured.get("supports_backfill", configured.get("backfill", False)),
        "supports_multi_article": configured.get("supports_multi_article", configured.get("multi_article", False)),
        "coverage_mode": configured.get("coverage_mode", "fixture" if fixture else "feed_only"),
        "declared_capabilities_verified": False,
        "is_fixture": fixture,
        "observed_article_count": len(poll_ids),
    }
    if refresh_result and refresh_result.get("acquisition_mode") == "weread_mp":
        capabilities.update(supports_listing=False, supports_backfill=False, supports_multi_article=False,
                            coverage_mode="latest_one_only", latest_article_verified=True)
    result = {"articles": new_items, "cursor": next_cursor, "capabilities": capabilities}
    if refresh_result is not None:
        result["upstream_refresh"] = {key: value for key, value in refresh_result.items() if key not in {"articles", "continuation_token"}}
    return result


class _ArticleHTML(HTMLParser):
    """Collect only identified article containers; do not use the whole page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.candidates: list[dict] = []
        self.active: list[dict] = []
        self.metadata: dict[str, str] = {}
        self.all_text: list[str] = []
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.author_parts: list[str] = []
        self.canonical_url = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        identifier = attrs.get("id", "")
        hidden = any(frame["hidden"] for frame in self.stack) or tag in {"script", "style", "noscript", "template"} or "hidden" in attrs or "display:none" in re.sub(r"\s+", "", attrs.get("style", "")).lower()
        frame = {"tag": tag, "hidden": hidden, "title": tag == "title", "heading": identifier == "activity-name", "author": identifier == "js_name", "candidate": None}
        if not hidden and (identifier == "js_content" or tag == "article"):
            candidate = {"selector": "#js_content" if identifier == "js_content" else "article", "text": [], "blocks": 0, "images": 0, "embedded": 0, "truncated": False}
            self.candidates.append(candidate)
            self.active.append(candidate)
            frame["candidate"] = candidate
        if tag == "meta":
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            if name and attrs.get("content"):
                self.metadata[name] = attrs["content"]
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonical_url = attrs.get("href", "")
        if not hidden:
            for candidate in self.active:
                if tag in {"p", "div", "section", "br", "li", "tr", "h1", "h2", "h3"}:
                    candidate["text"].append("\n")
                    if tag in {"p", "li", "tr", "h1", "h2", "h3"}:
                        candidate["blocks"] += 1
                if tag == "img":
                    candidate["images"] += 1
                if tag in {"iframe", "canvas", "video", "audio"}:
                    candidate["embedded"] += 1
                if attrs.get("data-truncated") == "true" or "read-more" in attrs.get("class", "").split():
                    candidate["truncated"] = True
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(frame)

    def handle_startendtag(self, tag, attrs):
        before = len(self.stack)
        self.handle_starttag(tag, attrs)
        if len(self.stack) > before:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        match = next((index for index in range(len(self.stack) - 1, -1, -1) if self.stack[index]["tag"] == tag), None)
        if match is None:
            return
        frames = self.stack[match:]
        del self.stack[match:]
        for frame in frames:
            if frame["candidate"] is not None and frame["candidate"] in self.active:
                self.active.remove(frame["candidate"])
        for candidate in self.active:
            if tag in {"p", "div", "section", "li", "tr", "h1", "h2", "h3"}:
                candidate["text"].append("\n")

    def handle_data(self, data):
        if any(frame["hidden"] for frame in self.stack):
            return
        self.all_text.append(data)
        if any(frame["title"] for frame in self.stack):
            self.title_parts.append(data)
        if any(frame["heading"] for frame in self.stack):
            self.heading_parts.append(data)
        if any(frame["author"] for frame in self.stack):
            self.author_parts.append(data)
        for candidate in self.active:
            candidate["text"].append(data)


def _wechat_html_identity(markup: str, url: str) -> str | None:
    if urlsplit(url).hostname != "mp.weixin.qq.com":
        return None
    values: dict[str, str] = {}
    for name in ("biz", "mid", "appmsgid", "idx"):
        assignment = re.search(r"\bvar\s+" + name + r"\s*=\s*([^;\n]{1,400})", markup)
        if assignment:
            candidates = re.findall(r"[\"']([A-Za-z0-9+/=_-]*)[\"']", assignment.group(1))
            values[name] = next((value for value in candidates if value), "")
    biz = values.get("biz")
    mid = values.get("mid") or values.get("appmsgid")
    idx = values.get("idx")
    if biz and mid and mid.isdigit() and idx and idx.isdigit():
        return "wechat:" + ":".join((biz, mid, idx))
    return None


def acquire(article: dict, source: dict) -> dict:
    """Read an article, preserving incomplete bodies as explicit diagnostics.

    ``full`` requires a real #js_content/article container, sufficient readable
    body, and no known truncation, inaccessible embed, or verification response.
    It describes text extraction coverage, not semantic truth or image OCR.
    """
    if not isinstance(article, dict) or not isinstance(source, dict):
        raise SourceError("invalid_article", "Article and source configuration must be objects.")
    original_url = normalize_url(article.get("url", ""))
    fixture = source.get("type") == "fixture"
    if fixture:
        if isinstance(article.get("html"), str) and article["html"].strip():
            body = article["html"].encode("utf-8")
            _, maximum = _limits(source)
            if len(body) > maximum:
                raise SourceError("response_too_large", "The fixture exceeds the configured size limit.")
        elif article.get("html_path"):
            html_path = Path(article["html_path"])
            if not html_path.is_absolute():
                if not source.get("path"):
                    raise SourceError("invalid_source", "Relative fixture HTML requires a fixture source path.")
                html_path = Path(source["path"]).parent / html_path
            body = _read_fixture(html_path, source)
        else:
            # A fixture content field can still exercise partial-source recovery.
            body = ("<html><body>" + str(article.get("content", "")) + "</body></html>").encode("utf-8")
        content_type = "text/html; charset=utf-8"
        final_url = original_url
    else:
        body, content_type, final_url = _fetch(article.get("url", ""), source)
    markup = _decode(body, content_type)
    return extract_html(
        markup, final_url, article=article, source=source, is_fixture=fixture,
        source_html_sha256=hashlib.sha256(body).hexdigest(),
        acquisition_method="fixture" if fixture else "http",
        acquired_at=_utc_now(),
    )


def extract_html(
    html: str,
    url: str,
    *,
    article: dict | None = None,
    source: dict | None = None,
    is_fixture: bool = False,
    source_html_sha256: str | None = None,
    acquisition_method: str = "provided_html",
    acquired_at: str | None = None,
) -> dict:
    """Extract supplied raw HTML without inventing its retrieval provenance.

    A caller importing connector-provided evidence must separately persist the
    connector's actual method, evidence file, URL, and retrieval time. This
    function makes no network requests and records ``parsed_at`` only; ``full``
    is an article text structure assessment, not proof of retrieval or truth.
    """
    if not isinstance(html, str):
        raise SourceError("invalid_html", "Article HTML must be text.")
    markup = html
    final_url = normalize_url(url)
    article = article or {}
    source = source or {}
    fixture = is_fixture
    _, maximum = _limits(source)
    if len(markup.encode("utf-8")) > maximum:
        raise SourceError("response_too_large", "The supplied HTML exceeds the configured size limit.")
    parser = _ArticleHTML()
    try:
        parser.feed(markup)
        parser.close()
    except (ValueError, RecursionError):
        raise SourceError("invalid_html", "The article HTML could not be parsed.") from None
    candidates = sorted(parser.candidates, key=lambda candidate: (candidate["selector"] == "#js_content", len("".join(candidate["text"]))), reverse=True)
    selected = candidates[0] if candidates else None
    content = _normalize_text("".join(selected["text"])) if selected else ""
    visible_page = " ".join(parser.all_text).lower()
    title = "".join(parser.heading_parts).strip() or parser.metadata.get("og:title") or "".join(parser.title_parts).strip() or article.get("title", "")
    author = "".join(parser.author_parts).strip() or parser.metadata.get("author") or article.get("author", "")
    issues: list[str] = []
    if any(marker in visible_page for marker in VERIFICATION_TEXT):
        issues.append("verification_or_unavailable_page")
    if selected is None:
        issues.append("article_body_not_found")
    try:
        minimum = int(source.get("minimum_text_chars", 80))
        if not 40 <= minimum <= 10000:
            raise ValueError("minimum")
    except (ValueError, TypeError, OverflowError):
        raise SourceError("invalid_source", "The article minimum text size is invalid.") from None
    text_chars = len(re.sub(r"\s", "", content))
    if text_chars < minimum:
        issues.append("insufficient_readable_text")
    if selected and selected["truncated"]:
        issues.append("declared_truncation")
    if selected and selected["embedded"]:
        issues.append("embedded_content_requires_review")
    if selected and selected["images"] and text_chars < minimum:
        issues.append("image_text_requires_ocr")
    if selected and selected in parser.active:
        issues.append("unclosed_article_body")
    # A canonical link is metadata, not a reason to issue another HTTP request.
    canonical_url = final_url
    if parser.canonical_url:
        try:
            proposed = normalize_url(urljoin(final_url, parser.canonical_url))
            if urlsplit(proposed).hostname == urlsplit(final_url).hostname:
                canonical_url = proposed
        except SourceError:
            issues.append("invalid_canonical_metadata")
    canonical_identity = _wechat_html_identity(markup, final_url) or article_identity(canonical_url, article.get("article_id"))
    if article.get("article_id") and not canonical_identity.startswith("wechat:"):
        canonical_identity = article["article_id"]
    result = {key: value for key, value in article.items() if key not in {"html", "html_path"}}
    result.update({
        "source_id": article.get("source_id") or source.get("id", ""),
        "article_id": article.get("article_id") or canonical_identity,
        "canonical_article_id": canonical_identity,
        "url": canonical_url,
        "title": _normalize_text(str(title)),
        "author": _normalize_text(str(author)),
        "published_at": parser.metadata.get("article:published_time") or article.get("published_at", ""),
        "content": content,
        "coverage": "partial" if issues else "full",
        "parsed_at": _utc_now(),
        "acquisition_method": acquisition_method,
        "body_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "source_html_sha256": source_html_sha256 or hashlib.sha256(markup.encode("utf-8")).hexdigest(),
        "is_fixture": fixture,
        "diagnostics": {
            "issues": issues,
            "body_selector": selected["selector"] if selected else None,
            "readable_characters": text_chars,
            "image_count": selected["images"] if selected else 0,
            "embedded_count": selected["embedded"] if selected else 0,
            "block_count": selected["blocks"] if selected else 0,
            "image_ocr_performed": False,
            "live_source_verified": acquisition_method == "http" and not fixture,
        },
    })
    if acquired_at is not None:
        result["acquired_at"] = acquired_at
    return result
