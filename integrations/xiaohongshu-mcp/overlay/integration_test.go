// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/xpzouying/xiaohongshu-mcp/configs"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func integrationTestRequest(t *testing.T) integrationRequest {
	t.Helper()
	p := filepath.Join(t.TempDir(), "image.png")
	if err := os.WriteFile(p, []byte("isolated fake test image, no platform access"), 0600); err != nil {
		t.Fatal(err)
	}
	h, err := fileSHA256(p)
	if err != nil {
		t.Fatal(err)
	}
	return integrationRequest{AttemptID: "attempt-test", ContentHash: strings.Repeat("a", 64), ExpectedAccount: integrationAccount{UserID: "profile-user", RedID: "offline-red-id", Nickname: "test"}, Title: "test title", Content: "test body", Images: []string{p}, ImageHashes: []string{h}, Tags: []string{"test"}, AIGenerated: true}
}

func integrationTestAccount(r integrationRequest) integrationAccount {
	a := r.ExpectedAccount
	a.Authenticated = true
	return a
}

func TestIntegrationIntentSurvivesCrashAndCannotResubmit(t *testing.T) {
	r := integrationTestRequest(t)
	j, err := newIntegrationJournal(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	a, fresh, err := j.begin(r, integrationTestAccount(r), nil)
	if err != nil || !fresh || a.Phase != "INTENT" {
		t.Fatalf("intent: %+v %v", a, err)
	}
	// Simulate a restart before a result was written: no browser or callback.
	restarted, _ := newIntegrationJournal(filepath.Dir(j.dir))
	a, fresh, err = restarted.begin(r, integrationTestAccount(r), nil)
	if err != nil || fresh || a.Phase != "SUBMIT_UNKNOWN" || a.RetryAllowed || a.Published || a.Confirmed {
		t.Fatalf("recovery unsafe: %+v %v", a, err)
	}
	service := &integrationService{journal: restarted}
	result, err := service.submit(context.Background(), r)
	if err != nil || result.Phase != "SUBMIT_UNKNOWN" {
		t.Fatalf("repeat must return without a prepared page: %+v %v", result, err)
	}
}

func TestIntegrationAttemptCannotReplaceFrozenPayload(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	if _, _, err := j.begin(r, integrationTestAccount(r), nil); err != nil {
		t.Fatal(err)
	}
	r.Content = "different body with same caller hash"
	if _, _, err := j.begin(r, integrationTestAccount(r), nil); err == nil || !strings.Contains(err.Error(), "ATTEMPT_CONFLICT") {
		t.Fatalf("must reject: %v", err)
	}
}

func TestIntegrationContentLockBlocksDifferentAttempt(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	if _, _, err := j.begin(r, integrationTestAccount(r), nil); err != nil {
		t.Fatal(err)
	}
	r.AttemptID = "attempt-other"
	if _, _, err := j.begin(r, integrationTestAccount(r), nil); err == nil || !strings.Contains(err.Error(), "CONTENT_ALREADY_INTENDED") {
		t.Fatalf("new attempt cannot bypass lock: %v", err)
	}
}

func TestIntegrationConcurrentIntentOnlyOneWinner(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	var mu sync.Mutex
	winners := 0
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			local := r
			local.AttemptID = "concurrent-" + string(rune('a'+i))
			_, fresh, err := j.begin(local, integrationTestAccount(r), nil)
			if err == nil && fresh {
				mu.Lock()
				winners++
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if winners != 1 {
		t.Fatalf("want exactly one intent winner, got %d", winners)
	}
}

func TestIntegrationPartialIntentFailsClosed(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	p, _ := j.path(r.AttemptID, ".intent.json")
	if err := os.WriteFile(p, []byte(`{"attempt_id":`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := j.begin(r, integrationTestAccount(r), nil); err == nil {
		t.Fatal("partial journal must not permit another click")
	}
}

func TestIntegrationResultNeverAllowsClaimingPublished(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	a, _, _ := j.begin(r, integrationTestAccount(r), nil)
	a.Phase = "PUBLISHED"
	a.Published = true
	if err := j.finish(a); err == nil {
		t.Fatal("navigation/toast cannot be persisted as confirmed publication")
	}
	a.Phase = "SUBMIT_UNKNOWN"
	a.Published = false
	a.ClickAttempted = true
	if err := j.finish(a); err != nil {
		t.Fatal(err)
	}
	got, err := j.read(r.AttemptID)
	if err != nil || !got.ClickAttempted || got.Phase != "SUBMIT_UNKNOWN" {
		t.Fatalf("bad readback: %+v %v", got, err)
	}
}

func TestIntegrationWrongOrIncompleteAccountRejected(t *testing.T) {
	r := integrationTestRequest(t)
	a := integrationTestAccount(r)
	if !integrationIdentityMatches(r.ExpectedAccount, a) {
		t.Fatal("matching account rejected")
	}
	for _, mutate := range []func(*integrationAccount){func(x *integrationAccount) { x.UserID = "another" }, func(x *integrationAccount) { x.RedID = "another" }, func(x *integrationAccount) { x.Nickname = "another" }, func(x *integrationAccount) { x.UserID = "" }, func(x *integrationAccount) { x.Authenticated = false }} {
		bad := a
		mutate(&bad)
		if integrationIdentityMatches(r.ExpectedAccount, bad) {
			t.Fatal("wrong or missing account accepted")
		}
	}
}

func TestIntegrationAIRequiredAndChangedFilesStopBeforeIntent(t *testing.T) {
	r := integrationTestRequest(t)
	if err := validateIntegrationRequest(r); err != nil {
		t.Fatal(err)
	}
	r.AIGenerated = false
	if err := validateIntegrationRequest(r); err == nil || err.Error() != "AI_DECLARATION_REQUIRED" {
		t.Fatalf("missing explicit AI declaration accepted: %v", err)
	}
	r.AIGenerated = true
	if err := os.WriteFile(r.Images[0], []byte("changed"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := validateIntegrationRequest(r); err == nil || !strings.Contains(err.Error(), "IMAGE_FILE_CHANGED") {
		t.Fatalf("changed image accepted: %v", err)
	}
}

func TestIntegrationPreparedSessionBindsReviewAndExactRequest(t *testing.T) {
	r := integrationTestRequest(t)
	evidence := filepath.Join(t.TempDir(), "preflight.png")
	if err := os.WriteFile(evidence, []byte("private screenshot evidence"), 0600); err != nil {
		t.Fatal(err)
	}
	h, _ := fileSHA256(evidence)
	session := &integrationSession{request: r, expires: time.Now().Add(time.Minute), preflight: integrationPreflight{EvidenceRef: evidence, EvidenceSHA256: h, ImageEvidenceRefs: []string{r.Images[0]}, ImageEvidenceSHA256: []string{r.ImageHashes[0]}}}
	if err := integrationSessionMatches(r, session); err == nil {
		t.Fatal("unreviewed session accepted")
	}
	r.VisualReviewed = true
	r.ReviewedEvidenceRef = evidence
	if err := integrationSessionMatches(r, session); err != nil {
		t.Fatal(err)
	}
	r.Content = "different"
	if err := integrationSessionMatches(r, session); err == nil {
		t.Fatal("review transferred to different content")
	}
	r = session.request
	r.VisualReviewed = true
	r.ReviewedEvidenceRef = evidence
	if err := os.WriteFile(evidence, []byte("replaced"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := integrationSessionMatches(r, session); err == nil {
		t.Fatal("replaced evidence accepted")
	}
	session.expires = time.Now().Add(-time.Second)
	if err := integrationSessionMatches(r, session); err == nil {
		t.Fatal("expired session accepted")
	}
}

func TestIntegrationFalseConfirmedResultIgnored(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	a, _, _ := j.begin(r, integrationTestAccount(r), nil)
	a.Phase = "SUBMIT_UNKNOWN"
	a.Published = true
	a.Confirmed = true
	b, _ := json.Marshal(a)
	if err := os.WriteFile(filepath.Join(j.dir, r.AttemptID+".result.json"), b, 0600); err != nil {
		t.Fatal(err)
	}
	recovered, err := j.read(r.AttemptID)
	if err != nil || recovered.Published || recovered.Confirmed || recovered.ErrorCode != "INTENT_RECOVERY" {
		t.Fatalf("forged result accepted: %+v %v", recovered, err)
	}
}

func TestIntegrationHTTPAuthAndAIRejectionBeforeBrowser(t *testing.T) {
	t.Setenv("XHS_STATE_DIR", "")
	app := NewAppServer(NewXiaohongshuService(), "isolated-test-token")
	var err error
	app.integration, err = newIntegrationService(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	router := setupRoutes(app)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/xhs-integration/health", nil))
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized integration exposed: %d", response.Code)
	}
	response = httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/xhs-integration/health", nil)
	request.Header.Set("Authorization", "Bearer isolated-test-token")
	router.ServeHTTP(response, request)
	if response.Code != 200 || !strings.Contains(response.Body.String(), `"automatic_confirmation":false`) {
		t.Fatalf("health contract: %d %s", response.Code, response.Body.String())
	}
	r := integrationTestRequest(t)
	r.AIGenerated = false
	b, _ := json.Marshal(r)
	response = httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/xhs-integration/preflight", bytes.NewReader(b))
	request.Header.Set("Authorization", "Bearer isolated-test-token")
	router.ServeHTTP(response, request)
	if response.Code != 409 || !strings.Contains(response.Body.String(), "AI_DECLARATION_REQUIRED") || !strings.Contains(response.Body.String(), "NOT_SUBMITTED") {
		t.Fatalf("AI rejection before any browser: %d %s", response.Code, response.Body.String())
	}
}

func TestIntegrationDisconnectedExpiryCleanupDoesNotPanic(t *testing.T) {
	service := &integrationService{sessions: map[string]*integrationSession{"closed": {expires: time.Now().Add(-time.Second), close: func() { panic("simulated disconnected browser") }}}}
	service.expireLocked()
	if len(service.sessions) != 0 {
		t.Fatal("expired disconnected session retained")
	}
}

func TestIntegrationCancelledLoginDoesNotLaunchBrowser(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	browser, err := loginBrowser(ctx)
	if browser != nil || err == nil || err.Error() != "LOGIN_BROWSER_TIMEOUT" {
		t.Fatalf("cancelled login must stop before browser: %v", err)
	}
}

func TestIntegrationCancelledLoginPageDoesNotAccessBrowser(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	page, err := loginPage(ctx, nil)
	if page != nil || err == nil || err.Error() != "LOGIN_BROWSER_PAGE_TIMEOUT" {
		t.Fatalf("cancelled login page must stop before browser: %v", err)
	}
}

func TestIntegrationLoginDiagnosticsRedactCredentials(t *testing.T) {
	safe := safeLoginDOMText("登录失败 Cookie=secret-cookie token:secret-token Bearer secret-auth 密码=secret-pass")
	for _, secret := range []string{"secret-cookie", "secret-token", "secret-auth", "secret-pass"} {
		if strings.Contains(safe, secret) {
			t.Fatalf("diagnostic leaked %s", secret)
		}
	}
	if len([]rune(safeLoginDOMText(strings.Repeat("中", 13000)))) != 12000 {
		t.Fatal("diagnostic text must be bounded")
	}
	urlText := safeLoginDOMText("失败页面 https://user:" + "secret-pass" + "@" + "www.xiaohongshu.com/publish/publish?xsec_token=secret-query&other=private-query#secret-fragment")
	for _, secret := range []string{"secret-pass", "secret-query", "private-query", "secret-fragment", "?", "#"} {
		if strings.Contains(urlText, secret) {
			t.Fatalf("URL diagnostic leaked query/credentials: %s", urlText)
		}
	}
	if urlText != "失败页面 https://www.xiaohongshu.com/publish/publish" {
		t.Fatalf("diagnostic lost safe page URL: %s", urlText)
	}
}

func TestIntegrationNetworkBlockPersistsAndStopsLoginWithoutBrowser(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XHS_STATE_DIR", dir)
	blocked := &loginDiagnosticError{Code: "LOGIN_NETWORK_RESTRICTED", EvidenceRef: "private-real-capture.png", RecordRef: "private-real-capture.json", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	if err := persistLoginNetworkBlock(dir, blocked); err != nil {
		t.Fatal(err)
	}
	if got := loadLoginNetworkBlock(dir); got == nil || got.State != "blocked_network" || got.EvidenceRef != blocked.EvidenceRef {
		t.Fatalf("lost durable restriction: %+v", got)
	}
	if _, err := NewXiaohongshuService().GetLoginQrcode(context.Background()); err == nil || err.Error() != "LOGIN_NETWORK_RESTRICTED" {
		t.Fatalf("must stop before browser request: %v", err)
	}
	service, _ := newIntegrationService(dir)
	if _, err := service.prepare(context.Background(), integrationRequest{}); err == nil || err.Error() != "LOGIN_NETWORK_RESTRICTED" {
		t.Fatalf("preflight bypassed block: %v", err)
	}
}

func TestIntegrationCorruptNetworkBlockFailsClosed(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "backend-block.json"), []byte("partial"), 0600); err != nil {
		t.Fatal(err)
	}
	if got := loadLoginNetworkBlock(dir); got == nil || got.State != "blocked_network" || got.Code != "LOGIN_BLOCK_STATE_UNREADABLE" {
		t.Fatalf("corrupt block must stay locked: %+v", got)
	}
}

func TestIntegrationVisibleOverrideOnlyAppliesToLogin(t *testing.T) {
	previous := configs.IsHeadless()
	defer configs.InitHeadless(previous)
	configs.InitHeadless(true)
	t.Setenv("XHS_LOGIN_VISIBLE", "1")
	if loginHeadlessMode() {
		t.Fatal("interactive login must be visible")
	}
	if !configs.IsHeadless() {
		t.Fatal("login override changed ordinary service browser mode")
	}
	t.Setenv("XHS_LOGIN_VISIBLE", "")
	if !loginHeadlessMode() {
		t.Fatal("ordinary login default unexpectedly changed")
	}
}

func TestIntegrationIdentityPanicFailsClosedWithoutBrowser(t *testing.T) {
	t.Setenv("XHS_STATE_DIR", t.TempDir())
	service := &integrationService{}
	account, err := service.identityOnPage(context.Background(), nil)
	diagnostic, ok := err.(*loginDiagnosticError)
	if !ok || diagnostic.Code != "IDENTITY_FAILED" || account.Authenticated || account.UserID != "" {
		t.Fatalf("failed page must not authenticate: %+v %v", account, err)
	}
	if diagnostic.EvidenceRef != "" || diagnostic.RecordRef != "" {
		t.Fatal("nil page fabricated evidence")
	}
}

func TestIntegrationIdentityErrorEnvelopePreservesSafeDiagnostic(t *testing.T) {
	response := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(response)
	dir := t.TempDir()
	diagnostic := &loginDiagnosticError{Code: "ACCOUNT_ID_UNREADABLE", EvidenceRef: filepath.Join(dir, "identity.png"), RecordRef: filepath.Join(dir, "identity.json"), ObservedAt: "2026-10-04T09:00:00Z"}
	integrationError(ctx, diagnostic, "NOT_SUBMITTED")
	var result struct {
		Success bool   `json:"success"`
		Error   string `json:"error"`
	}
	// Decode only the concrete diagnostic contract consumed by the Python
	// client; neither the error text nor arbitrary page data is needed.
	var body map[string]json.RawMessage
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	json.Unmarshal(body["success"], &result.Success)
	json.Unmarshal(body["error"], &result.Error)
	var data struct {
		Confirmed    bool `json:"confirmed"`
		Published    bool `json:"published"`
		RetryAllowed bool `json:"retry_allowed"`
		Diagnostic   struct {
			BackendCode string `json:"backend_code"`
			Code        string `json:"code"`
			EvidenceRef string `json:"evidence_ref"`
			RecordRef   string `json:"record_ref"`
			ObservedAt  string `json:"observed_at"`
		} `json:"diagnostic"`
	}
	if err := json.Unmarshal(body["data"], &data); err != nil {
		t.Fatal(err)
	}
	if response.Code != 409 || result.Success || result.Error != diagnostic.Code || data.Confirmed || data.Published || data.RetryAllowed || data.Diagnostic.BackendCode != diagnostic.Code || data.Diagnostic.Code != diagnostic.Code || data.Diagnostic.EvidenceRef != diagnostic.EvidenceRef || data.Diagnostic.RecordRef != diagnostic.RecordRef || data.Diagnostic.ObservedAt != diagnostic.ObservedAt {
		t.Fatalf("unsafe/incomplete identity failure response: %s", response.Body.String())
	}
}

func TestIntegrationPreflightDiagnosticNeverExposesBrowserException(t *testing.T) {
	t.Setenv("XHS_STATE_DIR", t.TempDir())
	cause := &xiaohongshu.IntegrationEditorError{Stage: "editor_readback", Cause: errors.New("browser failed https://example.com/?token=secret-browser-token")}
	diagnostic := capturePreflightFailure(nil, "editor_prepare", cause)
	if diagnostic.Code != "PREFLIGHT_FAILED" || diagnostic.Stage != "editor_readback" || diagnostic.ErrorClass != "BROWSER_OPERATION_FAILED" || diagnostic.EvidenceRef != "" || diagnostic.RecordRef != "" {
		t.Fatalf("wrong/no-page failure diagnostic: %+v", diagnostic)
	}
	response := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(response)
	integrationError(ctx, diagnostic, "NOT_SUBMITTED")
	if response.Code != 409 || !strings.Contains(response.Body.String(), `"stage":"editor_readback"`) || strings.Contains(response.Body.String(), "secret-browser-token") || !strings.Contains(response.Body.String(), `"phase":"NOT_SUBMITTED"`) {
		t.Fatalf("unsafe preflight error: %s", response.Body.String())
	}
}

func TestIntegrationPreflightSafeClassesPreserveKnownFailure(t *testing.T) {
	for _, test := range []struct {
		cause error
		want  string
	}{{context.DeadlineExceeded, "TIMEOUT"}, {context.Canceled, "CANCELED"}, {&xiaohongshu.IntegrationEditorError{Stage: "editor_validate", Cause: errors.New("EDITOR_BODY_TAGS_MISMATCH")}, "EDITOR_BODY_TAGS_MISMATCH"}, {errors.New("AI声明未确认，中止发布: arbitrary browser secret"), "AI_DECLARATION_UNVERIFIED"}, {errors.New("PREFLIGHT_PANIC"), "PANIC"}, {errors.New("unknown browser exception password=private"), "BROWSER_OPERATION_FAILED"}} {
		if got := preflightSafeErrorClass(test.cause); got != test.want {
			t.Fatalf("wrong safe class: got %s want %s", got, test.want)
		}
	}
	if diagnostic := capturePreflightFailure(nil, "unsafe stage token=private", errors.New("unsafe failure")); diagnostic.Stage != "preflight" {
		t.Fatal("unrecognized stage reached response")
	}
}

func TestIntegrationPreflightErrorEnvelopeCarriesActualEvidencePaths(t *testing.T) {
	response := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(response)
	dir := t.TempDir()
	diagnostic := &loginDiagnosticError{Code: "PREFLIGHT_FAILED", Stage: "editor_prepare", ErrorClass: "AI_DECLARATION_UNVERIFIED", EvidenceRef: filepath.Join(dir, "preflight-failure.png"), RecordRef: filepath.Join(dir, "preflight-failure.json"), ObservedAt: "2026-10-04T09:35:00Z"}
	integrationError(ctx, diagnostic, "NOT_SUBMITTED")
	var body struct {
		Success bool `json:"success"`
		Data    struct {
			Confirmed  bool `json:"confirmed"`
			Published  bool `json:"published"`
			Diagnostic struct {
				BackendCode string `json:"backend_code"`
				EvidenceRef string `json:"evidence_ref"`
				RecordRef   string `json:"record_ref"`
				Stage       string `json:"stage"`
				ErrorClass  string `json:"error_class"`
			} `json:"diagnostic"`
		} `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if response.Code != 409 || body.Success || body.Data.Confirmed || body.Data.Published || body.Data.Diagnostic.BackendCode != diagnostic.Code || body.Data.Diagnostic.EvidenceRef != diagnostic.EvidenceRef || body.Data.Diagnostic.RecordRef != diagnostic.RecordRef || body.Data.Diagnostic.Stage != diagnostic.Stage || body.Data.Diagnostic.ErrorClass != diagnostic.ErrorClass {
		t.Fatalf("lost preflight diagnostic contract: %s", response.Body.String())
	}
}

func TestIntegrationStructuredEditorDiagnosticsRedactNestedReferences(t *testing.T) {
	value := map[string]any{"editor_tree": map[string]any{"tag": "span", "text": "#健康科普[话题]# token=private-token", "attribute_names": []any{"data-topic-id", "class"}}, "image_preview_trees": []any{map[string]any{"image_src": "https://example.com/image.jpg?xsec_token=private-query#private-fragment"}}}
	safe := safeEditorDiagnosticValue(value)
	encoded, err := json.Marshal(safe)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"private-token", "private-query", "private-fragment"} {
		if strings.Contains(string(encoded), secret) {
			t.Fatalf("nested diagnostic leaked credential: %s", encoded)
		}
	}
	if !strings.Contains(string(encoded), "#健康科普[话题]#") || !strings.Contains(string(encoded), "data-topic-id") || !strings.Contains(string(encoded), "https://example.com/image.jpg") {
		t.Fatal("redaction removed structural/topic evidence")
	}
}

func TestIntegrationCandidateUsesLedgerRedIDKeepsUserIDUnverified(t *testing.T) {
	account := integrationAccount{Authenticated: true, UserID: "profile-user-123", RedID: "offline-red-id", Nickname: "test"}
	entry := integrationCandidateEntry("actual-note-id", "actual title", account)
	if entry["account_id"] != account.RedID || entry["user_id"] != account.UserID || entry["account_id"] == entry["user_id"] {
		t.Fatalf("candidate mixed profile userId with ledger RedId: %+v", entry)
	}
	if entry["platform_state"] != "unverified" || entry["confirmed"] != false || entry["published"] != false || entry["image_match_verified"] != false {
		t.Fatalf("candidate was presented as a confirmed platform record: %+v", entry)
	}
}

func TestIntegrationFourImageEvidencePreservesOrderAndRejectsReplacement(t *testing.T) {
	var refs, hashes []string
	for index := 0; index < 4; index++ {
		path := filepath.Join(t.TempDir(), fmt.Sprintf("actual-image-%d.png", index))
		if err := os.WriteFile(path, []byte(fmt.Sprintf("actual platform screenshot %d", index)), 0600); err != nil {
			t.Fatal(err)
		}
		hash, _ := fileSHA256(path)
		refs = append(refs, path)
		hashes = append(hashes, hash)
	}
	if err := integrationImageEvidenceMatches(refs, hashes, 4); err != nil {
		t.Fatal(err)
	}
	swapped := append([]string{}, refs...)
	swapped[0], swapped[1] = swapped[1], swapped[0]
	if err := integrationImageEvidenceMatches(swapped, hashes, 4); err == nil {
		t.Fatal("reordered PNGs accepted with original ordered digest vector")
	}
	if err := integrationImageEvidenceMatches(refs[:3], hashes[:3], 4); err == nil {
		t.Fatal("missing fourth image proof accepted")
	}
	if err := os.WriteFile(refs[2], []byte("replacement screenshot"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := integrationImageEvidenceMatches(refs, hashes, 4); err == nil {
		t.Fatal("replaced supplementary image proof accepted")
	}
}

func TestIntegrationImageEvidenceCannotReuseOnePNGForFourImages(t *testing.T) {
	path := filepath.Join(t.TempDir(), "one.png")
	if err := os.WriteFile(path, []byte("one platform screenshot"), 0600); err != nil {
		t.Fatal(err)
	}
	hash, _ := fileSHA256(path)
	if err := integrationImageEvidenceMatches([]string{path, path, path, path}, []string{hash, hash, hash, hash}, 4); err == nil {
		t.Fatal("one PNG reused as proof of all four images")
	}
}

func TestIntegrationManagementRecordIsReadOnlyEvenWhenTextSaysPublished(t *testing.T) {
	account := integrationAccount{Authenticated: true, UserID: "profile-id", RedID: "ledger-id", Nickname: "test account"}
	view := &xiaohongshu.IntegrationManagementView{URL: "https://creator.xiaohongshu.com/fixture-readonly?token=private-query#private-fragment", VisibleText: "已发布：普通页面文字 token=private-token", Ready: true, HeaderVerified: true, HeaderMatch: map[string]any{"exact_text": account.Nickname, "method": "visible_top_account_band"}, VisibleLinks: []xiaohongshu.IntegrationManagementLink{{Text: "真实笔记", URL: "https://www.xiaohongshu.com/explore/fixture?xsec_token=private-link-token"}}}
	record, err := integrationManagementRecord(account, view, "2026-10-04T10:30:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if record["phase"] != "READ_ONLY" || record["unverified"] != true || record["confirmed"] != false || record["published"] != false || record["url"] != "https://creator.xiaohongshu.com/fixture-readonly" {
		t.Fatalf("management capture inferred publication state: %+v", record)
	}
	encoded, _ := json.Marshal(record)
	for _, secret := range []string{"private-query", "private-fragment", "private-token", "private-link-token"} {
		if strings.Contains(string(encoded), secret) {
			t.Fatalf("management record leaked token/query: %s", encoded)
		}
	}
	account.RedID = ""
	if _, err := integrationManagementRecord(account, view, "time"); err == nil {
		t.Fatal("missing actual account accepted")
	}
	account.RedID = "ledger-id"
	view.HeaderMatch["exact_text"] = "different account"
	if _, err := integrationManagementRecord(account, view, "time"); err == nil {
		t.Fatal("different creator header accepted")
	}
}

func TestIntegrationManagementLinksExcludeUnsafeSchemesAndStripQuery(t *testing.T) {
	links := []xiaohongshu.IntegrationManagementLink{{Text: "普通笔记", URL: "https://creator.xiaohongshu.com/fixture?token=private"}, {Text: "token=private-text", URL: "https://www.xiaohongshu.com/explore/fixture#private-fragment"}, {Text: "unsafe", URL: "javascript:alert('private')"}, {Text: "unsafe", URL: "data:text/plain,private"}, {Text: "unsafe", URL: "/relative-fixture"}}
	safe := safeManagementLinks(links)
	if len(safe) != 2 || safe[0]["url"] != "https://creator.xiaohongshu.com/fixture" || safe[1]["url"] != "https://www.xiaohongshu.com/explore/fixture" {
		t.Fatalf("unsafe visible links: %+v", safe)
	}
	encoded, _ := json.Marshal(safe)
	if strings.Contains(string(encoded), "private") {
		t.Fatalf("link text/URL leaked secret: %s", encoded)
	}
}

func TestIntegrationManagementEndpointRequiresPrivateAuthBeforeBrowser(t *testing.T) {
	t.Setenv("XHS_STATE_DIR", "")
	app := NewAppServer(NewXiaohongshuService(), "isolated-test-token")
	var err error
	app.integration, err = newIntegrationService(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	setupRoutes(app).ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/xhs-integration/management-evidence", nil))
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("private management endpoint exposed: %d", response.Code)
	}
}

func TestIntegrationCancelledManagementBrowserStopsBeforeLaunch(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if browser, err := boundedIntegrationBrowser(ctx); browser != nil || !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled management browser launched: %v", err)
	}
}

func TestIntegrationInvalidManagementViewStopsBeforeBrowser(t *testing.T) {
	service := &integrationService{}
	if evidence, err := service.managementEvidence(context.Background(), "delete"); evidence != nil || err == nil || err.Error() != "INVALID_MANAGEMENT_VIEW" {
		t.Fatalf("invalid view entered browser flow: %+v %v", evidence, err)
	}
	t.Setenv("XHS_STATE_DIR", "")
	app := NewAppServer(NewXiaohongshuService(), "isolated-test-token")
	var err error
	app.integration, err = newIntegrationService(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/xhs-integration/management-evidence?view=delete", nil)
	request.Header.Set("Authorization", "Bearer isolated-test-token")
	response := httptest.NewRecorder()
	setupRoutes(app).ServeHTTP(response, request)
	if response.Code != 409 || !strings.Contains(response.Body.String(), "INVALID_MANAGEMENT_VIEW") {
		t.Fatalf("invalid view was not rejected before browser: %d %s", response.Code, response.Body.String())
	}
}

func TestIntegrationFilteredManagementRecordNeverInfersPublished(t *testing.T) {
	account := integrationAccount{Authenticated: true, UserID: "profile-id", RedID: "ledger-id", Nickname: "test account"}
	view := &xiaohongshu.IntegrationManagementView{URL: "https://creator.xiaohongshu.com/new/note-manager", VisibleText: "已发布", Ready: true, HeaderVerified: true, HeaderMatch: map[string]any{"exact_text": account.Nickname}, View: "published", FilterLabel: "已发布", FilterTabs: []xiaohongshu.IntegrationManagementFilterTab{{Label: "已发布", Class: "actual-dom-fixture", AriaSelected: "true", Role: "tab"}}}
	record, err := integrationManagementRecord(account, view, "2026-10-04T10:50:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if record["view"] != "published" || record["filter_label"] != "已发布" || record["unverified"] != true || record["confirmed"] != false || record["published"] != false {
		t.Fatalf("requested filter was treated as business publication state: %+v", record)
	}
	view.FilterLabel = "审核中"
	if _, err = integrationManagementRecord(account, view, "time"); err == nil {
		t.Fatal("requested view and actual label mismatch accepted")
	}
}
