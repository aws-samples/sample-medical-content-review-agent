# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run record: which stages of a review ran, and what failed without raising

The review pipeline records every tool call it makes, and the tools note the failures
that leave no error behind (a page that could not be read, a model reply with no
parsable answer). When the request ends, the record is written next to the findings as
`reviews/{session}/run_record.json`, with a verdict on whether every stage completed.
"""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath

from utils.s3 import STAGING_BUCKET, put_json, safe_session_id

RECORD_NAME = "run_record.json"

TOOL_STAGES = {
    "process_pdf": "ocr",
    "batch_content": "batch",
    "load_claims_library": "claims_library",
    "extract_claims": "extract",
    "match_claims": "match",
    "get_claims": "claims_report",
    "run_generic_review": "generic_review",
    "run_internal_review": "internal_review",
    "run_external_review": "external_review",
    "get_reviews": "aggregate",
    "publish_review": "report",
}
BATCH_STAGES = (
    "extract",
    "match",
    "generic_review",
    "internal_review",
    "external_review",
)
REVIEWER_TOOLS = ("run_generic_review", "run_internal_review", "run_external_review")
MAX_TEXT = 500

_lock = threading.Lock()
_issues: dict[str, list[dict]] = {}


def note_issue(
    session_id: str, stage: str, message: str, severity: str = "failure", **detail
) -> None:
    """Record a problem a tool survived, for the session's run record

    Parameters
    ----------
    session_id : str
        Session the review runs under
    stage : str
        Stage name, as in :data:`TOOL_STAGES`
    message : str
        What went wrong, in a sentence
    severity : str
        `failure` when the stage's output is missing or unusable, so the run does not
        count as complete; `warning` when it is degraded but usable
    **detail
        Extra fields to keep with the issue (batch, pages, ...)
    """
    print(f"[RUN] {severity} in {stage}: {message}")
    entry = {"stage": stage, "severity": severity, "message": message, **detail}
    with _lock:
        _issues.setdefault(safe_session_id(session_id), []).append(entry)


def _take_issues(session_id: str) -> list[dict]:
    """Remove and return the issues noted for a session

    Parameters
    ----------
    session_id : str
        Session the review runs under

    Returns
    -------
    list[dict]
        The issues noted with :func:`note_issue`, oldest first
    """
    with _lock:
        return _issues.pop(safe_session_id(session_id), [])


def _stem(s3_uri: object) -> str:
    """File name of an S3 URI without its extension

    Parameters
    ----------
    s3_uri : object
        S3 URI, or None

    Returns
    -------
    str
        The stem, or `""` when there is no URI
    """
    return PurePosixPath(str(s3_uri or "")).stem


def _batch_of(tool: str, tool_input: dict) -> str:
    """Name of the batch a per-batch tool call worked on

    Parameters
    ----------
    tool : str
        Tool name
    tool_input : dict
        Arguments the tool was called with

    Returns
    -------
    str
        The batch name, or `""` for tools that do not run per batch
    """
    if tool == "match_claims":
        return _stem(tool_input.get("extracted_claims_s3_uri")).replace(
            "extracted_", "", 1
        )
    if tool == "extract_claims" or tool in REVIEWER_TOOLS:
        return _stem(tool_input.get("batch_md_s3_uri"))
    return ""


def _summary(tool: str, text: str) -> dict:
    """The small fields of a tool's JSON reply: counts, totals and batch names

    Parameters
    ----------
    tool : str
        Tool name
    text : str
        The tool's reply

    Returns
    -------
    dict
        The reply's fields other than URIs, or {} when it is not a JSON object
    """
    try:
        reply = json.loads(text.split("\n\n[", 1)[0])
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(reply, dict):
        return {}
    summary = {
        k: v for k, v in reply.items() if not k.endswith("_uri") and k != "batches"
    }
    if tool == "batch_content":
        summary["batches"] = [
            {"batch": _stem(b.get("s3_uri")), "pages": b.get("pages")}
            for b in reply.get("batches") or []
        ]
    return summary


class RunRecorder:
    """Record the review's tool calls and write the session's run record"""

    def __init__(
        self,
        session_id: str,
        content_pdf_uri: str,
        claims_expected: bool,
        references_expected: bool,
        external_expected: bool,
    ):
        """Start the record for one review request

        Parameters
        ----------
        session_id : str
            Session the review runs under
        content_pdf_uri : str
            S3 URI of the content PDF under review
        claims_expected : bool
            Whether the claims stages should run
        references_expected : bool
            Whether the internal reviewer should run
        external_expected : bool
            Whether the external reviewer should run
        """
        self.session_id = session_id
        self.content_pdf_uri = content_pdf_uri
        self.claims_expected = claims_expected
        # with no references the internal reviewer has nothing to check, so skip it
        self.references_expected = references_expected
        self.external_expected = external_expected
        self.started_at = datetime.now(UTC)
        self._t0 = time.monotonic()
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def record(
        self,
        tool: str,
        tool_input: dict,
        started: float,
        text: str = "",
        error: str = "",
    ) -> None:
        """Record one finished tool call

        Parameters
        ----------
        tool : str
            Tool name, as in :data:`TOOL_STAGES`
        tool_input : dict
            Arguments the tool was called with
        started : float
            `time.monotonic()` when the call started
        text : str
            The tool's reply, when it returned one
        error : str
            The exception it raised, when it did
        """
        status = "error" if error else "success"
        now = time.monotonic()
        call = {
            "tool": tool,
            "stage": TOOL_STAGES[tool],
            "batch": _batch_of(tool, tool_input),
            "status": status,
            "seconds": round(now - started, 1),
            "started": round(started - self._t0, 1),
            "ended": round(now - self._t0, 1),
        }
        if tool == "process_pdf":
            call["document"] = str(tool_input.get("s3_uri") or "")
        if status == "success":
            call["summary"] = _summary(tool, text)
        else:
            call["error"] = (error or text)[:MAX_TEXT]
        with self._lock:
            self.calls.append(call)

    def _succeeded(self, stage: str) -> list[dict]:
        """The successful calls recorded for a stage

        Parameters
        ----------
        stage : str
            Stage name, as in :data:`TOOL_STAGES`

        Returns
        -------
        list[dict]
            The calls, in the order they were recorded
        """
        return [
            c for c in self.calls if c["stage"] == stage and c["status"] == "success"
        ]

    def _stages(self, issues: list[dict]) -> dict:
        """Decide, for every stage the request should run, whether it completed

        Parameters
        ----------
        issues : list[dict]
            The issues noted for the session

        Returns
        -------
        dict
            Per stage, `ok` plus any missing batches and failure count
        """
        batch_calls = self._succeeded("batch")
        batches = (
            [b["batch"] for b in (batch_calls[-1]["summary"].get("batches") or [])]
            if batch_calls
            else []
        )

        expected = ["ocr", "batch"]
        if self.claims_expected:
            expected += ["claims_library", "extract", "match", "claims_report"]
        expected.append("generic_review")
        if self.references_expected:
            expected.append("internal_review")
        if self.external_expected:
            expected.append("external_review")
        expected += ["aggregate", "report"]

        stages: dict[str, dict] = {}
        for stage in expected:
            entry: dict = {}
            if stage == "ocr":
                done = [
                    c
                    for c in self._succeeded("ocr")
                    if not self.content_pdf_uri
                    or c.get("document") == self.content_pdf_uri
                ]
                entry["ok"] = bool(done)
            elif stage in BATCH_STAGES:
                done = {c["batch"] for c in self._succeeded(stage)}
                missing = [b for b in batches if b not in done]
                entry["ok"] = bool(batches) and not missing
                if missing:
                    entry["missing_batches"] = missing
            else:
                entry["ok"] = bool(self._succeeded(stage))
            failures = [
                i["message"]
                for i in issues
                if i["stage"] == stage and i["severity"] == "failure"
            ]
            if failures:
                entry["ok"] = False
                entry["failures"] = len(failures)
            stages[stage] = entry
        return stages

    def finish(self, error: str | None = None) -> dict:
        """Write the run record to S3 and return it

        Parameters
        ----------
        error : str | None
            The exception that ended the request, if any

        Returns
        -------
        dict
            The run record
        """
        issues = _take_issues(self.session_id)
        with self._lock:
            stages = self._stages(issues)
            calls = list(self.calls)
        record = {
            "session_id": self.session_id,
            "content_pdf_uri": self.content_pdf_uri,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - self._t0, 1),
            "completed": error is None and all(s["ok"] for s in stages.values()),
            "error": error or "",
            "stages": stages,
            "issues": issues,
            "calls": calls,
        }
        failed = [name for name, s in stages.items() if not s["ok"]]
        print(
            f"[RUN] {self.session_id}: "
            + (
                "completed"
                if record["completed"]
                else f"incomplete ({', '.join(failed) or error})"
            )
            + f", {len(issues)} issues"
        )
        if STAGING_BUCKET:
            try:
                put_json(
                    f"reviews/{safe_session_id(self.session_id)}/{RECORD_NAME}", record
                )
            except Exception as e:  # noqa: BLE001 - the review itself is already published
                print(f"[RUN] Failed to write the run record: {e}")
        return record
