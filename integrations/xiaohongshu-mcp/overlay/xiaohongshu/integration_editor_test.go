// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package xiaohongshu

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestIntegrationAIReadbackRejectsMenuAndEditorOnly(t *testing.T) {
	if IntegrationAIReadbackOK(1, 1, true) || IntegrationAIReadbackOK(1, 0, false) || IntegrationAIReadbackOK(2, 0, true) || IntegrationAIReadbackOK(2, 1, false) {
		t.Fatal("menu item/single occurrence cannot verify declared content")
	}
	if !IntegrationAIReadbackOK(2, 0, false) {
		t.Fatal("selected control plus preview should verify")
	}
}

func TestIntegrationEditorRejectsMissingAIOrChangedImages(t *testing.T) {
	s := &IntegrationEditorSnapshot{Title: "sample", Content: "body\n\n#test ", PreviewSrcs: []string{"blob:actual1", "blob:actual2"}, AIDeclared: true, URL: "https://creator.xiaohongshu.com/publish/publish?source=official"}
	if err := ValidateIntegrationEditor(s, "sample", "body", []string{"test"}, 2); err != nil {
		t.Fatal(err)
	}
	s.AIDeclared = false
	if err := ValidateIntegrationEditor(s, "sample", "body", []string{"test"}, 2); err == nil {
		t.Fatal("no AI declaration accepted")
	}
	s.AIDeclared = true
	s.PreviewSrcs = []string{""}
	if err := ValidateIntegrationEditor(s, "sample", "body", []string{"test"}, 1); err == nil {
		t.Fatal("missing preview evidence accepted")
	}
	s.PreviewSrcs = []string{"blob:actual"}
	s.Content = "body edited #test"
	if err := ValidateIntegrationEditor(s, "sample", "body", []string{"test"}, 1); err == nil {
		t.Fatal("changed body accepted")
	}
}

func TestIntegrationNetworkRestrictionUsesActualPlatformPage(t *testing.T) {
	if got := IntegrationLoginRestriction("https://www.xiaohongshu.com/website-login/error", "IP存在风险，请切换可靠网络环境后重试 300012"); got != "LOGIN_NETWORK_RESTRICTED" {
		t.Fatalf("actual restriction missed: %s", got)
	}
	if got := IntegrationLoginRestriction("https://www.xiaohongshu.com/explore", "登录超时"); got != "" {
		t.Fatalf("generic error misclassified: %s", got)
	}
	if got := IntegrationLoginRestriction("https://example.com/website-login/error", "IP存在风险 300012"); got != "" {
		t.Fatalf("another site's text trusted: %s", got)
	}
}

func TestIntegrationLoginWaitsForHydratedEvidence(t *testing.T) {
	var tracker integrationLoginTracker
	now := time.Date(2026, 10, 4, 0, 0, 0, 0, time.UTC)
	for _, state := range []integrationLoginReadiness{{}, {LoggedLink: true}, {Ready: true}, {Ready: true, Guest: true}} {
		if resolved, _ := tracker.observe(state, now); resolved {
			t.Fatal("loading/transient guest resolved login too early")
		}
		now = now.Add(250 * time.Millisecond)
	}
	if resolved, authenticated := tracker.observe(integrationLoginReadiness{Ready: true, UserID: "actual-account-id"}, now); !resolved || !authenticated {
		t.Fatal("hydrated actual user ID did not resolve login")
	}
}

func TestIntegrationLoginRequiresStableExplicitGuest(t *testing.T) {
	var tracker integrationLoginTracker
	now := time.Date(2026, 10, 4, 0, 0, 0, 0, time.UTC)
	state := integrationLoginReadiness{Ready: true, Guest: true, LoggedLink: true, UserID: "stale-id"}
	if resolved, _ := tracker.observe(state, now); resolved {
		t.Fatal("first guest snapshot resolved too early")
	}
	if resolved, _ := tracker.observe(state, now.Add(time.Second)); resolved {
		t.Fatal("guest grace period ignored")
	}
	if resolved, authenticated := tracker.observe(state, now.Add(2*time.Second)); !resolved || authenticated {
		t.Fatal("stable explicit guest accepted stale account evidence")
	}
}

