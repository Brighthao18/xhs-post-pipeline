// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package xiaohongshu

import (
	"context"
	"encoding/json"
	"net/url"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/pkg/errors"
)

type LoginAction struct {
	page *rod.Page
}

func NewLogin(page *rod.Page) *LoginAction {
	return &LoginAction{page: page}
}

func (a *LoginAction) CheckLoginStatus(ctx context.Context) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	pp := a.page.Context(ctx)
	if err := pp.Navigate("https://www.xiaohongshu.com/explore"); err != nil {
		if ctx.Err() != nil {
			return false, errors.New("LOGIN_STATUS_TIMEOUT")
		}
		return false, errors.New("IDENTITY_FAILED")
	}
	var tracker integrationLoginTracker
	for {
		if restriction, err := readLoginRestriction(pp); err == nil && restriction != "" {
			return false, errors.New(restriction)
		}
		status, err := readLoginReadiness(pp)
		if err == nil {
			if resolved, authenticated := tracker.observe(status, time.Now()); resolved {
				return authenticated, nil
			}
		} else {
			tracker.observe(integrationLoginReadiness{}, time.Now())
		}
		select {
		case <-ctx.Done():
			return false, errors.New("LOGIN_STATUS_TIMEOUT")
		case <-time.After(250 * time.Millisecond):
		}
	}
}

type integrationLoginReadiness struct {
	Ready, LoggedLink, QRVisible, Guest bool
	UserID                              string
}

func IntegrationLoginAuthenticated(s integrationLoginReadiness) bool {
	return s.Ready && !s.Guest && (s.LoggedLink || s.UserID != "")
}

type integrationLoginTracker struct{ guestSince time.Time }

// A transient guest/QR before hydration is not immediately treated as logged
// out. Inconclusive states reset the timer; only stable explicit guest evidence
// resolves a negative status. Actual identity is independently required later.
func (t *integrationLoginTracker) observe(s integrationLoginReadiness, now time.Time) (bool, bool) {
	if IntegrationLoginAuthenticated(s) {
		return true, true
	}
	if s.Ready && (s.Guest || s.QRVisible) {
		if t.guestSince.IsZero() {
			t.guestSince = now
		}
		if now.Sub(t.guestSince) >= 2*time.Second {
			return true, false
		}
	} else {
		t.guestSince = time.Time{}
	}
	return false, false
}

const loginUserStateJS = `const unwrap=o=>o&&o.value!==undefined?o.value:(o&&o._value!==undefined?o._value:o);
const u=window.__INITIAL_STATE__&&window.__INITIAL_STATE__.user;
const info=unwrap(u&&u.userInfo);`

func readLoginReadiness(page *rod.Page) (integrationLoginReadiness, error) {
	r, err := page.Eval(`() => {` + loginUserStateJS + `
const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
return JSON.stringify({Ready:document.readyState==='interactive'||document.readyState==='complete',LoggedLink:visible(document.querySelector('.main-container .user .link-wrapper .channel')),QRVisible:visible(document.querySelector('.login-container .qrcode-img')),Guest:!!(info&&info.guest===true),UserID:String(info&&(info.userId||info.user_id)||'')});}`)
	var state integrationLoginReadiness
	if err != nil {
		return state, err
	}
	err = json.Unmarshal([]byte(r.Value.Str()), &state)
	return state, err
}

// CurrentUser 当前登录用户的基础信息。
type CurrentUser struct {
	Nickname string `json:"nickname"`
	UserID   string `json:"userId"`
}

// CurrentUser 从当前页面的 __INITIAL_STATE__ 读取登录用户信息。
// 需在 CheckLoginStatus 之后调用：复用已加载的 explore 页，不做额外导航。
func (a *LoginAction) CurrentUser(ctx context.Context) (*CurrentUser, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	pp := a.page.Context(ctx)
	for {
		res, err := pp.Eval(`() => {` + loginUserStateJS + `if(!info||info.guest===true)return '';return JSON.stringify({nickname:String(info.nickname||info.nickName||''),userId:String(info.userId||info.user_id||'')});}`)
		if err == nil && res.Value.Str() != "" {
			var user CurrentUser
			if json.Unmarshal([]byte(res.Value.Str()), &user) == nil && user.UserID != "" && user.Nickname != "" {
				return &user, nil
			}
		}
		select {
		case <-ctx.Done():
			return nil, errors.New("ACCOUNT_ID_UNREADABLE")
		case <-time.After(250 * time.Millisecond):
		}
	}
}

func (a *LoginAction) Login(ctx context.Context) error {
	pp := a.page.Context(ctx)

	// 导航到小红书首页，这会触发二维码弹窗
	pp.MustNavigate("https://www.xiaohongshu.com/explore").MustWaitLoad()

	time.Sleep(2 * time.Second)

	if exists, _, _ := pp.Has(".main-container .user .link-wrapper .channel"); exists {
		return nil
	}

	pp.MustElement(".main-container .user .link-wrapper .channel")

	return nil
}

func (a *LoginAction) FetchQrcodeImage(ctx context.Context) (string, bool, error) {
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	pp := a.page.Context(ctx)
	if err := pp.Navigate("https://www.xiaohongshu.com/explore"); err != nil {
		return "", false, errors.New("LOGIN_QR_NAVIGATION_FAILED")
	}
	// Has queries return immediately. No Must*, window-load wait, or unbounded
	// element lookup is used in this login discovery path.
	for {
		if restriction, err := readLoginRestriction(pp); err == nil && restriction != "" {
			return "", false, errors.New(restriction)
		}
		if exists, _, err := pp.Has(".main-container .user .link-wrapper .channel"); err == nil && exists {
			return "", true, nil
		}
		if exists, image, err := pp.Has(".login-container .qrcode-img"); err == nil && exists {
			if src, e := image.Attribute("src"); e == nil && src != nil && len(*src) > 0 {
				return *src, false, nil
			}
		}
		select {
		case <-ctx.Done():
			return "", false, errors.New("LOGIN_QR_TIMEOUT")
		case <-time.After(250 * time.Millisecond):
		}
	}
}

// Detect the platform's actual visible network restriction. This does not
// change network/browser/session settings or attempt another login endpoint.
func IntegrationLoginRestriction(pageURL, visibleText string) string {
	u, err := url.Parse(pageURL)
	if err != nil {
		return ""
	}
	risk := strings.Contains(visibleText, "IP存在风险")
	code := strings.Contains(visibleText, "300012")
	if u.Hostname() == "www.xiaohongshu.com" && ((strings.Contains(u.Path, "/website-login/error") && (risk || code)) || (risk && code)) {
		return "LOGIN_NETWORK_RESTRICTED"
	}
	return ""
}

func readLoginRestriction(page *rod.Page) (string, error) {
	r, err := page.Eval(`() => JSON.stringify({url:location.href,text:document.body?document.body.innerText:''})`)
	if err != nil {
		return "", err
	}
	var state struct{ URL, Text string }
	if err = json.Unmarshal([]byte(r.Value.Str()), &state); err != nil {
		return "", err
	}
	return IntegrationLoginRestriction(state.URL, state.Text), nil
}

func (a *LoginAction) WaitForLogin(ctx context.Context) bool {
	pp := a.page.Context(ctx)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
			exists, _, err := pp.Has(".main-container .user .link-wrapper .channel")
			if err == nil && exists {
				return true
			}
		}
	}
}
