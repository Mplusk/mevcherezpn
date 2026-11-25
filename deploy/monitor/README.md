# External VPN monitor

`vpn-monitor.yml` runs every 15 minutes from one GitHub-hosted runner and on manual dispatch. It checks three stored VLESS/WS/TLS profiles independently through Xray and checks the subscription endpoint separately. Each node is retried and uses two external IP services; an exit IP must match the expected node. `check_seconds` includes setup and retries, and is not network ping.

Set these GitHub Actions secrets, never commit their contents:

* `MONITOR_PROFILE_JSON`: JSON mapping `de`, `pl`, and `nl` to the respective complete Xray VLESS outbounds for a dedicated monitoring account. Rotate it if the account or endpoints change.
* `MONITOR_SUBSCRIPTION_URL`: HTTPS monitoring-account Xray subscription URL ending in `/xray/` on an allowed service domain.
* `MONITOR_SMTP_USERNAME`, `MONITOR_SMTP_PASSWORD`: credentials for `smtp.spaceweb.ru:465` using SMTP over TLS.
* `MONITOR_ALERT_TO`: one recipient email address.

Only status strings are cached. SMTP email is sent when the result changes, including recovery; the first healthy result establishes a baseline, and the first unhealthy result alerts. A failed delivery does not update the saved state, so it can be retried. To test delivery, manually run the workflow with `test_email` enabled; the message is marked as a test and does not change the prior state. The diagnostic probes still run. SMTP errors and missing SMTP credentials make the run fail. Neither credentials nor subscription data are printed or cached.

GitHub scheduled jobs can be delayed or disabled after repository inactivity. One runner does not determine which Russian ISP is affected, and one cannot detect failure of the scheduler itself. Use an independent heartbeat/external monitor for those cases. GitHub email delivery settings are separate from these SMTP alerts. The cached status may be evicted, which resets the baseline. Changes to exit IPs, subscription routes, or supported protocols require updating this monitor deliberately.
