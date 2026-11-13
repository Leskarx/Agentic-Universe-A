from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .normalizers import IST, normalize_header


@dataclass(frozen=True)
class WorkflowField:
    name: str
    field_type: str
    required: bool
    pii: bool
    reference: bool
    values: tuple[str, ...]
    default: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: tuple[WorkflowField, ...]
    reference_field: str
    header_map: dict[str, str]
    defaults: dict[str, str]


@dataclass(frozen=True)
class DispositionInfo:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def load_schemas(schemas_dir: Path) -> dict[str, WorkflowSchema]:
    schemas: dict[str, WorkflowSchema] = {}
    for json_path in sorted(schemas_dir.glob("*.json")):
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        wf_id = raw["workflow_id"]
        fields: list[WorkflowField] = []
        header_map: dict[str, str] = {}
        defaults: dict[str, str] = {}
        reference_field = ""

        for f in raw["fields"]:
            wf_field = WorkflowField(
                name=f["name"],
                field_type=f["type"],
                required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)),
                reference=bool(f.get("reference", False)),
                values=tuple(v.lower() for v in f.get("values", [])),
                default=f.get("default"),
                aliases=tuple(f.get("aliases", [])),
            )
            fields.append(wf_field)
            if wf_field.reference:
                reference_field = wf_field.name
            if wf_field.default is not None:
                defaults[wf_field.name] = wf_field.default

            header_map[normalize_header(wf_field.name)] = wf_field.name
            for alias in wf_field.aliases:
                header_map[normalize_header(alias)] = wf_field.name

        if not reference_field:
            raise ValueError(f"Workflow schema {wf_id} has no field marked reference=true")

        schemas[wf_id] = WorkflowSchema(
            workflow_id=wf_id,
            description=raw.get("description", ""),
            fields=tuple(fields),
            reference_field=reference_field,
            header_map=header_map,
            defaults=defaults,
        )
    return schemas


def resolve_attributes_with_defaults(schema: WorkflowSchema, attributes_json: str) -> dict[str, str]:
    """Apply workflow schema defaults to any attributes that remain null after merging."""
    attrs: dict[str, str] = json.loads(attributes_json) if attributes_json else {}
    for k, default_val in schema.defaults.items():
        if attrs.get(k) is None:
            attrs[k] = default_val
    return attrs


def load_dispositions(schemas_dir: Path) -> dict[tuple[str, str], DispositionInfo]:
    csv_path = schemas_dir / "dispositions.csv"
    dispositions: dict[tuple[str, str], DispositionInfo] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wf_id = row["workflow_id"].strip()
            code = row["code"].strip()
            dispositions[(wf_id, code)] = DispositionInfo(
                workflow_id=wf_id,
                code=code,
                label=row["label"].strip(),
                is_terminal=row["is_terminal"].strip().lower() == "true",
                is_success=row["is_success"].strip().lower() == "true",
            )
    return dispositions
