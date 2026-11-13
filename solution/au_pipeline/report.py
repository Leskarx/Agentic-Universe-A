from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .crypto import PiiCrypto

BUILD_CALL_FACTS_SQL = """
DROP TABLE IF EXISTS temp.tmp_call_facts;
CREATE TEMP TABLE tmp_call_facts AS
WITH
initiated_ranked AS (
    SELECT
        e.workflow_id,
        e.call_id,
        l.lead_id,
        e.occurred_at_utc,
        e.occurred_at_ist,
        e.occurred_date_ist,
        ROW_NUMBER() OVER (
            PARTITION BY e.workflow_id, e.call_id
            ORDER BY e.occurred_at_utc ASC, e.seq ASC
        ) AS rn
    FROM interaction_ledger e
    LEFT JOIN call_phone_links cpl
        ON cpl.workflow_id = e.workflow_id AND cpl.call_id = e.call_id
    JOIN leads l
        ON l.workflow_id = e.workflow_id
       AND l.phone_hmac = COALESCE(e.phone_hmac, cpl.phone_hmac)
    WHERE e.event_type = 'call.initiated'
      AND COALESCE(cpl.is_conflicted, 0) = 0
      AND NOT EXISTS (
          SELECT 1 FROM unresolved_events ue
          WHERE ue.event_id = e.event_id AND ue.resolved_at IS NULL
      )
),
calls AS (
    SELECT
        workflow_id,
        call_id,
        lead_id,
        occurred_at_utc AS initiated_at_utc,
        occurred_at_ist AS initiated_at_ist,
        occurred_date_ist AS initiated_date_ist
    FROM initiated_ranked
    WHERE rn = 1
),
call_flags AS (
    SELECT
        e.workflow_id,
        e.call_id,
        MAX(CASE WHEN e.event_type = 'call.connected' THEN 1 ELSE 0 END) AS is_connected,
        MAX(CASE WHEN e.event_type = 'call.telephony_failed' THEN 1 ELSE 0 END) AS is_telephony_failed
    FROM interaction_ledger e
    WHERE e.event_type IN ('call.connected', 'call.telephony_failed')
      AND NOT EXISTS (
          SELECT 1 FROM unresolved_events ue
          WHERE ue.event_id = e.event_id AND ue.resolved_at IS NULL
      )
    GROUP BY e.workflow_id, e.call_id
),
valid_dispositions_ranked AS (
    SELECT
        e.workflow_id,
        e.call_id,
        e.disposition_code,
        d.is_terminal,
        d.is_success,
        ROW_NUMBER() OVER (
            PARTITION BY e.workflow_id, e.call_id
            ORDER BY
                CASE WHEN e.event_type = 'disposition.override' THEN 1 ELSE 0 END DESC,
                e.occurred_at_utc DESC,
                e.seq DESC
        ) AS rn
    FROM interaction_ledger e
    JOIN dispositions d
        ON d.workflow_id = e.workflow_id
       AND d.code = e.disposition_code
    WHERE e.event_type IN ('disposition.set', 'disposition.override')
      AND NOT EXISTS (
          SELECT 1 FROM unresolved_events ue
          WHERE ue.event_id = e.event_id AND ue.resolved_at IS NULL
      )
),
call_dispositions AS (
    SELECT
        workflow_id,
        call_id,
        disposition_code,
        is_terminal,
        is_success
    FROM valid_dispositions_ranked
    WHERE rn = 1
)
SELECT
    c.workflow_id,
    c.call_id,
    c.lead_id,
    c.initiated_at_utc,
    c.initiated_at_ist,
    c.initiated_date_ist,
    COALESCE(cf.is_connected, 0) AS is_connected,
    COALESCE(cf.is_telephony_failed, 0) AS is_telephony_failed,
    cd.disposition_code AS effective_disposition,
    COALESCE(cd.is_terminal, 0) AS is_terminal,
    COALESCE(cd.is_success, 0) AS is_success
FROM calls c
LEFT JOIN call_flags cf
    ON cf.workflow_id = c.workflow_id AND cf.call_id = c.call_id
LEFT JOIN call_dispositions cd
    ON cd.workflow_id = c.workflow_id AND cd.call_id = c.call_id;

CREATE INDEX IF NOT EXISTS temp.idx_tmp_cf_lead ON tmp_call_facts (lead_id, initiated_at_utc, call_id);
CREATE INDEX IF NOT EXISTS temp.idx_tmp_cf_wf_date ON tmp_call_facts (workflow_id, initiated_date_ist);
"""

