// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/proto"
	"github.com/xpzouying/xiaohongshu-mcp/browser"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
)

type integrationPreflight struct {
	Phase               string             `json:"phase"`
	Prepared            bool               `json:"prepared"`
	ContentHash         string             `json:"content_hash"`
	EditorSessionID     string             `json:"editor_session_id"`
	ExpiresAt           string             `json:"expires_at"`
	Account             integrationAccount `json:"account"`
	Title               string             `json:"title"`
	Content             string             `json:"content"`
	Tags                []string           `json:"tags"`
	ImageCount          int                `json:"image_count"`
	UploadOrderVerified bool               `json:"upload_order_verified"`
	ImagesOrderVerified bool               `json:"images_order_verified"`
	ImageMatchVerified  bool               `json:"image_match_verified"`
	AIDeclared          bool               `json:"ai_declared"`
	ObservedAt          string             `json:"observed_at"`
	EvidenceRef         string             `json:"evidence_ref"`
	EvidenceSHA256      string             `json:"evidence_sha256"`
	RecordRef           string             `json:"record_ref"`
	ImageEvidenceRefs   []string           `json:"image_evidence_refs"`
	ImageEvidenceSHA256 []string           `json:"image_evidence_sha256"`
}

type integrationSession struct {
	request   integrationRequest
	preflight integrationPreflight
	snapshot  *xiaohongshu.IntegrationEditorSnapshot
	page      *rod.Page
	close     func()
	expires   time.Time
}

type integrationService struct {
	mu       sync.Mutex // serializes account/editor operations and attempt consumption
	dir      string
	journal  *integrationJournal
	sessions map[string]*integrationSession
}

func newIntegrationService(dir string) (*integrationService, error) {
	j, err := newIntegrationJournal(dir)
	if err != nil {
		return nil, err
	}
	if err = os.MkdirAll(filepath.Join(dir, "evidence"), 0700); err != nil {
		return nil, err
	}
	return &integrationService{dir: dir, journal: j, sessions: make(map[string]*integrationSession)}, nil
}

func integrationNonce() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func fileSHA256(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:]), nil
}

func validateIntegrationRequest(r integrationRequest) error {
	if !integrationID.MatchString(r.AttemptID) || !integrationHash.MatchString(r.ContentHash) {
		return errors.New("INVALID_ATTEMPT_OR_HASH")
	}
	if r.ExpectedAccount.UserID == "" || r.ExpectedAccount.RedID == "" {
		return errors.New("EXPECTED_ACCOUNT_REQUIRED")
	}
	if !r.AIGenerated {
		return errors.New("AI_DECLARATION_REQUIRED")
	}
	if strings.TrimSpace(r.Title) == "" || strings.TrimSpace(r.Content) == "" {
		return errors.New("EMPTY_TITLE_OR_BODY")
	}
	if len(r.Images) == 0 || len(r.Images) > 18 || len(r.Images) != len(r.ImageHashes) || len(r.Tags) > 10 {
		return errors.New("INVALID_IMAGE_OR_TAG_COUNT")
	}
	for i, p := range r.Images {
		if !filepath.IsAbs(p) || !integrationHash.MatchString(r.ImageHashes[i]) {
			return errors.New("LOCAL_IMAGE_PATH_AND_HASH_REQUIRED")
		}
		actual, err := fileSHA256(p)
		if err != nil || actual != r.ImageHashes[i] {
			return fmt.Errorf("IMAGE_FILE_CHANGED: index %d", i)
		}
	}
	return nil
}

func integrationIdentityMatches(expected, actual integrationAccount) bool {
	return actual.Authenticated && actual.UserID != "" && actual.RedID != "" && expected.UserID == actual.UserID && expected.RedID == actual.RedID && (expected.Nickname == "" || expected.Nickname == actual.Nickname)
}

func (s *integrationService) capture(page *rod.Page, label string, record any) (string, string, string, error) {
	return s.captureScreenshot(page, label, record, true)
}

