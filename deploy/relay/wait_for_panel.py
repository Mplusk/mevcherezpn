"""Wait for the verified panel release before probing dependent relays."""

import json
import os
import re
import subprocess
import time


def wait_for_panel(revision, fetch_runs, *, sleep=time.sleep, attempts=90, interval=10):
    for attempt in range(attempts):
        try:
            runs = fetch_runs()
        except (OSError, subprocess.TimeoutExpired, ValueError):
            runs = []
        matching = [
            run for run in runs
            if run.get("head_sha") == revision and run.get("event") == "workflow_run"
            and isinstance(run.get("id"), int)
        ]
        if matching:
            latest = max(matching, key=lambda run: run["id"])
            if latest.get("status") == "completed":
                if latest.get("conclusion") == "success":
                    return
                raise RuntimeError("panel deployment did not succeed")
        if attempt + 1 < attempts:
            sleep(interval)
    raise RuntimeError("panel deployment did not finish before the deadline")


def main():
    revision = os.environ["DEPLOY_SHA"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("invalid release revision")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    endpoint = (
        f"repos/{repository}/actions/workflows/deploy.yml/runs"
        f"?head_sha={revision}&event=workflow_run&per_page=30"
    )

    def fetch_runs():
        result = subprocess.run(
            ["gh", "api", endpoint], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        if result.returncode:
            raise ValueError("could not read panel deployment status")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or not isinstance(response.get("workflow_runs"), list):
            raise ValueError("invalid panel deployment response")
        return [run for run in response["workflow_runs"] if isinstance(run, dict)]

    wait_for_panel(revision, fetch_runs)
    print("Panel deployment completed successfully")


if __name__ == "__main__":
    main()
