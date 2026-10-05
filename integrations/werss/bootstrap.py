"""Run WeRSS's real ASGI app without main.py's environment dump or schedulers."""

import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class _SensitiveLineFilter:
    """Keep third-party diagnostic output while suppressing credential lines."""

    marker = re.compile(r"token|cookie|secret|password|authorization|sessionid|pass_ticket|x-wr-ticket|\[参数\]", re.I)

    def __init__(self, stream):
        self.stream = stream
        self.buffer = ""
        self.lock = threading.RLock()

    def write(self, value):
        with self.lock:
            self.buffer += str(value)
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                if self.marker.search(line):
                    line = "[WeRSS private credential output suppressed]"
                self.stream.write(line + "\n")
            self.stream.flush()
        return len(value)

    def flush(self):
        with self.lock:
            if self.buffer:
                self.stream.write("[WeRSS private credential output suppressed]" if self.marker.search(self.buffer) else self.buffer)
                self.buffer = ""
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


sys.stdout = _SensitiveLineFilter(sys.stdout)
sys.stderr = _SensitiveLineFilter(sys.stderr)

os.chdir("/app")
sys.path.insert(0, "/app")

import requests

# web mode explicitly passes verify=False upstream. Enforce normal certificate
# verification for HTTPS without altering the pinned image's files.
_original_http_request = requests.Session.request
_refresh_evidence = threading.local()
_integration_health_path = Path("/app/data/xhs-integration-health.json")


