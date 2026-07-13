#!/usr/bin/env python3
"""Build frontend/data/dashboard.json from a Pipeline Bus checkout.

This script is the read-only DashboardPayload v1 exporter described in
`DESIGN.md` (task 013). It walks the repository once, pulls what it can from
live status files, task markdown titles, analytics/stats.py --json, the roster
configuration, and recent git activity, and writes a single JSON file the
renderer can load directly.

The script never edits tasks/, status/, branches, the protocol, the poller,
or the reviewer. It only writes the path given by `--output` (default
`frontend/data/dashboard.json`).

Usage:
    python3 frontend/export-dashboard.py \
        --repo /path/to/pipeline-bus \
        --output frontend/data/dashboard.json

Exit codes:
    0 — output written
    2 — repository root not found / not a git checkout
    3 — output could not be written

Standard library only; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_OUTPUT = "frontend/data/dashboard.json"
ANALYTICS_TIMEOUT_SECONDS = 60
ACTIVITY_LIMIT = 12
GIT_LOG_LIMIT = 80

# Bus timezone convention. Every committed timestamp on this pipeline bus has
# historically been authored from a JST-located workstation and is either:
#   - ISO-8601 with explicit offset (newest rows, e.g. `+09:00` or `+00:00`)
#   - naive ISO-8601 with no offset (older rows; see status/011.json,
#     status/012.json — these were written before the offset was added)
# Per analytics/stats.py the bus interprets naive git timestamps as JST, so we
# do the same for naive `updated` values. Aware values are preserved and
# re-emitted with their original offset so downstream tooling can keep treating
# them as UTC or local — we never silently re-zone a value the operator
# explicitly annotated.
JST = timezone(timedelta(hours=9))

# Subject-line patterns for activity classification. We deliberately mirror the
# analytics regexes (`analytics/stats.py`) so the two readers tell the same
# story from commit history.
RE_CLAIM = re.compile(r"\bclaim\s+task\s+(\d{1,})")
RE_SUBMIT = re.compile(r"\btask\s+(\d{1,})\s*→\s*review\b")
RE_VERDICT = re.compile(
    r"\btask\s+(\d{1,})\s*:\s*review\s*r?(\d+)\s*→\s*"
    r"(approved|changes_requested|stuck)\b"
)
RE_MERGED = re.compile(r"\btask\s+(\d{1,})\s*→\s*merged\b")
# Recognised-but-not-promoted: a subject that only matches RE_TASK_ANY is
# NOT emitted. We never copy raw commit subjects into the dashboard — only
# whitelisted event text from the classifier above reaches the payload.

# Allowed crew `status` values. We intentionally do NOT expose `online`,
# `working`, or `reviewing` from this exporter — those would falsely advertise
# runtime liveness. See DESIGN.md → Privacy.
CREW_STATUSES = {"configured", "waiting", "absent"}


def task_key(value: Any) -> str:
    """Coerce any task identifier into the canonical zero-padded 3-digit
    string. Falls back to the trimmed string when no digits are present so
    that non-numeric ids still pass through the merge step without crashing."""
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    if match:
        return match.group(0).zfill(3)
    return text


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` until we find a `.git` entry. Returns `start`
    if nothing is found; the caller then errors loudly via `--repo` validation."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def read_json(path: Path, default: Any = None) -> Any:
    """Load JSON from `path`. Returns `default` on parse failure (after a
    warning) so callers can distinguish "valid empty object" from "could
    not parse". `default=None` lets callers explicitly check for None."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return default