func TestIntegrationLoginInconclusiveStateResetsGuestTimer(t *testing.T) {
	var tracker integrationLoginTracker
	now := time.Date(2026, 10, 4, 0, 0, 0, 0, time.UTC)
	guest := integrationLoginReadiness{Ready: true, QRVisible: true}
	tracker.observe(guest, now)
	tracker.observe(integrationLoginReadiness{Ready: true}, now.Add(time.Second))
	if resolved, _ := tracker.observe(guest, now.Add(3*time.Second)); resolved {
		t.Fatal("nonconsecutive guest snapshots resolved logged-out state")
	}
	if resolved, authenticated := tracker.observe(integrationLoginReadiness{Ready: true, LoggedLink: true}, now.Add(3500*time.Millisecond)); !resolved || !authenticated {
		t.Fatal("actual logged-in DOM did not replace transient QR")
	}
}

func TestIntegrationEditorPreparationPanicReportsActualStageWithoutBrowser(t *testing.T) {
	action := &PublishAction{}
	snapshot, err := action.PrepareIntegration(context.Background(), PublishImageContent{Title: "test", Content: "body", ImagePaths: []string{"not-accessed.png"}, AIGenerated: true})
	var stageError *IntegrationEditorError
	if snapshot != nil || !errors.As(err, &stageError) || stageError.Stage != "editor_upload" || stageError.Cause.Error() != "EDITOR_OPERATION_PANIC" {
		t.Fatalf("panic escaped preparation stage: %v", err)
	}
}

func TestIntegrationTopicSuggestionsRequireExactUniqueName(t *testing.T) {
	for _, test := range []struct {
		texts     []string
		requested string
		want      int
	}{
		{[]string{"#健康科普howto\n100篇笔记", "#健康科普\n20篇笔记"}, "健康科普", 1},
		{[]string{"#健康科普howto", "#健康科普达人"}, "健康科普", -1},
		{[]string{"无关话题\n健康科普"}, "健康科普", -1},
		{[]string{"健康科普 100篇笔记"}, "健康科普", -1},
		{[]string{"#健康科普", "健康科普"}, "健康科普", -1},
		{[]string{" #睡前放松\r\n20篇笔记"}, "#睡前放松", 0},
	} {
		if got := integrationExactTopicIndex(test.texts, test.requested); got != test.want {
			t.Fatalf("wrong exact topic choice: got %d want %d for %q", got, test.want, test.texts)
		}
	}
}

func TestIntegrationTopicRawMarkersRemainFailClosed(t *testing.T) {
	s := &IntegrationEditorSnapshot{Title: "sample", Content: "正文\n#健康科普[话题]#", RawContent: "正文\n#健康科普[话题]#", PreviewSrcs: []string{"blob:actual"}, AIDeclared: true, URL: "https://creator.xiaohongshu.com/publish/publish"}
	if err := ValidateIntegrationEditor(s, "sample", "正文", []string{"健康科普"}, 1); err == nil {
		t.Fatal("unconfirmed DOM marker formatting was globally normalized")
	}
	s.Content = "正文里原本写着[话题]\n#健康科普"
	if err := ValidateIntegrationEditor(s, "sample", "正文里原本写着[话题]", []string{"健康科普"}, 1); err != nil {
		t.Fatal("ordinary body marker text must remain exact", err)
	}
}

func TestIntegrationConfirmedTopicDOMNormalizesOnlyHiddenMarker(t *testing.T) {
	fixture := `<div class="tiptap ProseMirror" contenteditable="true"><p>正文中的[话题]#必须保留</p><p><br class="ProseMirror-trailingBreak"></p><p><a class="tiptap-topic" data-topic="fixture-only" contenteditable="false">#睡前放松<span class="content-hide">[话题]#</span></a>&nbsp;#健康科普 </p></div>`
	content, topics, err := NormalizeIntegrationEditorHTML(fixture)
	if err != nil {
		t.Fatal(err)
	}
	if content != "正文中的[话题]#必须保留\n\n#睡前放松\u00a0#健康科普 " || len(topics) != 1 || topics[0] != "睡前放松" {
		t.Fatalf("normalization altered body or topic identity: %q %+v", content, topics)
	}
	s := &IntegrationEditorSnapshot{Title: "sample", Content: content, RawContent: "正文中的[话题]#必须保留\n\n#睡前放松[话题]# #健康科普", PreviewSrcs: []string{"blob:actual"}, AIDeclared: true, URL: "https://creator.xiaohongshu.com/publish/publish"}
	if err := ValidateIntegrationEditor(s, "sample", "正文中的[话题]#必须保留", []string{"睡前放松", "健康科普"}, 1); err != nil {
		t.Fatal("confirmed topic marker failed strict body validation", err)
	}
	if !strings.Contains(s.RawContent, "#睡前放松[话题]#") {
		t.Fatal("raw readback was lost")
	}
}

