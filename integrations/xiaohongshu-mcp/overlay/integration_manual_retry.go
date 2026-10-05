// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"
)

// This private file is created only for an explicitly authorized human retry.
// There is no HTTP route to grant retries; ordinary submissions remain locked.
type integrationManualRetryGrant struct {
	ParentAttemptID     string `json:"parent_attempt_id"`
	AttemptID           string `json:"attempt_id"`
	UserID              string `json:"user_id"`
	RedID               string `json:"red_id"`
	ContentHash         string `json:"content_hash"`
	PayloadHash         string `json:"payload_hash"`
	AuthorizationRef    string `json:"authorization_ref"`
	AuthorizationSHA256 string `json:"authorization_sha256"`
	AuthorizedAt        string `json:"authorized_at"`
	ExpiresAt           string `json:"expires_at"`
	MaxExtraSubmissions int    `json:"max_extra_submissions"`
}

func (j *integrationJournal) consumeManualRetry(r integrationRequest, account integrationAccount, lockPath string) (string, error) {
	denied := errors.New("CONTENT_ALREADY_INTENDED: this account/content already has a submit intent; a bound human grant is required for one additional attempt")
	var grant integrationManualRetryGrant
	b, err := os.ReadFile(filepath.Join(j.dir, "manual-retry-grants", r.AttemptID+".json"))
	if err != nil || json.Unmarshal(b, &grant) != nil || !integrationID.MatchString(grant.ParentAttemptID) || grant.AttemptID != r.AttemptID || grant.ParentAttemptID == r.AttemptID || grant.MaxExtraSubmissions != 1 {
		return "", denied
	}
	var lock struct {
		AttemptID string `json:"attempt_id"`
	}
	b, err = os.ReadFile(lockPath)
	if err != nil || json.Unmarshal(b, &lock) != nil || lock.AttemptID != grant.ParentAttemptID {
		return "", denied
	}
	parent, err := j.read(grant.ParentAttemptID)
	if err != nil || parent.ManualRetryOf != "" || parent.Phase != "SUBMIT_UNKNOWN" || parent.PayloadHash != r.payloadHash() || parent.ContentHash != r.ContentHash || !integrationIdentityMatches(parent.Account, account) || grant.UserID != account.UserID || grant.RedID != account.RedID || grant.ContentHash != r.ContentHash || grant.PayloadHash != parent.PayloadHash {
		return "", denied
	}
	authorized, e1 := time.Parse(time.RFC3339Nano, grant.AuthorizedAt)
	expires, e2 := time.Parse(time.RFC3339Nano, grant.ExpiresAt)
	now := time.Now()
	if e1 != nil || e2 != nil || authorized.After(now) || !expires.After(now) || expires.Sub(authorized) > time.Hour || !expires.After(authorized) || !filepath.IsAbs(grant.AuthorizationRef) {
		return "", denied
	}
	hash, err := fileSHA256(grant.AuthorizationRef)
	if err != nil || hash != grant.AuthorizationSHA256 {
		return "", denied
	}
	consumed := filepath.Join(j.dir, "manual-retry-consumed")
	if err = os.MkdirAll(consumed, 0700); err != nil {
		return "", err
	}
	// Consume before creating the new intent. Crashes and repeated grants never
	// release the old content lock or permit another additional mouse event.
	if err = exclusiveJSON(filepath.Join(consumed, grant.ParentAttemptID+".json"), grant); err != nil {
		return "", denied
	}
	return grant.ParentAttemptID, nil
}
