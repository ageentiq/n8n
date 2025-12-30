#!/usr/bin/env python3
"""
Track WhatsApp message status by scanning n8n workflow executions.

Usage:
    # List mode: show recent messages (10 most recent by default)
    python track_message_status.py [--limit 200] [--max-messages 10]
    
    # List mode with wa_id filter: show messages for specific user
    python track_message_status.py <wa_id> [--max-messages 10]
    
    # Track mode: track specific message
    python track_message_status.py <wa_id> <message_id> [--limit 200] [--since 1767000000]

Examples:
    # Show 10 most recent messages
    python track_message_status.py --max-messages 10
    
    # Show recent messages for specific user
    python track_message_status.py 966532127070 --max-messages 20
    
    # Track specific message
    python track_message_status.py 966532127070 wamid.HBgMOTY2NTMyMTI3MDcwFQIAERgSMkFCQzEyMzQ1Njc4OTBERUYw
"""

import os
import sys
import json
import argparse
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
import requests

try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import BulkWriteError
    MONGODB_AVAILABLE = True
except ImportError:
    MONGODB_AVAILABLE = False

# Timezone configuration - Asia/Riyadh (UTC+3)
try:
    from zoneinfo import ZoneInfo
    RIYADH_TZ = ZoneInfo("Asia/Riyadh")
except ImportError:
    # Fallback for Python < 3.9 or missing tzdata
    RIYADH_TZ = timezone(timedelta(hours=3))  # UTC+3


# ============================================================================
# Environment & Configuration Helpers
# ============================================================================

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


# ============================================================================
# HTTP Helpers
# ============================================================================

def get_auth_headers(api_key: Optional[str], basic_user: Optional[str], basic_pass: Optional[str]) -> Dict[str, str]:
    """
    Build authentication headers.
    Prefer API key, fallback to basic auth if provided.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    
    if api_key:
        headers["X-N8N-API-KEY"] = api_key
    elif basic_user and basic_pass:
        import base64
        credentials = f"{basic_user}:{basic_pass}"
        encoded = base64.b64encode(credentials.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    else:
        raise SystemExit("Authentication required: set N8N_API_KEY or N8N_BASIC_USER + N8N_BASIC_PASS")
    
    return headers


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    headers: Dict[str, str],
    timeout: int,
    json_body: Any = None,
    max_retries: int = 5,
) -> requests.Response:
    """Make HTTP request with exponential backoff retry on transient errors."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(method, url, headers=headers, json=json_body, timeout=timeout)
            # Retry on transient problems
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        except Exception as e:
            if attempt == max_retries:
                raise
            backoff = (2 ** (attempt - 1)) * 0.6 + random.random() * 0.3
            print(f"[retry] {method} {url} attempt {attempt}/{max_retries} failed: {e}. sleep {backoff:.2f}s", file=sys.stderr)
            time.sleep(backoff)
    raise RuntimeError("unreachable")


# ============================================================================
# MongoDB Helpers
# ============================================================================

def get_mongodb_client(uri: str) -> Optional[MongoClient]:
    """Create MongoDB client."""
    if not MONGODB_AVAILABLE:
        print("[warn] pymongo not installed, MongoDB features disabled", file=sys.stderr)
        return None
    
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        # Test connection
        client.admin.command('ping')
        return client
    except Exception as e:
        print(f"[error] MongoDB connection failed: {e}", file=sys.stderr)
        return None