func (s *integrationService) captureScreenshot(page *rod.Page, label string, record any, fullPage bool) (string, string, string, error) {
	stem := label + "-" + integrationNonce()
	png := filepath.Join(s.dir, "evidence", stem+".png")
	b, err := page.Screenshot(fullPage, &proto.PageCaptureScreenshot{Format: proto.PageCaptureScreenshotFormatPng})
	if err != nil {
		return "", "", "", err
	}
	if err = os.WriteFile(png, b, 0600); err != nil {
		return "", "", "", err
	}
	h := sha256.Sum256(b)
	path := filepath.Join(s.dir, "evidence", stem+".json")
	if err = exclusiveJSON(path, record); err != nil {
		return "", "", "", err
	}
	return png, hex.EncodeToString(h[:]), path, nil
}

// Uses a fresh page in the same isolated browser as the prepared editor. It
// reads actual login and profile state; it never substitutes expected identity.
func (s *integrationService) identityOnPage(ctx context.Context, page *rod.Page) (a integrationAccount, err error) {
	a = integrationAccount{ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	// Upstream profile navigation still uses Must* helpers. A selector timeout
	// or disconnected page is an identity failure with evidence, never a login.
	defer func() {
		if recover() != nil {
			a = integrationAccount{ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
			err = captureIdentityFailure(page, "IDENTITY_FAILED")
		}
	}()
	login := xiaohongshu.NewLogin(page)
	ok, err := login.CheckLoginStatus(ctx)
	if err != nil {
		code := "IDENTITY_FAILED"
		if err.Error() == "LOGIN_STATUS_TIMEOUT" || err.Error() == "LOGIN_NETWORK_RESTRICTED" {
			code = err.Error()
		}
		return a, captureIdentityFailure(page, code)
	}
	if !ok {
		return a, captureIdentityFailure(page, "NOT_AUTHENTICATED")
	}
	user, err := login.CurrentUser(ctx)
	if err != nil || user == nil || user.UserID == "" || user.Nickname == "" {
		return a, captureIdentityFailure(page, "ACCOUNT_ID_UNREADABLE")
	}
	profile, err := xiaohongshu.NewUserProfileAction(page).GetMyProfileViaSidebar(ctx, xiaohongshu.TabNotes)
	if err != nil || profile == nil || profile.UserBasicInfo.RedId == "" {
		return a, captureIdentityFailure(page, "ACCOUNT_RED_ID_UNREADABLE")
	}
	// The current personal profile URL must agree with the authenticated user.
	info, err := page.Info()
	if err != nil {
		return a, captureIdentityFailure(page, "PROFILE_ACCOUNT_MISMATCH")
	}
	profileURL, urlErr := url.Parse(info.URL)
	if urlErr != nil || profileURL.Hostname() != "www.xiaohongshu.com" || strings.TrimRight(profileURL.Path, "/") != "/user/profile/"+user.UserID {
		return a, captureIdentityFailure(page, "PROFILE_ACCOUNT_MISMATCH")
	}
	if profile.UserBasicInfo.Nickname != user.Nickname {
		return a, captureIdentityFailure(page, "PROFILE_NICKNAME_MISMATCH")
	}
	a.Authenticated, a.UserID, a.RedID, a.Nickname = true, user.UserID, profile.UserBasicInfo.RedId, user.Nickname
	png, _, _, err := s.capture(page, "identity", a)
	if err != nil {
		return integrationAccount{}, captureIdentityFailure(page, "IDENTITY_FAILED")
	}
	a.EvidenceRef = png
	return a, nil
}

func (s *integrationService) identity(ctx context.Context) (integrationAccount, error) {
	if blocked := loadLoginNetworkBlock(s.dir); blocked != nil {
		return integrationAccount{}, blocked
	}
	b := newBrowser()
	defer safeIntegrationClose(b.Close)
	p := b.NewPage()
	defer p.Close()
	return s.identityOnPage(ctx, p)
}

func (s *integrationService) expireLocked() {
	for id, session := range s.sessions {
		if time.Now().After(session.expires) {
			safeIntegrationClose(session.close)
			delete(s.sessions, id)
		}
	}
}

func (s *integrationService) prepare(ctx context.Context, r integrationRequest) (result *integrationPreflight, err error) {
	if blocked := loadLoginNetworkBlock(s.dir); blocked != nil {
		return nil, blocked
	}
	if err := validateIntegrationRequest(r); err != nil {
		return nil, err
	}
	s.expireLocked()
	if len(s.sessions) >= 3 {
		return nil, errors.New("PREPARED_SESSION_LIMIT: wait for expiry")
	}
	stage := "browser_open"
	var p, identityPage *rod.Page
	var closeBrowser func()
	kept := false
	defer func() {
		if recover() != nil {
			result = nil
			err = errors.New("PREFLIGHT_PANIC")
		}
		if err != nil {
			// Identity errors already carry an actual-page diagnostic. All other
			// preflight failures are captured BEFORE any page/browser is closed,
			// with an independent bounded context even after HTTP cancellation.
			if _, diagnosed := err.(*loginDiagnosticError); !diagnosed {
				failurePage := p
				if stage == "identity_check" && identityPage != nil {
					failurePage = identityPage
				}
				err = capturePreflightFailure(failurePage, stage, err)
			}
		}
		if identityPage != nil {
			safeIntegrationClose(func() { identityPage.Close() })
		}
		if !kept {
			if p != nil {
				safeIntegrationClose(func() { p.Close() })
			}
			if closeBrowser != nil {
				safeIntegrationClose(closeBrowser)
			}
		}
	}()
	b := newBrowser()
	closeBrowser = b.Close
	p = b.NewPage()
	stage = "identity_check"
	identityPage = b.NewPage()
	account, err := s.identityOnPage(ctx, identityPage)
	if err != nil {
		return nil, err
	}
	if !integrationIdentityMatches(r.ExpectedAccount, account) {
		return nil, errors.New("ACCOUNT_MISMATCH")
	}
	safeIntegrationClose(func() { identityPage.Close() })
	identityPage = nil
	stage = "editor_open"
	action, err := xiaohongshu.NewPublishImageAction(p.Context(ctx))
	if err != nil {
		return nil, err
	}
	stage = "editor_prepare"
	snapshot, err := action.PrepareIntegration(ctx, xiaohongshu.PublishImageContent{Title: r.Title, Content: r.Content, Tags: r.Tags, ImagePaths: r.Images, AIGenerated: true})
	if err != nil {
		return nil, err
	}
	stage = "publish_available"
	if err = xiaohongshu.IntegrationPublishAvailable(p.Context(ctx).Timeout(10 * time.Second)); err != nil {
		return nil, err
	}
	stage = "image_evidence"
	imageEvidence, err := s.captureUploadedImages(p.Context(ctx), snapshot.PreviewSrcs)
	if err != nil {
		return nil, err
	}
	// The temporary evidence layer never edits platform content. Prove its
	// removal left the same editor, ordered previews and strict AI declaration.
	after, err := xiaohongshu.ReadIntegrationEditor(p.Context(ctx).Timeout(30 * time.Second))
	if err != nil {
		return nil, err
	}
	if err = xiaohongshu.ValidateIntegrationEditor(after, r.Title, r.Content, r.Tags, len(r.Images)); err != nil {
		return nil, err
	}
	if after.RawContent != snapshot.RawContent || after.Content != snapshot.Content || after.Title != snapshot.Title || !reflect.DeepEqual(after.PreviewSrcs, snapshot.PreviewSrcs) || !reflect.DeepEqual(after.TopicNames, snapshot.TopicNames) {
		return nil, errors.New("PREPARED_EDITOR_CHANGED")
	}
	sessionID := integrationNonce()
	expires := time.Now().Add(15 * time.Minute)
	record := map[string]any{"account": account, "editor": snapshot, "content_hash": r.ContentHash, "payload_hash": r.payloadHash(), "image_hashes": r.ImageHashes, "expected_paths": r.Images, "image_match_verified": false, "upload_order_verified": true, "editor_session_id": sessionID, "observed_at": time.Now().UTC().Format(time.RFC3339Nano), "image_evidence_refs": imageEvidence.Refs, "image_evidence_sha256": imageEvidence.SHA256, "image_evidence": imageEvidence.Images}
	stage = "evidence_capture"
	png, sha, recordRef, err := s.capture(p.Context(ctx).Timeout(30*time.Second), "preflight", record)
	if err != nil {
		return nil, err
	}
	pre := integrationPreflight{Phase: "PREPARED", Prepared: true, ContentHash: r.ContentHash, EditorSessionID: sessionID, ExpiresAt: expires.UTC().Format(time.RFC3339Nano), Account: account, Title: snapshot.Title, Content: snapshot.Content, Tags: r.Tags, ImageCount: len(snapshot.PreviewSrcs), UploadOrderVerified: true, ImagesOrderVerified: false, ImageMatchVerified: false, AIDeclared: snapshot.AIDeclared, ObservedAt: time.Now().UTC().Format(time.RFC3339Nano), EvidenceRef: png, EvidenceSHA256: sha, RecordRef: recordRef, ImageEvidenceRefs: imageEvidence.Refs, ImageEvidenceSHA256: imageEvidence.SHA256}
	// Detach from the completed HTTP request. The page lives until explicit
	// consumption/expiry; submit gets its own short request context.
	session := &integrationSession{request: r, preflight: pre, snapshot: snapshot, page: p.Context(context.Background()), expires: expires, close: func() { safeIntegrationClose(func() { p.Close() }); safeIntegrationClose(b.Close) }}
	s.sessions[sessionID] = session
	kept = true
	time.AfterFunc(15*time.Minute, func() { s.mu.Lock(); defer s.mu.Unlock(); s.expireLocked() })
	return &pre, nil
}

func integrationSessionMatches(r integrationRequest, session *integrationSession) error {
	if session == nil || time.Now().After(session.expires) {
		return errors.New("PREPARED_SESSION_EXPIRED")
	}
	if r.payloadHash() != session.request.payloadHash() {
		return errors.New("PREPARED_PAYLOAD_CHANGED")
	}
	if !r.VisualReviewed || r.ReviewedEvidenceRef != session.preflight.EvidenceRef {
		return errors.New("VISUAL_REVIEW_REQUIRED")
	}
	sha, err := fileSHA256(r.ReviewedEvidenceRef)
	if err != nil || sha != session.preflight.EvidenceSHA256 {
		return errors.New("REVIEWED_EVIDENCE_CHANGED")
	}
	if err = integrationImageEvidenceMatches(session.preflight.ImageEvidenceRefs, session.preflight.ImageEvidenceSHA256, len(r.Images)); err != nil {
		return err
	}
	return nil
}

func (s *integrationService) submit(ctx context.Context, r integrationRequest) (*integrationAttempt, error) {
	// Existing journal is checked before session/file checks, allowing readback
	// after restart without recreating a browser or clicking a second time.
	if prior, err := s.journal.read(r.AttemptID); err == nil {
		if prior.PayloadHash != r.payloadHash() {
			return nil, errors.New("ATTEMPT_CONFLICT")
		}
		return prior, nil
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if blocked := loadLoginNetworkBlock(s.dir); blocked != nil {
		return nil, blocked
	}
	if err := validateIntegrationRequest(r); err != nil {
		return nil, err
	}
	s.expireLocked()
	session := s.sessions[r.EditorSessionID]
	if err := integrationSessionMatches(r, session); err != nil {
		return nil, err
	}
	p := session.page.Context(ctx).Timeout(60 * time.Second)
	// Separate same-browser tab: no editor navigation/recreation.
	identityPage, err := p.Browser().Page(proto.TargetCreateTarget{URL: "about:blank"})
	if err != nil {
		return nil, err
	}
	account, err := s.identityOnPage(ctx, identityPage)
	identityPage.Close()
	if err != nil {
		return nil, err
	}
	if !integrationIdentityMatches(r.ExpectedAccount, account) {
		return nil, errors.New("ACCOUNT_MISMATCH")
	}
	current, err := xiaohongshu.ReadIntegrationEditor(p)
	if err != nil {
		return nil, err
	}
	if err = xiaohongshu.ValidateIntegrationEditor(current, r.Title, r.Content, r.Tags, len(r.Images)); err != nil {
		return nil, err
	}
	if !reflect.DeepEqual(current.PreviewSrcs, session.snapshot.PreviewSrcs) || current.Content != session.snapshot.Content || current.Title != session.snapshot.Title {
		return nil, errors.New("PREPARED_EDITOR_CHANGED")
	}
	if err = xiaohongshu.IntegrationPublishAvailable(p); err != nil {
		return nil, err
	}
	// No mutation above this boundary can submit. Intent is fsynced before any
	// mouse event; both attempt and account/content remain locked permanently.
	attempt, newIntent, err := s.journal.begin(r, account, session.preflight)
	if err != nil || !newIntent {
		return attempt, err
	}
	delete(s.sessions, r.EditorSessionID)
	defer safeIntegrationClose(session.close)
	attempt.ClickAttempted = true
	err = xiaohongshu.ClickPreparedIntegration(p)
	// Keep the prepared browser alive while the platform handles the one click.
	// A returned mouse event is not completion of the asynchronous publish request.
	// A client disconnect must not immediately cancel or close that page either.
	postClickPage := session.page.Context(context.Background()).Timeout(60 * time.Second)
	waitErr := xiaohongshu.WaitPreparedIntegrationOutcome(postClickPage, 45*time.Second)
	attempt.Phase = "SUBMIT_UNKNOWN"
	attempt.ObservedAt = time.Now().UTC().Format(time.RFC3339Nano)
	attempt.ErrorCode = "MANAGEMENT_RECONCILIATION_REQUIRED"
	attempt.Reason = "A single submit click was attempted; no platform record has been verified"
	if err != nil {
		attempt.ErrorCode = "CLICK_RESULT_UNKNOWN"
		attempt.Reason = err.Error()
	}
	if waitErr != nil && err == nil {
		attempt.ErrorCode = "POST_CLICK_RESULT_UNKNOWN"
		attempt.Reason = waitErr.Error()
	}
	// Page/navigation/toast never converts this into published. A screenshot is
	// diagnostic evidence only. Failed capture still retains the intent lock.
	diagnostic := map[string]any{"attempt_id": r.AttemptID, "phase": "SUBMIT_UNKNOWN", "observed_at": attempt.ObservedAt}
	if waitErr != nil {
		diagnostic["post_click_wait_error"] = waitErr.Error()
	}
	if info, e := postClickPage.Info(); e == nil {
		diagnostic["url"] = info.URL
	}
	if body, e := postClickPage.Element("body"); e == nil {
		if visibleText, e := body.Text(); e == nil {
			diagnostic["visible_text"] = visibleText
		}
	}
	if png, _, _, e := s.capture(session.page.Context(context.Background()).Timeout(15*time.Second), "submit", diagnostic); e == nil {
		attempt.EvidenceRef = png
	}
	if e := s.journal.finish(attempt); e != nil {
		attempt.ErrorCode = "RESULT_SAVE_UNKNOWN"
		attempt.Reason = e.Error()
	}
	return attempt, nil
}

func integrationError(c *gin.Context, err error, phase string) {
	if blocked, ok := err.(*loginDiagnosticError); ok {
		c.JSON(http.StatusConflict, gin.H{"success": false, "error": blocked.Code, "message": "Actual backend diagnostic evidence is available", "data": gin.H{"phase": phase, "state": blocked.State, "confirmed": false, "published": false, "retry_allowed": false, "evidence_ref": blocked.EvidenceRef, "record_ref": blocked.RecordRef, "observed_at": blocked.ObservedAt, "diagnostic": gin.H{"backend_code": blocked.Code, "code": blocked.Code, "evidence_ref": blocked.EvidenceRef, "record_ref": blocked.RecordRef, "observed_at": blocked.ObservedAt, "stage": blocked.Stage, "error_class": blocked.ErrorClass}}})
		return
	}
	code := strings.SplitN(err.Error(), ":", 2)[0]
	if strings.Contains(code, " ") || len(code) > 64 {
		code = "INTEGRATION_ERROR"
	}
	c.JSON(http.StatusConflict, gin.H{"success": false, "error": code, "message": err.Error(), "data": gin.H{"phase": phase, "confirmed": false, "published": false, "retry_allowed": false}})
}

func bindIntegration(c *gin.Context) (integrationRequest, error) {
	var r integrationRequest
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
	d := json.NewDecoder(c.Request.Body)
	d.DisallowUnknownFields()
	err := d.Decode(&r)
	if err == nil {
		var extra any
		if e := d.Decode(&extra); e != io.EOF {
			err = errors.New("INVALID_JSON_TRAILING_DATA")
		}
	}
	return r, err
}

func (a *AppServer) registerIntegrationRoutes(group *gin.RouterGroup) {
	g := group.Group("/xhs-integration")
	g.Use(func(c *gin.Context) {
		if a.integration == nil {
			c.AbortWithStatusJSON(503, gin.H{"success": false, "error": "INTEGRATION_NOT_CONFIGURED"})
			return
		}
		c.Next()
	})
	g.GET("/health", func(c *gin.Context) {
		state := "ready"
		var block any
		if restricted := loadLoginNetworkBlock(a.integration.dir); restricted != nil {
			state = "blocked_network"
			block = restricted
		}
		c.JSON(200, gin.H{"success": true, "data": gin.H{"api_version": "xhs-integration-v1", "upstream_version": "v2.5.5", "state": state, "block": block, "state_dir": a.integration.dir, "browser": gin.H{"mode": map[bool]string{true: "native_chrome", false: "upstream_bundled"}[browser.NativeModeConfigured()], "configured_path": os.Getenv("XHS_BROWSER_PATH"), "login_visible": os.Getenv("XHS_LOGIN_VISIBLE") == "1", "custom_fingerprint": !browser.NativeModeConfigured(), "ua_override": !browser.NativeModeConfigured(), "stealth_injection": false, "sandbox_disabled": !browser.NativeModeConfigured()}, "capabilities": gin.H{"preflight": true, "single_submit": true, "ai_declaration": true, "prepared_session": true, "management_evidence": true, "automatic_confirmation": false}}})
	})
	g.GET("/management-evidence", func(c *gin.Context) {
		s := a.integration
		s.mu.Lock()
		defer s.mu.Unlock()
		result, err := s.managementEvidence(c.Request.Context(), c.DefaultQuery("view", "all"))
		if err != nil {
			integrationError(c, err, "READ_ONLY")
			return
		}
		c.JSON(200, gin.H{"success": true, "data": result})
	})
	g.GET("/identity", func(c *gin.Context) {
		s := a.integration
		s.mu.Lock()
		defer s.mu.Unlock()
		result, err := s.identity(c.Request.Context())
		if err != nil {
			integrationError(c, err, "NOT_SUBMITTED")
			return
		}
		c.JSON(200, gin.H{"success": true, "data": result})
	})
	g.POST("/preflight", func(c *gin.Context) {
		r, err := bindIntegration(c)
		if err != nil {
			integrationError(c, err, "NOT_SUBMITTED")
			return
		}
		s := a.integration
		s.mu.Lock()
		defer s.mu.Unlock()
		result, err := s.prepare(c.Request.Context(), r)
		if err != nil {
			integrationError(c, err, "NOT_SUBMITTED")
			return
		}
		c.JSON(200, gin.H{"success": true, "data": result})
	})
	g.POST("/submit", func(c *gin.Context) {
		r, err := bindIntegration(c)
		if err != nil {
			integrationError(c, err, "NOT_SUBMITTED")
			return
		}
		s := a.integration
		s.mu.Lock()
		defer s.mu.Unlock()
		result, err := s.submit(c.Request.Context(), r)
		if err != nil {
			phase := "NOT_SUBMITTED"
			if prior, e := s.journal.read(r.AttemptID); e == nil && prior != nil {
				phase = "SUBMIT_UNKNOWN"
			}
			if p, e := s.journal.path(r.AttemptID, ".intent.json"); e == nil {
				if _, e = os.Stat(p); e == nil {
					phase = "SUBMIT_UNKNOWN"
				}
			}
			integrationError(c, err, phase)
			return
		}
		c.JSON(200, gin.H{"success": true, "data": result})
	})
	g.GET("/attempts/:id/observations", func(c *gin.Context) {
		s := a.integration
		s.mu.Lock()
		defer s.mu.Unlock()
		result, err := s.observations(c.Request.Context(), c.Param("id"))
		if err != nil {
			integrationError(c, err, "SUBMIT_UNKNOWN")
			return
		}
		c.JSON(200, gin.H{"success": true, "data": result})
	})
}

func (s *integrationService) observations(ctx context.Context, id string) (result map[string]any, err error) {
	attempt, err := s.journal.read(id)
	if err != nil {
		return nil, err
	}
	result = map[string]any{"attempt_id": id, "phase": attempt.Phase, "confirmed": false, "published": false, "retry_allowed": false, "observations": []any{}, "candidates": []any{}, "unverified": true, "requires_management_evidence": true, "observed_at": time.Now().UTC().Format(time.RFC3339Nano)}
	// Public readback may panic in upstream Must* helpers. Preserve any already
	// captured management evidence and return the same unverified result.
	defer func() {
		if recover() != nil {
			result["error_code"] = "READBACK_OPERATION_FAILED"
			err = nil
		}
	}()
	ctx, cancel := context.WithTimeout(ctx, 110*time.Second)
	defer cancel()
	if blocked := loadLoginNetworkBlock(s.dir); blocked != nil {
		result["state"], result["error_code"], result["evidence_ref"] = blocked.State, blocked.Code, blocked.EvidenceRef
		return result, nil
	}
	b, browserErr := boundedIntegrationBrowser(ctx)
	if browserErr != nil {
		result["error_code"] = "READBACK_BROWSER_UNAVAILABLE"
		return result, nil
	}
	defer safeIntegrationClose(b.Close)
	p, pageCreationErr := loginPage(ctx, b)
	if pageCreationErr != nil {
		result["error_code"] = "READBACK_PAGE_UNAVAILABLE"
		return result, nil
	}
	defer safeIntegrationClose(func() { p.Close() })
	account, err := s.identityOnPage(ctx, p)
	if err != nil || !integrationIdentityMatches(attempt.Account, account) {
		result["error_code"] = "ACCOUNT_UNVERIFIED"
		return result, nil
	}
	result["account"] = account
	// A separate page in this same freshly verified isolated browser captures
	// management evidence before public candidates. Public/self-visible detail
	// access never substitutes for the platform's actual management state.
	managementCtx, cancelManagement := context.WithTimeout(ctx, 45*time.Second)
	managementPage, pageErr := loginPage(managementCtx, b)
	if pageErr == nil {
		management, managementErr := s.managementEvidenceOnPage(managementCtx, managementPage, account)
		if managementErr == nil {
			result["management_evidence"] = management
		} else {
			result["management_error_code"] = "MANAGEMENT_READBACK_FAILED"
			if diagnostic, ok := managementErr.(*loginDiagnosticError); ok {
				result["management_diagnostic"] = diagnostic
			}
		}
		safeIntegrationClose(func() { managementPage.Close() })
	} else {
		result["management_error_code"] = "MANAGEMENT_READBACK_FAILED"
	}
	cancelManagement()
	// Public profile has no reliable pending/rejected/scheduled state. Only
	// candidate records are exposed; they are never confirmation observations.
	profile, err := xiaohongshu.NewUserProfileAction(p).GetMyProfileViaSidebar(ctx, xiaohongshu.TabNotes)
	if err != nil {
		result["error_code"] = "PROFILE_READBACK_FAILED"
		return result, nil
	}
	candidates := []any{}
	for _, feed := range profile.Feeds {
		if feed.NoteCard.DisplayTitle != attempt.Request.Title || feed.NoteCard.User.UserID != account.UserID || feed.ID == "" || feed.XsecToken == "" {
			continue
		}
		entry := integrationCandidateEntry(feed.ID, feed.NoteCard.DisplayTitle, account)
		detail, err := xiaohongshu.NewFeedDetailAction(p).GetFeedDetailWithConfig(ctx, feed.ID, feed.XsecToken, false, xiaohongshu.DefaultCommentLoadConfig())
		if err == nil && detail.Note.NoteID == feed.ID && detail.Note.User.UserID == account.UserID {
			entry["title"], entry["content"], entry["images"], entry["platform_created_at"] = detail.Note.Title, detail.Note.Desc, detail.Note.ImageList, detail.Note.Time
			if png, _, record, e := s.capture(p, "candidate", entry); e == nil {
				entry["evidence_ref"], entry["record_ref"] = png, record
			}
		} else {
			entry["error_code"] = "DETAIL_UNVERIFIED"
		}
		candidates = append(candidates, entry)
		if len(candidates) >= 5 {
			break
		}
	}
	result["candidates"], result["account"] = candidates, account
	if len(candidates) == 0 {
		result["error_code"] = "NO_VERIFIED_MANAGEMENT_RECORD"
	}
	return result, nil
}

func integrationCandidateEntry(noteID, title string, account integrationAccount) map[string]any {
	// account_id is the publication ledger's RedId. The separate profile ID is
	// used only for platform ownership checks; neither identifies a verified
	// pending/published management record by itself.
	return map[string]any{"note_id": noteID, "note_url": "https://www.xiaohongshu.com/explore/" + noteID, "title": title, "account_id": account.RedID, "user_id": account.UserID, "platform_state": "unverified", "confirmed": false, "published": false, "image_match_verified": false}
}
