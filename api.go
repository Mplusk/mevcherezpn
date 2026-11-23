package main

import (
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"time"
)

var customerID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)

type terms struct {
	ValidThrough string `json:"valid_through"`
	QuotaGB      int    `json:"quota_gb"`
}

type subscription struct {
	Customer string `json:"customer_id"`
	terms
	Enabled    bool   `json:"enabled"`
	Sync       string `json:"sync"`
	ProfileURL string `json:"profile_url,omitempty"`
}

func jsonReply(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *service) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := s.db.PingContext(r.Context()); err != nil {
			http.Error(w, "database unavailable", 503)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	for _, method := range []string{"GET", "PUT", "DELETE"} {
		mux.HandleFunc(method+" /v1/subscriptions/{customer}", s.subscription)
	}
	return mux
}

func (s *service) subscription(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+s.token)) != 1 {
		w.Header().Set("WWW-Authenticate", "Bearer")
		http.Error(w, "unauthorized", 401)
		return
	}
	id := r.PathValue("customer")
	if !customerID.MatchString(id) {
		http.Error(w, "invalid customer ID", 400)
		return
	}
	ctx := r.Context()
	var err error
	switch r.Method {
	case "PUT":
		var t terms
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&t) != nil || decoder.Decode(&struct{}{}) != io.EOF {
			http.Error(w, "invalid JSON", 400)
			return
		}
		end, dateErr := time.Parse(time.DateOnly, t.ValidThrough)
		today := time.Now().UTC().Truncate(24 * time.Hour)
		if dateErr != nil || end.Before(today) || end.After(today.AddDate(10, 0, 0)) || t.QuotaGB < 1 || t.QuotaGB > 1000000 {
			http.Error(w, "expected a current or future UTC date within 10 years and quota_gb between 1 and 1000000", 400)
			return
		}
		_, err = s.db.ExecContext(ctx, `INSERT INTO subscriptions(customer_id,valid_through,quota_gb) VALUES ($1,$2,$3)
			ON CONFLICT(customer_id) DO UPDATE SET valid_through=EXCLUDED.valid_through,quota_gb=EXCLUDED.quota_gb,
			enabled=true,version=subscriptions.version+1,failures=0,next_attempt=now()
			WHERE (subscriptions.valid_through, subscriptions.quota_gb, subscriptions.enabled)
			   IS DISTINCT FROM (EXCLUDED.valid_through, EXCLUDED.quota_gb, true)`, id, end, t.QuotaGB)
	case "DELETE":
		_, err = s.db.ExecContext(ctx, `UPDATE subscriptions SET enabled=false,version=version+1,next_attempt=now(),failures=0
			WHERE customer_id=$1 AND enabled`, id)
	}
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	var sub subscription
	var uuid string
	var applied bool
	err = s.db.QueryRowContext(ctx, `SELECT customer_id,panel_uuid,valid_through::text,quota_gb,
		enabled AND valid_through >= (now() AT TIME ZONE 'UTC')::date,
		version=applied_version AND (NOT enabled OR valid_through >= (now() AT TIME ZONE 'UTC')::date)
		FROM subscriptions WHERE customer_id=$1`, id).
		Scan(&sub.Customer, &uuid, &sub.ValidThrough, &sub.QuotaGB, &sub.Enabled, &applied)
	if errors.Is(err, sql.ErrNoRows) {
		http.Error(w, "subscription not found", 404)
		return
	}
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	code := http.StatusOK
	sub.Sync = "pending"
	if applied {
		sub.Sync = "applied"
		if sub.Enabled {
			sub.ProfileURL = s.clientBase + "/" + uuid + "/"
		}
	} else if r.Method != "GET" {
		code = http.StatusAccepted
	}
	jsonReply(w, code, sub)
}
