"""Actionable reviews (PLAN-CODE phase 5) — RULEBOOK R4.8b.

Every review offers the choices that resolve it and names the show page it's
about. `open_review` opens (or extends) one with its choices and the data the
choice needs; `resolve_choice` applies the option you picked and closes it.

Kinds with choices:
- `add_check:needs_user` — add to the proposed show (a cour of the season it
  follows) / make it an individual season until TVDB has it / don't add;
- `same_tvdb_show` — merge the shows (R1.14) / they aren't the same show (your
  note says which TVDB id is wrong);
- `remote_completed` — a list said completed over unaired episodes (R4.8a):
  accept (every episode watched) / revert to watching;
- `later_planned` — a remote pause/drop would skip seasons you planned (Q-J4):
  skip them too / keep them planned;
- `list_entry_removed` — an entry that was on AniList/MAL is gone (R4.10): add it
  back / stop mirroring that season to that list;
- `remote_progress_lower` — a list's progress is below LCARS's by more than 2
  episodes, or on a completed season (R4.10): unwatch the difference / keep
  LCARS's and write it back;
- `list_not_settled` — a list change hasn't reached every list after 15 minutes:
  try the write again / release the lock;
- `list_readback_differs` — a list holds another value than LCARS wrote (it
  clamped or moved it itself): acknowledged, not pushed again;
- `tvdb_link` — an add whose TVDB link nothing confirmed (PLAN-CODE 8.8): link
  it (or "despite" its mismatches) / link the TVDB id in your note / keep it an
  individual season.
- `tvdb_candidate` — a TVDB id only one source suggests for an existing show (R3.7e): it is
  this show / it isn't / the TVDB id in your note is (R1.14 and R3.2a still apply).
"""

from __future__ import annotations

import json

from lcars import pending_review, util

LABELS = {
    "add_to_show": "Add to the proposed show",
    "individual": "Individual season until TVDB has it",
    "dont_add": "Don't add",
    "attach_span": "Attach it to every season it fills (one entry)",
    "use_tvdb_id": "Use the TVDB id in my note",
    "merge": "Merge into one show",
    "not_same": "Not the same show",
    "accept_completed": "Accept: completed, every episode watched",
    "revert_watching": "Revert to watching, don't mark the episodes",
    "skip_later": "Skip the later seasons too",
    "keep_later": "Keep the later seasons planned",
    "link_tvdb": "Link this TVDB show",
    "link_tvdb_despite": "Link this TVDB show despite the mismatches",
    "link_other_tvdb": "Link the TVDB id in my note instead",
    "reject_tvdb": "Not this show",
    "keep_individual": "Keep it an individual season for now",
    "re_add": "Add it back to the list",
    "stop_mirroring": "Stop mirroring this season to that list",
    "accept_lower": "Accept the list's lower progress (unwatch the difference)",
    "keep_lcars": "Keep LCARS's progress and write it back to the list",
    "retry_now": "Try to write it to the lists again now",
    "unlock": "Release the lock without confirming",
    "acknowledge": "Understood, leave it as it is",
}


class ReviewError(ValueError):
    """A choice that doesn't fit the review. `reopen`: new evidence the
    review should carry from now on (a TVDB id from your note that didn't
    fit becomes the review's candidate, PLAN-CODE 8.8)."""

    def __init__(self, message: str, reopen: dict | None = None):
        super().__init__(message)
        self.reopen = reopen


def open_review(
    conn, entity_type: str, entity_id: str, field: str, source: str, value: str,
    choices: list[str], payload: dict, show_id: str | None = None,
) -> None:
    pending_review.open_or_extend(conn, entity_type, entity_id, field, source, None, value)
    conn.execute(
        "UPDATE pending_review SET choices = ?, payload = ?, show_id = ?"
        " WHERE entity_type = ? AND entity_id = ? AND field = ? AND resolved_at IS NULL",
        (json.dumps([{"id": c, "label": LABELS[c]} for c in choices]), json.dumps(payload),
         show_id, entity_type, entity_id, field),
    )


