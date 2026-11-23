package main

import (
	"context"
	"database/sql"
	_ "embed"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	_ "github.com/lib/pq"
)

//go:embed schema.sql
var schema string

type service struct {
	db         *sql.DB
	token      string
	panel      panelClient
	clientBase string
}

func httpsBase(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Scheme == "https" && u.Hostname() != "" && u.User == nil &&
		u.RawQuery == "" && u.Fragment == "" && strings.Trim(u.Path, "/") != ""
}

func run(ctx context.Context) error {
	adminBase := strings.TrimRight(os.Getenv("HIDDIFY_ADMIN_BASE"), "/")
	clientBase := strings.TrimRight(os.Getenv("HIDDIFY_CLIENT_BASE"), "/")
	key, token := os.Getenv("HIDDIFY_API_KEY"), os.Getenv("VPN_ADMIN_TOKEN")
	if !httpsBase(adminBase) || !httpsBase(clientBase) || key == "" || len(token) < 32 {
		return errors.New("set HIDDIFY_ADMIN_BASE, HIDDIFY_CLIENT_BASE, HIDDIFY_API_KEY and VPN_ADMIN_TOKEN (32+ characters)")
	}
	if os.Getenv("PGPASSWORD") == "" {
		return errors.New("PGPASSWORD is required")
	}
	db, err := sql.Open("postgres", "")
	if err != nil {
		return errors.New("invalid PostgreSQL configuration")
	}
	defer db.Close()
	db.SetMaxOpenConns(12)
	setupCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	_, err = db.ExecContext(setupCtx, schema)
	cancel()
	if err != nil {
		return fmt.Errorf("database initialization: %w", err)
	}
	s := service{db: db, token: token, clientBase: clientBase, panel: panelClient{
		base: adminBase + "/api/v2/admin/user/", key: key,
		http: &http.Client{Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
	}}
	server := &http.Server{Addr: ":8080", Handler: s.routes(), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 10 * time.Second, WriteTimeout: 12 * time.Second, IdleTimeout: 60 * time.Second}
	workerCtx, stop := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { defer close(done); s.worker(workerCtx) }()
	defer func() { stop(); <-done }()
	errorsCh := make(chan error, 1)
	go func() { errorsCh <- server.ListenAndServe() }()
	select {
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(shutdown)
	case err := <-errorsCh:
		return err
	}
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx); err != nil {
		log.Fatal(err)
	}
}
