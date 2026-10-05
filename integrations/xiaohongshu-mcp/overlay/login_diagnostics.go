// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/proto"
	"github.com/xpzouying/headless_browser"
	"github.com/xpzouying/xiaohongshu-mcp/browser"
	"github.com/xpzouying/xiaohongshu-mcp/configs"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
)

type loginDiagnosticError struct {
	Code        string `json:"code"`
	State       string `json:"state,omitempty"`
	EvidenceRef string `json:"evidence_ref,omitempty"`
	RecordRef   string `json:"record_ref,omitempty"`
	ObservedAt  string `json:"observed_at"`
	Stage       string `json:"stage,omitempty"`
	ErrorClass  string `json:"error_class,omitempty"`
}

func (e *loginDiagnosticError) Error() string { return e.Code }

// Closing an already failed browser must not panic in an expiry timer or login
// goroutine. No retry or session deletion is performed by this cleanup helper.
func safeIntegrationClose(closeFunc func()) { defer func() { _ = recover() }(); closeFunc() }

type loginBrowserResult struct {
	browser *headless_browser.Browser
	err     error
}

type loginPageResult struct {
	page *rod.Page
	err  error
}

func loginPage(ctx context.Context, browser *headless_browser.Browser) (*rod.Page, error) {
	if ctx.Err() != nil {
		return nil, &loginDiagnosticError{Code: "LOGIN_BROWSER_PAGE_TIMEOUT", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	}
	ready := make(chan loginPageResult)
	go func() {
		var result loginPageResult
		func() {
			defer func() {
				if recover() != nil {
					result.err = &loginDiagnosticError{Code: "LOGIN_BROWSER_PAGE_FAILED", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
				}
			}()
			result.page = browser.NewPage()
		}()
		select {
		case ready <- result:
		case <-ctx.Done():
			if result.page != nil {
				safeIntegrationClose(func() { result.page.Close() })
			}
		}
	}()
	select {
	case result := <-ready:
		return result.page, result.err
	case <-ctx.Done():
		return nil, &loginDiagnosticError{Code: "LOGIN_BROWSER_PAGE_TIMEOUT", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	}
}

func loginBrowser(ctx context.Context) (*headless_browser.Browser, error) {
	if ctx.Err() != nil {
		return nil, &loginDiagnosticError{Code: "LOGIN_BROWSER_TIMEOUT", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	}
	ready := make(chan loginBrowserResult)
	go func() {
		var result loginBrowserResult
		func() {
			defer func() {
				if recover() != nil {
					result.err = &loginDiagnosticError{Code: "LOGIN_BROWSER_LAUNCH_FAILED", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
				}
			}()
			result.browser = browser.NewBrowser(loginHeadlessMode(), browser.WithFingerprintSeed(configs.FingerprintSeed()), browser.WithProxy(configs.Proxy()))
		}()
		select {
		case ready <- result:
		case <-ctx.Done():
			if result.browser != nil {
				safeIntegrationClose(result.browser.Close)
			}
		}
	}()
	select {
	case result := <-ready:
		return result.browser, result.err
	case <-ctx.Done():
		return nil, &loginDiagnosticError{Code: "LOGIN_BROWSER_TIMEOUT", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	}
}

// Only the explicit interactive login operation can use this override. Normal
// identity/preflight/submission browser creation continues to use service mode.
func loginHeadlessMode() bool {
	if os.Getenv("XHS_LOGIN_VISIBLE") == "1" {
		return false
	}
	return configs.IsHeadless()
}

var loginSecretText = regexp.MustCompile(`(?i)(bearer\s+|(?:cookie|token|session|authorization|password|密码)[\s:=]+)[^\s,;]{4,}`)
var diagnosticVisibleURL = regexp.MustCompile(`https?://[^\s<>"']+`)

func diagnosticURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	u.RawQuery = ""
	u.Fragment = ""
	u.User = nil
	return u.String()
}

func safeLoginDOMText(text string) string {
	text = diagnosticVisibleURL.ReplaceAllStringFunc(text, diagnosticURL)
	text = loginSecretText.ReplaceAllString(text, "[REDACTED]")
	runes := []rune(text)
	if len(runes) > 12000 {
		text = string(runes[:12000])
	}
	return text
}

// Only visible text and a query-free URL are recorded. Cookies, localStorage,
// input values, QR source URLs, and raw HTML are never written to reports.
func captureLoginFailure(page *rod.Page, code string) *loginDiagnosticError {
	return captureBrowserFailure(page, code, "login-failure")
}

func captureIdentityFailure(page *rod.Page, code string) *loginDiagnosticError {
	return captureBrowserFailure(page, code, "identity-failure")
}

type browserFailureContext struct {
	Stage, ErrorClass string
	EditorControls    bool
	TopicSuggestions  []xiaohongshu.IntegrationTagObservation
}

func safePreflightStage(stage string) string {
	switch stage {
	case "browser_open", "identity_check", "editor_open", "editor_upload", "editor_prepare", "editor_readback", "editor_validate", "publish_available", "image_evidence", "evidence_capture":
		return stage
	}
	return "preflight"
}

// Return fixed categories only. Browser error strings are inspected locally
// for known failure classes and never persisted or returned to the caller.
func preflightSafeErrorClass(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return "TIMEOUT"
	}
	if errors.Is(err, context.Canceled) {
		return "CANCELED"
	}
	var pathError *os.PathError
	if errors.As(err, &pathError) {
		return "LOCAL_IO_FAILED"
	}
	var editorError *xiaohongshu.IntegrationEditorError
	if errors.As(err, &editorError) {
		err = editorError.Cause
	}
	if err == nil {
		return "BROWSER_OPERATION_FAILED"
	}
	message := err.Error()
	switch message {
	case "PREFLIGHT_PANIC", "EDITOR_OPERATION_PANIC":
		return "PANIC"
	case "EDITOR_TITLE_MISMATCH", "EDITOR_BODY_TAGS_MISMATCH", "EDITOR_IMAGE_COUNT_MISMATCH", "EDITOR_PREVIEW_EVIDENCE_MISSING", "AI_DECLARATION_NOT_VERIFIED", "EDITOR_LOCATION_CHANGED", "ACCOUNT_MISMATCH", "EDITOR_DOM_STRUCTURE_UNVERIFIED", "PREPARED_EDITOR_CHANGED", "IMAGE_EVIDENCE_CLEANUP_FAILED":
		return message
	}
	if strings.HasPrefix(message, "visible exact declaration control ") {
		return "AI_CONTROL_UNAVAILABLE"
	}
	if strings.Contains(message, "AI声明未确认") || strings.HasPrefix(message, "AI declaration ") {
		return "AI_DECLARATION_UNVERIFIED"
	}
	if strings.HasPrefix(message, "PUBLISH_BUTTON_UNAVAILABLE:") {
		return "PUBLISH_CONTROL_UNAVAILABLE"
	}
	return "BROWSER_OPERATION_FAILED"
}

func capturePreflightFailure(page *rod.Page, stage string, cause error) *loginDiagnosticError {
	var editorError *xiaohongshu.IntegrationEditorError
	var suggestions []xiaohongshu.IntegrationTagObservation
	if errors.As(cause, &editorError) {
		stage = editorError.Stage
		suggestions = editorError.TopicSuggestions
	}
	stage = safePreflightStage(stage)
	return captureBrowserFailure(page, "PREFLIGHT_FAILED", "preflight-failure", browserFailureContext{Stage: stage, ErrorClass: preflightSafeErrorClass(cause), EditorControls: stage != "browser_open" && stage != "identity_check", TopicSuggestions: suggestions})
}

// Structured diagnostic snapshots contain only declared safe fields, and all
// text/image references still receive the same query/credential redaction.
func safeEditorDiagnosticValue(value any) any {
	switch v := value.(type) {
	case string:
		return safeLoginDOMText(v)
	case []any:
		for i, child := range v {
			v[i] = safeEditorDiagnosticValue(child)
		}
		return v
	case map[string]any:
		for key, child := range v {
			v[key] = safeEditorDiagnosticValue(child)
		}
		return v
	default:
		return value
	}
}

func captureBrowserFailure(page *rod.Page, code, label string, details ...browserFailureContext) *loginDiagnosticError {
	result := &loginDiagnosticError{Code: code, ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	var detail browserFailureContext
	if len(details) > 0 {
		detail = details[0]
		result.Stage = detail.Stage
		result.ErrorClass = detail.ErrorClass
	}
	dir := os.Getenv("XHS_STATE_DIR")
	if page == nil || !filepath.IsAbs(dir) {
		return result
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	p := page.Context(ctx)
	root := filepath.Join(dir, "evidence")
	if os.MkdirAll(root, 0700) != nil {
		return result
	}
	stem := label + "-" + integrationNonce()
	record := map[string]any{"code": code, "observed_at": result.ObservedAt}
	if detail.Stage != "" {
		record["stage"] = detail.Stage
		record["error_class"] = detail.ErrorClass
	}
	if len(detail.TopicSuggestions) > 0 {
		if bytes, e := json.Marshal(detail.TopicSuggestions); e == nil {
			var suggestions any
			if json.Unmarshal(bytes, &suggestions) == nil {
				record["topic_suggestions"] = safeEditorDiagnosticValue(suggestions)
			}
		}
	}
	// Preserve the actual visible page before any diagnostic evaluation can
	// exhaust the bounded evidence budget on a disconnected/timing-out page.
	if shot, err := p.Screenshot(false, &proto.PageCaptureScreenshot{Format: proto.PageCaptureScreenshotFormatPng}); err == nil {
		png := filepath.Join(root, stem+".png")
		if os.WriteFile(png, shot, 0600) == nil {
			result.EvidenceRef = png
			record["evidence_ref"] = png
		}
	}
	if info, err := p.Info(); err == nil {
		record["url"] = diagnosticURL(info.URL)
	}
	if text, err := p.Eval(`() => document.body ? document.body.innerText : ''`); err == nil {
		record["visible_text"] = safeLoginDOMText(text.Value.Str())
	}
	if detail.EditorControls {
		if controls, err := xiaohongshu.ReadIntegrationAIControls(p); err == nil {
			record["ai_controls"] = controls
		} else {
			record["ai_controls"] = map[string]any{"readable": false, "error_class": preflightSafeErrorClass(err)}
		}
		if shape, err := xiaohongshu.ReadIntegrationEditorShape(p); err == nil {
			record["editor_shape"] = safeEditorDiagnosticValue(shape)
		} else if len(shape) > 0 {
			record["editor_shape"] = safeEditorDiagnosticValue(shape)
		}
	}
	// Shape keys only help distinguish .value/. _value hydration failures. No
	// raw initial state, account tokens, storage values, or input values are saved.
	if shape, err := p.Eval(`() => {const u=window.__INITIAL_STATE__&&window.__INITIAL_STATE__.user;const raw=u&&u.userInfo;const info=raw&&raw.value!==undefined?raw.value:(raw&&raw._value!==undefined?raw._value:raw);return JSON.stringify({document_ready:document.readyState,user_info_wrapper_keys:raw&&typeof raw==='object'?Object.keys(raw):[],user_info_keys:info&&typeof info==='object'?Object.keys(info):[]});}`); err == nil {
		var value any
		if json.Unmarshal([]byte(shape.Value.Str()), &value) == nil {
			record["state_shape"] = value
		}
	}
	if pageURL, ok := record["url"].(string); ok {
		if text, ok := record["visible_text"].(string); ok && xiaohongshu.IntegrationLoginRestriction(pageURL, text) != "" {
			result.Code = "LOGIN_NETWORK_RESTRICTED"
			record["code"] = result.Code
		}
	}
	path := filepath.Join(root, stem+".json")
	if b, err := json.MarshalIndent(record, "", "  "); err == nil && os.WriteFile(path, b, 0600) == nil {
		result.RecordRef = path
	}
	if result.Code == "LOGIN_NETWORK_RESTRICTED" {
		result.State = "blocked_network"
		_ = persistLoginNetworkBlock(dir, result)
	}
	return result
}

func loginSafeCode(err error) string {
	if err != nil {
		for _, code := range []string{"LOGIN_NETWORK_RESTRICTED", "LOGIN_QR_TIMEOUT", "LOGIN_QR_NAVIGATION_FAILED", "LOGIN_BROWSER_TIMEOUT", "LOGIN_BROWSER_LAUNCH_FAILED"} {
			if strings.Contains(err.Error(), code) {
				return code
			}
		}
	}
	return "LOGIN_QR_UNAVAILABLE"
}

func persistLoginNetworkBlock(dir string, diagnostic *loginDiagnosticError) error {
	if !filepath.IsAbs(dir) || diagnostic == nil || diagnostic.Code != "LOGIN_NETWORK_RESTRICTED" {
		return nil
	}
	diagnostic.State = "blocked_network"
	path := filepath.Join(dir, "backend-block.json")
	if err := exclusiveJSON(path, diagnostic); err != nil && !os.IsExist(err) {
		return err
	}
	return nil
}

// A network block is durable and never cleared on a timer, restart, new login
// request, or another endpoint. Explicit trusted-network recovery is required.
func loadLoginNetworkBlock(dir string) *loginDiagnosticError {
	if !filepath.IsAbs(dir) {
		return nil
	}
	b, err := os.ReadFile(filepath.Join(dir, "backend-block.json"))
	if os.IsNotExist(err) {
		return nil
	}
	result := &loginDiagnosticError{Code: "LOGIN_BLOCK_STATE_UNREADABLE", State: "blocked_network", ObservedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	if err == nil {
		var stored loginDiagnosticError
		if json.Unmarshal(b, &stored) == nil && stored.State == "blocked_network" && stored.Code == "LOGIN_NETWORK_RESTRICTED" {
			return &stored
		}
	}
	return result
}

func currentLoginNetworkBlock() *loginDiagnosticError {
	return loadLoginNetworkBlock(os.Getenv("XHS_STATE_DIR"))
}
