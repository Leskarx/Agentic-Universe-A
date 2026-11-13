from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import WorkflowField, WorkflowSchema
from .crypto import PiiCrypto
from .normalizers import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    parse_timestamp,
)

BATCH_SIZE = 5000


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _parse_field_value(field: WorkflowField, raw: str) -> str | None:
    if field.field_type == "string":
        return raw
    if field.field_type == "phone":
        return normalize_phone(raw)
    if field.field_type == "date":
        return normalize_date(raw)
    if field.field_type == "decimal":
        return normalize_decimal(raw)
    if field.field_type == "enum":
        return normalize_enum(raw, field.values)
    raise ValueError(f"Unsupported field type: {field.field_type}")


def discover_drops(target: Path) -> list[Path]:
    """Return ordered list of drop directories to ingest."""
    if not target.exists():
        raise FileNotFoundError(f"Ingest path does not exist: {target}")
    if (target / "leads").is_dir() or (target / "events").is_dir():
        return [target]
    drops = [
        p
        for p in sorted(target.iterdir(), key=lambda p: p.name)
        if p.is_dir() and ((p / "leads").is_dir() or (p / "events").is_dir())
    ]
    if not drops:
        raise ValueError(f"No drop directories (containing 'leads/' or 'events/') found under {target}")
    return drops


def ingest_path(
    conn: sqlite3.Connection,
    crypto: PiiCrypto,
    schemas: dict[str, WorkflowSchema],
    target: Path,
) -> dict[str, int]:
    """Ingest one drop or a parent folder of drops, idempotently."""
    drops = discover_drops(target)
    summary = {
        "drops_processed": len(drops),
        "lead_files_ingested": 0,
        "lead_files_skipped": 0,
        "event_files_ingested": 0,
        "event_files_skipped": 0,
    }

    known_leads: set[tuple[str, bytes]] = {
        (row[0], bytes(row[1]))
        for row in conn.execute("SELECT workflow_id, phone_hmac FROM leads")
    }

    for drop_dir in drops:
        leads_root = drop_dir / "leads"
        if leads_root.is_dir():
            for wf_dir in sorted(leads_root.iterdir(), key=lambda p: p.name):
                if not wf_dir.is_dir():
                    continue
                wf_id = wf_dir.name
                if wf_id not in schemas:
                    raise ValueError(f"Unknown workflow_id '{wf_id}' in {wf_dir}")
                schema = schemas[wf_id]
                for csv_path in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
                    rel_path = csv_path.relative_to(drop_dir).as_posix()
                    ingested = _ingest_lead_csv(
                        conn=conn,
                        crypto=crypto,
                        schema=schema,
                        csv_path=csv_path,
                        rel_path=rel_path,
                        known_leads=known_leads,
                    )
                    if ingested:
                        summary["lead_files_ingested"] += 1
                    else:
                        summary["lead_files_skipped"] += 1

        events_root = drop_dir / "events"
        if events_root.is_dir():
            for jsonl_path in sorted(events_root.glob("*.jsonl"), key=lambda p: p.name):
                rel_path = jsonl_path.relative_to(drop_dir).as_posix()
                ingested = _ingest_event_jsonl(
                    conn=conn,
                    crypto=crypto,
                    jsonl_path=jsonl_path,
                    rel_path=rel_path,
                    known_leads=known_leads,
                )
                if ingested:
                    summary["event_files_ingested"] += 1
                else:
                    summary["event_files_skipped"] += 1

    return summary


def _is_file_ingested(conn: sqlite3.Connection, rel_path: str, file_sha256: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ingested_files WHERE rel_path = ? AND file_sha256 = ?",
        (rel_path, file_sha256),
    ).fetchone()
    return row is not None