BUILD_LEAD_SUMMARY_SQL = """
DROP TABLE IF EXISTS temp.tmp_lead_summary;
CREATE TEMP TABLE tmp_lead_summary AS
WITH
lead_call_agg AS (
    SELECT
        lead_id,
        COUNT(*) AS total_calls,
        SUM(is_connected) AS connected_calls,
        SUM(is_telephony_failed) AS telephony_failed_calls,
        MAX(is_terminal) AS has_terminal,
        MAX(is_success) AS ever_success
    FROM temp.tmp_call_facts
    GROUP BY lead_id
),
lead_last_call AS (
    SELECT
        lead_id,
        initiated_at_ist AS last_call_at,
        ROW_NUMBER() OVER (
            PARTITION BY lead_id
            ORDER BY initiated_at_utc DESC, call_id DESC
        ) AS rn
    FROM temp.tmp_call_facts
),
lead_curr_disp AS (
    SELECT
        lead_id,
        effective_disposition AS current_disposition,
        ROW_NUMBER() OVER (
            PARTITION BY lead_id
            ORDER BY initiated_at_utc DESC, call_id DESC
        ) AS rn
    FROM temp.tmp_call_facts
    WHERE effective_disposition IS NOT NULL
)
SELECT
    l.lead_id,
    l.workflow_id,
    l.external_ref,
    l.batch_id,
    l.phone_enc,
    CASE
        WHEN COALESCE(a.total_calls, 0) = 0 THEN 'yet_to_reach_out'
        WHEN COALESCE(a.has_terminal, 0) = 1 THEN 'completed'
        WHEN a.telephony_failed_calls = a.total_calls THEN 'in_progress_telephony_failed'
        ELSE 'in_progress_dialed'
    END AS bucket,
    COALESCE(a.total_calls, 0) AS total_calls,
    COALESCE(a.connected_calls, 0) AS connected_calls,
    COALESCE(llc.last_call_at, '') AS last_call_at,
    COALESCE(lcd.current_disposition, '') AS current_disposition,
    CASE WHEN COALESCE(a.ever_success, 0) = 1 THEN 'true' ELSE 'false' END AS ever_success
FROM leads l
LEFT JOIN lead_call_agg a ON a.lead_id = l.lead_id
LEFT JOIN lead_last_call llc ON llc.lead_id = l.lead_id AND llc.rn = 1
LEFT JOIN lead_curr_disp lcd ON lcd.lead_id = l.lead_id AND lcd.rn = 1;
"""