def normalize_iso8601(raw: Any) -> str | None:
    """Coerce a raw timestamp into ISO 8601 with a real offset.

    Behaviour:
      - string with explicit offset  → re-emit via `astimezone()` so the
        seconds/microseconds and offset are preserved exactly as written.
        We deliberately do NOT convert a `+00:00` value into `+09:00`;
        the operator's annotation is honest and we keep it.
      - naive string                → assume JST per bus convention (see
        analytics/stats.py: `JST = timezone(timedelta(hours=9))` and
        `to_jst()`). This matches status/011.json and status/012.json.
      - everything else (None, non-string, unparseable) → None. We do NOT
        surface unparseable timestamps as opaque strings; that would let
        a malformed status file smuggle raw text into the dashboard.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.isoformat()


def collect_statuses(root: Path) -> list[dict[str, Any]]:
    """Read every `status/NNN.json`. Live status is the authoritative source
    for `state`, `round`, and `updated`; analytics fills the gaps.

    A file that fails to parse is logged and skipped — never silently
    synthesised from the filename, because a malformed entry tells us
    nothing trustworthy about the task. The `updated` value is normalised
    into offset-aware ISO 8601 here, so downstream consumers never have to
    re-disambiguate naive timestamps.
    """
    statuses: list[dict[str, Any]] = []
    status_dir = root / "status"
    if not status_dir.is_dir():
        return statuses
    for path in sorted(status_dir.glob("*.json")):
        payload = read_json(path, default=None)
        if not isinstance(payload, dict):
            # Already warned by read_json. Don't fabricate a row.
            continue
        item = dict(payload)
        item["task"] = task_key(item.get("task") or path.stem)
        item.setdefault("state", None)
        item.setdefault("round", 0)
        item.setdefault("updated", None)
        # Normalise the updated timestamp once, at the source. Naive values
        # become JST-aware (see normalize_iso8601); unparseable values
        # collapse to None instead of leaking into the renderer.
        item["updated"] = normalize_iso8601(item.get("updated"))
        statuses.append(item)
    return statuses


def title_from_markdown(content: str) -> str:
    """Pull a short title from task markdown without copying body content.

    Order of preference:
      1. The first non-heading line after a `## title` / `## 标题` block
      2. The first H1 heading, with optional `Task NNN — ` decoration stripped
      3. An empty string (the caller falls back to the filename stem)
    """
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*##\s+(title|标题)\s*", line, re.IGNORECASE):
            for candidate in lines[index + 1:]:
                stripped = candidate.strip()
                if stripped and not stripped.startswith("#"):
                    return stripped
    for line in lines:
        if line.startswith("# "):
            return re.sub(r"(?i)^Task\s+\d+\s*[—:.-]?\s*", "", line[2:].strip())
    return ""


def collect_task_specs(root: Path) -> dict[str, dict[str, str]]:
    """Extract `{task_id: {title, source}}` from `tasks/NNN-*.md`. Body content
    is intentionally discarded — DESIGN.md → Privacy."""
    specs: dict[str, dict[str, str]] = {}
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        return specs
    for path in sorted(tasks_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)
            continue
        title = title_from_markdown(content) or path.stem
        specs[task_key(path.name)] = {
            "title": title,
            "source": path.relative_to(root).as_posix(),
        }
    return specs


def run_analytics(root: Path) -> dict[str, Any]:
    """Invoke `analytics/stats.py --json` and parse the output.

    Returns the full decoded payload (with `tasks`, `summary`, and the
    top-level `include_tokens` flag) so the summary block can be passed
    through verbatim — see infer_summary() for why we no longer rebuild it
    from scratch.
    """
    stats = root / "analytics" / "stats.py"
    if not stats.exists():
        return {"tasks": [], "summary": {}, "include_tokens": False}
    command = [sys.executable, str(stats), "--json", "--repo", str(root)]
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
            timeout=ANALYTICS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"warning: analytics timed out after {ANALYTICS_TIMEOUT_SECONDS}s",
              file=sys.stderr)
        return {"tasks": [], "summary": {}, "include_tokens": False}
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"warning: analytics unavailable: {exc}", file=sys.stderr)
        return {"tasks": [], "summary": {}, "include_tokens": False}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"warning: analytics returned invalid JSON: {exc}", file=sys.stderr)
        return {"tasks": [], "summary": {}, "include_tokens": False}
    if not isinstance(payload, dict):
        return {"tasks": [], "summary": {}, "include_tokens": False}
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    include_tokens = bool(payload.get("include_tokens", False))
    return {"tasks": tasks, "summary": summary, "include_tokens": include_tokens}


def activity_text(subject: str) -> tuple[str, str] | None:
    """Whitelist-only mapping from commit subject to display text.

    Every text string that ever reaches the dashboard comes from one of the
    fixed phrases below. We deliberately do NOT echo the raw subject — a
    private subject may include task body context, review wording, or
    personally identifying detail that we promised to keep out of the
    public payload. Anything we don't recognise returns None and the
    caller drops it.
    """
    if match := RE_CLAIM.search(subject):
        return task_key(match.group(1)), "Claimed by implementer"
    if match := RE_SUBMIT.search(subject):
        return task_key(match.group(1)), "Implementation submitted for review"
    if match := RE_VERDICT.search(subject):
        state = match.group(3).replace("_", " ").upper()
        return task_key(match.group(1)), f"Review r{match.group(2)}: {state}"
    if match := RE_MERGED.search(subject):
        return task_key(match.group(1)), "Route completed · merged"
    return None


def collect_activity(root: Path) -> list[dict[str, str]]:
    """Recent git subjects, classified by a whitelist. Only the subject line
    is read; commit body and diff are never inspected. Subjects that fail
    every whitelist rule are silently dropped so private phrasing never
    reaches the payload."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--all",
                "--date=iso-strict",
                "--pretty=%aI%x1f%s",
                f"-n{ACTIVITY_LIMIT * 4}",  # over-fetch; we'll filter & cap
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"warning: git log unavailable: {exc}", file=sys.stderr)
        return []

    entries: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        try:
            raw_time, subject = line.split("\x1f", 1)
        except ValueError:
            continue
        parsed = activity_text(subject)
        if not parsed:
            # Unknown / un-classified subject: drop it. We never echo raw
            # subjects into the payload. See activity_text() for the
            # whitelist rationale.
            continue
        task, text = parsed
        timestamp = normalize_iso8601(raw_time)
        # Display in the exporter's local timezone so the operator sees a
        # familiar wall clock; the ISO time on `updated` is still offset-
        # aware and unambiguous.
        if timestamp:
            display = datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M")
        else:
            display = "--:--"
        entries.append({"time": display, "task": task, "text": text})
        if len(entries) >= ACTIVITY_LIMIT:
            break
    return entries