class _UpstreamFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _health_record(**updates):
    try:
        record = json.loads(_integration_health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    record.update(updates)
    record["checked_at_epoch"] = int(time.time())
    temporary = _integration_health_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    temporary.replace(_integration_health_path)
    return record


def _upstream_failure(code):
    evidence = getattr(_refresh_evidence, "record", None)
    if evidence is not None:
        evidence["errors"].append(code)
    if code == "source_rate_limited":
        _health_record(last_error_code=code, retry_not_before_epoch=int(time.time()) + 900)
    raise _UpstreamFailure(code)


def _refresh_diagnostics(evidence):
    return {"refresh_id": evidence.get("refresh_id"),
            "upstream_request_count": evidence.get("upstream_request_count", 0),
            "upstream_requests": evidence.get("upstream_requests", [])}


def _record_upstream_result(evidence, observation, started, *, status=None, result_code=None, error=None, business_code=None):
    # Store only typed response metadata. Never include headers, query strings,
    # arbitrary err_msg text, or response bodies containing session credentials.
    observation["elapsed_ms"] = max(0, round((time.monotonic() - started) * 1000))
    observation["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
    observation["base_resp_ret"] = result_code if type(result_code) is int and -(2**31) <= result_code < 2**31 else None
    observation["transport_error"] = error
    if type(business_code) is int and -(2**31) <= business_code < 2**31:
        observation["business_error_code"] = business_code
    _health_record(last_refresh_id=evidence.get("refresh_id"),
                   last_upstream_request_count=evidence["upstream_request_count"],
                   last_upstream_requests=evidence["upstream_requests"],
                   last_upstream_http_status=observation["http_status"],
                   last_upstream_ret=business_code if type(business_code) is int else observation["base_resp_ret"])


def _verified_http_request(session, method, url, **kwargs):
    if urlsplit(url).scheme.lower() == "https":
        kwargs["verify"] = True
    evidence = getattr(_refresh_evidence, "record", None)
    is_weread = urlsplit(url).hostname == "weread.qq.com" and urlsplit(url).path in {"/api/mp/cover", "/web/mp/content"}
    if is_weread:
        kwargs["allow_redirects"] = False
    if is_weread and evidence is not None:
        evidence["upstream_request_count"] = evidence.get("upstream_request_count", 0) + 1
        observation = {"path": urlsplit(url).path, "request_index": evidence["upstream_request_count"],
                       "started_at_epoch": int(time.time())}
        evidence.setdefault("upstream_requests", []).append(observation)
        started = time.monotonic()
        try:
            response = _original_http_request(session, method, url, **kwargs)
        except requests.exceptions.RequestException as error:
            category = "upstream_tls_error" if isinstance(error, requests.exceptions.SSLError) else "upstream_timeout" if isinstance(error, requests.exceptions.Timeout) else "upstream_http_error"
            _record_upstream_result(evidence, observation, started, error=category)
            raise
        code = None
        try:
            payload = response.json()
            code = payload.get("errCode", payload.get("errcode", 0)) if isinstance(payload, dict) else None
        except ValueError:
            pass
        _record_upstream_result(evidence, observation, started, status=response.status_code, business_code=code)
        return response
    is_listing = urlsplit(url).hostname == "mp.weixin.qq.com" and urlsplit(url).path in {"/cgi-bin/appmsgpublish", "/cgi-bin/freepublish", "/cgi-bin/appmsg"}
    if is_listing and evidence is None:
        raise _UpstreamFailure("uncontrolled_collection_disabled")
    if is_listing:
        # A listing attempt must not silently follow an authentication redirect.
        kwargs["allow_redirects"] = False
        evidence["upstream_request_count"] = evidence.get("upstream_request_count", 0) + 1
        observation = {"path": urlsplit(url).path,
                       "request_index": evidence["upstream_request_count"],
                       "started_at_epoch": int(time.time())}
        evidence.setdefault("upstream_requests", []).append(observation)
        started = time.monotonic()
    try:
        response = _original_http_request(session, method, url, **kwargs)
    except requests.exceptions.SSLError:
        if is_listing:
            _record_upstream_result(evidence, observation, started, error="upstream_tls_error")
            _upstream_failure("upstream_tls_error")
        raise
    except requests.exceptions.Timeout:
        if is_listing:
            _record_upstream_result(evidence, observation, started, error="upstream_timeout")
            _upstream_failure("upstream_timeout")
        raise
    except requests.exceptions.RequestException:
        if is_listing:
            _record_upstream_result(evidence, observation, started, error="upstream_http_error")
            _upstream_failure("upstream_http_error")
        raise
    if evidence is not None and is_listing:
        # Rate-limit responses may be HTML, so inspect HTTP status before JSON.
        if response.status_code == 429:
            _record_upstream_result(evidence, observation, started, status=response.status_code)
            _upstream_failure("source_rate_limited")
        try:
            payload = response.json()
            result_code = payload.get("base_resp", {}).get("ret") if isinstance(payload, dict) and isinstance(payload.get("base_resp"), dict) else None
            _record_upstream_result(evidence, observation, started,
                                    status=response.status_code, result_code=result_code)
            if response.status_code != 200 or type(result_code) is not int or result_code != 0:
                if response.status_code == 429 or result_code == 200013:
                    _upstream_failure("source_rate_limited")
                if result_code == 200003:
                    _upstream_failure("source_auth_expired")
                if result_code == 200002:
                    _upstream_failure("source_invalid_arguments")
                _upstream_failure("upstream_access_or_http_error")
            else:
                page = payload.get("publish_page")
                page = json.loads(page) if isinstance(page, str) else page
                if not isinstance(page, dict) or not isinstance(page.get("publish_list"), list):
                    _upstream_failure("unverified_upstream_list_structure")
                else:
                    batches = page["publish_list"]
                    valid = True
                    article_count = 0
                    for batch in batches:
                        info = batch.get("publish_info") if isinstance(batch, dict) else None
                        info = json.loads(info) if isinstance(info, str) else info
                        if not isinstance(info, dict) or not isinstance(info.get("appmsgex"), list):
                            valid = False
                            break
                        article_count += len(info["appmsgex"])
                    if valid:
                        evidence["valid_pages"] += 1
                        evidence["observed_articles"] += article_count
                        if not batches:
                            evidence["end_of_list"] = True
                    else:
                        _upstream_failure("unverified_upstream_batch_structure")
        except (ValueError, TypeError, AttributeError):
            _record_upstream_result(evidence, observation, started,
                                    status=response.status_code, error="malformed_upstream_response")
            _upstream_failure("malformed_upstream_response")
    return response


requests.Session.request = _verified_http_request

from core.config import cfg

if cfg.get("server.enable_job", False) or cfg.get("gather.content_auto_check", False):
    raise RuntimeError("This deployment requires background collection schedules to remain disabled.")

credentials = json.loads(Path("/app/codex-admin.json").read_text(encoding="utf-8"))
from init_sys import sync_models, init_user
from core.db import DB

sync_models()
os.environ["USERNAME"] = credentials["username"]
os.environ["PASSWORD"] = credentials["password"]
try:
    init_user(DB)
finally:
    os.environ.pop("PASSWORD", None)
    os.environ.pop("USERNAME", None)

from core.auth import authenticate_user

if authenticate_user(credentials["username"], credentials["password"]) is None:
    raise RuntimeError("The private administrator account could not be initialized.")
credentials.clear()

import uvicorn
from fastapi import Depends, HTTPException, Query
from core.auth import get_current_user
from core.models.feed import Feed
from core.wx.base import WxGather
from core.queue import TaskQueueManager
from core.wx.model.web import MpsWeb
from jobs.article import UpdateArticle
from web import app
from codex_weread import WereadIntegration, WereadIntegrationError

_weread_integration = WereadIntegration(cfg)


def _strict_upstream_error(self, error, code=None):
    evidence = getattr(_refresh_evidence, "record", None)
    if evidence is not None:
        evidence["errors"].append("upstream_gather_error")
        raise RuntimeError("The upstream article listing failed.")
    # Outside our controlled integration, retain a safe explicit error rather
    # than upstream's silent failure/automatic notification branch.
    raise RuntimeError("The upstream collection requires authorization or verification.")


WxGather.Error = _strict_upstream_error
_original_request_headers = WxGather.fix_header


def _compatible_request_headers(self, url):
    headers = _original_request_headers(self, url).copy()
    referer = headers.pop("Refer", None)
    if referer is not None:
        headers.setdefault("Referer", referer)
    configured_agent = cfg.get("user_agent", "")
    if isinstance(configured_agent, str) and configured_agent.strip():
        headers["User-Agent"] = configured_agent
    return headers


WxGather.fix_header = _compatible_request_headers
_original_web_gather = MpsWeb.get_Articles


def _controlled_web_gather(self, *args, **kwargs):
    if getattr(_refresh_evidence, "record", None) is None:
        raise _UpstreamFailure("uncontrolled_collection_disabled")
    return _original_web_gather(self, *args, **kwargs)


MpsWeb.get_Articles = _controlled_web_gather


def _disabled_background_collection(self, task, *args, **kwargs):
    print("Automatic collection queue disabled; use verified refresh explicitly.")
    return False


TaskQueueManager.add_task = _disabled_background_collection
_refresh_lock = threading.Lock()
_refresh_continuations = {}


@app.get("/xhs-integration/status", tags=["xhs-post integration"])
def integration_status(current_user=Depends(get_current_user)):
    try:
        record = json.loads(_integration_health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    return {"ok": True, "background_collection_disabled": True,
            "acquisition_mode": cfg.get("gather.model"),
            "cookie_auto_refresh": {"enabled": cfg.get("gather.model") == "weread_mp",
                                    "cooldown_hours": 6, "trigger": "before_collection"},
            "refresh_in_progress": _refresh_lock.locked(),
            "retry_not_before_epoch": record.get("retry_not_before_epoch", 0),
            "last_error_code": record.get("last_error_code"),
            "last_success_epoch": record.get("last_success_epoch"),
            "last_valid_pages": record.get("last_valid_pages"),
            "last_observed_articles": record.get("last_observed_articles"),
            "last_refresh_id": record.get("last_refresh_id"),
            "last_upstream_http_status": record.get("last_upstream_http_status"),
            "last_upstream_ret": record.get("last_upstream_ret"),
            "last_upstream_request_count": record.get("last_upstream_request_count"),
            "last_upstream_requests": record.get("last_upstream_requests", []),
            "last_exception_type": record.get("last_exception_type"),
            "last_exception_frames": record.get("last_exception_frames", [])}


@app.post("/xhs-integration/refresh/{mp_id}", tags=["xhs-post integration"])
def verified_refresh(
    mp_id: str,
    start_page: int = Query(0, ge=0, le=1000),
    end_page: int = Query(1, ge=1, le=1001),
    continuation_token: str | None = Query(None, max_length=100),
    current_user=Depends(get_current_user),
):
    if not start_page < end_page or end_page - start_page > 5:
        raise HTTPException(400, detail={"code": "invalid_page_window"})
    mode = cfg.get("gather.model")
    if mode not in {"web", "weread_mp"}:
        raise HTTPException(409, detail={"code": "unverified_acquisition_mode"})
    if mode == "weread_mp" and (start_page != 0 or end_page != 1 or continuation_token is not None):
        raise HTTPException(400, detail={"code": "invalid_page_window"})
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(409, detail={"code": "refresh_busy"})
    session = DB.get_session()
    evidence = {"valid_pages": 0, "observed_articles": 0, "errors": [], "end_of_list": False,
                "refresh_id": secrets.token_hex(16), "upstream_request_count": 0,
                "upstream_requests": []}
    try:
        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        if not feed or feed.status != 1:
            raise HTTPException(404, detail={"code": "source_not_configured_or_disabled"})
        now = time.time()
        try:
            health = json.loads(_integration_health_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = {}
        if health.get("retry_not_before_epoch", 0) > now:
            raise HTTPException(429, detail={"code": "source_refresh_cooldown",
                                           "retry_not_before_epoch": health["retry_not_before_epoch"]})
        if mode == "weread_mp":
            if health.get("last_acquisition_mode") == mode and now - health.get("last_success_epoch", 0) < 60:
                raise HTTPException(409, detail={"code": "source_refresh_too_soon"})
            _refresh_evidence.record = evidence
            result = _weread_integration.refresh_feed(session, feed, evidence)
            _health_record(last_error_code=None, last_success_epoch=int(time.time()),
                           last_acquisition_mode=mode, last_valid_pages=1, last_observed_articles=1,
                           last_cookie_refresh=result["cookie_refresh"], last_exception_type=None, last_exception_frames=[])
            return {**result, "diagnostics": _refresh_diagnostics(evidence)}
        from driver.token import get as current_session_value
        if not current_session_value("token", "") or not current_session_value("cookie", ""):
            raise HTTPException(409, detail={"code": "source_auth_required"})
        for expired in [key for key, value in _refresh_continuations.items() if value["expires_at"] <= now]:
            _refresh_continuations.pop(expired, None)
        continued = False
        scanned_before = 0
        if continuation_token is not None:
            prior = _refresh_continuations.pop(continuation_token, None)
            if (not prior or prior["mp_id"] != mp_id or prior["username"] != current_user["username"]
                    or prior["next_page"] != start_page or prior["pages_scanned"] + end_page - start_page > 5):
                raise HTTPException(409, detail={"code": "invalid_refresh_continuation"})
            continued = True
            scanned_before = prior["pages_scanned"]
        if not continued and feed.update_time and now - feed.update_time < 60:
            raise HTTPException(409, detail={"code": "source_refresh_too_soon"})
        _refresh_evidence.record = evidence
        gatherer = WxGather().Model("web")
        gatherer.get_Articles(
            faker_id=feed.faker_id, Mps_id=feed.id, Mps_title=feed.mp_name,
            CallBack=UpdateArticle, start_page=start_page, MaxPage=end_page,
        )
        if evidence["errors"] or evidence["valid_pages"] != end_page - start_page:
            raise HTTPException(502, detail={"code": "upstream_listing_unverified"})
        if len(gatherer.articles) != evidence["observed_articles"]:
            raise HTTPException(502, detail={"code": "upstream_article_census_mismatch"})
        _health_record(last_error_code=None, last_success_epoch=int(time.time()),
                       last_valid_pages=evidence["valid_pages"], last_observed_articles=evidence["observed_articles"])
        articles = [{
            "id": str(item.get("id", "")),
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "author": feed.mp_name,
            "published_at": item.get("publish_time", ""),
            "content": "",
        } for item in gatherer.articles]
        next_token = None
        pages_scanned = scanned_before + end_page - start_page
        if not evidence["end_of_list"] and pages_scanned < 5:
            next_token = secrets.token_urlsafe(24)
            _refresh_continuations[next_token] = {
                "mp_id": mp_id, "username": current_user["username"], "next_page": end_page,
                "pages_scanned": pages_scanned, "expires_at": time.time() + 150,
            }
        return {"ok": True, "verified_upstream_listing": True, "source_id": mp_id,
                "valid_pages": evidence["valid_pages"], "observed_articles": evidence["observed_articles"],
                "articles": articles, "start_page": start_page, "end_page": end_page,
                "end_of_list": evidence["end_of_list"], "continuation_token": next_token,
                "acquisition_mode": "web", "diagnostics": _refresh_diagnostics(evidence)}
    except HTTPException as error:
        if isinstance(error.detail, dict):
            error.detail = {**error.detail, "diagnostics": _refresh_diagnostics(evidence)}
        raise
    except _UpstreamFailure as error:
        status = 429 if error.code == "source_rate_limited" else 409 if error.code == "source_auth_expired" else 502
        _health_record(last_error_code=error.code)
        raise HTTPException(status, detail={"code": error.code,
                                           "diagnostics": _refresh_diagnostics(evidence)}) from None
    except WereadIntegrationError as error:
        session.rollback()
        updates = {"last_error_code": error.code, "last_acquisition_mode": mode}
        if error.code == "source_rate_limited":
            updates["retry_not_before_epoch"] = int(time.time()) + 900
        _health_record(**updates)
        status = 429 if error.code == "source_rate_limited" else 409 if error.code in {"source_auth_required", "source_auth_expired"} else 502
        raise HTTPException(status, detail={"code": error.code, "diagnostics": _refresh_diagnostics(evidence)}) from None
    except Exception as error:
        session.rollback()
        frames = []
        trace = error.__traceback__
        while trace is not None:
            frames.append({"file": Path(trace.tb_frame.f_code.co_filename).name,
                           "function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
        _health_record(last_error_code="upstream_listing_failed", last_exception_type=type(error).__name__,
                       last_exception_frames=frames[-8:])
        raise HTTPException(502, detail={"code": "upstream_listing_failed",
                                       "diagnostics": _refresh_diagnostics(evidence)}) from None
    finally:
        _refresh_evidence.record = None
        session.close()
        _refresh_lock.release()

# The upstream SPA catch-all GET route precedes locally added routes. Put the
# two explicit integration routes first so status cannot return index.html.
_integration_routes = [route for route in app.router.routes if getattr(route, "path", "").startswith("/xhs-integration/")]
app.router.routes[:] = _integration_routes + [route for route in app.router.routes if route not in _integration_routes]

uvicorn.run(app, host="0.0.0.0", port=8001, reload=False, workers=1, access_log=False)
