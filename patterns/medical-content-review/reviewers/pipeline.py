# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Runs a review in code, starting each step of a batch as soon as its inputs exist

Every batch goes through extract -> match -> reviewers on its own, so a slow batch holds
up no other batch's steps. Only the claims report waits for every match, and the
aggregation waits for every reviewer. The model is called for the review work itself
and, at the end, once as the editor that picks findings to merge and drop.

The pipeline streams the same tool-call events a Strands agent would, so the frontend
shows each step as it starts and finishes.
"""

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from tools.run_record import RunRecorder, note_issue

from reviewers._common import load_prompt, read_s3_json, run_inner_agent_text
from reviewers.claim_extractor import extract_claims
from reviewers.claim_matcher import match_claims
from reviewers.claims_aggregator import get_claims
from reviewers.external_reviewer import run_external_review
from reviewers.generic_reviewer import run_generic_review
from reviewers.internal_reviewer import run_internal_review
from reviewers.report import publish_review
from reviewers.review_aggregator import get_reviews
from tools import batch_content, load_claims_library, process_pdf

READY_TEXT = "Your review is ready."
# A failed step is tried once more before the review gives up on it
STEP_ATTEMPTS = 2
# Results shown in the frontend are cut to this length; the pipeline keeps them whole
MAX_UI_TEXT = 3000
# Link tags tools append to their results (`[REVIEW_URL:...]`, `[REPORT_XLSX_URL:...]`)
URL_TAG_RE = re.compile(r"\[[A-Z_]+_URL:[^\]]+\]")
DECISIONS_RE = re.compile(r"<decisions>(.*?)</decisions>", re.DOTALL)
EDITOR_PROMPT = load_prompt("editor")


@dataclass
class ReviewInputs:
    session_id: str
    content_pdf_uri: str
    content_pdf_name: str
    reference_uris: list[str]
    reference_names: list[str]
    claims_uri: str
    claims_name: str
    enabled_sources: list[str]


def _ui_text(text: str) -> str:
    if len(text) <= MAX_UI_TEXT:
        return text
    # The frontend reads its links from these tags, so they survive the cut whole
    tags = URL_TAG_RE.findall(text)
    body = URL_TAG_RE.sub("", text).rstrip()
    if len(body) > MAX_UI_TEXT:
        body = body[:MAX_UI_TEXT] + "... (truncated)"
    return "\n\n".join([body, *tags])


def _reply(text: str) -> dict:
    """The JSON part of a tool's reply, without the link tags after it"""
    return json.loads(text.split("\n\n[", 1)[0])


class _Steps:
    """Run tools in worker threads, record them, and queue their stream events"""

    def __init__(self, recorder: RunRecorder):
        self.recorder = recorder
        self.events: asyncio.Queue[dict | None] = asyncio.Queue()
        self._ids = itertools.count(1)

    async def run(self, tool, **tool_input) -> str:
        name = tool.tool_name
        attempt = 1
        while True:
            tool_use_id = f"{name}-{next(self._ids)}"
            self._started(tool_use_id, name, tool_input)
            started = time.monotonic()
            try:
                text = await asyncio.to_thread(tool, **tool_input)
            except Exception as e:  # noqa: BLE001 - recorded, then retried or raised
                error = str(e) or type(e).__name__
                self.recorder.record(name, tool_input, started, error=error)
                self._finished(tool_use_id, "error", error)
                if attempt == STEP_ATTEMPTS:
                    raise
                attempt += 1
                continue
            self.recorder.record(name, tool_input, started, text=text)
            self._finished(tool_use_id, "success", text)
            return text

    def _started(self, tool_use_id: str, name: str, tool_input: dict) -> None:
        tool_use = {"toolUseId": tool_use_id, "name": name}
        self.events.put_nowait(
            {
                "current_tool_use": tool_use,
                "delta": {"toolUse": {"input": json.dumps(tool_input)}},
            }
        )
        self.events.put_nowait(
            {
                "message": {
                    "role": "assistant",
                    "content": [{"toolUse": {**tool_use, "input": tool_input}}],
                }
            }
        )

    def _finished(self, tool_use_id: str, status: str, text: str) -> None:
        self.events.put_nowait(
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "status": status,
                                "content": [{"text": _ui_text(text)}],
                            }
                        }
                    ],
                }
            }
        )


def _decisions(text: str) -> dict | None:
    """The editor's drop and merge lists, or None when its reply holds none"""
    for block in reversed(DECISIONS_RE.findall(text) or [text]):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        drop = data.get("drop") or []
        merge = data.get("merge") or []
        if isinstance(drop, list) and isinstance(merge, list):
            return {
                "drop": [str(i) for i in drop],
                "merge": [[str(i) for i in g] for g in merge if isinstance(g, list)],
            }
    return None


