// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package xiaohongshu

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestIntegrationSubmitWaitKeepsProcessingPageAlive(t *testing.T) {
	reads := 0
	err := waitIntegrationSubmitOutcome(func() (string, error) {
		reads++
		if reads < 4 {
			return "https://creator.xiaohongshu.com/publish/publish", nil
		}
		return "https://creator.xiaohongshu.com/publish/success", nil
	}, time.Second, time.Millisecond)
	if err != nil || reads != 4 {
		t.Fatalf("returned before asynchronous processing completed: reads=%d err=%v", reads, err)
	}
}

func TestIntegrationSubmitWaitTimeoutAndPageLossStayUnknown(t *testing.T) {
	for _, url := range []string{"https://creator.xiaohongshu.com/publish/publish", "https://example.com/success", ""} {
		err := waitIntegrationSubmitOutcome(func() (string, error) { return url, nil }, 5*time.Millisecond, time.Millisecond)
		if err == nil || !strings.Contains(err.Error(), "POST_CLICK_WAIT_TIMEOUT") {
			t.Fatalf("incomplete or unrelated page accepted: url=%q err=%v", url, err)
		}
	}
	want := errors.New("page connection lost")
	err := waitIntegrationSubmitOutcome(func() (string, error) { return "", want }, time.Second, time.Millisecond)
	if !errors.Is(err, want) {
		t.Fatalf("page loss was not retained as unknown: %v", err)
	}
}