def _attach_span(conn, payload: dict, candidate) -> None:
    """You said yes to one entry over every TVDB season it fills (seasons that are skipped
    included); a season that is missing or holds another entry still stops it."""
    from lcars import add_check

    numbers = payload.get("span_seasons") or []
    if not payload.get("show_id") or len(numbers) < 2:
        raise ReviewError("this review has no seasons to attach to")
    _levels, missing, taken, _skipped = add_check.span_levels(conn, payload["show_id"], numbers)
    if missing or taken:
        raise ReviewError("seasons " + ", ".join(f"S{n}" for n in missing + taken)
                          + " are missing or hold another entry now — look at the show page")
    add_check._attach_span(conn, add_check.Decision(
        "span", payload["show_id"], tvdb_id=payload.get("tvdb_id"), season_numbers=numbers),
        candidate, util.now_utc_iso())


def _use_tvdb_id(conn, row, payload: dict, candidate, note: str | None) -> None:
    """R3.7d: the TVDB id in your note is a second, independent source (R3.7a): the add check
    runs again with it, and what it decides is applied; when it still needs you the review stays
    open and says why."""
    import re

    from lcars import add_check, fribb

    m = re.search(r"\d+", re.sub(r"season\s*\d+", "", note or "", flags=re.I))
    if not m:
        raise ReviewError("put the TVDB id in the note")
    candidate.tvdb_id = int(m.group())
    decision = add_check.classify(conn, candidate, fribb.load_dataset())
    season = re.search(r"season\s*(\d+)", note or "", re.I)
    if decision.kind == "needs_user" and decision.reason == add_check.NO_SEASON and season:
        # you named the season too ("season 2"): your word on which one, the rest as usual
        show = add_check._tracked_show_for_tvdb(conn, candidate.tvdb_id)
        decision = add_check.place_in_season(conn, candidate, show, candidate.tvdb_id,
                                             int(season.group(1)))
    if decision.kind == "individual_season":
        add_check.create_individual_season(conn, candidate, payload.get("status"))
    elif decision.kind in add_check.AUTOMATIC:
        add_check.apply_decision(conn, decision, candidate)
    elif decision.kind == "new_show":  # a TVDB show LCARS doesn't track: yours to add
        raise ReviewError(f"TVDB {candidate.tvdb_id} is a show LCARS doesn't track yet: add it "
                          "from the add page, then this entry joins it")
    elif decision.kind != "already_tracked":
        raise ReviewError(f"TVDB {candidate.tvdb_id} doesn't settle it: {decision.reason}"
                          + (f" — {decision.proposal}" if decision.proposal else ""))


