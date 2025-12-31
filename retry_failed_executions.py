#!/usr/bin/env python3
import os
import time
import random
from typing import Any, Dict, List, Optional

import requests


def load_dotenv(path: str = ".env") -> None:
    """
    Minimal .env loader (no extra dependency).
    Supports: KEY=VALUE, quoted values, ignores comments and blank lines.
    Does NOT override already-set environment variables.
    """
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def getenv(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or v == "":
        raise SystemExit(f"Missing env var: {name}")
    return v


def getenv_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if not v else int(v)


def getenv_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def n8n_headers(api_key: str) -> Dict[str, str]:
    return {
        "X-N8N-API-KEY": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    headers: Dict[str, str],
    timeout: int,
    json_body: Any = None,
    max_retries: int = 5,
) -> requests.Response:
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(method, url, headers=headers, json=json_body, timeout=timeout)
            # retry on transient problems
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        except Exception as e:
            if attempt == max_retries:
                raise
            backoff = (2 ** (attempt - 1)) * 0.6 + random.random() * 0.3
            print(f"[retry] {method} {url} attempt {attempt}/{max_retries} failed: {e}. sleep {backoff:.2f}s")
            time.sleep(backoff)
    raise RuntimeError("unreachable")


def list_failed_executions(
    session: requests.Session,
    base_url: str,
    api_prefix: str,
    headers: Dict[str, str],
    workflow_id: str,
    limit: int,
    timeout: int,
) -> List[Dict[str, Any]]:
    """
    Uses: GET /executions?status=error&workflowId=...&limit=...
    n8n API supports cursor pagination (nextCursor) in many setups.
    """
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while True:
        params = {
            "status": "error",
            "workflowId": workflow_id,
            "limit": str(limit),
            "includeData": "false",
        }
        # Build query string
        q = "&".join([f"{k}={requests.utils.quote(v)}" for k, v in params.items()])
        if cursor:
            q += f"&cursor={requests.utils.quote(cursor)}"

        url = f"{base_url.rstrip('/')}{api_prefix}/executions?{q}"
        resp = request_with_retry(session, "GET", url, headers=headers, timeout=timeout)

        if resp.status_code != 200:
            raise RuntimeError(f"List executions failed ({workflow_id}): {resp.status_code} {resp.text}")

        data = resp.json()
        batch = data.get("data") or data.get("executions") or []
        out.extend(batch)

        cursor = data.get("nextCursor") or None
        if not cursor or not batch:
            break

    return out


def retry_execution(
    session: requests.Session,
    base_url: str,
    api_prefix: str,
    headers: Dict[str, str],
    execution_id: str,
    load_workflow: bool,
    timeout: int,
) -> bool:
    """
    Official retry endpoint:
      POST /api/v1/executions/{id}/retry
    We try with JSON body {loadWorkflow: true|false}. If your instance ignores it,
    it should still work (we fallback to empty body).
    """
    url = f"{base_url.rstrip('/')}{api_prefix}/executions/{execution_id}/retry"

    # Try with body first (commonly used by auto-retry templates)
    body = {"loadWorkflow": load_workflow}
    resp = request_with_retry(session, "POST", url, headers=headers, json_body=body, timeout=timeout)

    # If some instances reject body format, fallback once with empty JSON
    if resp.status_code in (400, 404, 422):
        resp2 = request_with_retry(session, "POST", url, headers=headers, json_body={}, timeout=timeout)
        return 200 <= resp2.status_code < 300

    return 200 <= resp.status_code < 300


def run_for_workflow(
    session: requests.Session,
    base_url: str,
    api_prefix: str,
    headers: Dict[str, str],
    workflow_id: str,
    workflow_label: str,
    limit: int,
    max_exec: int,
    load_workflow: bool,
    timeout: int,
    sleep_ms: int,
) -> None:
    executions = list_failed_executions(
        session=session,
        base_url=base_url,
        api_prefix=api_prefix,
        headers=headers,
        workflow_id=workflow_id,
        limit=limit,
        timeout=timeout,
    )

    print(f"\n[info] {workflow_label} ({workflow_id}): found {len(executions)} failed executions")

    tried = 0
    ok = 0
    fail = 0

    for e in executions:
        if max_exec and tried >= max_exec:
            break

        exec_id = str(e.get("id") or e.get("executionId") or "")
        if not exec_id:
            continue

        tried += 1
        try:
            success = retry_execution(
                session=session,
                base_url=base_url,
                api_prefix=api_prefix,
                headers=headers,
                execution_id=exec_id,
                load_workflow=load_workflow,
                timeout=timeout,
            )
            if success:
                ok += 1
                print(f"[ok] retried execution {exec_id}")
            else:
                fail += 1
                print(f"[fail] could not retry execution {exec_id}")

        except Exception as ex:
            fail += 1
            print(f"[error] execution {exec_id}: {ex}")

        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)

    print(f"[summary] {workflow_label}: tried={tried} ok={ok} fail={fail}")


def main() -> None:
    load_dotenv(".env")

    base_url = getenv("N8N_BASE_URL")
    api_key = getenv("N8N_API_KEY")
    api_prefix = os.getenv("N8N_API_PREFIX", "/api/v1")

    # From your message (also in .env)
    webhook_workflow_id = os.getenv("WORKFLOW_ID_WEBHOOK", "9OpT6F6d3TH6CHOG")
    whatsapp_workflow_id = os.getenv("WORKFLOW_ID_WHATSAPP", "tc3qpQJxlWVVTjeq")

    limit = getenv_int("LIMIT", 100)
    max_exec = getenv_int("MAX_EXECUTIONS_PER_WORKFLOW", 0)  # 0 = unlimited
    timeout = getenv_int("REQUEST_TIMEOUT", 30)
    sleep_ms = getenv_int("SLEEP_BETWEEN_RETRIES_MS", 150)
    load_workflow = getenv_bool("LOAD_WORKFLOW", True)

    session = requests.Session()
    headers = n8n_headers(api_key)

    run_for_workflow(
        session=session,
        base_url=base_url,
        api_prefix=api_prefix,
        headers=headers,
        workflow_id=webhook_workflow_id,
        workflow_label="WEBHOOK",
        limit=limit,
        max_exec=max_exec,
        load_workflow=load_workflow,
        timeout=timeout,
        sleep_ms=sleep_ms,
    )

    run_for_workflow(
        session=session,
        base_url=base_url,
        api_prefix=api_prefix,
        headers=headers,
        workflow_id=whatsapp_workflow_id,
        workflow_label="WHATSAPP",
        limit=limit,
        max_exec=max_exec,
        load_workflow=load_workflow,
        timeout=timeout,
        sleep_ms=sleep_ms,
    )


if __name__ == "__main__":
    main()
