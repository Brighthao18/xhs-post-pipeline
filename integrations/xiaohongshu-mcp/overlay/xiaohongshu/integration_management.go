// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package xiaohongshu

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/xpzouying/xiaohongshu-mcp/humanize"
)

type IntegrationManagementLink struct {
	Text string `json:"text"`
	URL  string `json:"url"`
}

type IntegrationManagementView struct {
	URL            string                           `json:"url"`
	VisibleText    string                           `json:"visible_text"`
	VisibleLinks   []IntegrationManagementLink      `json:"visible_links"`
	HeaderMatch    map[string]any                   `json:"header_match"`
	Ready          bool                             `json:"-"`
	HeaderVerified bool                             `json:"-"`
	View           string                           `json:"view"`
	FilterLabel    string                           `json:"filter_label"`
	FilterTabs     []IntegrationManagementFilterTab `json:"filter_tabs"`
}

type IntegrationManagementFilterTab struct {
	Label        string `json:"label"`
	Class        string `json:"class"`
	AriaSelected string `json:"aria-selected"`
	Role         string `json:"role"`
}

func IntegrationManagementFilterLabel(view string) (string, error) {
	switch view {
	case "all":
		return "", nil
	case "published":
		return "已发布", nil
	case "pending_review":
		return "审核中", nil
	case "rejected":
		return "未通过", nil
	default:
		return "", errors.New("INVALID_MANAGEMENT_VIEW")
	}
}

// A requested filter is one read-only click at most. There is deliberately no
// retry or mapping from this action to a selected/published platform state.
func ApplyIntegrationManagementFilter(view string, click func(string) error) (string, error) {
	label, err := IntegrationManagementFilterLabel(view)
	if err != nil || label == "" {
		return label, err
	}
	if err = click(label); err != nil {
		return "", err
	}
	return label, nil
}

func IntegrationCreatorURLAllowed(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Scheme == "https" && u.Host == "creator.xiaohongshu.com" && u.User == nil
}

func IntegrationManagementURLChanged(initial, actual string) bool {
	if !IntegrationCreatorURLAllowed(initial) || !IntegrationCreatorURLAllowed(actual) {
		return false
	}
	before, _ := url.Parse(initial)
	after, _ := url.Parse(actual)
	return before.Path != after.Path || before.Fragment != after.Fragment
}

func readIntegrationManagementView(page *rod.Page, nickname string) (*IntegrationManagementView, error) {
	result, err := page.Eval(`(nickname) => {
const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&r.bottom>0&&r.top<innerHeight};
const candidates=[...document.querySelectorAll('span,div,a,button')].filter(e=>!e.children.length&&visible(e)&&e.textContent.trim()===nickname);
let header=null;
for(const e of candidates){const r=e.getBoundingClientRect();const semantic=!!e.closest('header,[role="banner"]');if(r.top>=0&&r.bottom<=160&&r.left>=innerWidth/2){header={exact_text:nickname,method:semantic?'semantic_header':'visible_top_account_band',box:{x:r.x,y:r.y,width:r.width,height:r.height}};break}}
const clean=raw=>{try{const u=new URL(raw,location.href);u.search='';u.hash='';u.username='';u.password='';return u.href}catch{return ''}};
const links=[...document.querySelectorAll('a[href]')].filter(visible).slice(0,100).map(e=>({text:e.innerText.slice(0,300),url:clean(e.href)}));
const labels=new Set(['全部','已发布','审核中','未通过']);
const tabs=[...document.querySelectorAll('span,div,a,button')].filter(e=>!e.children.length&&visible(e)&&labels.has(e.textContent.trim())).slice(0,20).map(e=>({label:e.textContent.trim(),class:e.getAttribute('class')||'','aria-selected':e.getAttribute('aria-selected')||'',role:e.getAttribute('role')||''}));
return JSON.stringify({url:location.href,visible_text:document.body?document.body.innerText.slice(0,16000):'',visible_links:links,header_match:header,filter_tabs:tabs,ready:document.readyState==='interactive'||document.readyState==='complete',header_verified:!!header});
}`, nickname)
	if err != nil {
		return nil, err
	}
	var view struct {
		URL            string                           `json:"url"`
		VisibleText    string                           `json:"visible_text"`
		VisibleLinks   []IntegrationManagementLink      `json:"visible_links"`
		HeaderMatch    map[string]any                   `json:"header_match"`
		Ready          bool                             `json:"ready"`
		HeaderVerified bool                             `json:"header_verified"`
		FilterTabs     []IntegrationManagementFilterTab `json:"filter_tabs"`
	}
	if err = json.Unmarshal([]byte(result.Value.Str()), &view); err != nil {
		return nil, err
	}
	return &IntegrationManagementView{URL: view.URL, VisibleText: view.VisibleText, VisibleLinks: view.VisibleLinks, HeaderMatch: view.HeaderMatch, Ready: view.Ready, HeaderVerified: view.HeaderVerified, FilterTabs: view.FilterTabs}, nil
}

