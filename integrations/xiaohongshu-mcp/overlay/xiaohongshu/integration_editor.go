// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package xiaohongshu

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/net/html"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/xpzouying/xiaohongshu-mcp/humanize"
)

const integrationAILabel = "笔记含AI合成内容"

// IntegrationEditorError reports the exact editor stage without exposing a
// browser exception (which may contain token-bearing URLs) in the API.
type IntegrationEditorError struct {
	Stage            string
	Cause            error
	TopicSuggestions []IntegrationTagObservation
}

type IntegrationTopicSuggestion struct {
	Text        string   `json:"text"`
	PrimaryName string   `json:"primary_name"`
	ClassName   string   `json:"class_name"`
	ChildTags   []string `json:"child_tags"`
	Visible     bool     `json:"visible"`
}

type IntegrationTagObservation struct {
	Requested    string                       `json:"requested"`
	Candidates   []IntegrationTopicSuggestion `json:"candidates"`
	SelectedName string                       `json:"selected_name,omitempty"`
	PlainText    bool                         `json:"plain_text_fallback"`
}

func (e *IntegrationEditorError) Error() string { return "EDITOR_OPERATION_FAILED" }
func (e *IntegrationEditorError) Unwrap() error { return e.Cause }

type IntegrationAIControls struct {
	AILabelCount        int  `json:"ai_label_count"`
	AddDeclarationCount int  `json:"add_declaration_count"`
	MenuVisible         bool `json:"declaration_menu_visible"`
	AIDeclared          bool `json:"ai_declared"`
}

// Read-only diagnostic of the same existing declaration selectors. It does
// not select a menu option, set a declaration, or inspect browser storage.
func ReadIntegrationAIControls(page *rod.Page) (*IntegrationAIControls, error) {
	a, b, m, err := declarationReadback(page)
	if err != nil {
		return nil, err
	}
	return &IntegrationAIControls{AILabelCount: a, AddDeclarationCount: b, MenuVisible: m, AIDeclared: IntegrationAIReadbackOK(a, b, m)}, nil
}

type IntegrationEditorSnapshot struct {
	Title       string   `json:"title"`
	Content     string   `json:"content"`
	RawContent  string   `json:"raw_content"`
	TopicNames  []string `json:"topic_names"`
	PreviewSrcs []string `json:"preview_srcs"`
	AIVisible   int      `json:"ai_label_count"`
	AddVisible  int      `json:"add_declaration_count"`
	MenuVisible bool     `json:"declaration_menu_visible"`
	AIDeclared  bool     `json:"ai_declared"`
	URL         string   `json:"editor_url"`
}

func IntegrationAIReadbackOK(labelCount, addCount int, menuVisible bool) bool {
	// A selected control and preview badge must both be present after the menu
	// closes. A menu item or a sentence typed into the editor is insufficient.
	return labelCount >= 2 && addCount == 0 && !menuVisible
}

func declarationReadback(page *rod.Page) (int, int, bool, error) {
	r, err := page.Eval(`() => {
        const visible = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e); return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
        const texts=[...document.querySelectorAll('span,div,button,li')].filter(e=>visible(e)&&!e.closest('[contenteditable="true"]')&&!e.children.length).map(e=>e.textContent.trim());
        return JSON.stringify({ai:texts.filter(t=>t==='笔记含AI合成内容').length,add:texts.filter(t=>t==='添加内容类型声明').length,menu:texts.some(t=>t==='虚构演绎，仅供娱乐'||t==='内容包含营销广告')});
    }`)
	if err != nil {
		return 0, 0, false, err
	}
	var v struct {
		AI, Add int
		Menu    bool
	}
	if err := json.Unmarshal([]byte(r.Value.Str()), &v); err != nil {
		return 0, 0, false, err
	}
	return v.AI, v.Add, v.Menu, nil
}

func exactVisibleText(page *rod.Page, text string) (*rod.Element, error) {
	elems, err := page.Elements("span,div,button,li")
	if err != nil {
		return nil, err
	}
	for _, e := range elems {
		ok, err := e.Eval(`(text) => { const r=this.getBoundingClientRect(); const s=getComputedStyle(this); return !this.children.length&&!this.closest('[contenteditable="true"]')&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&this.textContent.trim()===text; }`, text)
		if err == nil && ok.Value.Bool() {
			return e, nil
		}
	}
	return nil, fmt.Errorf("visible exact declaration control %q not found", text)
}

