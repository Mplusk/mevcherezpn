package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

type panelClient struct {
	base, key string
	http      *http.Client
}

type panelUser struct {
	UUID      string `json:"uuid,omitempty"`
	Name      string `json:"name,omitempty"`
	Enable    bool   `json:"enable"`
	Mode      string `json:"mode,omitempty"`
	StartDate string `json:"start_date,omitempty"`
	Days      *int   `json:"package_days,omitempty"`
	Quota     int    `json:"usage_limit_GB,omitempty"`
}

func (p panelClient) send(ctx context.Context, method, endpoint, uuid string, body panelUser) (int, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return 0, err
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(payload))
	if err != nil {
		return 0, errors.New("invalid panel address")
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Hiddify-API-Key", p.key)
	resp, err := p.http.Do(req)
	if err != nil {
		return 0, errors.New("panel unavailable")
	} // URLs contain private paths and UUIDs.
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return resp.StatusCode, fmt.Errorf("panel returned HTTP %d", resp.StatusCode)
	}
	var result struct {
		UUID   string `json:"uuid"`
		Enable *bool  `json:"enable"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&result) != nil ||
		result.UUID != uuid || result.Enable == nil || *result.Enable != body.Enable {
		return resp.StatusCode, errors.New("unexpected panel response")
	}
	return resp.StatusCode, nil
}

func (p panelClient) apply(ctx context.Context, uuid string, through time.Time, quota int, enabled bool, now time.Time) error {
	today := now.UTC().Truncate(24 * time.Hour)
	year, month, day := through.Date()
	end := time.Date(year, month, day, 0, 0, 0, 0, time.UTC)
	days := int(end.Sub(today) / (24 * time.Hour))
	user := panelUser{Enable: enabled && days >= 0}
	if user.Enable {
		user.StartDate, user.Days = today.Format(time.DateOnly), &days
		user.Mode, user.Quota = "no_reset", quota
	}
	status, err := p.send(ctx, http.MethodPatch, p.base+uuid+"/", uuid, user)
	if status != http.StatusNotFound {
		return err
	}
	if !user.Enable {
		return nil
	} // Absent accounts already have no access.
	user.UUID, user.Name = uuid, "vpn-"+uuid[:8]
	_, err = p.send(ctx, http.MethodPost, p.base, uuid, user)
	return err
}
