"""Managed Weread MP collection and Cookie renewal for this deployment."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit

import requests
import yaml
from bs4 import BeautifulSoup


class WereadIntegrationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_cover(payload, book_id, account_name):
    if not isinstance(payload, dict):
        raise WereadIntegrationError("malformed_upstream_response")
    code = payload.get("errCode", payload.get("errcode", 0))
    if code not in (None, 0, "0"):
        raise WereadIntegrationError("source_auth_expired" if code in (-2012, -2041, -2010) else "upstream_access_or_http_error")
    review_id = payload.get("reviewId")
    name = payload.get("name")
    title = payload.get("title")
    prefix = book_id + "_"
    if (not isinstance(review_id, str) or not review_id.startswith(prefix)
            or not re.fullmatch(r"[A-Za-z0-9_~=-]{1,200}", review_id[len(prefix):])
            or not isinstance(title, str) or not title.strip()
            or not isinstance(name, str) or name.strip() != account_name):
        raise WereadIntegrationError("source_identity_mismatch")
    return {"review_id": review_id, "title": title.strip(), "author": name.strip(),
            "url": "https://mp.weixin.qq.com/s/" + quote(review_id[len(prefix):], safe="~"),
            "cover": payload.get("pic", "") if isinstance(payload.get("pic", ""), str) else ""}


def parse_body(raw_html, article):
    soup = BeautifulSoup(raw_html, "html.parser")
    node = soup.select_one("#js_content")
    if node is None:
        raise WereadIntegrationError("upstream_content_unverified")
    for selector, expected in (("#activity-name", article["title"]), ("#js_name", article["author"])):
        observed = soup.select_one(selector)
        if observed and observed.get_text(" ", strip=True) != expected:
            raise WereadIntegrationError("source_identity_mismatch")
    for element in node.select("script,style,noscript,template"):
        element.decompose()
    text = node.get_text("\n", strip=True)
    chars = len(re.sub(r"\s", "", text))
    if chars < 80 or any(marker in text for marker in ("环境异常", "请完成下方验证", "该内容已被发布者删除", "访问过于频繁")):
        raise WereadIntegrationError("upstream_content_unverified")
    # Observed page timestamp; collection time must not become publication time.
    match = re.search(r"\bvar\s+ct\s*=\s*[\x22\x27]?(\d{10})", raw_html)
    published = int(match.group(1)) if match else 0
    return {"content": node.decode_contents().strip(), "readable_characters": chars,
            "image_count": len(node.select("img")), "publish_time": published,
            "html_sha256": hashlib.sha256(raw_html.encode("utf-8")).hexdigest()}


class WereadIntegration:
    def __init__(self, cfg, data_dir=Path("/app/data")):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.license_path = self.data_dir / "wx.lic"
        self.profile_path = self.data_dir / "weread-chrome-profile"
        self.evidence_dir = self.data_dir / "files/weread"

    def load_auth(self):
        document = yaml.safe_load(self.license_path.read_text(encoding="utf-8")) or {}
        data = document.get("weread_data", {})
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise WereadIntegrationError("source_auth_required")
        return document, data

    def write_license(self, document):
        temporary = self.license_path.with_suffix(".renewal.tmp")
        temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(self.license_path)

    def cookie(self):
        _, data = self.load_auth()
        value = self.cfg.get("weread.cookie", "") or data.get("cookie", "")
        if not isinstance(value, str) or not value.strip():
            raise WereadIntegrationError("source_auth_required")
        return value.strip()

    def request(self, path, params, cookie, *, html=False):
        headers = {"Cookie": cookie, "User-Agent": self.cfg.get("user_agent", "Mozilla/5.0"),
                   "Accept": "text/html,application/xhtml+xml,*/*" if html else "application/json, text/plain, */*",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "Origin": "https://weread.qq.com",
                   "Referer": "https://weread.qq.com/"}
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get("https://weread.qq.com" + path, params=params, headers=headers,
                                       verify=True, allow_redirects=False, timeout=(10, 30))
        except requests.exceptions.SSLError:
            raise WereadIntegrationError("upstream_tls_error") from None
        except requests.exceptions.Timeout:
            raise WereadIntegrationError("upstream_timeout") from None
        except requests.exceptions.RequestException:
            raise WereadIntegrationError("upstream_http_error") from None
        if response.status_code == 429:
            raise WereadIntegrationError("source_rate_limited")
        if response.status_code in (401, 403):
            raise WereadIntegrationError("source_auth_expired")
        if response.status_code != 200:
            raise WereadIntegrationError("upstream_access_or_http_error")
        if len(response.content) > 4 * 1024 * 1024:
            raise WereadIntegrationError("upstream_content_unverified")
        if html:
            return response.content.decode("utf-8")
        try:
            return response.json()
        except ValueError:
            raise WereadIntegrationError("malformed_upstream_response") from None

    def get_cover(self, book_id, account_name, cookie):
        return validate_cover(self.request("/api/mp/cover", {"bookId": book_id}, cookie), book_id, account_name)

    def renew_cookie(self, book_id, account_name):
        _, data = self.load_auth()
        url = data.get("cookie_refresh_url", "")
        expected_url = "https://weread.qq.com/web/mp/reader/" + book_id
        if not url:
            return {"status": "disabled"}, None
        if url != expected_url:
            raise WereadIntegrationError("cookie_refresh_config_invalid")
        if self.cfg.get("weread.cookie", ""):
            raise WereadIntegrationError("cookie_refresh_config_invalid")
        try:
            last = float(data.get("cookie_refresh_last_ts", 0))
        except (ValueError, TypeError):
            last = 0
        if 0 <= time.time() - last < 6 * 3600:
            return {"status": "not_due", "last_success_epoch": int(last), "cooldown_hours": 6}, None
        seed = self.cookie()
        candidate = self.browser_cookie(url, seed, data.get("browser_path", ""))
        if not candidate:
            raise WereadIntegrationError("source_auth_expired")
        # The new cover endpoint verifies both the candidate and the exact feed.
        cover = self.get_cover(book_id, account_name, candidate)
        document, current = self.load_auth()
        current["cookie"] = candidate
        for item in candidate.split(";"):
            name, _, value = item.strip().partition("=")
            if name == "wr_vid":
                current["vid"] = value
        current["cookie_refresh_last_ts"] = time.time()
        document["weread_data"] = current
        self.write_license(document)
        return {"status": "refreshed", "last_success_epoch": int(current["cookie_refresh_last_ts"]),
                "cooldown_hours": 6, "cookie_value_changed": seed != candidate,
                "verified_with": "/api/mp/cover", "browser_profile": str(self.profile_path)}, cover

    def browser_cookie(self, url, seed, browser_path):
        # This is a dedicated service profile, seeded from its managed login.
        from playwright.sync_api import sync_playwright
        self.profile_path.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            executable = browser_path or playwright.chromium.executable_path
            if not Path(executable).is_file():
                raise WereadIntegrationError("cookie_refresh_browser_unavailable")
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(self.profile_path), executable_path=executable, headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"], ignore_https_errors=False)
            except Exception:
                raise WereadIntegrationError("cookie_refresh_browser_unavailable") from None
            try:
                cookies = {}
                for item in seed.split(";"):
                    name, _, value = item.strip().partition("=")
                    if name and value and name not in cookies:
                        cookies[name] = {"name": name, "value": value, "url": "https://weread.qq.com"}
                context.add_cookies(list(cookies.values()))
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    raise WereadIntegrationError("cookie_refresh_page_unavailable") from None
                current = context.cookies("https://weread.qq.com")
                return "; ".join(f"{item['name']}={item['value']}" for item in current)
            finally:
                context.close()

    def refresh_feed(self, session, feed, evidence):
        from core.models.article import Article
        renewed, article = self.renew_cookie(feed.id, feed.mp_name)
        if article is None:
            article = self.get_cover(feed.id, feed.mp_name, self.cookie())
        identifier = f"{feed.id}-{article['review_id']}".replace("MP_WXS_", "")
        row = session.query(Article).filter(Article.id == identifier).first()
        evidence_path = self.evidence_dir / (hashlib.sha256(article["review_id"].encode("utf-8")).hexdigest() + ".html")
        cached = False
        if row and row.has_content == 1 and evidence_path.is_file():
            try:
                raw_html = evidence_path.read_text(encoding="utf-8")
                parsed = parse_body(raw_html, article)
                cached = True
            except WereadIntegrationError:
                pass
        if not cached:
            time.sleep(max(float(self.cfg.get("weread.content_interval", 2) or 2), 2))
            raw_html = self.request("/web/mp/content", {"reviewId": article["review_id"]}, self.cookie(), html=True)
            parsed = parse_body(raw_html, article)
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            temporary = evidence_path.with_suffix(".tmp")
            temporary.write_text(raw_html, encoding="utf-8")
            temporary.replace(evidence_path)
        # Validate the body before committing. A failed body remains retryable.
        if row is None:
            row = Article(id=identifier, mp_id=feed.id, status=1, created_at=datetime.now())
            session.add(row)
        row.title, row.url, row.pic_url = article["title"], article["url"], article["cover"]
        row.content = row.content_html = parsed["content"]
        row.has_content = 1
        row.publish_time = row.create_time = parsed["publish_time"]
        row.updated_at = int(time.time())
        row.updated_at_millis = int(time.time() * 1000)
        row.extinfo = json.dumps({"method": "weread_mp", "review_id": article["review_id"],
                                  "source_html_sha256": parsed["html_sha256"], "evidence_file": evidence_path.name})
        feed.sync_time = int(time.time())
        if parsed["publish_time"]:
            feed.update_time = parsed["publish_time"]
        session.commit()
        evidence["valid_pages"] = 1
        evidence["observed_articles"] = 1
        return {"ok": True, "verified_upstream_listing": False, "verified_latest_article": True,
                "source_id": feed.id, "valid_pages": 1, "observed_articles": 1,
                "acquisition_mode": "weread_mp", "coverage_mode": "latest_one_only",
                "supports_backfill": False, "supports_multi_article": False,
                "end_of_list": False, "continuation_token": None, "cookie_refresh": renewed,
                "body_validation": {"selector": "#js_content", "readable_characters": parsed["readable_characters"],
                                    "image_count": parsed["image_count"], "images_ocr_performed": False,
                                    "cached": cached, "html_sha256": parsed["html_sha256"]},
                "articles": [{"id": identifier, "url": article["url"], "title": article["title"],
                              "author": article["author"], "published_at": parsed["publish_time"] or "", "content": ""}]}
