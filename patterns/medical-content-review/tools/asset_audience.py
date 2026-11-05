# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Who an asset is written for, and which library claims are approved for that audience.

A library claim's Audience is a usage restriction: the same fact is often approved
once in HCP wording and once in patient wording, and HCP copy must not be reviewed
against the patient version. The batcher decides the asset's audience once per document;
the claim matcher reads it back for every batch.
"""

from utils.s3 import STAGING_BUCKET, put_json, read_json, safe_session_id

AUDIENCE_LABELS = {"hcp": "HCP", "patient": "patient or consumer"}

# library Audience values ("Physician, Nurse", "General Public, Patient") by audience
_TERMS = {
    "hcp": (
        "physician",
        "nurse",
        "hcp",
        "healthcare professional",
        "health care professional",
        "pharmacist",
        "prescriber",
    ),
    "patient": ("patient", "general public", "consumer", "caregiver", "dtc"),
}
INTERNAL_TERM = "internal"


def _key(session_dir: str) -> str:
    """S3 key of the session's stored asset audience

    Parameters
    ----------
    session_dir : str
        Session id

    Returns
    -------
    str
        Key under the staging bucket
    """
    return f"claims/{safe_session_id(session_dir)}/asset_audience.json"


def claim_audiences(value: object) -> set[str] | None:
    """Map a library claim's Audience to the asset audiences it is approved for

    Returns None when the value names no audience this module knows (empty, or a label
    such as a channel), so the claim is not restricted. An Internal-only claim maps to
    the empty set: no asset may use it.

    Parameters
    ----------
    value : object
        The claim's Audience field, a comma-separated list of labels

    Returns
    -------
    set[str] | None
        Asset audiences (`hcp`, `patient`) the claim is approved for, or None when
        the claim is not restricted
    """
    parts = [p.strip().lower() for p in str(value or "").split(",") if p.strip()]
    found = {
        audience
        for part in parts
        for audience, terms in _TERMS.items()
        if any(term in part for term in terms)
    }
    if found:
        return found
    if any(INTERNAL_TERM in part for part in parts):
        return set()
    return None


def approved_for(library_claim: dict, asset_audience: str) -> bool:
    """Whether a library claim may be used in an asset written for `asset_audience`

    Parameters
    ----------
    library_claim : dict
        Library claim, with its `audience` field
    asset_audience : str
        Audience of the asset: `hcp`, `patient` or `""` when undecided

    Returns
    -------
    bool
        True when the claim is approved for the audience, or the audience is unknown
    """
    if asset_audience not in AUDIENCE_LABELS:
        return True
    audiences = claim_audiences(library_claim.get("audience"))
    return audiences is None or asset_audience in audiences


def write_asset_audience(session_dir: str, audience: str, reason: str) -> None:
    """Store the asset's audience ("hcp", "patient" or "" when undecided) for the session

    Parameters
    ----------
    session_dir : str
        Session id
    audience : str
        `hcp`, `patient` or `""` when undecided
    reason : str
        Why the batcher chose that audience
    """
    put_json(_key(session_dir), {"audience": audience, "reason": reason})


def read_asset_audience(session_id: str) -> dict:
    """The stored `{"audience", "reason"}` for the session, or {} when none was stored

    Parameters
    ----------
    session_id : str
        Session id

    Returns
    -------
    dict
        The stored audience and reason, or {} when none was stored
    """
    if not STAGING_BUCKET:
        return {}
    data = read_json(_key(session_id))
    return data if isinstance(data, dict) else {}