func setAIDeclaration(page *rod.Page) error {
	a, b, menu, err := declarationReadback(page)
	if err != nil {
		return err
	}
	if IntegrationAIReadbackOK(a, b, menu) {
		return nil
	}
	trigger, err := exactVisibleText(page, "添加内容类型声明")
	if err != nil {
		return err
	}
	if err = humanize.Click(trigger); err != nil {
		return err
	}
	deadline := time.Now().Add(5 * time.Second)
	var option *rod.Element
	for time.Now().Before(deadline) {
		option, err = exactVisibleText(page, integrationAILabel)
		if err == nil {
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	if option == nil {
		return errors.New("AI declaration option unavailable")
	}
	if err = humanize.Click(option); err != nil {
		return err
	}
	for time.Now().Before(deadline) {
		a, b, menu, err = declarationReadback(page)
		if err == nil && IntegrationAIReadbackOK(a, b, menu) {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return errors.New("AI declaration selected control and preview badge were not both verified")
}

// PrepareIntegration fills and verifies the editor; it never presses publish.
func (p *PublishAction) PrepareIntegration(ctx context.Context, content PublishImageContent) (snapshot *IntegrationEditorSnapshot, err error) {
	stage := "editor_prepare"
	var suggestions []IntegrationTagObservation
	defer func() {
		if recover() != nil {
			snapshot = nil
			err = errors.New("EDITOR_OPERATION_PANIC")
		}
		if err != nil {
			err = &IntegrationEditorError{Stage: stage, Cause: err, TopicSuggestions: suggestions}
		}
	}()
	if !content.AIGenerated {
		return nil, errors.New("AI_DECLARATION_REQUIRED")
	}
	if len(content.Tags) > 10 || len(content.ImagePaths) == 0 {
		return nil, errors.New("invalid image/tag count")
	}
	stage = "editor_upload"
	page := p.page.Context(ctx).Timeout(5 * time.Minute)
	if err := uploadImages(page, content.ImagePaths); err != nil {
		return nil, err
	}
	stage = "editor_prepare"
	if err := preparePublish(ctx, page, content.Title, content.Content, content.Tags, nil, false, true, "公开可见", nil, func(observation IntegrationTagObservation) { suggestions = append(suggestions, observation) }); err != nil {
		return nil, err
	}
	stage = "editor_readback"
	snapshot, err = ReadIntegrationEditor(page)
	if err != nil {
		return nil, err
	}
	stage = "editor_validate"
	if err = ValidateIntegrationEditor(snapshot, content.Title, content.Content, content.Tags, len(content.ImagePaths)); err != nil {
		return nil, err
	}
	return snapshot, nil
}

func ReadIntegrationEditor(page *rod.Page) (*IntegrationEditorSnapshot, error) {
	title, err := page.Element("div.d-input input")
	if err != nil {
		return nil, err
	}
	v, err := title.Eval(`() => this.value`)
	if err != nil {
		return nil, err
	}
	body, err := getContentElement(page, 3*time.Second)
	if err != nil {
		return nil, err
	}
	w, err := body.Eval(`() => JSON.stringify({raw:this.innerText,html:this.outerHTML})`)
	if err != nil {
		return nil, err
	}
	var bodyDOM struct{ Raw, HTML string }
	if err = json.Unmarshal([]byte(w.Value.Str()), &bodyDOM); err != nil {
		return nil, err
	}
	content, topics, err := NormalizeIntegrationEditorHTML(bodyDOM.HTML)
	if err != nil {
		return nil, err
	}
	preview, err := page.Eval(`() => JSON.stringify([...document.querySelectorAll('.img-preview-area .pr')].map(e=>{ const im=e.querySelector('img'); return im?(im.currentSrc||im.src):(getComputedStyle(e).backgroundImage||''); }))`)
	if err != nil {
		return nil, err
	}
	var srcs []string
	if err = json.Unmarshal([]byte(preview.Value.Str()), &srcs); err != nil {
		return nil, err
	}
	a, b, m, err := declarationReadback(page)
	if err != nil {
		return nil, err
	}
	info, err := page.Info()
	if err != nil {
		return nil, err
	}
	return &IntegrationEditorSnapshot{Title: v.Value.Str(), Content: content, RawContent: bodyDOM.Raw, TopicNames: topics, PreviewSrcs: srcs, AIVisible: a, AddVisible: b, MenuVisible: m, AIDeclared: IntegrationAIReadbackOK(a, b, m), URL: info.URL}, nil
}

func integrationDOMAttr(n *html.Node, key string) (string, bool) {
	for _, attr := range n.Attr {
		if attr.Key == key {
			return attr.Val, true
		}
	}
	return "", false
}
func integrationDOMClass(n *html.Node, want string) bool {
	classes, _ := integrationDOMAttr(n, "class")
	for _, class := range strings.Fields(classes) {
		if class == want {
			return true
		}
	}
	return false
}
func integrationDOMText(n *html.Node) string {
	if n.Type == html.TextNode {
		return n.Data
	}
	if n.Type == html.ElementNode && n.Data == "br" {
		if integrationDOMClass(n, "ProseMirror-trailingBreak") {
			return ""
		}
		return "\n"
	}
	var b strings.Builder
	for child := n.FirstChild; child != nil; child = child.NextSibling {
		b.WriteString(integrationDOMText(child))
	}
	return b.String()
}

// Parse a detached copy of the actual contenteditable DOM. Only the platform
// structure observed in the real diagnostic is normalized: a noneditable
// a.tiptap-topic[data-topic] containing span.content-hide with exactly the
// literal marker. Ordinary body text and unconfirmed structures stay intact.
// The transient HTML is never stored; RawContent retains actual innerText.
func NormalizeIntegrationEditorHTML(rawHTML string) (string, []string, error) {
	doc, err := html.Parse(strings.NewReader(rawHTML))
	if err != nil {
		return "", nil, err
	}
	var editor *html.Node
	var find func(*html.Node)
	find = func(n *html.Node) {
		if editor != nil {
			return
		}
		editable, _ := integrationDOMAttr(n, "contenteditable")
		if n.Type == html.ElementNode && n.Data == "div" && editable == "true" && integrationDOMClass(n, "tiptap") && integrationDOMClass(n, "ProseMirror") {
			editor = n
			return
		}
		for child := n.FirstChild; child != nil; child = child.NextSibling {
			find(child)
		}
	}
	find(doc)
	if editor == nil {
		return "", nil, errors.New("EDITOR_DOM_STRUCTURE_UNVERIFIED")
	}
	var topics []string
	var normalize func(*html.Node)
	normalize = func(n *html.Node) {
		editable, _ := integrationDOMAttr(n, "contenteditable")
		_, dataTopic := integrationDOMAttr(n, "data-topic")
		if n.Type == html.ElementNode && n.Data == "a" && integrationDOMClass(n, "tiptap-topic") && dataTopic && editable == "false" {
			for child := n.FirstChild; child != nil; {
				next := child.NextSibling
				if child.Type == html.ElementNode && child.Data == "span" && integrationDOMClass(child, "content-hide") && integrationDOMText(child) == "[话题]#" {
					n.RemoveChild(child)
				}
				child = next
			}
			name := integrationDOMText(n)
			if strings.HasPrefix(name, "#") {
				topics = append(topics, strings.TrimPrefix(name, "#"))
			}
		}
		for child := n.FirstChild; child != nil; child = child.NextSibling {
			normalize(child)
		}
	}
	normalize(editor)
	var paragraphs []string
	for child := editor.FirstChild; child != nil; child = child.NextSibling {
		if child.Type == html.ElementNode && child.Data == "p" {
			paragraphs = append(paragraphs, integrationDOMText(child))
		} else if child.Type == html.TextNode && strings.TrimSpace(child.Data) != "" {
			return "", nil, errors.New("EDITOR_DOM_STRUCTURE_UNVERIFIED")
		} else if child.Type == html.ElementNode {
			return "", nil, errors.New("EDITOR_DOM_STRUCTURE_UNVERIFIED")
		}
	}
	if len(paragraphs) == 0 {
		return "", nil, errors.New("EDITOR_DOM_STRUCTURE_UNVERIFIED")
	}
	return strings.Join(paragraphs, "\n"), topics, nil
}

// Read-only bounded diagnostic tree, not raw HTML: only tag/class, text,
// attribute NAMES, editability, geometry and query-free image references. No
// input values, links, storage, private attributes or event handlers are read.
// This lets an actual failure establish topic/preview structure before adding
// normalization or image-selection selectors.
func ReadIntegrationEditorShape(page *rod.Page) (map[string]any, error) {
	result := map[string]any{}
	const treeJS = `const cleanSrc=raw=>{if(!raw)return '';if(raw.startsWith('data:'))return 'data:image/[OMITTED]';try{const u=new URL(raw,location.href);u.search='';u.hash='';u.username='';u.password='';return u.href}catch{return ''}};
let remaining=180;
const tree=(n,depth=0)=>{if(!n||remaining--<=0||depth>5)return null;if(n.nodeType===3)return {tag:'#text',text:(n.textContent||'').slice(0,240)};if(n.nodeType!==1)return null;const r=n.getBoundingClientRect();const s={tag:n.tagName.toLowerCase(),class_name:typeof n.className==='string'?n.className:'',attribute_names:[...n.attributes].map(a=>a.name),contenteditable:n.getAttribute('contenteditable'),box:{x:r.x,y:r.y,width:r.width,height:r.height}};if(n.tagName==='IMG')s.image_src=cleanSrc(n.currentSrc||n.src);s.children=[...n.childNodes].slice(0,30).map(c=>tree(c,depth+1)).filter(Boolean);return s;};`
	if body, err := getContentElement(page, 400*time.Millisecond); err == nil {
		if value, e := body.Eval(`() => {` + treeJS + `return JSON.stringify(tree(this));}`); e == nil {
			var shape any
			if json.Unmarshal([]byte(value.Value.Str()), &shape) == nil {
				result["editor_tree"] = shape
			}
		}
	}
	value, err := page.Eval(`() => {` + treeJS + `
const all=[...document.querySelectorAll('[class]')].filter(e=>typeof e.className==='string'&&e.className.toLowerCase().includes('preview'));
const roots=all.filter(e=>!all.some(p=>p!==e&&p.contains(e))).slice(0,8);
const suggestions=[...document.querySelectorAll('#creator-editor-topic-container .item')].slice(0,12);
return JSON.stringify({image_preview_trees:roots.map(e=>tree(e)).filter(Boolean),topic_suggestion_trees:suggestions.map(e=>tree(e)).filter(Boolean)});}`)
	if err != nil {
		return result, err
	}
	var shapes map[string]any
	if err = json.Unmarshal([]byte(value.Value.Str()), &shapes); err != nil {
		return result, err
	}
	for key, shape := range shapes {
		result[key] = shape
	}
	return result, nil
}

func integrationCompact(s string) string {
	return strings.Map(func(r rune) rune {
		if r == '\u200b' || r == '\u200c' || r == '\u200d' || r == '\ufeff' || r == ' ' || r == '\n' || r == '\r' || r == '\t' || r == '\u00a0' {
			return -1
		}
		return r
	}, s)
}

func ValidateIntegrationEditor(s *IntegrationEditorSnapshot, title, body string, tags []string, imageCount int) error {
	if s == nil || strings.TrimSpace(s.Title) != strings.TrimSpace(title) {
		return errors.New("EDITOR_TITLE_MISMATCH")
	}
	expected := body
	for _, tag := range tags {
		expected += "#" + strings.TrimLeft(tag, "#")
	}
	if integrationCompact(s.Content) != integrationCompact(expected) {
		return errors.New("EDITOR_BODY_TAGS_MISMATCH")
	}
	if len(s.PreviewSrcs) != imageCount {
		return errors.New("EDITOR_IMAGE_COUNT_MISMATCH")
	}
	for _, src := range s.PreviewSrcs {
		if src == "" || src == "none" {
			return errors.New("EDITOR_PREVIEW_EVIDENCE_MISSING")
		}
	}
	if !s.AIDeclared {
		return errors.New("AI_DECLARATION_NOT_VERIFIED")
	}
	if !strings.HasPrefix(s.URL, "https://creator.xiaohongshu.com/publish/publish") {
		return errors.New("EDITOR_LOCATION_CHANGED")
	}
	return nil
}

// Called only after the integration journal has durably recorded INTENT.
// A click error is ambiguous: callers must never retry this method.
func ClickPreparedIntegration(page *rod.Page) error { return clickPublishButton(page) }

// WaitPreparedIntegrationOutcome allows the asynchronous request to finish
// before its browser is closed. Navigation is diagnostic only: the integration
// still needs independent management/detail evidence to confirm publication.
func WaitPreparedIntegrationOutcome(page *rod.Page, timeout time.Duration) error {
	return waitIntegrationSubmitOutcome(func() (string, error) {
		info, err := page.Info()
		if err != nil {
			return "", err
		}
		return info.URL, nil
	}, timeout, 500*time.Millisecond)
}

func waitIntegrationSubmitOutcome(readURL func() (string, error), timeout, interval time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		url, err := readURL()
		if err != nil {
			return err
		}
		if strings.HasPrefix(url, "https://creator.xiaohongshu.com/") && !strings.Contains(url, "/publish/publish") {
			return nil
		}
		if !time.Now().Before(deadline) {
			return errors.New("POST_CLICK_WAIT_TIMEOUT: platform page has not finished processing; retain the submission lock")
		}
		remaining := time.Until(deadline)
		if remaining < interval {
			time.Sleep(remaining)
		} else {
			time.Sleep(interval)
		}
	}
}

// Check without clicking; do this before recording INTENT.
func IntegrationPublishAvailable(page *rod.Page) error {
	btn, reason, err := findPublishButton(page)
	if err != nil {
		return err
	}
	if btn == nil || reason != "" {
		return errors.New("PUBLISH_BUTTON_UNAVAILABLE: " + reason)
	}
	return nil
}