// Only navigate from the already observed creator publish URL via exact
// visible sidebar text. No guessed management URL, edit/save/publish action,
// login operation, storage inspection or retry of the navigation click.
func OpenIntegrationManagement(ctx context.Context, page *rod.Page, nickname string, requestedViews ...string) (*IntegrationManagementView, error) {
	requested := "all"
	if len(requestedViews) > 0 {
		requested = requestedViews[0]
	}
	if _, err := IntegrationManagementFilterLabel(requested); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, 35*time.Second)
	defer cancel()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if nickname == "" {
		return nil, errors.New("MANAGEMENT_ACCOUNT_UNVERIFIED")
	}
	p := page.Context(ctx)
	if err := p.Navigate(urlOfPublic); err != nil {
		return nil, err
	}
	for {
		view, err := readIntegrationManagementView(p, nickname)
		if err == nil && IntegrationCreatorURLAllowed(view.URL) && view.Ready && view.HeaderVerified {
			link, lookupErr := exactVisibleText(p, "笔记管理")
			if lookupErr == nil {
				initial := view.URL
				if err = humanize.Click(link); err != nil {
					return nil, err
				}
				// Observe actual navigation and stable DOM before recording. This
				// proves only which page was read, not any note's publication state.
				for {
					managed, readErr := readIntegrationManagementView(p, nickname)
					if readErr == nil && managed.Ready && managed.HeaderVerified && IntegrationManagementURLChanged(initial, managed.URL) {
						if stableErr := p.WaitDOMStable(500*time.Millisecond, 0.1); stableErr != nil {
							return nil, stableErr
						}
						final, finalErr := readIntegrationManagementView(p, nickname)
						if finalErr != nil {
							return nil, finalErr
						}
						if !final.Ready || !final.HeaderVerified || !IntegrationManagementURLChanged(initial, final.URL) {
							return nil, errors.New("MANAGEMENT_ACCOUNT_UNVERIFIED")
						}
						clicked, filterErr := ApplyIntegrationManagementFilter(requested, func(label string) error {
							target, lookupErr := exactVisibleText(p, label)
							if lookupErr != nil {
								return lookupErr
							}
							actual, readErr := target.Text()
							if readErr != nil {
								return readErr
							}
							if strings.TrimSpace(actual) != label {
								return errors.New("MANAGEMENT_FILTER_LABEL_CHANGED")
							}
							return humanize.Click(target)
						})
						if filterErr != nil {
							return nil, filterErr
						}
						if clicked != "" {
							if stableErr := p.WaitDOMStable(500*time.Millisecond, 0.1); stableErr != nil {
								return nil, stableErr
							}
							final, finalErr = readIntegrationManagementView(p, nickname)
							if finalErr != nil {
								return nil, finalErr
							}
							if !final.Ready || !final.HeaderVerified || !IntegrationManagementURLChanged(initial, final.URL) {
								return nil, errors.New("MANAGEMENT_ACCOUNT_UNVERIFIED")
							}
						}
						final.View, final.FilterLabel = requested, clicked
						return final, nil
					}
					select {
					case <-ctx.Done():
						return nil, ctx.Err()
					case <-time.After(250 * time.Millisecond):
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
}