def resolve_choice(conn, review_id: str, choice: str, client: str, note: str | None) -> None:
    from lcars import add_check, consolidation, fribb

    row = conn.execute("SELECT * FROM pending_review WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        raise ReviewError(f"no such review: {review_id}")
    if row["resolved_at"] is not None:
        raise ReviewError("this review is already resolved")
    offered = [c["id"] for c in json.loads(row["choices"] or "[]")]
    if choice not in offered:
        raise ReviewError(f"{choice!r} isn't one of this review's choices {offered}")
    payload = json.loads(row["payload"] or "{}")
    field = row["field"]

    if field == "add_check:needs_user":
        candidate = add_check.Candidate(
            add_check.USER, anilist_id=payload.get("anilist_id"), mal_id=payload.get("mal_id"),
            titles=payload.get("titles") or [], media_type=payload.get("media_type"),
        )
        if choice == "add_to_show":
            prequel = conn.execute(
                "SELECT * FROM season WHERE id = ?", (payload["season_id"],)
            ).fetchone()
            tvdb_season_id = prequel["parent_id"] if prequel["kind"] == "part" else prequel["id"]
            tvdb_season = conn.execute(
                "SELECT season_number FROM season WHERE id = ?", (tvdb_season_id,)
            ).fetchone()
            add_check.apply_decision(conn, add_check.Decision(
                "part", payload["show_id"], tvdb_season_id, None, tvdb_season[0],
            ), candidate)
        elif choice == "individual":
            add_check.create_individual_season(conn, candidate, payload.get("status"))
        elif choice == "attach_span":
            _attach_span(conn, payload, candidate)
        elif choice == "use_tvdb_id":
            _use_tvdb_id(conn, row, payload, candidate, note)
    elif field == "remote_completed":
        # R4.8a: you completed it on the list.
        from lcars import list_sync, status_rules

        season_id = payload["season_id"]
        if choice == "accept_completed":
            fx = status_rules.set_level_status(
                conn, season_id, "completed", client, confirmed=True
            )
        else:
            fx = status_rules.set_level_status(conn, season_id, "watching", client)
        for sid in {season_id, *(sid for sid, _o, _n in fx.seasons)}:
            list_sync.push(conn, sid)  # both lists follow what you chose
        row_show = conn.execute(
            "SELECT show_id FROM season WHERE id = ?", (season_id,)
        ).fetchone()
        if row_show and row_show[0]:
            list_sync.push_progress_for_show(conn, row_show[0])
    elif field == "later_planned" and choice == "skip_later":
        from lcars import list_sync, status_rules

        fx = status_rules.set_level_status(
            conn, payload["season_id"], payload["status"], client, confirmed=True
        )
        for sid, old, new in fx.seasons:
            if new == "skipped":
                list_sync.delete_if_auto_skipped(conn, sid, old)
    elif field == "list_entry_removed":
        from lcars import list_sync

        season_id, service = payload["season_id"], payload["service"]
        if choice == "re_add":
            list_sync.push(conn, season_id, services=(service,))
        else:
            ext = list_sync.list_ids(conn, season_id).get(service)
            conn.execute("UPDATE season SET list_sync = 0 WHERE id = ?", (season_id,))
            if ext is not None:
                conn.execute("DELETE FROM list_baseline WHERE service = ? AND external_id = ?",
                             (service, ext))
    elif field == "remote_progress_lower":
        from lcars import list_hub, list_sync, status_rules

        season_id, service = payload["season_id"], payload["service"]
        season = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
        if choice == "accept_lower":
            list_hub.unwatch_last(conn, season, list_sync.level_progress(conn, season)
                                  - int(payload["progress"]))
            if season["status"] == "completed":  # no longer every episode watched (R2.7)
                status_rules.set_level_status(conn, season_id, "watching", client)
            list_sync.push(conn, season_id, services=tuple(
                s for s in ("anilist", "mal") if s != service))
            ext = list_sync.list_ids(conn, season_id).get(service)
            if ext is not None:
                from lcars import list_baseline

                list_baseline.record(conn, service, ext, progress=int(payload["progress"]),
                                     lcars_progress=int(payload["progress"]))
        else:
            list_sync.push(conn, season_id, status=False, services=(service,))
    elif field == "list_not_settled":
        from lcars import list_hub, list_sync

        season_id = payload["season_id"]
        if choice == "retry_now":
            list_sync.push(conn, season_id)
            list_hub.settle_locked(conn)
        else:
            list_hub.unlock(conn, season_id)
    elif field == "tvdb_link":
        from lcars import tvdb_vetting

        tvdb_vetting.resolve(conn, payload, choice, note)
    elif field == "tvdb_candidate":
        from lcars import tvdb_guard

        tvdb_guard.resolve(conn, row["entity_id"], payload, choice, note)
    elif field == "same_tvdb_show" and choice == "merge":
        result = consolidation.apply_group(conn, payload["tvdb_id"], fribb.load_dataset())
        if not result.get("merged"):
            raise ReviewError(f"couldn't merge: {result.get('reason')}")

    conn.execute(
        "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?, resolution_note = ?"
        " WHERE id = ?",
        (util.now_utc_iso(), client, f"{LABELS[choice]}" + (f" — {note}" if note else ""),
         review_id),
    )
    conn.commit()
