"""Offline connector acceptance cases, including actual local HTTP transport."""

import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pipeline.v2.sources import SourceError, acquire, article_identity, discover, extract_html, normalize_url, refresh_werss


LONG_TEXT = "这是一段用于验证来源正文获取的文字，包含明确的事实、适用条件和完整解释。" * 8
ARTICLE_HTML = (
    '<html><head><title>页面标题</title></head><body>'
    '<h1 id="activity-name">真实标题</h1><span id="js_name">目标公众号</span>'
    '<div id="js_content"><p>' + LONG_TEXT + '</p>'
    '<p>另一个正文段落。</p><script>do_not_extract_secret</script>'
    '<div style="display: none">hidden_secret</div><img src="example.png"></div>'
    '<footer>页脚不可混入正文</footer></body></html>'
)


class _HTTPFixture:
    def __init__(self, routes):
        self.routes = routes

    def __enter__(self):
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.do_GET()

            def do_GET(self):
                status, content_type, body = routes.get(self.path, (404, "text/plain", b"not found"))
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return "http://127.0.0.1:" + str(self.server.server_port)

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture_path = Path(self.temp.name) / "feed.json"

    def fixture(self, items):
        self.fixture_path.write_text(json.dumps({"articles": items}, ensure_ascii=False), encoding="utf-8")
        return {"id": "test-source", "type": "fixture", "path": str(self.fixture_path)}

    def article(self, **overrides):
        item = {"url": "https://example.com/post", "id": "stable-1", "title": "文章标题", "content": "摘要", "html": ARTICLE_HTML}
        item.update(overrides)
        return item

    def test_wechat_url_variants_share_identity_but_index_differs(self):
        short = "https://mp.weixin.qq.com/s?__biz=MzAw==&mid=123&idx=1&sn=abc&scene=27&utm_source=x#wechat_redirect"
        alternate = "https://mp.weixin.qq.com/s?idx=1&appmsgid=123&__biz=MzAw%3D%3D&wxfrom=5"
        self.assertEqual(article_identity(short), article_identity(alternate))
        self.assertNotEqual(article_identity(short), article_identity(alternate.replace("idx=1", "idx=2")))
        normalized = normalize_url(short)
        self.assertIn("sn=abc", normalized)
        self.assertIn("__biz=", normalized)
        self.assertNotIn("scene", normalized)
        self.assertNotIn("utm_", normalized)
        self.assertNotIn("#", normalized)

    def test_invalid_protocol_and_credentials_are_never_requested(self):
        for url in ("file:///C:/secret", "ftp://example.com/feed", "https://secret:password@example.com/feed", "http://example.com:bad/feed"):
            with self.subTest(url=url), self.assertRaises(SourceError):
                normalize_url(url)

    def test_feed_stable_id_survives_url_change(self):
        self.assertEqual(article_identity("https://example.com/a", "id-1"), article_identity("https://example.com/b", "id-1"))

    def test_repeated_poll_and_revision_do_not_mutate_input_cursor(self):
        source = self.fixture([self.article()])
        first = discover(source)
        original = copy.deepcopy(first["cursor"])
        self.assertEqual(len(first["articles"]), 1)
        self.assertEqual(discover(source, original)["articles"], [])
        self.assertEqual(first["cursor"], original)
        self.fixture([self.article(content="来源修改后的摘要")])
        changed = discover(source, original)
        self.assertEqual(len(changed["articles"]), 1)
        self.assertEqual(changed["articles"][0]["article_id"], first["articles"][0]["article_id"])
        self.assertEqual(original, first["cursor"])

    def test_multi_article_poll_keeps_distinct_indexes(self):
        source = self.fixture([
            self.article(url="https://mp.weixin.qq.com/s?__biz=Mabc&mid=456&idx=1", id="same-id"),
            self.article(url="https://mp.weixin.qq.com/s?__biz=Mabc&mid=456&idx=2", id="same-id"),
        ])
        source["capabilities"] = {"supports_listing": True, "backfill": False, "multi_article": True, "coverage_mode": "latest_batch"}
        result = discover(source)
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(result["capabilities"]["observed_article_count"], 2)
        self.assertTrue(result["capabilities"]["supports_multi_article"])
        self.assertFalse(result["capabilities"]["supports_backfill"])
        self.assertFalse(result["capabilities"]["declared_capabilities_verified"])

    def test_duplicate_event_collapses_only_identical_article(self):
        result = discover(self.fixture([self.article(), self.article()]))
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["cursor"]["last_item_count"], 1)
        with self.assertRaises(SourceError) as error:
            discover(self.fixture([self.article(), self.article(title="矛盾标题")]))
        self.assertEqual(error.exception.code, "conflicting_feed_items")

    def test_parse_failure_preserves_cursor_and_is_not_empty_success(self):
        source = self.fixture([self.article()])
        cursor = discover(source)["cursor"]
        old = copy.deepcopy(cursor)
        self.fixture_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(SourceError):
            discover(source, cursor)
        self.assertEqual(cursor, old)
        self.fixture_path.write_text('{"articles": []}', encoding="utf-8")
        result = discover(source, cursor)
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["cursor"]["seen"], old["seen"])

    def test_malformed_item_does_not_silently_advance_cursor(self):
        source = self.fixture([self.article(), {"id": "missing-url", "title": "坏记录"}])
        cursor = {"seen": {}, "marker": "unchanged"}
        with self.assertRaises(SourceError):
            discover(source, cursor)
        self.assertEqual(cursor, {"seen": {}, "marker": "unchanged"})

    def test_plain_feed_summary_never_claims_full_body(self):
        source = self.fixture([self.article(content=LONG_TEXT)])
        result = discover(source)
        self.assertEqual(result["articles"][0]["coverage"], "partial")
        self.assertTrue(result["articles"][0]["is_fixture"])

    def test_actual_http_rss_to_html_pipeline(self):
        rss = '<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title><item><guid>stable</guid><title>Feed title</title><link>/article</link><description><![CDATA[<p>摘要</p>]]></description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>'
        with _HTTPFixture({"/rss": (200, "application/rss+xml", rss), "/article": (200, "text/html; charset=utf-8", ARTICLE_HTML)}) as base:
            source = {"id": "rss-live-local", "type": "rss", "url": base + "/rss"}
            discovered = discover(source)
            self.assertEqual(len(discovered["articles"]), 1)
            self.assertEqual(discovered["articles"][0]["content"], "摘要")
            acquired = acquire(discovered["articles"][0], source)
        self.assertEqual(acquired["coverage"], "full")
        self.assertEqual(acquired["title"], "真实标题")
        self.assertEqual(acquired["author"], "目标公众号")
        self.assertIn(LONG_TEXT, acquired["content"])
        self.assertNotIn("页脚", acquired["content"])
        self.assertNotIn("secret", acquired["content"])
        self.assertEqual(acquired["diagnostics"]["image_count"], 1)
        self.assertTrue(acquired["diagnostics"]["live_source_verified"])

    def test_atom_with_namespaces_relative_link_and_feed_author(self):
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><author><name>作者</name></author><entry><id>urn:uuid:1</id><title>标题</title><link rel="self" href="/api/1" type="application/atom+xml"/><link href="/article"/><updated>2026-10-04T00:00:00Z</updated><content type="html">&lt;p&gt;Atom 正文&lt;/p&gt;</content></entry></feed>'
        with _HTTPFixture({"/feed": (200, "application/atom+xml", atom)}) as base:
            result = discover({"id": "atom", "type": "atom", "url": base + "/feed"})
        self.assertEqual(result["articles"][0]["url"], base + "/article")
        self.assertEqual(result["articles"][0]["author"], "作者")
        self.assertEqual(result["articles"][0]["content"], "Atom 正文")

    def test_json_feed_and_empty_valid_feed(self):
        feed = {"version": "https://jsonfeed.org/version/1.1", "title": "Source", "items": [{"id": "p1", "url": "/article", "title": "标题", "content_text": "正文", "authors": [{"name": "作者"}]}]}
        with _HTTPFixture({"/feed": (200, "application/feed+json", json.dumps(feed)), "/empty": (200, "application/feed+json", json.dumps(dict(feed, items=[])))}) as base:
            result = discover({"id": "json", "type": "json_feed", "url": base + "/feed"})
            empty = discover({"id": "json", "type": "json_feed", "url": base + "/empty"}, result["cursor"])
        self.assertEqual(result["articles"][0]["author"], "作者")
        self.assertEqual(empty["articles"], [])
        self.assertEqual(empty["cursor"]["seen"], result["cursor"]["seen"])

    def test_http_failure_is_sanitized_and_preserves_cursor(self):
        with _HTTPFixture({"/feed?secret=hidden": (403, "text/plain", "password=must_not_leak")}) as base:
            cursor = {"seen": {}, "marker": "persist"}
            with self.assertRaises(SourceError) as caught:
                discover({"id": "rss", "type": "rss", "url": base + "/feed?secret=hidden"}, cursor)
        self.assertEqual(caught.exception.code, "source_access_denied")
        self.assertNotIn("hidden", str(caught.exception))
        self.assertNotIn("must_not_leak", str(caught.exception))
        self.assertEqual(cursor, {"seen": {}, "marker": "persist"})

    def test_feed_type_or_malformed_xml_not_treated_as_no_new_articles(self):
        for body in ('<html><body>验证页</body></html>', '<rss><channel/></rss>', '<rss>', '<feed><title>Unnamespaced</title></feed>'):
            with self.subTest(body=body), patch("pipeline.v2.sources._fetch", return_value=(body.encode(), "text/xml", "https://example.com/feed")):
                with self.assertRaises(SourceError):
                    discover({"id": "rss", "type": "rss", "url": "https://example.com/feed"})

    def test_xml_external_entities_are_rejected(self):
        malicious = '<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///C:/secret">]><rss><channel><title>&secret;</title></channel></rss>'
        with patch("pipeline.v2.sources._fetch", return_value=(malicious.encode(), "text/xml", "https://example.com/feed")):
            with self.assertRaises(SourceError) as caught:
                discover({"id": "rss", "type": "rss", "url": "https://example.com/feed"})
        self.assertEqual(caught.exception.code, "unsafe_xml")

    def test_legacy_chinese_feeds_are_decoded_before_parsing(self):
        # Expat rejects every multi-byte declaration, yet GB/Big5 feeds remain common.
        cases = (("GB2312", "gb2312", "示例频道"), ("gb2312", "gbk", "朱镕基谈阅读"), ("x-gbk", "gbk", "朱镕基谈阅读"),
                 ("GB18030", "gb18030", "𠀀字扩展"), ("big5", "big5", "範例頻道"), ("gb2312", "utf-8", "过期声明的频道"))
        routes = {}
        for index, (label, codec, title) in enumerate(cases):
            rss = ('<?xml version="1.0" encoding="' + label + '"?><rss version="2.0"><channel><title>' + title + '</title>'
                   '<item><guid>legacy</guid><title>' + title + '</title><link>/article</link>'
                   '<description>摘要' + title + '</description></item></channel></rss>')
            routes["/rss/" + str(index)] = (200, "application/rss+xml", rss.encode(codec))
        with _HTTPFixture(routes) as base:
            for index, (label, codec, title) in enumerate(cases):
                with self.subTest(label=label, codec=codec):
                    result = discover({"id": "legacy", "type": "rss", "url": base + "/rss/" + str(index)})
                    self.assertEqual(result["articles"][0]["title"], title)
                    self.assertEqual(result["articles"][0]["content"], "摘要" + title)

    def test_unsupported_feed_encoding_is_a_typed_source_failure(self):
        cursor = {"seen": {}, "marker": "persist"}
        for label in ("shift_jis", "unknown-charset"):
            rss = '<?xml version="1.0" encoding="' + label + '"?><rss version="2.0"><channel><title>Feed</title></channel></rss>'
            with self.subTest(label=label), patch("pipeline.v2.sources._fetch", return_value=(rss.encode("ascii"), "text/xml", "https://example.com/feed")):
                with self.assertRaises(SourceError) as caught:
                    discover({"id": "rss", "type": "rss", "url": "https://example.com/feed"}, cursor)
                self.assertEqual(caught.exception.code, "decode_failed")
        self.assertEqual(cursor, {"seen": {}, "marker": "persist"})

    def test_legacy_feed_decoding_keeps_entity_guard(self):
        malicious = '<?xml version="1.0" encoding="gb2312"?><!DOCTYPE rss [<!ENTITY 秘密 SYSTEM "file:///C:/secret">]><rss><channel><title>&秘密;</title></channel></rss>'
        with patch("pipeline.v2.sources._fetch", return_value=(malicious.encode("gb2312"), "text/xml", "https://example.com/feed")):
            with self.assertRaises(SourceError) as caught:
                discover({"id": "rss", "type": "rss", "url": "https://example.com/feed"})
        self.assertEqual(caught.exception.code, "unsafe_xml")

    def test_mislabelled_legacy_pages_decode_without_changing_decodable_text(self):
        page = '<html><head><meta charset="gb2312"></head><body><div id="js_content"><p>{}</p></div></body></html>'
        routes = {
            "/gbk": (200, "text/html", page.format("朱镕基" + LONG_TEXT).encode("gbk")),
            "/stale-label": (200, "text/html", page.format(LONG_TEXT).encode("utf-8")),
            # 0xA1A4 decodes under GB2312 itself, so its GB2312 mapping (U+30FB) must survive.
            "/decodable": (200, "text/html; charset=GB2312", page.format(LONG_TEXT).encode("gb2312").replace(b"</p>", b"\xa1\xa4</p>")),
        }
        with _HTTPFixture(routes) as base:
            gbk, stale, decodable = (acquire({"url": base + path}, {"id": "reader"}) for path in routes)
        self.assertEqual(gbk["coverage"], "full")
        self.assertIn("朱镕基", gbk["content"])
        self.assertIn(LONG_TEXT, stale["content"])
        self.assertTrue(decodable["content"].endswith(LONG_TEXT + "\u30fb"))

    def test_utf8_labelled_json_feed_may_start_with_byte_order_mark(self):
        feed = {"version": "https://jsonfeed.org/version/1.1", "title": "Source", "items": [{"id": "p1", "url": "/article", "title": "标题", "content_text": "正文"}]}
        body = b"\xef\xbb\xbf" + json.dumps(feed, ensure_ascii=False).encode("utf-8")
        with _HTTPFixture({"/feed": (200, "application/feed+json; charset=utf-8", body)}) as base:
            result = discover({"id": "json", "type": "json_feed", "url": base + "/feed"})
        self.assertEqual(result["articles"][0]["title"], "标题")

    def test_response_size_limit_is_enforced(self):
        with _HTTPFixture({"/large": (200, "text/html", "X" * 3000)}) as base:
            with self.assertRaises(SourceError) as caught:
                acquire({"url": base + "/large"}, {"id": "reader", "max_bytes": 1024})
        self.assertEqual(caught.exception.code, "response_too_large")

    def test_fixture_html_full_body_is_explicitly_not_live_verification(self):
        source = self.fixture([self.article()])
        item = discover(source)["articles"][0]
        result = acquire(item, source)
        self.assertEqual(result["coverage"], "full")
        self.assertTrue(result["is_fixture"])
        self.assertFalse(result["diagnostics"]["live_source_verified"])
        self.assertNotIn("html", result)

    def test_plain_long_page_is_partial_without_article_structure(self):
        result = acquire(self.article(html="<html><body>" + LONG_TEXT * 3 + "</body></html>"), {"type": "fixture"})
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["content"], "")
        self.assertIn("article_body_not_found", result["diagnostics"]["issues"])

    def test_verification_page_does_not_become_complete_article(self):
        result = acquire(self.article(html='<div id="js_content"><p>请完成下方验证</p><p>' + LONG_TEXT + '</p></div>'), {"type": "fixture"})
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("verification_or_unavailable_page", result["diagnostics"]["issues"])

    def test_image_only_and_embedded_body_require_further_acquisition(self):
        result = acquire(self.article(html='<div id="js_content"><p>短正文</p><img src="a.png"></div>'), {"type": "fixture"})
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("image_text_requires_ocr", result["diagnostics"]["issues"])
        embedded = acquire(self.article(html='<article><p>' + LONG_TEXT + '</p><iframe src="https://example.com/table"></iframe></article>'), {"type": "fixture"})
        self.assertEqual(embedded["coverage"], "partial")
        self.assertIn("embedded_content_requires_review", embedded["diagnostics"]["issues"])

    def test_truncation_and_unclosed_article_are_not_full(self):
        for html, issue in (
            ('<article data-truncated="true"><p>' + LONG_TEXT + '</p></article>', "declared_truncation"),
            ('<article><p>' + LONG_TEXT + '</p>', "unclosed_article_body"),
        ):
            with self.subTest(issue=issue):
                result = acquire(self.article(html=html), {"type": "fixture"})
                self.assertEqual(result["coverage"], "partial")
                self.assertIn(issue, result["diagnostics"]["issues"])

    def test_wechat_shortlink_gets_canonical_identity_from_real_html(self):
        html = ARTICLE_HTML + '<script>var biz = "" || "MzAw123=="; var mid = "123456"; var idx = "2";</script>'
        result = acquire(self.article(url="https://mp.weixin.qq.com/s/opaque", html=html), {"type": "fixture"})
        self.assertEqual(result["canonical_article_id"], "wechat:MzAw123==:123456:2")
        self.assertEqual(result["coverage"], "full")

    def test_local_paths_only_allowed_for_explicit_fixtures(self):
        with self.assertRaises(SourceError):
            discover({"id": "rss", "type": "rss", "path": str(self.fixture_path), "url": "https://example.com/rss"})
        with patch("pipeline.v2.sources._fetch", side_effect=SourceError("source_unavailable", "Unavailable")) as fetch:
            with self.assertRaises(SourceError):
                acquire({"url": "https://example.com/article", "html_path": str(self.fixture_path)}, {"id": "reader"})
            fetch.assert_called_once()

    def test_fixture_relative_html_path(self):
        (self.fixture_path.parent / "article.html").write_text(ARTICLE_HTML, encoding="utf-8")
        source = self.fixture([self.article(html="", html_path="article.html")])
        result = acquire(discover(source)["articles"][0], source)
        self.assertEqual(result["coverage"], "full")

    def test_provided_html_requires_real_retrieval_provenance_from_caller(self):
        result = extract_html(ARTICLE_HTML, "https://mp.weixin.qq.com/s/article")
        self.assertEqual(result["coverage"], "full")
        self.assertEqual(result["acquisition_method"], "provided_html")
        self.assertFalse(result["is_fixture"])
        self.assertFalse(result["diagnostics"]["live_source_verified"])
        self.assertNotIn("acquired_at", result)
        self.assertIn("parsed_at", result)

    def test_literal_html_entities_are_preserved_as_article_text(self):
        result = extract_html('<article><p>标题中的 &lt;条件&gt; 与 2 &lt; 3</p><p>' + LONG_TEXT + '</p></article>', "https://example.com/article")
        self.assertIn("<条件>", result["content"])
        self.assertIn("2 < 3", result["content"])

    def test_provided_html_still_requires_safe_url_and_article_structure(self):
        with self.assertRaises(SourceError):
            extract_html(ARTICLE_HTML, "file:///C:/article")
        result = extract_html("<html><body>" + LONG_TEXT + "</body></html>", "https://example.com/article")
        self.assertEqual(result["coverage"], "partial")

    def werss_source(self, base="http://127.0.0.1:8001"):
        credentials = Path(self.temp.name) / "private-admin.json"
        credentials.write_text(json.dumps({"username": "test-admin", "password": "offline-test-only"}), encoding="utf-8")
        return {"id": "wechat-test", "type": "rss", "url": base + "/rss/MP_WXS_123", "refresh": {
            "type": "werss", "base_url": base, "mp_id": "MP_WXS_123", "credentials_path": str(credentials),
        }}

    def test_werss_stock_success_zero_never_verifies_listing(self):
        source = self.werss_source()
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, {"code": 0, "data": {"total": 0}}]):
            with self.assertRaises(SourceError) as caught:
                refresh_werss(source)
        self.assertEqual(caught.exception.code, "upstream_listing_unverified")

    def test_werss_failure_does_not_request_stale_rss_or_advance_cursor(self):
        source = self.werss_source()
        cursor = {"seen": {}, "last_success_at": "earlier"}
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, SourceError("source_auth_required", "Authorization required")]), patch("pipeline.v2.sources._fetch") as fetch:
            with self.assertRaises(SourceError) as caught:
                discover(source, cursor)
            fetch.assert_not_called()
        self.assertEqual(caught.exception.code, "source_auth_required")
        self.assertEqual(cursor, {"seen": {}, "last_success_at": "earlier"})

    def test_werss_zero_items_requires_valid_observed_page(self):
        proof = {"ok": True, "verified_upstream_listing": True, "source_id": "MP_WXS_123", "acquisition_mode": "web", "valid_pages": 1, "observed_articles": 0, "articles": []}
        routes = {
            "/api/v1/wx/auth/token": (200, "application/json", json.dumps({"access_token": "offline-token"})),
            "/xhs-integration/refresh/MP_WXS_123?start_page=0&end_page=1": (200, "application/json", json.dumps(proof)),
            "/rss/MP_WXS_123": (200, "application/rss+xml", "<rss><channel><title>公众号</title></channel></rss>"),
        }
        with _HTTPFixture(routes) as base:
            result = discover(self.werss_source(base))
        self.assertEqual(result["articles"], [])
        self.assertTrue(result["upstream_refresh"]["verified_upstream_listing"])
        self.assertEqual(result["upstream_refresh"]["valid_pages"], 1)

    def test_werss_batch_items_survive_rss_size_limit(self):
        records = [dict(self.article(url="https://mp.weixin.qq.com/s?__biz=Mabc&mid=555&idx=" + str(index), id=str(index)), published_at=1728000000) for index in (1, 2)]
        proof = {"ok": True, "verified_upstream_listing": True, "source_id": "MP_WXS_123", "acquisition_mode": "web", "valid_pages": 1, "observed_articles": 2, "articles": records}
        rss = '<rss><channel><title>公众号</title><item><guid>1</guid><title>文章标题</title><link>https://mp.weixin.qq.com/s?__biz=Mabc&amp;mid=555&amp;idx=1</link></item></channel></rss>'
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, proof]), patch("pipeline.v2.sources._fetch", return_value=(rss.encode(), "text/xml", "https://example.com/rss")):
            result = discover(self.werss_source())
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(result["upstream_refresh"]["observed_articles"], 2)
        second = next(item for item in result["articles"] if item["article_id"].endswith(":2"))
        self.assertIn("+00:00", second["published_at"])

    def test_werss_census_mismatch_is_not_accepted(self):
        proof = {"ok": True, "verified_upstream_listing": True, "source_id": "MP_WXS_123", "acquisition_mode": "web", "valid_pages": 1, "observed_articles": 1, "articles": []}
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, proof]):
            with self.assertRaises(SourceError):
                refresh_werss(self.werss_source())

    def test_werss_boolean_census_does_not_masquerade_as_integer_count(self):
        proof = {"ok": True, "verified_upstream_listing": True, "source_id": "MP_WXS_123", "acquisition_mode": "web", "valid_pages": True, "observed_articles": False, "articles": []}
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, proof]):
            with self.assertRaises(SourceError):
                refresh_werss(self.werss_source())

    def test_werss_access_failure_returns_safe_code(self):
        routes = {
            "/api/v1/wx/auth/token": (200, "application/json", json.dumps({"access_token": "offline-token"})),
            "/xhs-integration/refresh/MP_WXS_123?start_page=0&end_page=1": (409, "application/json", json.dumps({"detail": {"code": "source_auth_required", "secret": "do-not-print"}})),
        }
        with _HTTPFixture(routes) as base:
            with self.assertRaises(SourceError) as caught:
                refresh_werss(self.werss_source(base))
        self.assertEqual(caught.exception.code, "source_auth_required")
        self.assertNotIn("do-not-print", str(caught.exception))

    def test_werss_rate_limit_preserves_safe_code_and_bypasses_environment_proxy(self):
        class RecordingRoutes(dict):
            def __init__(self, *args):
                super().__init__(*args)
                self.requests = []

            def get(self, key, default=None):
                self.requests.append(key)
                return super().get(key, default)

        auth_path = "/api/v1/wx/auth/token"
        refresh_path = "/xhs-integration/refresh/MP_WXS_123?start_page=0&end_page=1"
        private_token = "offline-private-bearer-do-not-print"
        private_detail = "external-private-error-do-not-print"
        for code, status in (("source_rate_limited", 429), ("source_refresh_cooldown", 409)):
            with self.subTest(code=code):
                direct_routes = RecordingRoutes({
                    auth_path: (200, "application/json", json.dumps({"access_token": private_token})),
                    refresh_path: (status, "application/json", json.dumps({
                        "detail": {"code": code, "message": private_detail, "token": private_token},
                    })),
                    "/rss/MP_WXS_123": (200, "application/rss+xml", "<rss><channel><title>stale feed</title></channel></rss>"),
                })
                proxy_routes = RecordingRoutes()
                cursor = {"seen": {"old-article": "old-fingerprint"}, "last_success_at": "earlier"}
                saved_cursor = copy.deepcopy(cursor)
                with _HTTPFixture(proxy_routes) as proxy, _HTTPFixture(direct_routes) as base:
                    environment = {"HTTP_PROXY": proxy, "http_proxy": proxy, "HTTPS_PROXY": proxy,
                                   "https_proxy": proxy, "ALL_PROXY": proxy, "all_proxy": proxy,
                                   "NO_PROXY": "", "no_proxy": ""}
                    with patch.dict("os.environ", environment), patch("pipeline.v2.sources._fetch") as rss:
                        with self.assertRaises(SourceError) as caught:
                            discover(self.werss_source(base), cursor)
                        rss.assert_not_called()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(cursor, saved_cursor)
                self.assertEqual(direct_routes.requests, [auth_path, refresh_path])
                self.assertEqual(proxy_routes.requests, [])
                for secret in (private_detail, private_token, "offline-test-only"):
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertNotIn(secret, repr(caught.exception))

    def test_fixture_cannot_trigger_live_refresh(self):
        source = self.fixture([self.article()])
        source["refresh"] = self.werss_source()["refresh"]
        with patch("pipeline.v2.sources.refresh_werss") as refresh:
            with self.assertRaises(SourceError):
                discover(source)
            refresh.assert_not_called()

    def test_werss_upstream_error_categories_survive_transport(self):
        for code, status in (("source_auth_expired", 409), ("source_invalid_arguments", 502),
                             ("upstream_tls_error", 502), ("upstream_timeout", 502),
                             ("malformed_upstream_response", 502)):
            with self.subTest(code=code):
                routes = {
                    "/api/v1/wx/auth/token": (200, "application/json", json.dumps({"access_token": "offline-token"})),
                    "/xhs-integration/refresh/MP_WXS_123?start_page=0&end_page=1":
                        (status, "application/json", json.dumps({"detail": {"code": code}})),
                }
                with _HTTPFixture(routes) as base:
                    with self.assertRaises(SourceError) as caught:
                        refresh_werss(self.werss_source(base))
                self.assertEqual(caught.exception.code, code)

    def test_werss_response_diagnostics_exclude_credentials_and_untrusted_text(self):
        private = "private-value-must-not-enter-diagnostics"
        evidence = {"refresh_id": "a" * 32, "upstream_request_count": 1, "cookie": private,
                    "upstream_requests": [
                        {"path": "/cgi-bin/appmsgpublish", "request_index": 1,
                         "started_at_epoch": 1791110000, "elapsed_ms": 17,
                         "http_status": 200, "base_resp_ret": 200013,
                         "headers": {"Authorization": private}, "err_msg": private, "url": private},
                        {"path": "/cgi-bin/appmsgpublish?token=" + private, "http_status": 200},
                        {"path": [private], "http_status": True},
                    ]}
        routes = {
            "/api/v1/wx/auth/token": (200, "application/json", json.dumps({"access_token": private})),
            "/xhs-integration/refresh/MP_WXS_123?start_page=0&end_page=1":
                (429, "application/json", json.dumps({"detail": {"code": "source_rate_limited",
                    "diagnostics": evidence, "message": private, "retry_not_before_epoch": 1791111000}})),
        }
        with _HTTPFixture(routes) as base:
            with self.assertRaises(SourceError) as caught:
                refresh_werss(self.werss_source(base))
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["upstream_request_count"], 1)
        self.assertEqual(diagnostics["upstream_requests"], [{
            "path": "/cgi-bin/appmsgpublish", "request_index": 1, "started_at_epoch": 1791110000,
            "elapsed_ms": 17, "http_status": 200, "base_resp_ret": 200013}])
        self.assertNotIn(private, json.dumps(diagnostics))
        self.assertNotIn(private, repr(caught.exception))

    def test_werss_malformed_diagnostics_do_not_obscure_failure(self):
        from pipeline.v2.sources import _safe_werss_diagnostics
        result = _safe_werss_diagnostics({"diagnostics": {
            "refresh_id": ["private"], "upstream_request_count": True,
            "upstream_requests": [{"path": "/cgi-bin/appmsgpublish", "http_status": True,
                "base_resp_ret": "private", "elapsed_ms": -1, "transport_error": ["private"]}]},
            "retry_not_before_epoch": True})
        self.assertEqual(result, {"upstream_requests": [{"path": "/cgi-bin/appmsgpublish"}]})

    def werss_page(self, items, *, continuation=None, ended=False):
        return {"verified_upstream_listing": True, "source_id": "MP_WXS_123", "acquisition_mode": "web", "valid_pages": 1,
                "observed_articles": len(items), "articles": items, "continuation_token": continuation, "end_of_list": ended,
                "refreshed_at": "2026-10-04T00:00:00+00:00"}

    def weread_source_and_proof(self):
        source = self.werss_source()
        source["refresh"]["acquisition_mode"] = "weread_mp"
        source["capabilities"] = {"supports_listing": True, "supports_backfill": True, "supports_multi_article": True}
        proof = {"ok": True, "verified_upstream_listing": False, "verified_latest_article": True,
                 "source_id": "MP_WXS_123", "acquisition_mode": "weread_mp", "valid_pages": 1,
                 "observed_articles": 1, "articles": [self.article()], "coverage_mode": "latest_one_only",
                 "supports_backfill": False, "supports_multi_article": False, "end_of_list": False,
                 "continuation_token": None, "cookie_refresh": {"status": "refreshed", "cooldown_hours": 6},
                 "body_validation": {"selector": "#js_content", "readable_characters": 100,
                                     "image_count": 0, "cached": False, "html_sha256": "b" * 64}}
        return source, proof

    def test_weread_latest_probe_does_not_claim_historical_interval_or_batch_coverage(self):
        source, proof = self.weread_source_and_proof()
        cursor = {"seen": {"previous-article": "old-fingerprint"}}
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, proof]), patch("pipeline.v2.sources._fetch", return_value=(b"<rss><channel><title>MP</title></channel></rss>", "text/xml", source["url"])):
            result = discover(source, cursor)
        self.assertFalse(result["upstream_refresh"]["verified_upstream_listing"])
        self.assertTrue(result["upstream_refresh"]["verified_latest_article"])
        self.assertFalse(result["upstream_refresh"]["historical_interval_verified"])
        self.assertEqual(result["upstream_refresh"]["coverage_probe"], "latest_cover_only")
        for capability in ("supports_listing", "supports_backfill", "supports_multi_article"):
            self.assertFalse(result["capabilities"][capability])
        self.assertEqual(result["capabilities"]["coverage_mode"], "latest_one_only")
        self.assertEqual(cursor, {"seen": {"previous-article": "old-fingerprint"}})

    def test_weread_rejects_full_list_claim_and_unverified_or_empty_body(self):
        source, original = self.weread_source_and_proof()
        for change in ({"verified_upstream_listing": True}, {"observed_articles": 0, "articles": []},
                       {"supports_backfill": True}, {"end_of_list": True}, {"body_validation": {}},
                       {"acquisition_mode": "web"}):
            with self.subTest(change=change), patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, {**original, **change}]):
                with self.assertRaises(SourceError):
                    refresh_werss(source)

    def test_weread_cannot_request_a_historical_page(self):
        source, proof = self.weread_source_and_proof()
        source["refresh"].update(start_page=1, end_page=2)
        with patch("pipeline.v2.sources._werss_json") as transport:
            with self.assertRaises(SourceError) as error:
                refresh_werss(source)
            transport.assert_not_called()
        self.assertEqual(error.exception.code, "invalid_werss_config")

    def test_weread_live_author_survives_rss_omission_without_discarding_body(self):
        source, proof = self.weread_source_and_proof()
        proof["articles"][0]["author"] = "目标公众号"
        rss = '<rss><channel><title>MP</title><item><guid>stable-1</guid><title>文章标题</title><link>https://example.com/post</link><description>实际 RSS 正文</description></item></channel></rss>'
        with patch("pipeline.v2.sources._werss_json", side_effect=[{"access_token": "offline-token"}, proof]), patch("pipeline.v2.sources._fetch", return_value=(rss.encode(), "text/xml", source["url"])):
            result = discover(source)
        self.assertEqual(result["articles"][0]["author"], "目标公众号")
        self.assertEqual(result["articles"][0]["content"], "实际 RSS 正文")

    def test_weread_response_metadata_rejects_arbitrary_private_strings(self):
        from pipeline.v2.sources import _safe_weread_metadata
        result = _safe_weread_metadata({"cookie_refresh": {"status": ["private"], "last_success_epoch": True},
                                       "body_validation": {"selector": "private", "html_sha256": "private", "cached": "private"}})
        self.assertEqual(result, {"cookie_refresh": {}, "body_validation": {}})

    def test_werss_stored_rss_overlap_cannot_hide_upstream_coverage_gap(self):
        source = self.werss_source()
        old = self.article(url="https://example.com/old", id="old")
        new = self.article(url="https://example.com/new", id="new")
        cursor = {"seen": {article_identity(old["url"], old["id"]): "old-fingerprint"}}
        saved = copy.deepcopy(cursor)
        with patch("pipeline.v2.sources.refresh_werss", return_value=self.werss_page([new])), patch("pipeline.v2.sources._fetch") as rss:
            with self.assertRaises(SourceError) as caught:
                discover(source, cursor)
            rss.assert_not_called()
        self.assertEqual(caught.exception.code, "source_coverage_gap")
        self.assertEqual(cursor, saved)
        self.assertEqual(caught.exception.diagnostics["pages_scanned"], 1)
        self.assertFalse(caught.exception.diagnostics["continuation_available"])

    def test_werss_continues_real_pages_until_prior_cursor_overlap(self):
        source = self.werss_source()
        old = self.article(url="https://example.com/old", id="old")
        new = self.article(url="https://example.com/new", id="new")
        cursor = {"seen": {article_identity(old["url"], old["id"]): "old-fingerprint"}}
        empty_rss = "<rss><channel><title>公众号</title></channel></rss>".encode("utf-8")
        with patch("pipeline.v2.sources.refresh_werss", side_effect=[self.werss_page([new], continuation="next-page-proof"), self.werss_page([old])]) as refresh, patch("pipeline.v2.sources._fetch", return_value=(empty_rss, "text/xml", source["url"])):
            result = discover(source, cursor)
        self.assertEqual(result["upstream_refresh"]["coverage_probe"], "prior_cursor_overlap")
        self.assertEqual(result["upstream_refresh"]["pages_scanned"], 2)
        second_config = refresh.call_args_list[1].args[0]["refresh"]
        self.assertEqual(second_config["start_page"], 1)
        self.assertEqual(second_config["end_page"], 2)
        self.assertEqual(second_config["continuation_token"], "next-page-proof")
        self.assertNotIn("continuation_token", source["refresh"])
        self.assertNotIn("continuation_token", result["upstream_refresh"])

    def test_werss_page_budget_exhaustion_preserves_cursor(self):
        source = self.werss_source()
        source["refresh"]["max_pages_per_poll"] = 2
        cursor = {"seen": {article_identity("https://example.com/old", "old"): "old-fingerprint"}}
        pages = [self.werss_page([self.article(url="https://example.com/" + str(index), id=str(index))], continuation="continue-" + str(index)) for index in (1, 2)]
        saved = copy.deepcopy(cursor)
        with patch("pipeline.v2.sources.refresh_werss", side_effect=pages), patch("pipeline.v2.sources._fetch") as rss:
            with self.assertRaises(SourceError) as caught:
                discover(source, cursor)
            rss.assert_not_called()
        self.assertEqual(caught.exception.code, "source_coverage_gap")
        self.assertEqual(caught.exception.diagnostics["pages_scanned"], 2)
        self.assertEqual(caught.exception.diagnostics["page_budget"], 2)
        self.assertEqual(cursor, saved)

    def test_werss_explicit_empty_upstream_page_proves_list_end(self):
        source = self.werss_source()
        cursor = {"seen": {article_identity("https://example.com/old", "old"): "old-fingerprint"}}
        pages = [self.werss_page([self.article()], continuation="next-page"), self.werss_page([], ended=True)]
        empty_rss = "<rss><channel><title>公众号</title></channel></rss>".encode("utf-8")
        with patch("pipeline.v2.sources.refresh_werss", side_effect=pages), patch("pipeline.v2.sources._fetch", return_value=(empty_rss, "text/xml", source["url"])):
            result = discover(source, cursor)
        self.assertEqual(result["upstream_refresh"]["coverage_probe"], "explicit_list_end")
        self.assertEqual(result["upstream_refresh"]["pages_scanned"], 2)

    def test_werss_auth_loss_mid_backfill_records_gap_cause(self):
        source = self.werss_source()
        cursor = {"seen": {article_identity("https://example.com/old", "old"): "old-fingerprint"}}
        with patch("pipeline.v2.sources.refresh_werss", side_effect=[self.werss_page([self.article()], continuation="next-page"), SourceError("source_auth_required", "auth lost")]):
            with self.assertRaises(SourceError) as caught:
                discover(source, cursor)
        self.assertEqual(caught.exception.code, "source_coverage_gap")
        self.assertEqual(caught.exception.diagnostics["upstream_error_code"], "source_auth_required")

    def test_werss_first_baseline_is_one_window_without_full_history_claim(self):
        source = self.werss_source()
        empty_rss = "<rss><channel><title>公众号</title></channel></rss>".encode("utf-8")
        with patch("pipeline.v2.sources.refresh_werss", return_value=self.werss_page([self.article()], continuation="next-page")) as refresh, patch("pipeline.v2.sources._fetch", return_value=(empty_rss, "text/xml", source["url"])):
            result = discover(source)
        refresh.assert_called_once()
        self.assertEqual(result["upstream_refresh"]["coverage_probe"], "initial_baseline_window")
        self.assertFalse(result["capabilities"]["supports_backfill"])


if __name__ == "__main__":
    unittest.main()
