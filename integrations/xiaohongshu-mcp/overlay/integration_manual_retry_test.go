// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestIntegrationManualRetryIsBoundAndSingleUse(t *testing.T) {
	r := integrationTestRequest(t)
	j, _ := newIntegrationJournal(t.TempDir())
	parent, _, _ := j.begin(r, integrationTestAccount(r), nil)
	proof := filepath.Join(t.TempDir(), "human-authorization.txt")
	os.WriteFile(proof, []byte("Explicit human authorization for exactly one additional attempt, accepting duplicate risk."), 0600)
	proofHash, _ := fileSHA256(proof)
	r.AttemptID = "human-retry"
	grant := integrationManualRetryGrant{ParentAttemptID: parent.AttemptID, AttemptID: r.AttemptID, UserID: parent.Account.UserID, RedID: parent.Account.RedID, ContentHash: parent.ContentHash, PayloadHash: parent.PayloadHash, AuthorizationRef: proof, AuthorizationSHA256: proofHash, AuthorizedAt: time.Now().Add(-time.Second).UTC().Format(time.RFC3339Nano), ExpiresAt: time.Now().Add(time.Minute).UTC().Format(time.RFC3339Nano), MaxExtraSubmissions: 1}
	os.MkdirAll(filepath.Join(j.dir, "manual-retry-grants"), 0700)
	exclusiveJSON(filepath.Join(j.dir, "manual-retry-grants", r.AttemptID+".json"), grant)
	a, fresh, err := j.begin(r, integrationTestAccount(r), nil)
	if err != nil || !fresh || a.ManualRetryOf != parent.AttemptID {
		t.Fatalf("bound human retry rejected: %+v %v", a, err)
	}
	if _, fresh, err = j.begin(r, integrationTestAccount(r), nil); err != nil || fresh {
		t.Fatalf("same intent was clicked again: fresh=%v err=%v", fresh, err)
	}
	r.AttemptID = "second-human-retry"
	grant.AttemptID = r.AttemptID
	exclusiveJSON(filepath.Join(j.dir, "manual-retry-grants", r.AttemptID+".json"), grant)
	if _, fresh, err = j.begin(r, integrationTestAccount(r), nil); err == nil || fresh {
		t.Fatal("one authorization allowed more than one extra attempt")
	}
	got, err := j.read(parent.AttemptID)
	if err != nil || got.Phase != "SUBMIT_UNKNOWN" || got.Request.Title != parent.Request.Title {
		t.Fatal("original unknown intent was overwritten")
	}
}

func TestIntegrationManualRetryRejectsChangedContentAndExpiredGrant(t *testing.T) {
	for _, changed := range []bool{false, true} {
		r := integrationTestRequest(t)
		j, _ := newIntegrationJournal(t.TempDir())
		parent, _, _ := j.begin(r, integrationTestAccount(r), nil)
		r.AttemptID = "human-retry"
		proof := filepath.Join(t.TempDir(), "authorization.txt")
		os.WriteFile(proof, []byte("one human retry"), 0600)
		hash, _ := fileSHA256(proof)
		expires := time.Now().Add(-time.Second)
		if changed {
			expires = time.Now().Add(time.Minute)
			r.Content = "different body"
		}
		grant := integrationManualRetryGrant{ParentAttemptID: parent.AttemptID, AttemptID: r.AttemptID, UserID: parent.Account.UserID, RedID: parent.Account.RedID, ContentHash: parent.ContentHash, PayloadHash: parent.PayloadHash, AuthorizationRef: proof, AuthorizationSHA256: hash, AuthorizedAt: time.Now().Add(-time.Minute).UTC().Format(time.RFC3339Nano), ExpiresAt: expires.UTC().Format(time.RFC3339Nano), MaxExtraSubmissions: 1}
		os.MkdirAll(filepath.Join(j.dir, "manual-retry-grants"), 0700)
		exclusiveJSON(filepath.Join(j.dir, "manual-retry-grants", r.AttemptID+".json"), grant)
		if _, fresh, err := j.begin(r, integrationTestAccount(r), nil); err == nil || fresh || !strings.Contains(err.Error(), "CONTENT_ALREADY_INTENDED") {
			t.Fatal("mismatched/expired grant was accepted")
		}
	}
}
