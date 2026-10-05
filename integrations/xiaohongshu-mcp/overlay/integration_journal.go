// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"time"
)

var integrationID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)
var integrationHash = regexp.MustCompile(`^[a-f0-9]{64}$`)

type integrationAccount struct {
	Authenticated bool   `json:"authenticated"`
	UserID        string `json:"user_id"`
	RedID         string `json:"red_id"`
	Nickname      string `json:"nickname"`
	ObservedAt    string `json:"observed_at,omitempty"`
	EvidenceRef   string `json:"evidence_ref,omitempty"`
}

type integrationRequest struct {
	AttemptID           string             `json:"attempt_id"`
	ContentHash         string             `json:"content_hash"`
	ExpectedAccount     integrationAccount `json:"expected_account"`
	Title               string             `json:"title"`
	Content             string             `json:"content"`
	Tags                []string           `json:"tags"`
	Images              []string           `json:"images"`
	ImageHashes         []string           `json:"image_hashes"`
	AIGenerated         bool               `json:"ai_generated"`
	EditorSessionID     string             `json:"editor_session_id,omitempty"`
	VisualReviewed      bool               `json:"visual_reviewed,omitempty"`
	ReviewedEvidenceRef string             `json:"reviewed_evidence_ref,omitempty"`
}

// The caller's content hash identifies its canonical content bundle. This second
// hash binds the exact request independently, so a reused caller hash cannot
// silently replace title/body/account/images.
func (r integrationRequest) payloadHash() string {
	r.AttemptID, r.EditorSessionID, r.ReviewedEvidenceRef = "", "", ""
	r.VisualReviewed = false
	r.ExpectedAccount.Authenticated = false
	r.ExpectedAccount.ObservedAt, r.ExpectedAccount.EvidenceRef = "", ""
	b, _ := json.Marshal(r)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

type integrationAttempt struct {
	AttemptID            string             `json:"attempt_id"`
	ContentHash          string             `json:"content_hash"`
	PayloadHash          string             `json:"payload_hash"`
	Account              integrationAccount `json:"account"`
	Request              integrationRequest `json:"request"`
	Phase                string             `json:"phase"`
	Confirmed            bool               `json:"confirmed"`
	Published            bool               `json:"published"`
	RetryAllowed         bool               `json:"retry_allowed"`
	ClickAttempted       bool               `json:"click_attempted"`
	ClickMayHaveOccurred bool               `json:"click_may_have_occurred"`
	SubmittedAt          string             `json:"submitted_at"`
	ObservedAt           string             `json:"observed_at"`
	EvidenceRef          string             `json:"evidence_ref,omitempty"`
	ErrorCode            string             `json:"error_code,omitempty"`
	Reason               string             `json:"reason,omitempty"`
	Preflight            any                `json:"preflight,omitempty"`
	ManualRetryOf        string             `json:"manual_retry_of,omitempty"`
}

type integrationJournal struct{ dir string }

func newIntegrationJournal(stateDir string) (*integrationJournal, error) {
	if !filepath.IsAbs(stateDir) {
		return nil, errors.New("XHS_STATE_DIR must be an absolute private path")
	}
	dir := filepath.Join(stateDir, "attempts")
	if err := os.MkdirAll(filepath.Join(dir, "content-locks"), 0700); err != nil {
		return nil, err
	}
	return &integrationJournal{dir: dir}, nil
}

func (j *integrationJournal) path(id, suffix string) (string, error) {
	if !integrationID.MatchString(id) {
		return "", errors.New("invalid attempt_id")
	}
	return filepath.Join(j.dir, id+suffix), nil
}

func exclusiveJSON(path string, value any) error {
	b, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	// A failed/partial write is deliberately retained. An uncertain durable
	// marker must block another click rather than being removed and retried.
	defer f.Close()
	if _, err = f.Write(b); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	return f.Close()
}

func (j *integrationJournal) read(id string) (*integrationAttempt, error) {
	p, err := j.path(id, ".intent.json")
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	var a integrationAttempt
	if err = json.Unmarshal(b, &a); err != nil || a.AttemptID != id || a.Phase != "INTENT" {
		return nil, fmt.Errorf("JOURNAL_UNREADABLE: frozen intent for %s cannot be verified", id)
	}
	// Any interrupted intent is UNKNOWN on read. The immutable INTENT is never
	// deleted, even if mouse-down was not reached or the browser crashed.
	a.Phase, a.RetryAllowed = "SUBMIT_UNKNOWN", false
	a.ClickMayHaveOccurred = true
	a.ErrorCode = "INTENT_RECOVERY"
	a.Reason = "Existing submit intent; reconcile only, never submit again"
	if result, e := os.ReadFile(filepath.Join(j.dir, id+".result.json")); e == nil {
		var final integrationAttempt
		if json.Unmarshal(result, &final) == nil && final.AttemptID == id && final.PayloadHash == a.PayloadHash && final.Phase == "SUBMIT_UNKNOWN" && !final.Confirmed && !final.Published && !final.RetryAllowed {
			return &final, nil
		}
	}
	return &a, nil
}

func (j *integrationJournal) begin(r integrationRequest, account integrationAccount, preflight any) (*integrationAttempt, bool, error) {
	if prior, err := j.read(r.AttemptID); err == nil {
		if prior.PayloadHash != r.payloadHash() {
			return nil, false, errors.New("ATTEMPT_CONFLICT: attempt_id is frozen to another payload")
		}
		return prior, false, nil
	} else if !os.IsNotExist(err) {
		return nil, false, err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	a := &integrationAttempt{AttemptID: r.AttemptID, ContentHash: r.ContentHash, PayloadHash: r.payloadHash(), Account: account, Request: r, Phase: "INTENT", SubmittedAt: now, ObservedAt: now, Preflight: preflight}
	a.ClickMayHaveOccurred = true
	key := sha256.Sum256([]byte(account.UserID + "\x00" + r.ContentHash))
	lock := filepath.Join(j.dir, "content-locks", hex.EncodeToString(key[:])+".json")
	if err := exclusiveJSON(lock, map[string]string{"attempt_id": r.AttemptID, "content_hash": r.ContentHash, "user_id": account.UserID, "created_at": now}); err != nil {
		if os.IsExist(err) {
			parent, grantErr := j.consumeManualRetry(r, account, lock)
			if grantErr != nil {
				return nil, false, grantErr
			}
			a.ManualRetryOf = parent
		} else {
			return nil, false, err
		}
	}
	p, err := j.path(r.AttemptID, ".intent.json")
	if err != nil {
		return nil, false, err
	}
	if err = exclusiveJSON(p, a); err != nil {
		// An orphan content lock also stays locked after a failed attempt write.
		return nil, false, err
	}
	return a, true, nil
}

func (j *integrationJournal) finish(a *integrationAttempt) error {
	if a.Phase != "SUBMIT_UNKNOWN" || a.Confirmed || a.Published || a.RetryAllowed {
		return errors.New("integration cannot assert a platform confirmation")
	}
	p, err := j.path(a.AttemptID, ".result.json")
	if err != nil {
		return err
	}
	return exclusiveJSON(p, a)
}
