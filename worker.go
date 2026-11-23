package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"time"
)

func (s *service) worker(ctx context.Context) {
	for ctx.Err() == nil {
		more, err := s.syncOne(ctx)
		if err != nil && ctx.Err() == nil {
			log.Printf("subscription worker: %v", err)
		}
		if more && err == nil {
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func (s *service) syncOne(ctx context.Context) (bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	// Expire the desired state before selecting work, including after downtime.
	_, err = tx.ExecContext(ctx, `UPDATE subscriptions SET enabled=false,version=version+1,next_attempt=now(),failures=0
		WHERE enabled AND valid_through < (now() AT TIME ZONE 'UTC')::date`)
	if err != nil {
		return false, err
	}
	var uuid string
	var end time.Time
	var quota int
	var enabled bool
	err = tx.QueryRowContext(ctx, `SELECT panel_uuid,valid_through,quota_gb,enabled FROM subscriptions
		WHERE version>applied_version AND next_attempt<=now()
		ORDER BY next_attempt,customer_id LIMIT 1 FOR UPDATE SKIP LOCKED`).Scan(&uuid, &end, &quota, &enabled)
	if errors.Is(err, sql.ErrNoRows) {
		return false, tx.Commit()
	}
	if err != nil {
		return false, err
	}
	// Other workers and concurrent changes to this subscription wait for the panel write.
	attempt, cancel := context.WithTimeout(ctx, 12*time.Second)
	panelErr := s.panel.apply(attempt, uuid, end, quota, enabled, time.Now())
	cancel()
	if panelErr != nil {
		log.Printf("subscription sync will retry: %v", panelErr)
		_, err = tx.ExecContext(ctx, `UPDATE subscriptions SET failures=failures+1,
			next_attempt=clock_timestamp()+make_interval(secs=>least(300,5*(1<<least(failures,6))))
			WHERE panel_uuid=$1`, uuid)
	} else {
		_, err = tx.ExecContext(ctx, `UPDATE subscriptions SET applied_version=version,failures=0 WHERE panel_uuid=$1`, uuid)
	}
	if err != nil {
		return false, err
	}
	return true, tx.Commit()
}