def initials(value: str) -> str:
    """Turn a role identifier into 2-letter initials. Pure presentation; never
    used to fake identity claims."""
    chunks = re.findall(r"[A-Za-z0-9]+", value or "")
    if not chunks:
        return "—"
    if len(chunks) == 1:
        return chunks[0][:2].upper()
    return "".join(chunk[0] for chunk in chunks[:2]).upper()


def collect_crew(root: Path) -> list[dict[str, str]]:
    """Read the configured roles from `roster.json`. Always reports
    `status: configured` so the renderer never mistakes a configured occupant
    for a runtime heartbeat."""
    roster_path = root / "roster.json"
    if not roster_path.exists():
        return []  # missing is fine — public target repos won't have one
    roster = read_json(roster_path, default={})
    if not isinstance(roster, dict):
        return []
    roles = roster.get("roles", {})
    if not isinstance(roles, dict):
        return []

    definitions = [
        ("owner", "Owner / Dispatcher"),
        ("worker", "Implementer"),
        ("reviewer", "Reviewer"),
    ]
    crew: list[dict[str, str]] = []
    for key, role_label in definitions:
        entry = roles.get(key, {})
        if not isinstance(entry, dict):
            continue
        current = str(entry.get("current") or "").strip()
        if not current:
            continue
        crew.append({
            "initials": initials(current),
            "role": role_label,
            "model": current,
            "kind": key,
            "status": "configured",
            "label": "configured",
        })
    return crew