def deduplicate_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove duplicate status entries from history.
    Duplicates are identified by status, timestamp, and executionId.
    """
    seen = set()
    deduplicated = []
    
    for item in history:
        # Create unique key
        unique_key = (
            item.get("status"),
            item.get("timestamp"),
            item.get("executionId"),
        )
        
        if unique_key not in seen:
            seen.add(unique_key)
            deduplicated.append(item)
    
    return deduplicated


def save_messages_to_mongodb(
    messages: List[Dict[str, Any]],
    workflow_id: str,
    mongo_uri: str,
    db_name: str,
    collection_name: str,
) -> int:
    """
    Save or update messages in MongoDB.
    
    Uses upsert to:
    - Insert new messages
    - Update existing messages if status changed
    - Preserve full status history
    
    Returns: number of messages processed
    """
    client = get_mongodb_client(mongo_uri)
    if not client:
        return 0
    
    try:
        db = client[db_name]
        collection = db[collection_name]
        
        # Ensure indexes exist
        collection.create_index("messageId", unique=True)
        collection.create_index("conversation_id")
        collection.create_index([("conversation_id", 1), ("latestTimestamp", -1)])
        collection.create_index("latestStatus")
        
        now = datetime.now(RIYADH_TZ)
        updates = []
        
        for msg in messages:
            message_id = msg.get("messageId")
            if not message_id:
                continue
            
            # Check if message exists
            existing = collection.find_one({"messageId": message_id})
            
            if existing:
                # Update only if status changed or timestamp is newer
                existing_ts = existing.get("latestTimestamp", 0)
                new_ts = msg.get("latestTimestamp", 0)
                
                if new_ts > existing_ts or msg.get("latestStatus") != existing.get("latestStatus"):
                    # Status changed - update
                    update_doc = {
                        "$set": {
                            "conversation_id": msg.get("waId"),
                            "latestStatus": msg.get("latestStatus"),
                            "latestTimestamp": msg.get("latestTimestamp"),
                            "latestTimestampFormatted": msg.get("latestTimestampFormatted"),
                            "statusCount": msg.get("statusCount", 1),
                            "workflowId": workflow_id,
                            "lastUpdatedAt": now,
                            "lastScannedAt": now,
                        },
                        "$setOnInsert": {
                            "firstSeenAt": existing.get("firstSeenAt", now),
                        }
                    }
                    
                    # Add to history if we have detailed history (deduplicate first)
                    deduplicated_history = []
                    if msg.get("history"):
                        deduplicated_history = deduplicate_history(msg["history"])
                        update_doc["$set"]["statusHistory"] = deduplicated_history
                        update_doc["$set"]["statusCount"] = len(deduplicated_history)
                    
                    updates.append(UpdateOne(
                        {"messageId": message_id},
                        update_doc,
                        upsert=True
                    ))
                else:
                    # No change, just update scan time
                    updates.append(UpdateOne(
                        {"messageId": message_id},
                        {"$set": {"lastScannedAt": now}},
                        upsert=False
                    ))
            else:
                # New message - insert
                doc = {
                    "messageId": message_id,
                    "conversation_id": msg.get("waId"),
                    "latestStatus": msg.get("latestStatus"),
                    "latestTimestamp": msg.get("latestTimestamp"),
                    "latestTimestampFormatted": msg.get("latestTimestampFormatted"),
                    "statusCount": msg.get("statusCount", 1),
                    "workflowId": workflow_id,
                    "firstSeenAt": now,
                    "lastUpdatedAt": now,
                    "lastScannedAt": now,
                }
                
                # Add full history if available (deduplicate first)
                deduplicated_history = []
                if msg.get("history"):
                    deduplicated_history = deduplicate_history(msg["history"])
                    doc["statusHistory"] = deduplicated_history
                    doc["statusCount"] = len(deduplicated_history)
                
                updates.append(UpdateOne(
                    {"messageId": message_id},
                    {"$setOnInsert": doc},
                    upsert=True
                ))
        
        if updates:
            result = collection.bulk_write(updates, ordered=False)
            print(f"[mongodb] Processed {len(updates)} messages: "
                  f"{result.upserted_count} inserted, {result.modified_count} updated", 
                  file=sys.stderr)
            return len(updates)
        
        return 0
        
    except BulkWriteError as e:
        print(f"[mongodb] Bulk write error: {e.details}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"[mongodb] Error saving to MongoDB: {e}", file=sys.stderr)
        return 0
    finally:
        client.close()



# ============================================================================
# n8n API Functions
# ============================================================================

def fetch_executions(
    session: requests.Session,
    base_url: str,
    api_prefix: str,
    headers: Dict[str, str],
    workflow_id: str,
    limit: int,
    timeout: int,
) -> List[Dict[str, Any]]:
    """
    Fetch executions for a workflow from n8n API with pagination support.
    Returns list of execution objects (most recent first).
    
    Uses: GET /executions?workflowId=...&limit=...&includeData=true
    """
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    fetched = 0

    while fetched < limit:
        batch_limit = min(100, limit - fetched)  # n8n often caps at 100 per request
        
        params = {
            "workflowId": workflow_id,
            "limit": str(batch_limit),
            "includeData": "true",  # CRITICAL: we need execution data to scan
        }
        
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
        fetched += len(batch)

        cursor = data.get("nextCursor") or None
        if not cursor or not batch:
            break

    return out[:limit]  # Ensure we don't exceed requested limit


# ============================================================================
# WhatsApp Status Extraction
# ============================================================================

def normalize_timestamp(ts: Any) -> Optional[int]:
    """Convert timestamp to int, handle strings and None."""
    if ts is None:
        return None
    if isinstance(ts, int):
        return ts
    if isinstance(ts, str):
        try:
            return int(ts)
        except ValueError:
            return None
    return None


def format_timestamp(ts: Optional[int]) -> Optional[str]:
    """
    Convert Unix timestamp to human-readable format in Asia/Riyadh timezone.
    Returns: "2025-12-30 15:01" or None
    """
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=RIYADH_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return None



def extract_status_updates(
    execution: Dict[str, Any],
    message_id: str,
    wa_id: str,
) -> List[Dict[str, Any]]:
    """
    Extract all status updates matching message_id from an execution.
    
    Returns list of:
    {
        "status": str,
        "timestamp": int or None,
        "recipient_id": str or None,
        "executionId": str,
        "recipientMismatch": bool
    }
    """
    results = []
    execution_id = str(execution.get("id", "unknown"))
    
    # Execution data can be in different places depending on n8n version
    # Try: execution.data, execution.executionData, or root level
    exec_data = execution.get("data") or execution.get("executionData") or {}
    
    # The webhook payload is typically in the trigger node's output
    # or in the execution's resultData
    result_data = exec_data.get("resultData") or {}
    run_data = result_data.get("runData") or {}
    
    # Scan all nodes for status webhooks
    for node_name, node_runs in run_data.items():
        if not isinstance(node_runs, list):
            continue
            
        for run in node_runs:
            if not isinstance(run, dict):
                continue
                
            # Check in data.main (typical location for node output)
            main_data = run.get("data", {}).get("main") or []
            
            for main_item in main_data:
                if not isinstance(main_item, list):
                    continue
                    
                for item in main_item:
                    if not isinstance(item, dict):
                        continue
                    
                    # Look for webhook body structure
                    body = item.get("json", {}).get("body") or item.get("json", {})
                    
                    # Handle both direct body and body[0] format
                    if isinstance(body, list) and len(body) > 0:
                        body = body[0]
                    
                    # Extract statuses array
                    statuses = body.get("statuses") or []
                    if not isinstance(statuses, list):
                        continue
                    
                    for status_obj in statuses:
                        if not isinstance(status_obj, dict):
                            continue
                        
                        # Match message_id
                        status_msg_id = status_obj.get("id", "")
                        if status_msg_id != message_id:
                            continue
                        
                        # Extract fields
                        status = status_obj.get("status", "unknown")
                        timestamp = normalize_timestamp(status_obj.get("timestamp"))
                        recipient_id = status_obj.get("recipient_id")
                        
                        # Check for wa_id mismatch
                        recipient_mismatch = False
                        if recipient_id and recipient_id != wa_id:
                            recipient_mismatch = True
                        
                        results.append({
                            "status": status,
                            "timestamp": timestamp,
                            "timestampFormatted": format_timestamp(timestamp),
                            "recipient_id": recipient_id,
                            "executionId": execution_id,
                            "recipientMismatch": recipient_mismatch,
                        })
    
    return results


def get_status_priority(status: str) -> int:
    """
    Return priority for status ordering (higher = more important).
    Terminal states get highest priority.
    """
    priority_map = {
        "failed": 100,
        "undelivered": 100,
        "read": 50,
        "delivered": 40,
        "sent": 30,
        "unknown": 10,
    }
    return priority_map.get(status.lower(), 10)


def is_terminal_status(status: str) -> bool:
    """Check if status is terminal (won't change further)."""
    return status.lower() in ("failed", "undelivered", "read")


def determine_latest_status(history: List[Dict[str, Any]]) -> Tuple[str, Optional[int]]:
    """
    Determine latest status from history based on:
    1. Primary: timestamp (newest first)
    2. Fallback: priority (terminal > read > delivered > sent)
    
    Returns: (status, timestamp)
    """
    if not history:
        return "not_found", None
    
    # Check for wa_id mismatch
    if all(h.get("recipientMismatch") for h in history):
        return "wa_id_mismatch", history[0].get("timestamp")
    
    # Filter out mismatches for status determination
    valid_history = [h for h in history if not h.get("recipientMismatch")]
    if not valid_history:
        return "wa_id_mismatch", history[0].get("timestamp")
    
    # Sort by timestamp (descending), then by priority
    sorted_history = sorted(
        valid_history,
        key=lambda x: (
            x.get("timestamp") or 0,  # timestamp descending (0 if None goes to bottom)
            get_status_priority(x.get("status", "unknown"))
        ),
        reverse=True
    )
    
    latest = sorted_history[0]
    return latest.get("status", "unknown"), latest.get("timestamp")


# ============================================================================
# Main Logic
# ============================================================================

def list_recent_messages(
    wa_id: Optional[str],
    workflow_id: str,
    base_url: str,
    api_prefix: str,
    headers: Dict[str, str],
    limit: int = 200,
    max_messages: int = 10,
    since: Optional[int] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    List recent messages with their statuses.
    
    Filters:
    - wa_id: if provided, only show messages for this user
    - since: if provided, only show messages with timestamps >= since
    
    Returns list of messages with their latest statuses.
    """
    session = requests.Session()
    
    print(f"[info] Fetching up to {limit} executions for workflow {workflow_id}...", file=sys.stderr)
    executions = fetch_executions(
        session=session,
        base_url=base_url,
        api_prefix=api_prefix,
        headers=headers,
        workflow_id=workflow_id,
        limit=limit,
        timeout=timeout,
    )
    print(f"[info] Retrieved {len(executions)} executions", file=sys.stderr)
    
    # Collect all status updates from all executions
    all_statuses = []
    
    for execution in executions:
        try:
            execution_id = str(execution.get("id", "unknown"))
            exec_data = execution.get("data") or execution.get("executionData") or {}
            result_data = exec_data.get("resultData") or {}
            run_data = result_data.get("runData") or {}
            
            # Scan all nodes for status webhooks
            for node_name, node_runs in run_data.items():
                if not isinstance(node_runs, list):
                    continue
                    
                for run in node_runs:
                    if not isinstance(run, dict):
                        continue
                        
                    main_data = run.get("data", {}).get("main") or []
                    
                    for main_item in main_data:
                        if not isinstance(main_item, list):
                            continue
                            
                        for item in main_item:
                            if not isinstance(item, dict):
                                continue
                            
                            body = item.get("json", {}).get("body") or item.get("json", {})
                            
                            if isinstance(body, list) and len(body) > 0:
                                body = body[0]
                            
                            statuses = body.get("statuses") or []
                            if not isinstance(statuses, list):
                                continue
                            
                            for status_obj in statuses:
                                if not isinstance(status_obj, dict):
                                    continue
                                
                                msg_id = status_obj.get("id", "")
                                if not msg_id:
                                    continue
                                
                                status = status_obj.get("status", "unknown")
                                timestamp = normalize_timestamp(status_obj.get("timestamp"))
                                recipient_id = status_obj.get("recipient_id")
                                
                                # Apply filters
                                if since and (timestamp or 0) < since:
                                    continue
                                
                                if wa_id and recipient_id and recipient_id != wa_id:
                                    continue
                                
                                all_statuses.append({
                                    "messageId": msg_id,
                                    "waId": recipient_id,
                                    "status": status,
                                    "timestamp": timestamp,
                                    "timestampFormatted": format_timestamp(timestamp),
                                    "executionId": execution_id,
                                })
        except Exception as e:
            print(f"[warn] Error processing execution {execution.get('id')}: {e}", file=sys.stderr)
            continue
    
    # Group by message_id and find latest status for each
    messages_map: Dict[str, List[Dict[str, Any]]] = {}
    for status in all_statuses:
        msg_id = status["messageId"]
        if msg_id not in messages_map:
            messages_map[msg_id] = []
        messages_map[msg_id].append(status)
    
    # Determine latest status for each message
    messages = []
    for msg_id, statuses in messages_map.items():
        # Sort by timestamp descending, then by priority
        sorted_statuses = sorted(
            statuses,
            key=lambda x: (
                x.get("timestamp") or 0,
                get_status_priority(x.get("status", "unknown"))
            ),
            reverse=True
        )
        
        latest = sorted_statuses[0]
        messages.append({
            "messageId": msg_id,
            "waId": latest.get("waId"),
            "latestStatus": latest.get("status"),
            "latestTimestamp": latest.get("timestamp"),
            "latestTimestampFormatted": latest.get("timestampFormatted"),
            "statusCount": len(statuses),
            "history": sorted_statuses,  # Include full history for MongoDB
        })
    
    # Sort by timestamp (newest first) and limit
    messages.sort(key=lambda x: x.get("latestTimestamp") or 0, reverse=True)
    messages = messages[:max_messages]
    
    print(f"[info] Found {len(messages_map)} unique message(s), showing top {len(messages)}", file=sys.stderr)
    
    return {
        "workflowId": workflow_id,
        "filterWaId": wa_id,
        "totalMessages": len(messages_map),
        "messages": messages,
    }


def track_message_status(
    wa_id: str,
    message_id: str,
    workflow_id: str,
    base_url: str,
    api_prefix: str,
    headers: Dict[str, str],
    limit: int = 200,
    since: Optional[int] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Track message status by scanning n8n executions.
    
    Returns result dict with structure defined in requirements.
    """
    session = requests.Session()
    
    # Fetch executions
    print(f"[info] Fetching up to {limit} executions for workflow {workflow_id}...", file=sys.stderr)
    executions = fetch_executions(
        session=session,
        base_url=base_url,
        api_prefix=api_prefix,
        headers=headers,
        workflow_id=workflow_id,
        limit=limit,
        timeout=timeout,
    )
    print(f"[info] Retrieved {len(executions)} executions", file=sys.stderr)
    
    # Scan executions for status updates
    all_statuses = []
    found_terminal = False
    
    for execution in executions:
        try:
            statuses = extract_status_updates(execution, message_id, wa_id)
            
            # Apply since filter
            if since:
                statuses = [s for s in statuses if (s.get("timestamp") or 0) >= since]
            
            if statuses:
                all_statuses.extend(statuses)
                print(f"[info] Found {len(statuses)} status update(s) in execution {execution.get('id')}", file=sys.stderr)
                
                # Check for terminal status
                for s in statuses:
                    if is_terminal_status(s.get("status", "")):
                        found_terminal = True
                
                # Early exit if we found terminal status
                if found_terminal:
                    print(f"[info] Found terminal status, stopping scan", file=sys.stderr)
                    break
        except Exception as e:
            # Gracefully handle malformed executions
            print(f"[warn] Error processing execution {execution.get('id')}: {e}", file=sys.stderr)
            continue
    
    # Deduplicate statuses (same status can appear multiple times in execution data)
    seen = set()
    deduplicated_statuses = []
    
    for status in all_statuses:
        # Create unique key from all fields
        unique_key = (
            status.get("status"),
            status.get("timestamp"),
            status.get("recipient_id"),
            status.get("executionId"),
        )
        
        if unique_key not in seen:
            seen.add(unique_key)
            deduplicated_statuses.append(status)
    
    print(f"[info] Deduplicated {len(all_statuses) - len(deduplicated_statuses)} duplicate status entries", file=sys.stderr)
    all_statuses = deduplicated_statuses
    
    # Sort history by timestamp descending
    all_statuses.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
    
    # Determine latest status
    latest_status, latest_timestamp = determine_latest_status(all_statuses)
    
    # Build result
    result = {
        "waId": wa_id,
        "messageId": message_id,
        "workflowId": workflow_id,
        "latestStatus": latest_status,
        "latestTimestamp": latest_timestamp,
        "latestTimestampFormatted": format_timestamp(latest_timestamp),
        "history": all_statuses,
        "foundInExecutionsCount": len(all_statuses),
    }
    
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Track WhatsApp message status from n8n workflow executions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("wa_id", nargs="?", help="WhatsApp user ID (e.g., 966532127070) - optional")
    parser.add_argument("message_id", nargs="?", help="WhatsApp message ID to track (e.g., wamid...) - optional")
    parser.add_argument("--limit", type=int, default=200, help="Max executions to scan (default: 200)")
    parser.add_argument("--since", type=int, help="Only consider statuses with timestamp >= this Unix timestamp")
    parser.add_argument("--max-messages", type=int, default=10, help="Max messages to return in list mode (default: 10)")
    parser.add_argument("--save-to-mongodb", action="store_true", help="Save results to MongoDB (requires env vars)")
    
    args = parser.parse_args()
    
    # Load environment
    load_dotenv(".env")
    
    workflow_id = getenv("WORKFLOW_ID_WHATSAPP")
    base_url = getenv("N8N_BASE_URL")
    api_prefix = os.getenv("N8N_API_PREFIX", "/api/v1")
    
    # Authentication
    api_key = os.getenv("N8N_API_KEY")
    basic_user = os.getenv("N8N_BASIC_USER")
    basic_pass = os.getenv("N8N_BASIC_PASS")
    
    # MongoDB configuration (optional)
    mongo_uri = os.getenv("MONGODB_URI")
    mongo_db = os.getenv("MONGODB_DATABASE", "n8n_whatsapp")
    mongo_collection = os.getenv("MONGODB_STATUS_COLLECTION", "message_statuses")
    
    headers = get_auth_headers(api_key, basic_user, basic_pass)
    
    # Determine mode: list or track
    if not args.message_id:
        # List mode: show recent messages
        result = list_recent_messages(
            wa_id=args.wa_id,
            workflow_id=workflow_id,
            base_url=base_url,
            api_prefix=api_prefix,
            headers=headers,
            limit=args.limit,
            max_messages=args.max_messages,
            since=args.since,
        )
        
        # Save to MongoDB if requested
        if args.save_to_mongodb and mongo_uri:
            messages_to_save = result.get("messages", [])
            if messages_to_save:
                save_messages_to_mongodb(
                    messages=messages_to_save,
                    workflow_id=workflow_id,
                    mongo_uri=mongo_uri,
                    db_name=mongo_db,
                    collection_name=mongo_collection,
                )
    else:
        # Track mode: track specific message
        if not args.wa_id:
            print("[error] wa_id is required when tracking a specific message_id", file=sys.stderr)
            sys.exit(1)
        
        result = track_message_status(
            wa_id=args.wa_id,
            message_id=args.message_id,
            workflow_id=workflow_id,
            base_url=base_url,
            api_prefix=api_prefix,
            headers=headers,
            limit=args.limit,
            since=args.since,
        )
        
        # Save to MongoDB if requested (single message)
        if args.save_to_mongodb and mongo_uri:
            message_data = {
                "messageId": result.get("messageId"),
                "waId": result.get("waId"),
                "latestStatus": result.get("latestStatus"),
                "latestTimestamp": result.get("latestTimestamp"),
                "latestTimestampFormatted": result.get("latestTimestampFormatted"),
                "statusCount": result.get("foundInExecutionsCount"),
                "history": result.get("history", []),
            }
            save_messages_to_mongodb(
                messages=[message_data],
                workflow_id=workflow_id,
                mongo_uri=mongo_uri,
                db_name=mongo_db,
                collection_name=mongo_collection,
            )
    
    # Output JSON (machine-readable only to stdout)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
