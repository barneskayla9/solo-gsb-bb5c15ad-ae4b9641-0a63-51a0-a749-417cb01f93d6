"""FastAPI application: temporary-event radio spectrum requests.

Endpoints
---------
POST   /api/applications                 submit (whole-request validation)
GET    /api/applications                 list submissions
GET    /api/applications/{id}            fetch one submission
POST   /api/applications/{id}/preview    conflicts + current revision
POST   /api/applications/{id}/approve    conditional approval (expected rev.)
POST   /api/applications/batch-approve   atomic all-or-nothing group approval
POST   /api/applications/{id}/revoke     revoke (changes future effective set)
GET    /api/revision/current             current revision + effective set
GET    /api/revision/{revision}          as-of snapshot (approvals/revocations)
GET    /health                           liveness probe

Concurrency
-----------
Approval locks the singleton ``revision_meta`` row (``SELECT ... FOR UPDATE``)
for the duration of the transaction.  Concurrent approvals therefore
serialise: the loser re-adjudicates against the winner's commit and is
refused with ``stale_revision`` or ``interference``.  Single approvals,
batch approvals and revocations all take the same lock, so they share one
linear revision verdict -- a batch either commits every approval event
(consecutive revisions, input order) or commits nothing at all.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from . import adjudication
from .adjudication import Circle, Conflict, License
from .config import settings
from .db import SessionLocal, engine, init_db, wait_for_database
from .errors import APIError, api_error_handler
from .models import (
    Application,
    ApprovalEvent,
    RevisionMeta,
    RevocationEvent,
)
from .schemas import (
    ApplicationCreate,
    ApprovalRequest,
    BatchApprovalRequest,
    RevocationRequest,
)
from .semantic_validation import validate_application

log = logging.getLogger("radio.api")

# Error codes
E_VALIDATION = "validation_failed"
E_NOT_FOUND = "not_found"
E_NOT_PENDING = "not_pending"
E_ALREADY_REVOKED = "already_revoked"
E_REVISION_OUT_OF_RANGE = "revision_out_of_range"
E_STALE_REVISION = "stale_revision"
E_INTERFERENCE = "interference"
E_BATCH_REJECTED = "batch_approval_rejected"


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def _utc(value: datetime) -> datetime:
    """Coerce a possibly naive timestamp (e.g. under SQLite) to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def serialize_application(app: Application) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "circle": {"cx": app.cx, "cy": app.cy, "radius": app.radius},
        "frequency": {"low": app.freq_low, "high": app.freq_high},
        "time_window": {"start": iso(app.start_time), "end": iso(app.end_time)},
        "status": app.status,
        "approval_revision": app.approval_revision,
        "created_at": iso(app.created_at),
    }


def serialize_conflict(conflict: Conflict) -> dict[str, Any]:
    """One conflicting effective grant with the trigger conditions."""
    other = conflict.other
    return {
        "application": {
            "id": other.application_id,
            "circle": {
                "cx": other.circle.cx,
                "cy": other.circle.cy,
                "radius": other.circle.radius,
            },
            "frequency": {"low": other.freq_low, "high": other.freq_high},
            "time_window": {
                "start": iso(other.start_time),
                "end": iso(other.end_time),
            },
        },
        "trigger": {
            "time_intersect": conflict.time_overlap,
            "area_intersect": conflict.area_overlap,
            "frequency_intersect": conflict.frequency_intersects,
            "minimum_frequency_gap": conflict.minimum_frequency_gap,
            "within_guard_band": (
                conflict.minimum_frequency_gap < settings.guard_band
            ),
        },
        "reason": conflict.reason,
    }


def serialize_group_conflict(conflict: Conflict, group_ids: set[int]) -> dict[str, Any]:
    """A conflict in a batch adjudication, tagged by which side is in the group."""
    record = serialize_conflict(conflict)
    record["in_group"] = conflict.other.application_id in group_ids
    return record


def to_license(app: Application) -> License:
    return License(
        application_id=app.id,
        circle=Circle(app.cx, app.cy, app.radius),
        freq_low=app.freq_low,
        freq_high=app.freq_high,
        start_time=_utc(app.start_time),
        end_time=_utc(app.end_time),
    )


def load_effective_licenses(session) -> list[License]:
    """Approved and not (yet) revoked = the effective set for adjudication."""
    rows = (
        session.query(Application)
        .filter(Application.status == "approved")
        .order_by(Application.id)
        .all()
    )
    return [to_license(row) for row in rows]


