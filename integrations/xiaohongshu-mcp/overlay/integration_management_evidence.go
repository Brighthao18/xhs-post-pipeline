// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"context"
	"errors"
	"net/url"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/xpzouying/headless_browser"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
)

type integrationManagementEvidence struct {
	Phase          string                                       `json:"phase"`
	Account        integrationAccount                           `json:"account"`
	ObservedAt     string                                       `json:"observed_at"`
	URL            string                                       `json:"url"`
	EvidenceRef    string                                       `json:"evidence_ref"`
	EvidenceSHA256 string                                       `json:"evidence_sha256"`
	RecordRef      string                                       `json:"record_ref"`
	Unverified     bool                                         `json:"unverified"`
	Confirmed      bool                                         `json:"confirmed"`
	Published      bool                                         `json:"published"`
	View           string                                       `json:"view"`
	FilterLabel    string                                       `json:"filter_label"`
	FilterTabs     []xiaohongshu.IntegrationManagementFilterTab `json:"filter_tabs"`
}

func captureManagementFailure(page *rod.Page, cause error) *loginDiagnosticError {
	return captureBrowserFailure(page, "MANAGEMENT_READBACK_FAILED", "management-failure", browserFailureContext{Stage: "management_navigation", ErrorClass: preflightSafeErrorClass(cause)})
}

// This helper keeps the service browser mode: XHS_LOGIN_VISIBLE never makes a
// read-only background operation open an interactive login window.
func boundedIntegrationBrowser(ctx context.Context) (*headless_browser.Browser, error) {
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	ready := make(chan loginBrowserResult)
	go func() {
		var result loginBrowserResult
		func() {
			defer func() {
				if recover() != nil {
					result.err = errors.New("MANAGEMENT_BROWSER_FAILED")
				}
			}()
			result.browser = newBrowser()
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
		return nil, ctx.Err()
	}
}

func safeManagementLinks(links []xiaohongshu.IntegrationManagementLink) []map[string]string {
	result := []map[string]string{}
	for _, link := range links {
		u, err := url.Parse(link.URL)
		if err != nil || u.Scheme != "https" || u.Hostname() == "" {
			continue
		}
		text := safeLoginDOMText(strings.TrimSpace(link.Text))
		if len([]rune(text)) > 300 {
			text = string([]rune(text)[:300])
		}
		result = append(result, map[string]string{"text": text, "url": diagnosticURL(link.URL)})
		if len(result) >= 100 {
			break
		}
	}
	return result
}

func integrationManagementRecord(account integrationAccount, view *xiaohongshu.IntegrationManagementView, observedAt string) (map[string]any, error) {
	if !account.Authenticated || account.UserID == "" || account.RedID == "" || account.Nickname == "" || view == nil || !view.Ready || !view.HeaderVerified || !xiaohongshu.IntegrationCreatorURLAllowed(view.URL) || view.HeaderMatch["exact_text"] != account.Nickname {
		return nil, errors.New("MANAGEMENT_ACCOUNT_UNVERIFIED")
	}
	requested := view.View
	if requested == "" {
		requested = "all"
	}
	label, err := xiaohongshu.IntegrationManagementFilterLabel(requested)
	if err != nil || view.FilterLabel != label {
		return nil, errors.New("MANAGEMENT_FILTER_LABEL_CHANGED")
	}
	return map[string]any{"phase": "READ_ONLY", "account": account, "observed_at": observedAt, "url": diagnosticURL(view.URL), "visible_text": safeLoginDOMText(view.VisibleText), "visible_links": safeManagementLinks(view.VisibleLinks), "header_match": safeEditorDiagnosticValue(view.HeaderMatch), "unverified": true, "confirmed": false, "published": false, "navigation_method": "exact_visible_sidebar_text", "view": requested, "filter_label": view.FilterLabel, "filter_tabs": view.FilterTabs}, nil
}

func (s *integrationService) managementEvidenceOnPage(ctx context.Context, page *rod.Page, account integrationAccount, requestedViews ...string) (evidence *integrationManagementEvidence, err error) {
	defer func() {
		if recover() != nil {
			evidence = nil
			err = captureManagementFailure(page, errors.New("MANAGEMENT_BROWSER_FAILED"))
		}
	}()
	view, err := xiaohongshu.OpenIntegrationManagement(ctx, page, account.Nickname, requestedViews...)
	if err != nil {
		return nil, captureManagementFailure(page, err)
	}
	observedAt := time.Now().UTC().Format(time.RFC3339Nano)
	record, err := integrationManagementRecord(account, view, observedAt)
	if err != nil {
		return nil, captureManagementFailure(page, err)
	}
	png, sha, recordRef, err := s.capture(page.Context(ctx).Timeout(10*time.Second), "management", record)
	if err != nil {
		return nil, captureManagementFailure(page, err)
	}
	return &integrationManagementEvidence{Phase: "READ_ONLY", Account: account, ObservedAt: observedAt, URL: record["url"].(string), EvidenceRef: png, EvidenceSHA256: sha, RecordRef: recordRef, Unverified: true, Confirmed: false, Published: false, View: record["view"].(string), FilterLabel: view.FilterLabel, FilterTabs: view.FilterTabs}, nil
}

func (s *integrationService) managementEvidence(ctx context.Context, requestedView string) (evidence *integrationManagementEvidence, err error) {
	if _, err := xiaohongshu.IntegrationManagementFilterLabel(requestedView); err != nil {
		return nil, err
	}
	if blocked := loadLoginNetworkBlock(s.dir); blocked != nil {
		return nil, blocked
	}
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	var page *rod.Page
	var closeBrowser func()
	defer func() {
		if recover() != nil {
			evidence = nil
			err = captureManagementFailure(page, errors.New("MANAGEMENT_BROWSER_FAILED"))
		}
		if page != nil {
			safeIntegrationClose(func() { page.Close() })
		}
		if closeBrowser != nil {
			safeIntegrationClose(closeBrowser)
		}
	}()
	b, err := boundedIntegrationBrowser(ctx)
	if err != nil {
		return nil, captureManagementFailure(nil, err)
	}
	closeBrowser = b.Close
	page, err = loginPage(ctx, b)
	if err != nil {
		return nil, captureManagementFailure(page, err)
	}
	account, err := s.identityOnPage(ctx, page)
	if err != nil {
		return nil, err
	}
	return s.managementEvidenceOnPage(ctx, page, account, requestedView)
}