func TestIntegrationTopicDOMCannotHideWrongPrefixOrUnknownMarker(t *testing.T) {
	fixtures := []string{
		`<a class="tiptap-topic" data-topic="fixture" contenteditable="false">#健康科普howto<span class="content-hide">[话题]#</span></a>`,
		`<a class="tiptap-topic" contenteditable="false">#健康科普<span class="content-hide">[话题]#</span></a>`,
		`<a class="tiptap-topic" data-topic="fixture" contenteditable="true">#健康科普<span class="content-hide">[话题]#</span></a>`,
		`<a class="tiptap-topic" data-topic="fixture" contenteditable="false">#健康科普<span class="other">[话题]#</span></a>`,
	}
	for _, topic := range fixtures {
		content, _, err := NormalizeIntegrationEditorHTML(`<div class="tiptap ProseMirror" contenteditable="true"><p>正文</p><p>` + topic + `</p></div>`)
		if err != nil {
			t.Fatal(err)
		}
		s := &IntegrationEditorSnapshot{Title: "sample", Content: content, PreviewSrcs: []string{"blob:actual"}, AIDeclared: true, URL: "https://creator.xiaohongshu.com/publish/publish"}
		if err := ValidateIntegrationEditor(s, "sample", "正文", []string{"健康科普"}, 1); err == nil {
			t.Fatalf("wrong topic/unknown marker accepted: %s", topic)
		}
	}
}

func TestIntegrationManagementNavigationOnlyAcceptsActualCreatorOrigin(t *testing.T) {
	initial := "https://creator.xiaohongshu.com/publish/publish?source=official"
	if !IntegrationCreatorURLAllowed(initial) || !IntegrationManagementURLChanged(initial, "https://creator.xiaohongshu.com/fixture-readonly-management") {
		t.Fatal("creator navigation fixture rejected")
	}
	for _, actual := range []string{"https://creator.xiaohongshu.com.evil.example/fixture", "http://creator.xiaohongshu.com/fixture", "https://creator.xiaohongshu.com:444/fixture", "https://www.xiaohongshu.com/fixture", "https://user:password" + "@" + "creator.xiaohongshu.com/fixture", initial, "https://creator.xiaohongshu.com/publish/publish?changed=query-only"} {
		if IntegrationManagementURLChanged(initial, actual) {
			t.Fatalf("wrong origin/unchanged document accepted: %s", actual)
		}
	}
}

func TestIntegrationCancelledManagementDoesNotNavigate(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if view, err := OpenIntegrationManagement(ctx, nil, "test account"); view != nil || !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled readback touched browser: %+v %v", view, err)
	}
}

func TestIntegrationManagementFiltersWhitelistAndClickExactlyOnce(t *testing.T) {
	for _, test := range []struct {
		view, label string
		clicks      int
	}{{"all", "", 0}, {"published", "已发布", 1}, {"pending_review", "审核中", 1}, {"rejected", "未通过", 1}} {
		calls := 0
		label, err := ApplyIntegrationManagementFilter(test.view, func(actual string) error {
			calls++
			if actual != test.label {
				t.Fatalf("wrong actual filter label %s", actual)
			}
			return nil
		})
		if err != nil || label != test.label || calls != test.clicks {
			t.Fatalf("wrong readonly filter action %s: label=%s calls=%d err=%v", test.view, label, calls, err)
		}
	}
	for _, view := range []string{"", "PUBLISHED", "published ", "scheduled", "edit", "delete"} {
		calls := 0
		if _, err := ApplyIntegrationManagementFilter(view, func(string) error { calls++; return nil }); err == nil || err.Error() != "INVALID_MANAGEMENT_VIEW" || calls != 0 {
			t.Fatalf("invalid view caused action: %q %d %v", view, calls, err)
		}
	}
	calls := 0
	failed := errors.New("simulated readonly click failure")
	label, err := ApplyIntegrationManagementFilter("published", func(string) error { calls++; return failed })
	if calls != 1 || label != "" || !errors.Is(err, failed) {
		t.Fatal("failed filter click retried or claimed clicked label")
	}
}
