"""The library browser, its filters, posters, and pinning. SPEC §11."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import replace
from math import ceil

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.db import session
from app.media import poster
from app.repo import (
    PAGE_SIZE,
    PIN_STATES,
    SORTS,
    LibraryFilter,
    bulk_pin,
    count_series,
    facet_counts,
    get_series,
    latest_bulk_batch,
    matching_ids,
    pinned_count,
    query_series,
    section_titles,
    set_pinned,
    undo_bulk_pin,
    watch_progress,
)
from app.web import templates

log = logging.getLogger(__name__)

router = APIRouter()

#: Shown when Plex has no artwork, or is unreachable. Inline so the grid never
#: depends on a static file or a second request that can also fail.
PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300">'
    b'<rect width="200" height="300" fill="#2d353e"/>'
    b'<text x="100" y="155" text-anchor="middle" fill="#6b7684"'
    b' font-family="sans-serif" font-size="15">no poster</text></svg>'
)


# ── Library (SPEC §11) ───────────────────────────


def _filter_from(request: Request) -> LibraryFilter:
    """Build a LibraryFilter from the querystring.

    Filter state lives in the URL so views are bookmarkable, which also means
    this is the one place that has to be tolerant of hand-edited params.
    """
    q = request.query_params

    def many(key: str) -> tuple[str, ...]:
        raw = ",".join(q.getlist(key))
        return tuple(v for v in (part.strip() for part in raw.split(",")) if v)

    sections: list[int] = []
    for value in many("section"):
        with suppress(ValueError):
            sections.append(int(value))

    page = 1
    with suppress(ValueError):
        page = max(1, int(q.get("page", "1")))

    pinned = q.get("pinned", "all")
    sort = q.get("sort", "recent")
    return LibraryFilter(
        search=q.get("q", "").strip(),
        sections=tuple(sections),
        statuses=many("status"),
        outlooks=many("outlook"),
        genres=many("genre"),
        networks=many("network"),
        pinned=pinned if pinned in PIN_STATES else "all",
        sort=sort if sort in SORTS else "recent",
        page=page,
    )


@router.get("/library")
async def library(request: Request):
    f = replace(_filter_from(request), user_id=int(request.state.user["id"]))
    with session() as conn:
        total = count_series(conn, f)
        rows = query_series(conn, f)
        facets = facet_counts(conn, f)
        sections = section_titles(conn)
        progress = watch_progress(conn, f.user_id)
        pinned_total = pinned_count(conn, f.user_id)
        can_undo = latest_bulk_batch(conn, f.user_id) is not None

    pages = max(1, ceil(total / PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "f": f,
            "series": rows,
            "facets": facets,
            "sections": sections,
            "progress": progress,
            "total": total,
            "pinned_total": pinned_total,
            "page": min(f.page, pages),
            "pages": pages,
            "can_undo": can_undo,
            "sorts": list(SORTS),
            "querystring": str(request.query_params),
        },
    )


@router.get("/poster/{series_id}")
async def poster_image(series_id: int):
    with session() as conn:
        row = get_series(conn, series_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such series")

    result = await poster(
        series_id,
        plex_thumb=row["poster_url"] or "",
        remote_url=row["remote_poster"] or "",
    )
    if result is None:
        # A placeholder rather than a 404: a broken-image icon in a poster
        # grid looks like the app is broken, not like Plex lacks artwork.
        return Response(PLACEHOLDER_SVG, media_type="image/svg+xml")

    content, content_type = result
    return Response(content, media_type=content_type, headers={"Cache-Control": "max-age=86400"})


@router.post("/api/series/{series_id}/pin")
async def pin_series(request: Request, series_id: int) -> JSONResponse:
    return _set_pin(int(request.state.user["id"]), series_id, True)


@router.post("/api/series/{series_id}/unpin")
async def unpin_series(request: Request, series_id: int) -> JSONResponse:
    return _set_pin(int(request.state.user["id"]), series_id, False)


def _set_pin(user_id: int, series_id: int, pinned: bool) -> JSONResponse:
    with session() as conn:
        if get_series(conn, series_id) is None:
            raise HTTPException(status_code=404, detail="no such series")
        set_pinned(conn, user_id, series_id, pinned)
        total = pinned_count(conn, user_id)
    return JSONResponse({"id": series_id, "pinned": pinned, "pinned_total": total})


@router.post("/api/series/bulk-pin")
async def bulk_pin_filtered(request: Request) -> JSONResponse:
    """Pin everything the filter matches.

    The request carries the filter, not a list of ids, and the server re-runs
    it — so nothing can go stale between rendering the grid and clicking the
    button (SPEC §11).
    """
    user_id = int(request.state.user["id"])
    with session() as conn:
        ids = matching_ids(conn, replace(_filter_from(request), user_id=user_id))
        count, batch = bulk_pin(conn, user_id, ids)
        total = pinned_count(conn, user_id)
    return JSONResponse({"pinned": count, "batch": batch, "pinned_total": total})


@router.post("/api/series/bulk-undo")
async def bulk_undo(request: Request) -> JSONResponse:
    user_id = int(request.state.user["id"])
    with session() as conn:
        batch = latest_bulk_batch(conn, user_id)
        undone = undo_bulk_pin(conn, user_id, batch) if batch else 0
        total = pinned_count(conn, user_id)
    return JSONResponse({"undone": undone, "pinned_total": total})