def generate_reports(conn: sqlite3.Connection, crypto: PiiCrypto, out_dir: Path) -> dict[str, Path]:
    """Generate all five required report files in out_dir using SQL aggregations."""
    out_dir.mkdir(parents=True, exist_ok=True)

    conn.executescript(BUILD_CALL_FACTS_SQL)
    conn.executescript(BUILD_LEAD_SUMMARY_SQL)

    paths: dict[str, Path] = {}

    # 1. batch_summary.csv
    batch_path = out_dir / "batch_summary.csv"
    with batch_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow([
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ])
        cur = conn.execute(
            """
            SELECT
                workflow_id,
                batch_id,
                COUNT(*) AS total_leads,
                SUM(CASE WHEN bucket = 'yet_to_reach_out' THEN 1 ELSE 0 END) AS yet_to_reach_out,
                SUM(CASE WHEN bucket = 'in_progress_telephony_failed' THEN 1 ELSE 0 END) AS in_progress_telephony_failed,
                SUM(CASE WHEN bucket = 'in_progress_dialed' THEN 1 ELSE 0 END) AS in_progress_dialed,
                SUM(CASE WHEN bucket = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM temp.tmp_lead_summary
            GROUP BY workflow_id, batch_id
            ORDER BY workflow_id ASC, batch_id ASC
            """
        )
        for row in cur:
            w.writerow([
                row["workflow_id"],
                row["batch_id"],
                row["total_leads"],
                row["yet_to_reach_out"],
                row["in_progress_telephony_failed"],
                row["in_progress_dialed"],
                row["completed"],
            ])
    paths["batch_summary"] = batch_path

    # 2. daily_overview.csv
    daily_path = out_dir / "daily_overview.csv"
    with daily_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow([
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ])
        cur = conn.execute(
            """
            SELECT
                workflow_id,
                initiated_date_ist AS date,
                COUNT(*) AS total_calls,
                SUM(is_connected) AS connected_calls,
                COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_id END) AS success_leads
            FROM temp.tmp_call_facts
            GROUP BY workflow_id, initiated_date_ist
            ORDER BY workflow_id ASC, date ASC
            """
        )
        for row in cur:
            total = row["total_calls"]
            conn_calls = row["connected_calls"]
            rate = f"{conn_calls / total:.4f}"
            w.writerow([
                row["workflow_id"],
                row["date"],
                total,
                conn_calls,
                rate,
                row["success_leads"],
            ])
    paths["daily_overview"] = daily_path

    # 3. lead_report.csv
    lead_path = out_dir / "lead_report.csv"
    with lead_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow([
            "workflow_id",
            "external_ref",
            "batch_id",
            "phone_masked",
            "bucket",
            "total_calls",
            "connected_calls",
            "last_call_at",
            "current_disposition",
            "ever_success",
        ])
        cur = conn.execute(
            """
            SELECT
                workflow_id,
                external_ref,
                batch_id,
                phone_enc,
                bucket,
                total_calls,
                connected_calls,
                last_call_at,
                current_disposition,
                ever_success
            FROM temp.tmp_lead_summary
            ORDER BY workflow_id ASC, external_ref ASC
            """
        )
        for row in cur:
            phone = crypto.decrypt_str(bytes(row["phone_enc"]))
            phone_masked = f"+91******{phone[-4:]}"
            w.writerow([
                row["workflow_id"],
                row["external_ref"],
                row["batch_id"],
                phone_masked,
                row["bucket"],
                row["total_calls"],
                row["connected_calls"],
                row["last_call_at"],
                row["current_disposition"],
                row["ever_success"],
            ])
    paths["lead_report"] = lead_path

    # 4. rejections.csv
    rej_path = out_dir / "rejections.csv"
    with rej_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["workflow_id", "batch_id", "row_number", "reason"])
        cur = conn.execute(
            """
            SELECT workflow_id, batch_id, row_number, reason
            FROM rejections
            ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
            """
        )
        for row in cur:
            w.writerow([row["workflow_id"], row["batch_id"], row["row_number"], row["reason"]])
    paths["rejections"] = rej_path

    # 5. data_quality.json
    lead_stats = conn.execute(
        """
        SELECT
            COALESCE(SUM(rows_total), 0) AS lead_rows_total,
            COALESCE(SUM(rows_rejected), 0) AS lead_rows_rejected,
            COALESCE(SUM(duplicates_merged), 0) AS duplicate_lead_rows_merged,
            COALESCE(SUM(field_warnings), 0) AS field_warnings
        FROM ingested_files
        WHERE file_kind = 'leads'
        """
    ).fetchone()

    event_stats = conn.execute(
        """
        SELECT
            COALESCE(SUM(events_total), 0) AS events_total,
            COALESCE(SUM(malformed_events), 0) AS malformed_events,
            COALESCE(SUM(duplicate_events_dropped), 0) AS duplicate_events_dropped
        FROM ingested_files
        WHERE file_kind = 'events'
        """
    ).fetchone()

    rej_by_reason = {
        row["reason"]: row["cnt"]
        for row in conn.execute(
            """
            SELECT reason, COUNT(*) AS cnt
            FROM rejections
            GROUP BY reason
            ORDER BY reason ASC
            """
        )
    }

    orphan_events = conn.execute(
        """
        SELECT COUNT(*)
        FROM unresolved_events
        WHERE resolved_at IS NULL
        """
    ).fetchone()[0]

    unknown_disposition_events = conn.execute(
        """
        SELECT COUNT(*)
        FROM interaction_ledger e
        LEFT JOIN call_phone_links cpl
            ON cpl.workflow_id = e.workflow_id AND cpl.call_id = e.call_id
        JOIN leads l
            ON l.workflow_id = e.workflow_id
           AND l.phone_hmac = COALESCE(e.phone_hmac, cpl.phone_hmac)
        LEFT JOIN dispositions d
            ON d.workflow_id = e.workflow_id
           AND d.code = e.disposition_code
        WHERE e.event_type IN ('disposition.set', 'disposition.override')
          AND d.code IS NULL
          AND COALESCE(cpl.is_conflicted, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM unresolved_events ue
              WHERE ue.event_id = e.event_id AND ue.resolved_at IS NULL
          )
        """
    ).fetchone()[0]

    dq = {
        "lead_rows_total": int(lead_stats["lead_rows_total"]),
        "lead_rows_rejected": int(lead_stats["lead_rows_rejected"]),
        "rejections_by_reason": rej_by_reason,
        "duplicate_lead_rows_merged": int(lead_stats["duplicate_lead_rows_merged"]),
        "field_warnings": int(lead_stats["field_warnings"]),
        "events_total": int(event_stats["events_total"]),
        "malformed_events": int(event_stats["malformed_events"]),
        "duplicate_events_dropped": int(event_stats["duplicate_events_dropped"]),
        "orphan_events": int(orphan_events),
        "unknown_disposition_events": int(unknown_disposition_events),
    }

    dq_path = out_dir / "data_quality.json"
    dq_path.write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
    paths["data_quality"] = dq_path

    return paths