# --------------------------------------------------------------------------- #
# Revision snapshots
# --------------------------------------------------------------------------- #
def build_snapshot(session, revision: int) -> dict[str, Any]:
    """Reconstruct the state of the world *at* ``revision``.

    Returns every approval event with revision <= the target and the
    application snapshot, flags those later revoked by the target revision,
    and includes all revocation events of the period.
    """
    approvals = (
        session.query(ApprovalEvent)
        .filter(ApprovalEvent.revision <= revision)
        .order_by(ApprovalEvent.revision)
        .all()
    )
    revocations = (
        session.query(RevocationEvent)
        .filter(RevocationEvent.revision <= revision)
        .order_by(RevocationEvent.revision)
        .all()
    )
    revoked_at_or_before = {ev.application_id: ev for ev in revocations}

    approval_records: list[dict[str, Any]] = []
    effective_ids: list[int] = []
    for ev in approvals:
        app = session.get(Application, ev.application_id)
        rev = revoked_at_or_before.get(ev.application_id)
        was_revoked_by_then = rev is not None and rev.revision <= revision
        record = {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "guard_band": ev.guard_band,
            "created_at": iso(ev.created_at),
            # Status of this grant *as of the queried revision*.
            "status_as_of": "revoked" if was_revoked_by_then else "approved",
            "application": serialize_application(app) if app else None,
        }
        if was_revoked_by_then:
            record["revoked_at_revision"] = rev.revision
        else:
            effective_ids.append(ev.application_id)
        approval_records.append(record)

    revocation_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        for ev in revocations
    ]

    return {
        "revision": revision,
        "effective_application_ids": effective_ids,
        "approvals": approval_records,
        "revocations": revocation_records,
    }


# --------------------------------------------------------------------------- #
# Application factory / lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_database()
    init_db()
    log.info(
        "startup complete; guard_band=%s listening on %s:%s",
        settings.guard_band,
        settings.app_host,
        settings.app_port,
    )
    yield
    engine.dispose()


