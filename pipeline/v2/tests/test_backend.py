"""Local HTTP fixtures exercise transport boundaries without platform actions."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pipeline.v2.backend import BackendClient, BackendError


TOKEN = "offline-test-token-do-not-use"
SUBMIT = "/xhs-integration/submit"


class HTTPFixture:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_fixture()

            def do_POST(self):
                self.handle_fixture()

            def handle_fixture(self):
                length = int(self.headers.get("Content-Length", 0))
                request_body = self.rfile.read(length)
                fixture.requests.append({
                    "method": self.command, "path": self.path,
                    "auth": self.headers.get("Authorization"), "body": request_body,
                })
                options = fixture.routes.get((self.command, self.path), {"status": 404, "json": {"success": False}})
                if options.get("delay"):
                    time.sleep(options["delay"])
                body = options.get("body")
                if body is None:
                    body = json.dumps(options.get("json", {"success": True, "data": {}}), ensure_ascii=False).encode("utf-8")
                elif isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(options.get("status", 200))
                self.send_header("Content-Type", options.get("content_type", "application/json; charset=utf-8"))
                for name, value in options.get("headers", {}).items():
                    self.send_header(name, value)
                if options.get("declare_length", True):
                    self.send_header("Content-Length", str(options.get("length", len(body))))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:" + str(self.server.server_port)
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-backend-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token_path = self.root / "private-token.json"
        self.token_path.write_text(json.dumps({"auth_token": TOKEN}), encoding="utf-8")

    def config(self, base_url, **overrides):
        config = {
            "type": "xiaohongshu_mcp", "base_url": base_url,
            "auth_token_path": str(self.token_path), "timeout_seconds": 2,
        }
        config.update(overrides)
        return config

    def payload(self, attempt_id="unit-attempt-1"):
        return {
            "attempt_id": attempt_id, "content_hash": "a" * 64,
            "expected_account": {"user_id": "unit-user", "red_id": "unit-red", "nickname": "示例作者"},
            "title": "读懂日常知识", "content": "独立组织的正文\n保留必要限定。",
            "tags": ["知识分享"], "images": [str(self.root / "card_0.jpg")],
            "image_hashes": ["b" * 64], "ai_generated": True,
            "editor_session_id": "unit-editor", "visual_reviewed": True,
            "reviewed_evidence_ref": str(self.root / "preflight.jpg"),
        }

    def assert_safe_failure(self, error):
        self.assertFalse(error.retry_allowed)
        message = str(error)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn("private-response-secret", message)
        self.assertNotIn("独立组织的正文", message)

    def test_only_loopback_service_root_urls_are_accepted(self):
        rejected = [
            "https://example.com", "http://192.168.1.1:18060", "http://0.0.0.0:18060",
            "http://[::]:18060", "http://127.0.0.1.evil.test", "file:///C:/secret",
            "http://user:secret@127.0.0.1:18060", "http://127.0.0.1:18060/api",
            "http://127.0.0.1:18060?token=secret", "http://127.0.0.1:18060/#secret",
            "http://127.0.0.1:bad", "http://127.0.0.1:0", "http://[::1%25eth0]:18060",
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(BackendError) as caught:
                BackendClient(self.config(url))
            self.assertEqual(caught.exception.code, "invalid_backend_url")
            self.assert_safe_failure(caught.exception)
        self.assertEqual(BackendClient(self.config("http://localhost:18060/")).base_url, "http://127.0.0.1:18060")
        self.assertEqual(BackendClient(self.config("http://[::1]:18060")).base_url, "http://[::1]:18060")

    def test_private_token_requires_absolute_real_valid_file(self):
        for path in ("relative-token.json", str(self.root / "missing.json")):
            with self.subTest(path=path), self.assertRaises(BackendError) as caught:
                BackendClient(self.config("http://127.0.0.1:18060", auth_token_path=path))
            self.assertEqual(caught.exception.code, "credentials_unavailable")
        for text in ('{"auth_token":""}', '{"token":"wrong-field"}', '["wrong-type"]', '{"auth_token":"line\\nbreak"}'):
            self.token_path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(BackendError):
                BackendClient(self.config("http://127.0.0.1:18060"))

    def test_plain_text_token_and_health_identity_envelopes(self):
        self.token_path.write_text(TOKEN + "\n", encoding="utf-8")
        routes = {
            ("GET", "/xhs-integration/health"): {"json": {"success": True, "data": {"api_version": "xhs-integration-v1", "capabilities": {"automatic_confirmation": False}}}},
            ("GET", "/xhs-integration/identity"): {"json": {"success": True, "data": {"authenticated": True, "user_id": "unit-user", "nickname": "示例作者"}}},
        }
        with HTTPFixture(routes) as server:
            client = BackendClient(self.config(server.base_url))
            self.assertEqual(client.health()["data"]["api_version"], "xhs-integration-v1")
            self.assertEqual(client.identity()["data"]["nickname"], "示例作者")
        self.assertEqual([request["auth"] for request in server.requests], ["Bearer " + TOKEN] * 2)
        self.assertTrue(all(request["method"] == "GET" for request in server.requests))

    def test_preflight_utf8_payload_and_current_session_schema_are_preserved(self):
        reply = {"success": True, "data": {
            "phase": "PREPARED", "editor_session_id": "unit-editor", "prepared": True,
            "image_match_verified": False, "upload_order_verified": True, "images_order_verified": False,
            "evidence_ref": str(self.root / "preflight.jpg"),
        }}
        with HTTPFixture({("POST", "/xhs-integration/preflight"): {"json": reply}}) as server:
            response = BackendClient(self.config(server.base_url)).preflight(self.payload())
        self.assertEqual(response, reply)
        self.assertEqual(json.loads(server.requests[0]["body"]), self.payload())
        self.assertEqual(len(server.requests), 1)

    def test_submit_returns_unconfirmed_action_and_duplicate_call_never_posts(self):
        reply = {"success": True, "data": {
            "attempt_id": "unit-attempt-1", "phase": "SUBMIT_UNKNOWN", "confirmed": False,
            "published": False, "retry_allowed": False, "click_attempted": True,
        }}
        with HTTPFixture({("POST", SUBMIT): {"json": reply}}) as server:
            client = BackendClient(self.config(server.base_url))
            self.assertEqual(client.submit(self.payload()), reply)
            with self.assertRaises(BackendError) as caught:
                client.submit(self.payload())
            self.assertEqual(caught.exception.code, "duplicate_submit")
        self.assertEqual(len(server.requests), 1)

    def test_timeout_after_submit_is_unknown_without_retry(self):
        with HTTPFixture({("POST", SUBMIT): {"delay": 0.2}}) as server:
            client = BackendClient(self.config(server.base_url, timeout_seconds=0.05))
            with self.assertRaises(BackendError) as caught:
                client.submit(self.payload())
            self.assertEqual(caught.exception.code, "backend_timeout")
            self.assertEqual(caught.exception.phase, "SUBMIT_UNKNOWN")
            self.assert_safe_failure(caught.exception)
            with self.assertRaises(BackendError):
                client.submit(self.payload())
        self.assertEqual(len(server.requests), 1)

    def test_concurrent_duplicate_submit_has_one_network_action(self):
        reply = {"success": True, "data": {"phase": "SUBMIT_UNKNOWN", "published": False, "confirmed": False}}
        with HTTPFixture({("POST", SUBMIT): {"delay": 0.1, "json": reply}}) as server:
            client = BackendClient(self.config(server.base_url))
            barrier = threading.Barrier(2)

            def invoke():
                barrier.wait(timeout=2)
                try:
                    return client.submit(self.payload())
                except BackendError as error:
                    return error

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: invoke(), range(2)))
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        errors = [value for value in results if isinstance(value, BackendError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "duplicate_submit")

    def test_environment_proxy_never_receives_local_bearer_token(self):
        with HTTPFixture({}) as proxy, HTTPFixture({("GET", "/xhs-integration/health"): {}}) as server:
            environment = {"http_proxy": proxy.base_url, "HTTP_PROXY": proxy.base_url, "no_proxy": "", "NO_PROXY": ""}
            with patch.dict("os.environ", environment):
                response = BackendClient(self.config(server.base_url)).health()
            self.assertTrue(response["success"])
            self.assertEqual(proxy.requests, [])
        self.assertEqual(len(server.requests), 1)

    def test_credential_redirect_is_blocked_without_reaching_target(self):
        with HTTPFixture({}) as target:
            routes = {("POST", SUBMIT): {"status": 307, "headers": {"Location": target.base_url + "/credential-trap"}}}
            with HTTPFixture(routes) as server:
                client = BackendClient(self.config(server.base_url))
                with self.assertRaises(BackendError) as caught:
                    client.submit(self.payload())
                self.assertEqual(caught.exception.code, "redirect_rejected")
                self.assertEqual(caught.exception.phase, "SUBMIT_UNKNOWN")
                self.assert_safe_failure(caught.exception)
            self.assertEqual(target.requests, [])
        self.assertEqual(len(server.requests), 1)

    def test_truncated_submit_response_is_unknown_and_never_repeated(self):
        body = b'{"success":true,"data":{"phase":"SUBMIT_UNKNOWN"}}'
        with HTTPFixture({("POST", SUBMIT): {"body": body, "length": len(body) + 100}}) as server:
            client = BackendClient(self.config(server.base_url))
            with self.assertRaises(BackendError) as caught:
                client.submit(self.payload())
            self.assertEqual(caught.exception.code, "truncated_response")
            self.assertEqual(caught.exception.phase, "SUBMIT_UNKNOWN")
            with self.assertRaises(BackendError):
                client.submit(self.payload())
        self.assertEqual(len(server.requests), 1)

    def test_size_limits_apply_to_declared_and_streamed_responses(self):
        for declared in (True, False):
            routes = {("GET", "/xhs-integration/health"): {"body": "X" * 2000, "declare_length": declared}}
            with self.subTest(declared=declared), HTTPFixture(routes) as server:
                with self.assertRaises(BackendError) as caught:
                    BackendClient(self.config(server.base_url, max_bytes=1024)).health()
                self.assertEqual(caught.exception.code, "response_too_large")

    def test_non_json_malformed_json_and_non_object_are_safe_failures(self):
        variants = [
            ({"content_type": "text/html", "body": "private-response-secret"}, "invalid_response_type"),
            ({"body": "{private-response-secret"}, "invalid_json_response"),
            ({"json": []}, "invalid_json_response"),
            ({"json": {"success": True, "data": []}}, "backend_rejected"),
            ({"body": b"\xff\xfe"}, "invalid_json_response"),
        ]
        for response, code in variants:
            with self.subTest(code=code), HTTPFixture({("POST", SUBMIT): response}) as server:
                with self.assertRaises(BackendError) as caught:
                    BackendClient(self.config(server.base_url)).submit(self.payload())
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.phase, "SUBMIT_UNKNOWN")
                self.assert_safe_failure(caught.exception)
                self.assertEqual(len(server.requests), 1)

    def test_http_error_is_sanitized_and_keeps_safe_phase(self):
        reply = {"success": False, "error": TOKEN, "message": "private-response-secret", "data": {"phase": "NOT_SUBMITTED"}}
        with HTTPFixture({("POST", SUBMIT): {"status": 401, "json": reply}}) as server:
            client = BackendClient(self.config(server.base_url))
            with self.assertRaises(BackendError) as caught:
                client.submit(self.payload())
            self.assertEqual(caught.exception.code, "backend_http_error")
            self.assertEqual(caught.exception.http_status, 401)
            self.assertEqual(caught.exception.phase, "NOT_SUBMITTED")
            self.assert_safe_failure(caught.exception)
            with self.assertRaises(BackendError):
                client.submit(self.payload())
        self.assertEqual(len(server.requests), 1)

    def diagnostic_files(self):
        directory = self.root / "session" / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        screenshot = directory / "identity-failure-unit.png"
        screenshot.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
        ))
        record = directory / "identity-failure-unit.json"
        record.write_text(json.dumps({"visible_text": "登录验证", "backend_code": "NOT_AUTHENTICATED"}), encoding="utf-8")
        return screenshot, record

    def test_http_identity_error_retains_only_controlled_diagnostic_evidence(self):
        screenshot, record = self.diagnostic_files()
        reply = {"success": False, "error": "LOGIN_NETWORK_RESTRICTED", "message": TOKEN,
                 "data": {"phase": "NOT_SUBMITTED", "diagnostic": {
                     "backend_code": "LOGIN_NETWORK_RESTRICTED", "code": "LOGIN_NETWORK_RESTRICTED",
                     "evidence_ref": str(screenshot), "record_ref": str(record),
                     "observed_at": "private-response-secret", "message": TOKEN, "cookie": TOKEN,
                 }}}
        path = "/xhs-integration/identity"
        with HTTPFixture({("GET", path): {"status": 409, "json": reply}}) as server:
            with self.assertRaises(BackendError) as caught:
                BackendClient(self.config(server.base_url)).identity()
        error = caught.exception
        self.assertEqual(error.code, "backend_http_error")
        self.assertEqual(error.http_status, 409)
        self.assertEqual(error.phase, "NOT_SUBMITTED")
        self.assertEqual(error.backend_code, "LOGIN_NETWORK_RESTRICTED")
        self.assertEqual(error.evidence_ref, str(screenshot.resolve()))
        self.assertEqual(error.record_ref, str(record.resolve()))
        self.assertEqual(set(vars(error)), {"code", "phase", "http_status", "retry_allowed", "backend_code", "evidence_ref", "record_ref"})
        self.assert_safe_failure(error)
        self.assertNotIn(TOKEN, json.dumps(vars(error)))
        self.assertNotIn("private-response-secret", json.dumps(vars(error)))
        self.assertEqual(len(server.requests), 1)

    def test_identity_diagnostic_accepts_only_fixed_codes_and_details_alias(self):
        screenshot, record = self.diagnostic_files()
        codes = ("NOT_AUTHENTICATED", "ACCOUNT_ID_UNREADABLE", "ACCOUNT_RED_ID_UNREADABLE",
                 "PROFILE_ACCOUNT_MISMATCH", "PROFILE_NICKNAME_MISMATCH", "LOGIN_NETWORK_RESTRICTED",
                 "LOGIN_STATUS_TIMEOUT", "IDENTITY_FAILED")
        options = {"status": 409}
        with HTTPFixture({("GET", "/xhs-integration/identity"): options}) as server:
            client = BackendClient(self.config(server.base_url))
            for container in ("diagnostic", "details"):
                for code in codes:
                    with self.subTest(container=container, code=code):
                        options["json"] = {"success": False, "error": TOKEN, "message": TOKEN,
                                           "data": {container: {"backend_code": code, "evidence_ref": str(screenshot), "record_ref": str(record)}}}
                        with self.assertRaises(BackendError) as caught:
                            client.identity()
                        self.assertEqual(caught.exception.backend_code, code)
                        self.assertEqual(caught.exception.evidence_ref, str(screenshot.resolve()))
                        self.assertEqual(caught.exception.record_ref, str(record.resolve()))
                        self.assert_safe_failure(caught.exception)

    def test_misleading_diagnostic_paths_are_not_trusted_or_exposed(self):
        screenshot, record = self.diagnostic_files()
        outside_png = self.root / "private-response-secret.png"
        outside_png.write_bytes(screenshot.read_bytes())
        outside_json = self.root / "private-response-secret.json"
        outside_json.write_text("{}", encoding="utf-8")
        sibling = screenshot.parent.parent / "evidence-extra"
        sibling.mkdir()
        sibling_png = sibling / "private-response-secret.png"
        sibling_png.write_bytes(screenshot.read_bytes())
        folder_png = screenshot.parent / "folder.png"
        folder_png.mkdir()
        folder_json = screenshot.parent / "folder.json"
        folder_json.mkdir()
        wrong_png = screenshot.parent / "image.jpg"
        wrong_png.write_bytes(screenshot.read_bytes())
        wrong_json = screenshot.parent / "record.txt"
        wrong_json.write_text("{}", encoding="utf-8")
        secret_png = screenshot.parent / (TOKEN + ".png")
        secret_png.write_bytes(screenshot.read_bytes())
        secret_json = screenshot.parent / (TOKEN + ".json")
        secret_json.write_text("{}", encoding="utf-8")
        bad_pairs = [
            (str(outside_png), str(outside_json)),
            (str(sibling_png), str(self.token_path)),
            (str(screenshot.parent / ".." / ".." / outside_png.name), str(record.parent / ".." / ".." / outside_json.name)),
            ("session/evidence/identity-failure-unit.png", "session/evidence/identity-failure-unit.json"),
            (str(screenshot.parent / "missing.png"), str(record.parent / "missing.json")),
            (str(folder_png), str(folder_json)),
            (str(wrong_png), str(wrong_json)),
            (str(secret_png), str(secret_json)),
            ("https://example.com/private-response-secret.png", "file://" + str(self.token_path)),
            ([str(screenshot)], {"path": str(record)}),
        ]
        link_png = screenshot.parent / "escape.png"
        link_json = screenshot.parent / "escape.json"
        try:
            link_png.symlink_to(outside_png)
            link_json.symlink_to(outside_json)
        except OSError:
            pass  # Unprivileged Windows installations may disallow symlinks.
        else:
            bad_pairs.append((str(link_png), str(link_json)))
        options = {"status": 409}
        with HTTPFixture({("GET", "/xhs-integration/identity"): options}) as server:
            client = BackendClient(self.config(server.base_url))
            for evidence, page_record in bad_pairs:
                with self.subTest(evidence=evidence):
                    options["json"] = {"success": False, "error": "IDENTITY_FAILED", "message": TOKEN,
                                       "data": {"diagnostic": {"backend_code": "IDENTITY_FAILED", "evidence_ref": evidence, "record_ref": page_record}}}
                    with self.assertRaises(BackendError) as caught:
                        client.identity()
                    self.assertEqual(caught.exception.backend_code, "IDENTITY_FAILED")
                    self.assertIsNone(caught.exception.evidence_ref)
                    self.assertIsNone(caught.exception.record_ref)
                    self.assert_safe_failure(caught.exception)
                    self.assertNotIn("private-response-secret", json.dumps(vars(caught.exception)))
                    self.assertNotIn(TOKEN, json.dumps(vars(caught.exception)))

    def test_unknown_backend_codes_and_messages_never_enter_error_fields(self):
        screenshot, record = self.diagnostic_files()
        options = {}
        with HTTPFixture({("GET", "/xhs-integration/identity"): options}) as server:
            client = BackendClient(self.config(server.base_url))
            for status in (200, 409):
                for code in (TOKEN, "UNKNOWN_IDENTITY_CODE", ["NOT_AUTHENTICATED"], {"code": "IDENTITY_FAILED"}):
                    with self.subTest(status=status, code=code):
                        options.update({"status": status, "json": {"success": False, "error": code,
                            "message": "private-response-secret " + TOKEN,
                            "data": {"diagnostic": {"backend_code": code, "message": TOKEN, "evidence_ref": str(screenshot), "record_ref": str(record)}}}})
                        with self.assertRaises(BackendError) as caught:
                            client.identity()
                        error = caught.exception
                        self.assertEqual(error.code, "backend_http_error" if status == 409 else "backend_rejected")
                        self.assertIsNone(error.backend_code)
                        self.assertIsNone(error.evidence_ref)
                        self.assertIsNone(error.record_ref)
                        self.assert_safe_failure(error)
                        self.assertNotIn(TOKEN, json.dumps(vars(error)))
                        self.assertNotIn("UNKNOWN_IDENTITY_CODE", json.dumps(vars(error)))

    def test_http_error_diagnostic_keeps_bounded_body_limit(self):
        screenshot, record = self.diagnostic_files()
        reply = {"success": False, "error": "IDENTITY_FAILED", "message": TOKEN * 1000,
                 "data": {"diagnostic": {"backend_code": "IDENTITY_FAILED", "evidence_ref": str(screenshot), "record_ref": str(record)}}}
        with HTTPFixture({("GET", "/xhs-integration/identity"): {"status": 409, "json": reply}}) as server:
            with self.assertRaises(BackendError) as caught:
                BackendClient(self.config(server.base_url, max_bytes=1024)).identity()
        self.assertEqual(caught.exception.code, "backend_http_error")
        self.assertIsNone(caught.exception.backend_code)
        self.assertIsNone(caught.exception.evidence_ref)
        self.assertIsNone(caught.exception.record_ref)
        self.assert_safe_failure(caught.exception)

    def test_preflight_failure_exposes_only_safe_code_and_controlled_capture(self):
        screenshot, record = self.diagnostic_files()
        variants = (
            ("PREFLIGHT_FAILED", str(screenshot), str(record), str(screenshot.resolve()), str(record.resolve())),
            ("PREFLIGHT_FAILED", "", str(record), None, str(record.resolve())),
            ("PREFLIGHT_FAILED", str(self.token_path), "https://example.com/private-response-secret.json", None, None),
            ("PREFLIGHT_FAILED_" + TOKEN, str(screenshot), str(record), None, None),
        )
        options = {"status": 409}
        with HTTPFixture({("POST", "/xhs-integration/preflight"): options}) as server:
            client = BackendClient(self.config(server.base_url))
            for code, evidence, page_record, expected_evidence, expected_record in variants:
                with self.subTest(code=code, evidence=evidence):
                    options["json"] = {"success": False, "error": code, "message": TOKEN,
                                       "data": {"phase": "NOT_SUBMITTED", "diagnostic": {
                                           "backend_code": code, "code": code, "evidence_ref": evidence,
                                           "record_ref": page_record, "message": TOKEN, "stage": TOKEN,
                                           "error_class": "private-response-secret",
                                       }, "details": TOKEN}}
                    with self.assertRaises(BackendError) as caught:
                        client.preflight(self.payload())
                    error = caught.exception
                    self.assertEqual(error.code, "backend_http_error")
                    self.assertEqual(error.http_status, 409)
                    self.assertEqual(error.phase, "NOT_SUBMITTED")
                    self.assertEqual(error.backend_code, "PREFLIGHT_FAILED" if code == "PREFLIGHT_FAILED" else None)
                    self.assertEqual(error.evidence_ref, expected_evidence)
                    self.assertEqual(error.record_ref, expected_record)
                    self.assert_safe_failure(error)
                    self.assertNotIn(TOKEN, json.dumps(vars(error)))
                    self.assertNotIn("private-response-secret", json.dumps(vars(error)))
                    self.assertNotIn("stage", vars(error))
                    self.assertNotIn("details", vars(error))
        self.assertEqual(len(server.requests), len(variants))

    def test_success_false_does_not_leak_backend_messages(self):
        reply = {"success": False, "error": "SUBMIT_ERROR", "message": TOKEN, "data": {"phase": "SUBMIT_UNKNOWN"}}
        with HTTPFixture({("POST", SUBMIT): {"json": reply}}) as server:
            with self.assertRaises(BackendError) as caught:
                BackendClient(self.config(server.base_url)).submit(self.payload())
            self.assertEqual(caught.exception.code, "backend_rejected")
            self.assert_safe_failure(caught.exception)

    def test_submit_cannot_report_verified_publication(self):
        for data in ({"published": True}, {"confirmed": True}, {"phase": "PUBLISHED"}):
            with self.subTest(data=data), HTTPFixture({("POST", SUBMIT): {"json": {"success": True, "data": data}}}) as server:
                with self.assertRaises(BackendError) as caught:
                    BackendClient(self.config(server.base_url)).submit(self.payload())
                self.assertEqual(caught.exception.code, "unverified_publication_state")
                self.assertEqual(caught.exception.phase, "SUBMIT_UNKNOWN")

    def test_observations_are_read_only_and_do_not_confirm_publication(self):
        path = "/xhs-integration/attempts/unit-attempt-1/observations"
        reply = {"success": True, "data": {
            "attempt_id": "unit-attempt-1", "phase": "SUBMIT_UNKNOWN", "confirmed": False,
            "published": False, "observations": [], "unverified": True, "requires_management_evidence": True,
        }}
        with HTTPFixture({("GET", path): {"json": reply}}) as server:
            self.assertEqual(BackendClient(self.config(server.base_url)).observations("unit-attempt-1"), reply)
        self.assertEqual(server.requests[0]["method"], "GET")
        self.assertEqual(server.requests[0]["body"], b"")
        with HTTPFixture({("GET", path): {"json": {"success": True, "data": {"published": True}}}}) as server:
            with self.assertRaises(BackendError) as caught:
                BackendClient(self.config(server.base_url)).observations("unit-attempt-1")
            self.assertEqual(caught.exception.code, "unverified_publication_state")

    def test_management_evidence_is_get_only_and_rejects_confirmation_claims(self):
        path = "/xhs-integration/management-evidence"
        reply = {"success": True, "data": {"phase": "READ_ONLY", "unverified": True,
                 "confirmed": False, "published": False, "url": "https://creator.xiaohongshu.com/new/note-manager"}}
        options = {"json": reply}
        with HTTPFixture({("GET", path): options}) as server:
            client = BackendClient(self.config(server.base_url))
            self.assertEqual(client.management_evidence(), reply)
            for claim in ({"confirmed": True}, {"published": True}, {"phase": "PUBLISHED"}):
                with self.subTest(claim=claim):
                    options["json"] = {"success": True, "data": claim}
                    with self.assertRaises(BackendError) as caught:
                        client.management_evidence()
                    self.assertEqual(caught.exception.code, "unverified_publication_state")
                    self.assertIsNone(caught.exception.phase)
            self.assertEqual(client._submitted_attempts, set())
        self.assertEqual(len(server.requests), 4)
        self.assertTrue(all(request["method"] == "GET" and request["path"] == path and request["body"] == b"" for request in server.requests))

    def test_management_readback_failure_keeps_only_safe_private_diagnostic(self):
        screenshot, record = self.diagnostic_files()
        options = {"status": 409}
        with HTTPFixture({("GET", "/xhs-integration/management-evidence"): options}) as server:
            client = BackendClient(self.config(server.base_url))
            for code in ("MANAGEMENT_READBACK_FAILED", "MANAGEMENT_READBACK_FAILED_" + TOKEN):
                with self.subTest(code=code):
                    options["json"] = {"success": False, "error": code, "message": TOKEN,
                        "data": {"diagnostic": {"backend_code": code, "evidence_ref": str(screenshot),
                                  "record_ref": str(record), "message": TOKEN, "stage": "private-response-secret"}}}
                    with self.assertRaises(BackendError) as caught:
                        client.management_evidence()
                    error = caught.exception
                    self.assertEqual(error.code, "backend_http_error")
                    self.assertEqual(error.http_status, 409)
                    self.assertEqual(error.backend_code, code if code == "MANAGEMENT_READBACK_FAILED" else None)
                    self.assertEqual(error.evidence_ref, str(screenshot.resolve()) if code == "MANAGEMENT_READBACK_FAILED" else None)
                    self.assertEqual(error.record_ref, str(record.resolve()) if code == "MANAGEMENT_READBACK_FAILED" else None)
                    self.assert_safe_failure(error)
                    self.assertNotIn(TOKEN, json.dumps(vars(error)))
                    self.assertNotIn("private-response-secret", json.dumps(vars(error)))
        self.assertTrue(all(request["method"] == "GET" and request["body"] == b"" for request in server.requests))

    def test_management_view_uses_only_whitelisted_query_constants(self):
        root = "/xhs-integration/management-evidence"
        views = ("all", "published", "pending_review", "rejected")
        routes = {}
        for view in views:
            path = root + ("?view=" + view if view != "all" else "")
            routes[("GET", path)] = {"json": {"success": True, "data": {"view": view, "phase": "READ_ONLY",
                                            "unverified": True, "published": False, "confirmed": False}}}
        with HTTPFixture(routes) as server:
            client = BackendClient(self.config(server.base_url))
            for view in views:
                self.assertEqual(client.management_evidence(view)["data"]["view"], view)
            routes[("GET", root + "?view=published")]["json"] = {"success": True, "data": {"confirmed": True}}
            with self.assertRaises(BackendError) as caught:
                client.management_evidence("published")
            self.assertEqual(caught.exception.code, "unverified_publication_state")
        expected = [root, root + "?view=published", root + "?view=pending_review", root + "?view=rejected", root + "?view=published"]
        self.assertEqual([request["path"] for request in server.requests], expected)
        self.assertTrue(all(request["method"] == "GET" and request["body"] == b"" for request in server.requests))

    def test_invalid_management_view_never_reaches_network(self):
        with HTTPFixture({}) as server:
            client = BackendClient(self.config(server.base_url))
            for view in ("", None, [], {}, True, 1, "scheduled", "ALL", "published ", "published&token=" + TOKEN):
                with self.subTest(view=view):
                    with self.assertRaises(BackendError) as caught:
                        client.management_evidence(view)
                    self.assertEqual(caught.exception.code, "invalid_management_view")
                    self.assert_safe_failure(caught.exception)
                    self.assertNotIn(TOKEN, json.dumps(vars(caught.exception)))
        self.assertEqual(server.requests, [])

    def test_invalid_attempt_or_payload_never_reaches_server(self):
        with HTTPFixture({}) as server:
            client = BackendClient(self.config(server.base_url))
            for attempt_id in ("../secret", "id/extra", "id?auth=secret", "", None):
                with self.subTest(attempt_id=attempt_id), self.assertRaises(BackendError):
                    client.observations(attempt_id)
            with self.assertRaises(BackendError):
                client.submit(self.payload(attempt_id="../submit"))
            with self.assertRaises(BackendError):
                client.preflight(["not-object"])
            with self.assertRaises(BackendError):
                client.submit(dict(self.payload(), illegal=float("nan")))
        self.assertEqual(server.requests, [])

    def test_invalid_timeout_and_size_configuration_are_rejected(self):
        for timeout in (0, -1, True, float("inf"), float("nan"), 181, "slow"):
            with self.subTest(timeout=timeout), self.assertRaises(BackendError):
                BackendClient(self.config("http://127.0.0.1:18060", timeout_seconds=timeout))
        for maximum in (True, 0, 1023, 17 * 1024 * 1024, "large"):
            with self.subTest(maximum=maximum), self.assertRaises(BackendError):
                BackendClient(self.config("http://127.0.0.1:18060", max_bytes=maximum))


if __name__ == "__main__":
    unittest.main()