def _ingest_lead_csv(
    conn: sqlite3.Connection,
    crypto: PiiCrypto,
    schema: WorkflowSchema,
    csv_path: Path,
    rel_path: str,
    known_leads: set[tuple[str, bytes]],
) -> bool:
    file_sha256 = _sha256_file(csv_path)
    if _is_file_ingested(conn, rel_path, file_sha256):
        return False

    wf_id = schema.workflow_id
    batch_id = csv_path.stem
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows_total = 0
    rows_rejected = 0
    duplicates_merged = 0
    field_warnings = 0

    lead_upsert_sql = """
        INSERT INTO leads (
            workflow_id, phone_hmac, phone_enc,
            external_ref, batch_id, attributes_json, pii_attributes_enc,
            duplicate_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(workflow_id, phone_hmac) DO UPDATE SET
            duplicate_count = leads.duplicate_count + 1,
            external_ref = excluded.external_ref,
            attributes_json = json_patch(leads.attributes_json, excluded.attributes_json),
            pii_attributes_enc = excluded.pii_attributes_enc,
            updated_at = excluded.updated_at
    """
    rejection_sql = """
        INSERT INTO rejections (workflow_id, batch_id, row_number, reason)
        VALUES (?, ?, ?, ?)
    """

    leads_batch: list[tuple] = []
    rejections_batch: list[tuple] = []
    pending_identities: list[tuple[str, bytes]] = []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            headers = []

        field_to_col: dict[str, int] = {}
        for idx, h in enumerate(headers):
            norm_h = normalize_header(h)
            mapped = schema.header_map.get(norm_h)
            if mapped is not None and mapped not in field_to_col:
                field_to_col[mapped] = idx

        row_number = 0
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue

            row_number += 1
            rows_total += 1

            parsed: dict[str, str | None] = {}
            row_warnings = 0
            rejection_reason: str | None = None

            for field in schema.fields:
                col_idx = field_to_col.get(field.name)
                raw = row[col_idx].strip() if col_idx is not None and col_idx < len(row) else ""

                if raw == "":
                    if field.required:
                        rejection_reason = f"missing_required:{field.name}"
                        break
                    parsed[field.name] = None
                    continue

                val = _parse_field_value(field, raw)
                if val is None:
                    if field.field_type == "phone":
                        rejection_reason = "invalid_phone"
                        break
                    if field.required:
                        rejection_reason = f"invalid_{field.field_type}:{field.name}"
                        break
                    row_warnings += 1
                    parsed[field.name] = None
                else:
                    parsed[field.name] = val

            if rejection_reason is not None:
                rows_rejected += 1
                rejections_batch.append((wf_id, batch_id, row_number, rejection_reason))
                continue

            field_warnings += row_warnings
            canonical_phone = parsed["phone"]
            assert canonical_phone is not None
            p_hmac = crypto.phone_hmac(canonical_phone)
            identity = (wf_id, p_hmac)

            if identity in known_leads:
                duplicates_merged += 1
            else:
                known_leads.add(identity)
                pending_identities.append(identity)

            phone_enc = crypto.encrypt_str(canonical_phone)
            external_ref = parsed[schema.reference_field] or ""

            attrs: dict[str, str] = {}
            pii_attrs: dict[str, str] = {}
            for field in schema.fields:
                if field.name == "phone" or field.name == schema.reference_field:
                    continue
                v = parsed.get(field.name)
                if v is None:
                    continue
                if field.pii:
                    pii_attrs[field.name] = v
                else:
                    attrs[field.name] = v

            attrs_json = json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
            pii_attr_enc = crypto.encrypt_json(pii_attrs)

            leads_batch.append(
                (
                    wf_id,
                    p_hmac,
                    phone_enc,
                    external_ref,
                    batch_id,
                    attrs_json,
                    pii_attr_enc,
                    now_iso,
                    now_iso,
                )
            )

    try:
        with conn:
            for i in range(0, len(leads_batch), BATCH_SIZE):
                conn.executemany(lead_upsert_sql, leads_batch[i : i + BATCH_SIZE])
            for i in range(0, len(rejections_batch), BATCH_SIZE):
                conn.executemany(rejection_sql, rejections_batch[i : i + BATCH_SIZE])

            # Resolve pending unresolved events for newly added leads
            conn.execute(
                """
                UPDATE unresolved_events
                SET resolved_at = ?
                WHERE resolved_at IS NULL
                  AND workflow_id = ?
                  AND phone_hmac IN (
                      SELECT phone_hmac FROM leads WHERE workflow_id = ?
                  )
                """,
                (now_iso, wf_id, wf_id),
            )

            conn.execute(
                """
                INSERT INTO ingested_files (
                    rel_path, file_sha256, file_kind, workflow_id, batch_id,
                    rows_total, rows_rejected, duplicates_merged, field_warnings,
                    ingested_at
                ) VALUES (?, ?, 'leads', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rel_path,
                    file_sha256,
                    wf_id,
                    batch_id,
                    rows_total,
                    rows_rejected,
                    duplicates_merged,
                    field_warnings,
                    now_iso,
                ),
            )
    except Exception:
        for identity in pending_identities:
            known_leads.discard(identity)
        raise

    return True


def _ingest_event_jsonl(
    conn: sqlite3.Connection,
    crypto: PiiCrypto,
    jsonl_path: Path,
    rel_path: str,
    known_leads: set[tuple[str, bytes]],
) -> bool:
    file_sha256 = _sha256_file(jsonl_path)
    if _is_file_ingested(conn, rel_path, file_sha256):
        return False

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    events_total = 0
    malformed_events = 0
    duplicate_events_dropped = 0
    seen_event_ids: set[str] = set()

    # In-memory lookup of call to phone links for fast processing
    call_links: dict[tuple[str, str], tuple[bytes, bool]] = {
        (row["workflow_id"], row["call_id"]): (bytes(row["phone_hmac"]), bool(row["is_conflicted"]))
        for row in conn.execute("SELECT workflow_id, call_id, phone_hmac, is_conflicted FROM call_phone_links")
    }

    ledger_insert_sql = """
        INSERT OR IGNORE INTO interaction_ledger (
            event_id, event_type, workflow_id, call_id,
            occurred_at_utc, occurred_at_ist, occurred_date_ist,
            phone_hmac, disposition_code, payload_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    link_insert_sql = """
        INSERT INTO call_phone_links (workflow_id, call_id, phone_hmac, is_conflicted)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(workflow_id, call_id) DO UPDATE SET
            is_conflicted = excluded.is_conflicted
    """
    conflict_insert_sql = """
        INSERT INTO call_phone_conflicts (
            workflow_id, call_id, existing_phone_hmac, conflicting_phone_hmac, event_id, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """
    unresolved_insert_sql = """
        INSERT OR IGNORE INTO unresolved_events (
            event_id, workflow_id, call_id, phone_hmac, unresolved_reason, resolved_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    event_batch: list[tuple] = []
    link_batch: list[tuple] = []
    conflict_batch: list[tuple] = []
    unresolved_batch: list[tuple] = []

    def flush_batches() -> None:
        if event_batch:
            conn.executemany(ledger_insert_sql, event_batch)
            event_batch.clear()
        if link_batch:
            conn.executemany(link_insert_sql, link_batch)
            link_batch.clear()
        if conflict_batch:
            conn.executemany(conflict_insert_sql, conflict_batch)
            conflict_batch.clear()
        if unresolved_batch:
            conn.executemany(unresolved_insert_sql, unresolved_batch)
            unresolved_batch.clear()

    with conn:
        seen_event_ids.update(
            row[0] for row in conn.execute("SELECT event_id FROM interaction_ledger")
        )
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                events_total += 1
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    malformed_events += 1
                    continue

                if not isinstance(obj, dict):
                    malformed_events += 1
                    continue

                event_id = obj.get("event_id")
                event_type = obj.get("event_type")
                workflow_id = obj.get("workflow_id")
                call_id = obj.get("call_id")
                ts = parse_timestamp(obj.get("occurred_at"))

                if (
                    not isinstance(event_id, str)
                    or not event_id.strip()
                    or not isinstance(event_type, str)
                    or not event_type.strip()
                    or not isinstance(workflow_id, str)
                    or not workflow_id.strip()
                    or not isinstance(call_id, str)
                    or not call_id.strip()
                    or ts is None
                ):
                    malformed_events += 1
                    continue

                # Deduplicate before touching call links or quarantine state.
                # This set also catches repeated IDs buffered from this file.
                if event_id in seen_event_ids:
                    duplicate_events_dropped += 1
                    continue
                seen_event_ids.add(event_id)

                occurred_at_utc, occurred_at_ist, occurred_date_ist = ts

                raw_phone = obj.get("phone")
                p_hmac: bytes | None = None
                if isinstance(raw_phone, str) and raw_phone.strip():
                    norm_phone = normalize_phone(raw_phone)
                    if norm_phone is not None:
                        p_hmac = crypto.phone_hmac(norm_phone)

                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                else:
                    if "phone" in payload or "customer_name" in payload:
                        payload = {k: v for k, v in payload.items() if k not in ("phone", "customer_name")}

                disp_code: str | None = None
                if event_type in ("disposition.set", "disposition.override"):
                    raw_code = payload.get("disposition_code")
                    if isinstance(raw_code, str) and raw_code.strip():
                        disp_code = raw_code.strip()

                payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

                # Check and update call-phone links & conflict handling
                call_key = (workflow_id, call_id)
                resolved_phone_hmac = p_hmac
                is_conflicted = False

                if p_hmac is not None:
                    if call_key in call_links:
                        existing_hmac, conflicted = call_links[call_key]
                        if existing_hmac != p_hmac:
                            # Conflicting phone mapping detected!
                            is_conflicted = True
                            call_links[call_key] = (existing_hmac, True)
                            link_batch.append((workflow_id, call_id, existing_hmac, 1))
                            conflict_batch.append((workflow_id, call_id, existing_hmac, p_hmac, event_id, now_iso))
                        else:
                            is_conflicted = conflicted
                    else:
                        call_links[call_key] = (p_hmac, False)
                        link_batch.append((workflow_id, call_id, p_hmac, 0))
                else:
                    # e.g. disposition.override without phone
                    if call_key in call_links:
                        resolved_phone_hmac, is_conflicted = call_links[call_key]

                event_batch.append(
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        occurred_at_utc,
                        occurred_at_ist,
                        occurred_date_ist,
                        p_hmac,
                        disp_code,
                        payload_json,
                        now_iso,
                    )
                )

                # Determine unresolved / quarantine status
                if is_conflicted:
                    unresolved_batch.append(
                        (event_id, workflow_id, call_id, resolved_phone_hmac, "conflicting_call_phone", None, now_iso)
                    )
                elif resolved_phone_hmac is None:
                    unresolved_batch.append(
                        (event_id, workflow_id, call_id, None, "call_unlinked", None, now_iso)
                    )
                else:
                    if (workflow_id, resolved_phone_hmac) in known_leads:
                        # Lead already known! Event is resolved immediately
                        pass
                    else:
                        # Lead not uploaded yet -> hold in unresolved_events
                        unresolved_batch.append(
                            (event_id, workflow_id, call_id, resolved_phone_hmac, "lead_not_found", None, now_iso)
                        )

                if len(event_batch) >= BATCH_SIZE:
                    flush_batches()

        flush_batches()

        # Update any pending 'call_unlinked' events for calls that now have links
        conn.execute(
            """
            UPDATE unresolved_events
            SET phone_hmac = (
                SELECT cpl.phone_hmac
                FROM call_phone_links cpl
                WHERE cpl.workflow_id = unresolved_events.workflow_id
                  AND cpl.call_id = unresolved_events.call_id
                  AND cpl.is_conflicted = 0
            )
            WHERE unresolved_reason = 'call_unlinked'
              AND resolved_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM call_phone_links cpl
                  WHERE cpl.workflow_id = unresolved_events.workflow_id
                    AND cpl.call_id = unresolved_events.call_id
                    AND cpl.is_conflicted = 0
              )
            """
        )

        # Mark resolved for any pending events whose phone_hmac now matches a known lead
        conn.execute(
            """
            UPDATE unresolved_events
            SET resolved_at = ?
            WHERE resolved_at IS NULL
              AND phone_hmac IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM leads l
                  WHERE l.workflow_id = unresolved_events.workflow_id
                    AND l.phone_hmac = unresolved_events.phone_hmac
              )
            """,
            (now_iso,),
        )

        conn.execute(
            """
            INSERT INTO ingested_files (
                rel_path, file_sha256, file_kind,
                events_total, malformed_events, duplicate_events_dropped,
                ingested_at
            ) VALUES (?, ?, 'events', ?, ?, ?, ?)
            """,
            (
                rel_path,
                file_sha256,
                events_total,
                malformed_events,
                duplicate_events_dropped,
                now_iso,
            ),
        )

    return True