app = FastAPI(
    title="Temporary Event Radio Spectrum API",
    version="1.0.0",
    description=(
        "Submit, preview, approve and revoke temporary radio applications. "
        "Frequencies are closed intervals, UTC times are half-open intervals, "
        "coverage is a planar circle."
    ),
    lifespan=lifespan,
)
app.add_exception_handler(APIError, api_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Normalise FastAPI structural errors into the standard error envelope."""
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": E_VALIDATION,
                "message": "request validation failed; the whole request was rejected",
                "details": details,
            }
        },
    )


# --------------------------------------------------------------------------- #
# Health / revisions
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/revision/current", tags=["revision"])
def current_revision() -> dict[str, Any]:
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        revision = meta.revision if meta else 0
        snapshot = build_snapshot(session, revision)
        effective_applications = [
            serialize_application(app)
            for app_id in snapshot["effective_application_ids"]
            if (app := session.get(Application, app_id)) is not None
        ]
    return {
        "current_revision": revision,
        "guard_band": settings.guard_band,
        "effective_application_ids": snapshot["effective_application_ids"],
        "effective_applications": effective_applications,
        "approvals": snapshot["approvals"],
        "revocations": snapshot["revocations"],
    }


@app.get("/api/revision/{revision}", tags=["revision"])
def revision_snapshot(revision: int) -> dict[str, Any]:
    if revision < 0:
        raise APIError(
            E_REVISION_OUT_OF_RANGE,
            "revision must be >= 0",
            status_code=422,
        )
    with SessionLocal() as session:
        current = session.get(RevisionMeta, 1).revision
        if revision > current:
            raise APIError(
                E_REVISION_OUT_OF_RANGE,
                f"revision {revision} is in the future; current revision is {current}",
                status_code=404,
                details={"current_revision": current},
            )
        return build_snapshot(session, revision)


# --------------------------------------------------------------------------- #
# Applications: create / list / get
# --------------------------------------------------------------------------- #
@app.post("/api/applications", status_code=201, tags=["applications"])
def create_application(payload: ApplicationCreate) -> dict[str, Any]:
    errors = validate_application(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the whole application was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )

    app_row = Application(
        name=payload.name,
        cx=payload.circle.cx,
        cy=payload.circle.cy,
        radius=payload.circle.radius,
        freq_low=payload.frequency.low,
        freq_high=payload.frequency.high,
        start_time=payload.time_window.start.astimezone(timezone.utc),
        end_time=payload.time_window.end.astimezone(timezone.utc),
        status="pending",
    )
    with SessionLocal() as session, session.begin():
        session.add(app_row)
        session.flush()
        result = serialize_application(app_row)
    return result


@app.get("/api/applications", tags=["applications"])
def list_applications(status: str | None = None) -> dict[str, Any]:
    with SessionLocal() as session:
        query = session.query(Application).order_by(Application.id)
        if status is not None:
            if status not in {"pending", "approved", "revoked"}:
                raise APIError(
                    E_VALIDATION,
                    "status filter must be one of pending, approved, revoked",
                    status_code=422,
                )
            query = query.filter(Application.status == status)
        rows = query.all()
        return {"applications": [serialize_application(row) for row in rows]}


@app.get("/api/applications/{application_id}", tags=["applications"])
def get_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        return serialize_application(row)


# --------------------------------------------------------------------------- #
# Preview (non-mutating)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/preview", tags=["applications"])
def preview_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}, only pending "
                "applications can be previewed",
                status_code=409,
                details={"status": row.status},
            )

        current = session.get(RevisionMeta, 1).revision
        effective = load_effective_licenses(session)
        conflicts = adjudication.evaluate(to_license(row), effective, settings.guard_band)

    return {
        "application_id": application_id,
        "current_revision": current,
        "guard_band": settings.guard_band,
        "has_conflicts": bool(conflicts),
        "conflicts": [serialize_conflict(c) for c in conflicts],
    }


# --------------------------------------------------------------------------- #
# Approve (conditional on expected revision, row-lock serialised)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/approve", tags=["applications"])
def approve_application(
    application_id: int, payload: ApprovalRequest
) -> dict[str, Any]:
    with SessionLocal() as session:
        try:
            with session.begin():
                # Serialise against every other approve/revoke on the
                # singleton row (a real row lock on PostgreSQL).
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked",
                        status_code=409,
                    )
                if row.status == "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is already approved at "
                        f"revision {row.approval_revision}",
                        status_code=409,
                        details={"approval_revision": row.approval_revision},
                    )

                # Optimistic revision check against the locked, latest revision.
                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and re-decide",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock against the latest effective set.
                effective = load_effective_licenses(session)
                conflicts = adjudication.evaluate(
                    to_license(row), effective, settings.guard_band
                )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"application {application_id} interferes with "
                        f"{len(conflicts)} effective grant(s); frequency not granted",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c) for c in conflicts
                            ],
                        },
                    )

                meta.revision = current + 1
                session.add(
                    ApprovalEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        guard_band=settings.guard_band,
                    )
                )
                row.status = "approved"
                row.approval_revision = meta.revision
                session.flush()
                response = {
                    "approved": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "guard_band": settings.guard_band,
                    "application": serialize_application(row),
                }
        except IntegrityError:
            # Last line of defence (e.g. backends without row locks such as
            # SQLite's tests): the revision slot was taken concurrently.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Batch approve (atomic group: all checks, then all commits or none)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/batch-approve", tags=["applications"])
def batch_approve_applications(payload: BatchApprovalRequest) -> dict[str, Any]:
    # Whole-request semantic checks on the id list (structural problems such
    # as a missing/wrong-typed field are normalised by FastAPI's handler).
    list_errors: list[dict[str, Any]] = []
    if not payload.application_ids:
        list_errors.append(
            {
                "field": "application_ids",
                "code": "application_ids_empty",
                "message": "application_ids must be a non-empty list",
            }
        )
    seen: set[int] = set()
    duplicates: list[int] = []
    for app_id in payload.application_ids:
        if app_id in seen and app_id not in duplicates:
            duplicates.append(app_id)
        seen.add(app_id)
    if duplicates:
        list_errors.append(
            {
                "field": "application_ids",
                "code": "application_ids_duplicate",
                "message": "application_ids must not contain duplicates",
                "duplicates": duplicates,
            }
        )
    if list_errors:
        raise APIError(
            E_VALIDATION,
            "the whole batch request was rejected due to invalid fields",
            status_code=422,
            details=list_errors,
        )

    requested = payload.application_ids
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton row lock as single approve / revoke, so all
                # three operations share one serialised revision verdict.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                rows_by_id = {
                    row.id: row
                    for row in session.query(Application)
                    .filter(Application.id.in_(requested))
                    .all()
                }

                # Failure bucket 1: unknown ids. 404 mirrors single approve.
                invalid = [
                    {"application_id": app_id}
                    for app_id in requested
                    if app_id not in rows_by_id
                ]
                if invalid:
                    raise APIError(
                        E_NOT_FOUND,
                        "batch approval rejected: some application ids do not exist",
                        status_code=404,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "invalid_application_ids": invalid,
                        },
                    )

                # Failure bucket 2: status must be pending for every member.
                not_pending: list[dict[str, Any]] = []
                pending_rows: list[Application] = []
                for app_id in requested:
                    row = rows_by_id[app_id]
                    if row.status != "pending":
                        not_pending.append(
                            {
                                "application_id": app_id,
                                "status": row.status,
                                "approval_revision": row.approval_revision,
                            }
                        )
                    else:
                        pending_rows.append(row)

                # Failure bucket 3: stale revision (same code as single).
                stale = payload.expected_revision != current
                stale_detail = {
                    "expected_revision": payload.expected_revision,
                    "current_revision": current,
                }

                # Failure bucket 4: interference.  Every pending member is
                # checked against the currently effective set and against the
                # other pending members of this group.  Members are appended
                # to ``group_licenses`` as we go in input order, so an
                # interference between two group members is reported on the
                # later one in input order.
                group_ids = set(requested)
                effective = load_effective_licenses(session)
                interference: list[dict[str, Any]] = []
                group_licenses: list[License] = []
                for row in pending_rows:
                    candidate = to_license(row)
                    conflicts = adjudication.evaluate(
                        candidate, effective + group_licenses, settings.guard_band
                    )
                    if conflicts:
                        interference.append(
                            {
                                "application_id": row.id,
                                "conflicts": [
                                    serialize_group_conflict(c, group_ids)
                                    for c in conflicts
                                ],
                            }
                        )
                    group_licenses.append(candidate)

                if not_pending or stale or interference:
                    # Nothing is written: the transaction rolls back, so the
                    # revision number is unchanged.
                    message_parts = []
                    if not_pending:
                        message_parts.append(
                            f"{len(not_pending)} item(s) not pending"
                        )
                    if stale:
                        message_parts.append("stale revision")
                    if interference:
                        message_parts.append(
                            f"{len(interference)} item(s) interfere"
                        )
                    raise APIError(
                        E_BATCH_REJECTED,
                        "batch approval rejected: " + "; ".join(message_parts),
                        status_code=409,
                        details={
                            **stale_detail,
                            "not_pending": not_pending,
                            "interference": interference,
                        },
                    )

                # All checks passed: one approval event per member, in input
                # order, consuming consecutive revision numbers in one commit.
                results: list[dict[str, Any]] = []
                for row in pending_rows:
                    current += 1
                    meta.revision = current
                    session.add(
                        ApprovalEvent(
                            revision=current,
                            application_id=row.id,
                            guard_band=settings.guard_band,
                        )
                    )
                    row.status = "approved"
                    row.approval_revision = current
                    results.append(
                        {
                            "application_id": row.id,
                            "revision": current,
                            "application": serialize_application(row),
                        }
                    )
                session.flush()
                response = {
                    "approved": True,
                    "expected_revision": payload.expected_revision,
                    "from_revision": payload.expected_revision,
                    "revision": current,
                    "new_revisions": [item["revision"] for item in results],
                    "count": len(results),
                    "guard_band": settings.guard_band,
                    "results": results,
                }
        except IntegrityError:
            # Backends without row locks (SQLite) are still protected by the
            # unique revision constraint: a concurrent commit took a slot.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Revoke (only affects the effective set used by later adjudications)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/revoke", tags=["applications"])
def revoke_application(
    application_id: int, payload: RevocationRequest
) -> dict[str, Any]:
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} is already revoked",
                        status_code=409,
                    )
                if row.status != "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is pending and cannot "
                        "be revoked",
                        status_code=409,
                        details={"status": row.status},
                    )

                meta.revision += 1
                session.add(
                    RevocationEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        reason=payload.reason,
                    )
                )
                row.status = "revoked"
                session.flush()
                response = {
                    "revoked": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "previous_approval_revision": row.approval_revision,
                    "reason": payload.reason,
                }
        except IntegrityError:
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); retry",
                status_code=409,
                details={"current_revision": latest},
            )
    return response
