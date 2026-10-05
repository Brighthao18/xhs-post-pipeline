// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package headless_browser

import (
	"strings"
	"testing"
)

func TestNativeChromeRemovesIncompatibleFeaturesAndKeepsSandbox(t *testing.T) {
	cfg := newDefaultConfig()
	// Deliberately set conflicting upstream options after the native option.
	for _, option := range []Option{WithNativeChrome(), WithFingerprint("windows"), WithFingerprintSeed(123456), WithStealthJS(true), WithUserAgent("custom-UA"), WithLanguage("zh-CN"), WithExtraFlags(map[string]string{"fingerprint-brand": "Chrome", "no-sandbox": "", "disable-setuid-sandbox": "", "window-size": "1280,900"})} {
		option(cfg)
	}
	l := configuredLauncher(cfg)
	if cfg.Fingerprint || cfg.StealthJS || cfg.UserAgent != "" || cfg.Language != "" {
		t.Fatal("native mode retained incompatible UA/stealth/fingerprint behavior")
	}
	for _, arg := range l.FormatArgs() {
		if strings.HasPrefix(arg, "--fingerprint") || strings.HasPrefix(arg, "--user-agent") || arg == "--no-sandbox" || arg == "--disable-setuid-sandbox" {
			t.Fatalf("native argument violation: %s", arg)
		}
	}
	if !l.Has("enable-automation") {
		t.Fatal("native mode must not conceal ordinary automation indicators")
	}
	if !l.Has("window-size") {
		t.Fatal("ordinary configuration unnecessarily changed")
	}
}

func TestNativeChromeHeadlessAndVisibleAreExplicit(t *testing.T) {
	for _, headless := range []bool{true, false} {
		cfg := newDefaultConfig()
		WithNativeChrome()(cfg)
		WithHeadless(headless)(cfg)
		l := configuredLauncher(cfg)
		if l.Has("headless") != headless {
			t.Fatalf("headless setting altered: %v", headless)
		}
	}
}

func TestNativeChromeDoesNotChangeUnrelatedUpstreamMode(t *testing.T) {
	cfg := newDefaultConfig()
	WithFingerprint("windows")(cfg)
	WithFingerprintSeed(123456)(cfg)
	l := configuredLauncher(cfg)
	if !cfg.Fingerprint || !l.Has("fingerprint") || !l.Has("no-sandbox") {
		t.Fatal("unrelated upstream mode changed")
	}
}
