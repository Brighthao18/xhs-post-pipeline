// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/go-rod/rod"
)

type integrationImageEvidence struct {
	Index          int     `json:"index"`
	ActualImageSrc string  `json:"actual_image_src"`
	CaptureMethod  string  `json:"capture_method"`
	NaturalWidth   int     `json:"natural_width"`
	NaturalHeight  int     `json:"natural_height"`
	RenderedWidth  float64 `json:"rendered_width"`
	RenderedHeight float64 `json:"rendered_height"`
	EvidenceRef    string  `json:"evidence_ref"`
	EvidenceSHA256 string  `json:"evidence_sha256"`
	RecordRef      string  `json:"record_ref"`
}

type integrationImageEvidenceBundle struct {
	Refs   []string
	SHA256 []string
	Images []integrationImageEvidence
}

// Every source comes from the actual .img-preview-area .pr img in DOM order.
// A temporary read-only layer displays that same uploaded blob/image source at
// full containment in the viewport; no local expected image is substituted and
// no editor/close/reorder/publish control is clicked. Existing styles, scroll,
// content and preview selection are never changed. The layer is removed even
// on cancellation, and the caller re-reads the original editor afterwards.
func (s *integrationService) captureUploadedImages(page *rod.Page, sources []string) (integrationImageEvidenceBundle, error) {
	bundle := integrationImageEvidenceBundle{Refs: []string{}, SHA256: []string{}, Images: []integrationImageEvidence{}}
	for index, source := range sources {
		evidence, err := s.captureUploadedImage(page, index, source, len(sources))
		if err != nil {
			return bundle, err
		}
		bundle.Refs = append(bundle.Refs, evidence.EvidenceRef)
		bundle.SHA256 = append(bundle.SHA256, evidence.EvidenceSHA256)
		bundle.Images = append(bundle.Images, evidence)
	}
	if err := integrationImageEvidenceMatches(bundle.Refs, bundle.SHA256, len(sources)); err != nil {
		return bundle, err
	}
	return bundle, nil
}

func (s *integrationService) captureUploadedImage(page *rod.Page, index int, source string, count int) (evidence integrationImageEvidence, err error) {
	id := "xhs-integration-evidence-" + integrationNonce()
	defer func() {
		cleaned, cleanupErr := page.Context(context.Background()).Timeout(3*time.Second).Eval(`(id) => {const e=document.getElementById(id);if(e)e.remove();return !document.getElementById(id);}`, id)
		if (cleanupErr != nil || !cleaned.Value.Bool()) && err == nil {
			err = errors.New("IMAGE_EVIDENCE_CLEANUP_FAILED")
		}
	}()
	pp := page.Timeout(15 * time.Second)
	result, err := pp.Eval(`async (index,expected,id,count) => {
const item=document.querySelectorAll('.img-preview-area .pr')[index];
const original=item&&item.querySelector('img');
const source=original&&(original.currentSrc||original.src);
if(!original||!source||source!==expected)throw new Error('IMAGE_EVIDENCE_SOURCE_CHANGED');
const layer=document.createElement('div');layer.id=id;
layer.style.cssText='position:fixed;inset:0;z-index:2147483647;background:white;display:flex;align-items:center;justify-content:center;pointer-events:none;';
const label=document.createElement('div');label.textContent='实际上传预览 '+(index+1)+' / '+count;
label.style.cssText='position:absolute;left:16px;top:8px;color:#333;font:16px sans-serif;';
const image=document.createElement('img');
image.style.cssText='display:block;max-width:calc(100vw - 32px);max-height:calc(100vh - 56px);width:auto;height:auto;object-fit:contain;';
layer.append(label,image);document.body.append(layer);
await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('IMAGE_EVIDENCE_LOAD_TIMEOUT')),10000);image.onload=()=>{clearTimeout(timer);resolve()};image.onerror=()=>{clearTimeout(timer);reject(new Error('IMAGE_EVIDENCE_LOAD_FAILED'))};image.src=source;if(image.complete&&image.naturalWidth>0){clearTimeout(timer);resolve()}});
await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const r=image.getBoundingClientRect();
if(!image.complete||image.naturalWidth<=0||image.naturalHeight<=0||r.width<=0||r.height<=0||r.x<0||r.y<0||r.right>innerWidth+1||r.bottom>innerHeight+1)throw new Error('IMAGE_EVIDENCE_INVALID_LAYOUT');
return JSON.stringify({actual_image_src:source,natural_width:image.naturalWidth,natural_height:image.naturalHeight,rendered_width:r.width,rendered_height:r.height});
}`, index, source, id, count)
	if err != nil {
		return evidence, err
	}
	if err = json.Unmarshal([]byte(result.Value.Str()), &evidence); err != nil {
		return evidence, err
	}
	evidence.Index = index
	evidence.ActualImageSrc = diagnosticURL(evidence.ActualImageSrc)
	evidence.CaptureMethod = "same_page_uploaded_preview_source"
	png, sha, record, err := s.captureScreenshot(pp, fmt.Sprintf("preflight-image-%d", index+1), evidence, false)
	if err != nil {
		return evidence, err
	}
	evidence.EvidenceRef, evidence.EvidenceSHA256, evidence.RecordRef = png, sha, record
	completeRecord, err := json.MarshalIndent(evidence, "", "  ")
	if err != nil {
		return evidence, err
	}
	if err = os.WriteFile(record, completeRecord, 0600); err != nil {
		return evidence, err
	}
	return evidence, nil
}

// Preserve ordered correspondence between the images and their private PNGs.
// Missing/reordered/replaced/duplicated evidence cannot pass submission review.
func integrationImageEvidenceMatches(refs, hashes []string, count int) error {
	if count <= 0 || len(refs) != count || len(hashes) != count {
		return errors.New("IMAGE_EVIDENCE_COUNT_MISMATCH")
	}
	seen := map[string]bool{}
	for index, path := range refs {
		if !filepath.IsAbs(path) || seen[path] || !integrationHash.MatchString(hashes[index]) {
			return errors.New("IMAGE_EVIDENCE_CHANGED")
		}
		seen[path] = true
		actual, err := fileSHA256(path)
		if err != nil || actual != hashes[index] {
			return errors.New("IMAGE_EVIDENCE_CHANGED")
		}
	}
	return nil
}