async def _edit(session_id: str, aggregate_uri: str) -> dict:
    """Ask the editor which findings to drop and merge; no decisions if it cannot say"""
    aggregate = read_s3_json(aggregate_uri)
    if not isinstance(aggregate, dict) or not aggregate.get("findings"):
        return {}
    try:
        text = await asyncio.to_thread(
            run_inner_agent_text,
            system_prompt=EDITOR_PROMPT,
            user_prompt=json.dumps(aggregate, indent=2),
            tools=[],
        )
    except Exception as e:  # noqa: BLE001 - the findings are published unedited
        text, error = "", str(e) or type(e).__name__
    else:
        error = "its reply had no parsable decisions"
    decisions = _decisions(text) if text else None
    if decisions is None:
        note_issue(
            session_id,
            "report",
            f"the editor failed ({error}), so every aggregated finding is published"
            " without editing decisions",
            severity="warning",
        )
        return {}
    return decisions


async def _review(inputs: ReviewInputs, steps: _Steps) -> None:
    sid = inputs.session_id
    documents = [(inputs.content_pdf_uri, inputs.content_pdf_name)] + [
        (uri, inputs.reference_names[i] if i < len(inputs.reference_names) else "")
        for i, uri in enumerate(inputs.reference_uris)
    ]
    reading = [
        steps.run(process_pdf, s3_uri=uri, original_filename=name)
        for uri, name in documents
    ]
    if inputs.claims_uri:
        reading.append(
            steps.run(
                load_claims_library,
                s3_uri=inputs.claims_uri,
                original_filename=inputs.claims_name,
            )
        )
    read = await asyncio.gather(*reading)
    content_md, reference_mds = read[0], read[1 : len(documents)]
    library_uri = _reply(read[-1])["claims_s3_uri"] if inputs.claims_uri else ""

    batched = _reply(
        await steps.run(batch_content, markdown_s3_uri=content_md, session_id=sid)
    )
    batch_uris = [b["s3_uri"] for b in batched["batches"]]

    async def claims(batch_uri: str) -> str:
        """Extract and match one batch's claims; the matched URI, or "" if that failed"""
        if not library_uri:
            return ""
        try:
            extracted = _reply(
                await steps.run(
                    extract_claims, batch_md_s3_uri=batch_uri, session_id=sid
                )
            )
            matched = _reply(
                await steps.run(
                    match_claims,
                    extracted_claims_s3_uri=extracted["extracted_claims_s3_uri"],
                    claims_library_s3_uri=library_uri,
                    session_id=sid,
                )
            )
        except Exception:  # noqa: BLE001 - recorded; the run record marks the batch
            return ""
        return matched["matched_claims_s3_uri"]

    async def reviews(batch_uri: str, matching: asyncio.Task) -> None:
        # The generic reviewer reads no claims, so it starts while the batch is matched
        generic = asyncio.ensure_future(
            steps.run(run_generic_review, batch_md_s3_uri=batch_uri, session_id=sid)
        )
        try:
            matched = await matching
        except BaseException:
            generic.cancel()
            raise
        calls = [generic]
        if reference_mds:
            calls.append(
                steps.run(
                    run_internal_review,
                    batch_md_s3_uri=batch_uri,
                    session_id=sid,
                    reference_md_uris=list(reference_mds),
                    matched_claims_s3_uri=matched,
                )
            )
        if inputs.enabled_sources:
            calls.append(
                steps.run(
                    run_external_review,
                    batch_md_s3_uri=batch_uri,
                    session_id=sid,
                    enabled_sources=inputs.enabled_sources,
                    matched_claims_s3_uri=matched,
                )
            )
        # a failed reviewer is recorded, and the other reviewers' findings still count
        await asyncio.gather(*calls, return_exceptions=True)

    matching = [asyncio.create_task(claims(uri)) for uri in batch_uris]
    reviewing = [
        asyncio.create_task(reviews(uri, task))
        for uri, task in zip(batch_uris, matching)
    ]
    try:
        if library_uri:
            await asyncio.gather(*matching)
            # a failure is recorded, and the review goes on without the claims report
            with contextlib.suppress(Exception):
                await steps.run(get_claims, session_id=sid)
        await asyncio.gather(*reviewing)
    finally:
        for task in matching + reviewing:
            task.cancel()

    aggregate = _reply(await steps.run(get_reviews, session_id=sid))
    decisions = await _edit(sid, aggregate["aggregate_s3_uri"])
    await steps.run(publish_review, session_id=sid, **decisions)


async def run_review(
    inputs: ReviewInputs, recorder: RunRecorder
) -> AsyncIterator[dict]:
    """Run one review, yielding the stream events that show its steps

    Raises the exception of a step the review cannot go on without (reading the
    content, batching it, aggregating or publishing the findings).
    """
    steps = _Steps(recorder)
    review = asyncio.create_task(_review(inputs, steps))
    review.add_done_callback(lambda _: steps.events.put_nowait(None))
    try:
        while (event := await steps.events.get()) is not None:
            yield event
        review.result()
    finally:
        review.cancel()
    yield {"data": READY_TEXT}
