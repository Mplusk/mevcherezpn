package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func TestSubscriptionLifecycle(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("set TEST_DATABASE_URL to run the PostgreSQL integration test")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	if _, err = db.Exec("SET search_path TO pg_temp"); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(strings.Replace(schema, "CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1)); err != nil {
		t.Fatal(err)
	}

	var stored panelUser
	calls, creates := 0, 0
	loseResponse := true
	panel := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("Hiddify-API-Key") != "test-panel-key" {
			t.Error("missing API key")
		}
		var body panelUser
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if r.Method == "PATCH" && stored.UUID == "" {
			http.NotFound(w, r)
			return
		}
		if r.Method == "POST" {
			creates++
			if r.URL.Path != "/user/" || body.UUID == "" {
				t.Error("invalid create request")
			}
			stored = body
			if loseResponse {
				loseResponse = false
				http.Error(w, "temporary error after commit", 502)
				return
			}
		} else {
			if r.Method != "PATCH" || r.URL.Path != "/user/"+stored.UUID+"/" {
				t.Error("unexpected update target")
			}
			body.UUID = stored.UUID
			stored = body
		}
		_ = json.NewEncoder(w).Encode(stored)
	}))
	defer panel.Close()
	s := service{db: db, token: "test-token", clientBase: "https://vpn.example/client",
		panel: panelClient{base: panel.URL + "/user/", key: "test-panel-key", http: panel.Client()}}
	handler := s.routes()
	request := func(method, body string, want int) subscription {
		t.Helper()
		req := httptest.NewRequest(method, "/v1/subscriptions/order-1", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer test-token")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != want {
			t.Fatalf("%s got %d: %s", method, w.Code, w.Body.String())
		}
		var sub subscription
		if w.Code < 300 {
			if err := json.Unmarshal(w.Body.Bytes(), &sub); err != nil {
				t.Fatal(err)
			}
		}
		return sub
	}
	syncNext := func() {
		t.Helper()
		more, err := s.syncOne(context.Background())
		if !more || err != nil {
			t.Fatalf("expected queued work, got %v %v", more, err)
		}
	}
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest("GET", "/v1/subscriptions/order-1", nil))
	if w.Code != 401 {
		t.Fatal("unauthorized access")
	}
	request("PUT", `{"valid_through":"2000-01-01","quota_gb":5}`, 400)
	end := time.Now().UTC().AddDate(0, 0, 30).Format(time.DateOnly)
	desired := fmt.Sprintf(`{"valid_through":%q,"quota_gb":50}`, end)
	request("PUT", desired, 202)
	syncNext()
	if sub := request("GET", "", 200); sub.Sync != "pending" || sub.ProfileURL != "" {
		t.Fatal("unconfirmed profile exposed")
	}
	if _, err := db.Exec("UPDATE subscriptions SET next_attempt=now()"); err != nil {
		t.Fatal(err)
	}
	syncNext()
	active := request("PUT", desired, 200)
	if active.ProfileURL == "" || active.Sync != "applied" || creates != 1 || calls != 3 || stored.Quota != 50 || stored.Days == nil || *stored.Days != 30 {
		t.Fatalf("incorrect provision or replay: %+v, creates=%d calls=%d", active, creates, calls)
	}
	renew := fmt.Sprintf(`{"valid_through":%q,"quota_gb":100}`, time.Now().UTC().AddDate(0, 0, 60).Format(time.DateOnly))
	request("PUT", renew, 202)
	syncNext()
	if request("GET", "", 200).ProfileURL != active.ProfileURL || stored.Quota != 100 || *stored.Days != 60 {
		t.Fatal("renewal changed profile or lost terms")
	}
	request("DELETE", "", 202)
	syncNext()
	if sub := request("DELETE", "", 200); sub.ProfileURL != "" || sub.Enabled || stored.Enable {
		t.Fatal("revoke failed")
	}
	request("PUT", desired, 202)
	syncNext()
	if _, err := db.Exec("UPDATE subscriptions SET valid_through=CURRENT_DATE-1"); err != nil {
		t.Fatal(err)
	}
	syncNext()
	if sub := request("GET", "", 200); sub.ProfileURL != "" || sub.Enabled || stored.Enable {
		t.Fatal("expiry failed")
	}
}