def merge_tasks(
    statuses: list[dict[str, Any]],
    analytics_tasks: list[dict[str, Any]],
    task_specs: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Fuse live status, analytics, and task markdown into a flat `tasks[]`.

    Merge priority (highest first):
      1. `status/NNN.json` — state / round / updated
      2. `analytics/stats.py --json` — timings, flags, end-to-end
      3. `tasks/NNN-*.md` — title only
    """
    by_task: dict[str, dict[str, Any]] = {}

    for status in statuses:
        tid = task_key(status.get("task"))
        if not tid:
            continue
        merged = by_task.setdefault(tid, {"task": tid})
        if status.get("state"):
            merged["state"] = status["state"]
        if status.get("round") is not None:
            merged["round"] = int(status["round"]) if isinstance(status["round"], (int, float)) else 0
        if status.get("updated"):
            # Already normalised by collect_statuses — guaranteed offset-aware.
            merged["updated"] = status["updated"]

    for entry in analytics_tasks:
        if not isinstance(entry, dict):
            continue
        tid = task_key(entry.get("task"))
        if not tid:
            continue
        merged = by_task.setdefault(tid, {"task": tid})
        for key in (
            "queue_to_claim",
            "impl_seconds",
            "review_seconds",
            "final_seconds",
            "end_to_end",
            "end_to_end_open",
            "flags",
            "review_rounds",
        ):
            if key in entry and entry[key] is not None:
                merged[key] = entry[key]
        if not merged.get("state") and entry.get("final_state"):
            merged["state"] = entry["final_state"]
        if not merged.get("updated"):
            normalised = normalize_iso8601(entry.get("merged_at"))
            if normalised:
                merged["updated"] = normalised
        if "round" not in merged and "review_rounds" in entry and entry["review_rounds"] is not None:
            merged["round"] = int(entry["review_rounds"])

    for tid, spec in task_specs.items():
        if tid not in by_task:
            by_task[tid] = {"task": tid}
        by_task[tid]["title"] = spec["title"]

    # Final pass: ensure every task has a title and every numeric field has a
    # null fallback instead of a missing key.
    rows: list[dict[str, Any]] = []
    for tid in sorted(by_task):
        merged = by_task[tid]
        merged.setdefault("task", tid)
        merged.setdefault("title", "")
        merged.setdefault("state", None)
        merged.setdefault("round", 0)
        merged.setdefault("updated", None)
        merged.setdefault("queue_to_claim", None)
        merged.setdefault("impl_seconds", None)
        merged.setdefault("review_seconds", None)
        merged.setdefault("review_rounds", 0)
        merged.setdefault("final_seconds", None)
        merged.setdefault("end_to_end", None)
        merged.setdefault("end_to_end_open", False)
        merged.setdefault("flags", [])
        if not isinstance(merged["flags"], list):
            merged["flags"] = []
        rows.append(merged)
    return rows


def _infer_live_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute the count-based summary fields from the merged task rows.

    Analytics only knows about git archaeology, so any time live status
    disagrees with what analytics inferred (e.g. status/013.json says
    `doing` even though analytics hasn't seen the final commit yet), we
    have to recompute. Everything that doesn't depend on `state` is left
    alone and passed through from the analytics summary.
    """
    merged = [r for r in rows if str(r.get("state") or "").lower() == "merged"]
    active = [r for r in rows if str(r.get("state") or "").lower() != "merged"]
    reworked = [r for r in rows if int(r.get("round") or 0) >= 2]
    stuck = [r for r in rows if any(
        isinstance(flag, str) and flag.lower().startswith("stuck")
        for flag in (r.get("flags") or [])
    )]
    n_tasks = len(rows)
    return {
        "n_tasks": n_tasks,
        "n_merged": len(merged),
        "n_inflight": len(active),
        "n_rework": len(reworked),
        "n_stuck": len(stuck),
        "rework_rate": (len(reworked) / n_tasks) if n_tasks else 0.0,
        "stuck_rate": (len(stuck) / n_tasks) if n_tasks else 0.0,
    }


def infer_summary(
    rows: list[dict[str, Any]],
    analytics_payload: dict[str, Any],
) -> dict[str, Any]:
    """Compose the v1 summary from analytics + live state.

    The previous version rebuilt the entire summary block from scratch,
    which silently dropped analytics fields like `n_stuck`, `stuck_rate`,
    `n_rollbacks`, `n_unstucks`, and the `first_queue`/`last_queue`
    window metadata. We now:

      1. Pass analytics' `summary` block through verbatim for fields the
         live row recomputation cannot affect (window metadata, e2e
         average, throughput, the n_rollbacks / n_unstucks counters).
      2. Overwrite the count-based fields (`n_tasks`, `n_merged`,
         `n_inflight`, `n_rework`, `n_stuck`, `rework_rate`, `stuck_rate`)
         with values re-derived from the merged rows so that live status
         always wins over analytics inferences.
      3. Surface `include_tokens` from the analytics top level. When the
         source analytics ran without `--tokens`, the four `sum_*` token
         fields collapse to `null` instead of the misleading `0` that
         the previous exporter inherited from the JSON. `0` would tell
         the UI "zero tokens were used"; `null` tells it "this run did
         not measure tokens" — the right semantic for a renderer.
    """
    analytics_summary = analytics_payload.get("summary", {}) if isinstance(
        analytics_payload.get("summary"), dict) else {}
    include_tokens = bool(analytics_payload.get("include_tokens", False))

    summary: dict[str, Any] = {}
    # Pass-through first: any analytics-provided field the renderer might
    # consume is forwarded as-is.
    for key, value in analytics_summary.items():
        summary[key] = value
    # Live counts always win.
    summary.update(_infer_live_counts(rows))
    # `avg_e2e` is conceptually live-derived too — recompute from the
    # merged rows so a disagreement between live and analytics surfaces
    # immediately. Otherwise the renderer could show a stale e2e.
    e2e_values = [
        float(r["end_to_end"]) for r in rows
        if str(r.get("state") or "").lower() == "merged"
        and isinstance(r.get("end_to_end"), (int, float))
    ]
    summary["avg_e2e"] = (sum(e2e_values) / len(e2e_values)) if e2e_values else None
    summary["include_tokens"] = include_tokens

    # Token attribution: when the source run did not enable `--tokens`,
    # the analytics summary legitimately reports `0` for the sum fields
    # because nothing was summed. From the renderer's perspective that is
    # "unmeasured", not "measured at zero". Expose it as `null` so the UI
    # can render "—" / "N/A" / hide the column instead of "0".
    for token_field in ("sum_in", "sum_out", "sum_cr", "sum_sessions"):
        if not include_tokens:
            summary[token_field] = None
        else:
            # Trust analytics' value when tokens were measured; coerce to
            # a numeric type so the schema is stable for downstream
            # consumers.
            value = summary.get(token_field)
            if isinstance(value, (int, float)):
                summary[token_field] = value
            else:
                summary[token_field] = None
    return summary


def build_payload(root: Path) -> dict[str, Any]:
    statuses = collect_statuses(root)
    analytics = run_analytics(root)
    task_specs = collect_task_specs(root)
    rows = merge_tasks(
        statuses,
        analytics.get("tasks", []),
        task_specs,
    )
    summary = infer_summary(rows, analytics)
    activity = collect_activity(root)
    crew = collect_crew(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "tasks": rows,
        "summary": summary,
        "activity": activity,
        "crew": crew,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export-dashboard.py",
        description=(
            "Build the read-only DashboardPayload v1 JSON for the Pipeline Bus "
            "control room."
        ),
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(),
                        help="path to a bus checkout (default: cwd)")
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT),
                        help="output JSON path (default: frontend/data/dashboard.json)")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress progress notes on stderr")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    root = find_repo_root(args.repo)
    if not (root / ".git").exists():
        print(f"error: {root} is not a git repository", file=sys.stderr)
        return 2

    payload = build_payload(root)

    output = args.output
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp_path = output.with_suffix(output.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_path, output)
    except OSError as exc:
        print(f"error: could not write {output}: {exc}", file=sys.stderr)
        return 3

    if not args.quiet:
        print(f"wrote {output}")
        print(
            f"tasks={len(payload['tasks'])} "
            f"activity={len(payload['activity'])} "
            f"crew={len(payload['crew'])} "
            f"schema_version={payload['schema_version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())