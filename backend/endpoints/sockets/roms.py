"""Socket.IO broadcasts for ROM mutations.

Emits:
- ``roms:updated`` - one or more ROMs changed (edit, match, per-user state,
  asset write).
- ``roms:deleted`` - ROMs were removed from the library.

Both carry ``{"ids": [...], "actor_user_id": int, "actor_client_id": str|None}``.

The payload carries **ids, not serialized ROMs**, deliberately. ``rom_user`` is
scoped to the requesting user (see ``RomUserSchema.for_user`` and the per-user
saves / states / notes filtering in ``DetailedRomSchema``), so broadcasting a
serialized ROM would show every client the acting user's playing status,
rating and last-played. Sending ids lets each client refetch and get its own
per-user state back, and avoids serializing once per recipient.

``actor_client_id`` is the Socket.IO sid of the connection that made the
request (from the ``X-Socket-Id`` header the frontend attaches), and lets that
one client skip the echo of a change it already applied. It is deliberately
per-connection, not per-user: two tabs of one account are two clients that
each need the update, and on a single-user instance every client is the same
user, so a per-user check would suppress the event everywhere. Absent for
callers that aren't holding a socket (API clients, curl), which is harmless —
nothing is delivered to them either. ``actor_user_id`` identifies who made the
change and is not used for suppression.

Emitted from REST handlers rather than a worker: the REST app shares its
process with the Socket.IO server, so it can use the already-initialised
Redis-backed server, which fans out across web workers (same approach as
``endpoints/activity.py``).
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import Request

from handler.socket_handler import socket_handler
from logger.logger import log

ROMS_UPDATED_EVENT = "roms:updated"
ROMS_DELETED_EVENT = "roms:deleted"

CLIENT_ID_HEADER = "x-socket-id"


def acting_client_id(request: Request) -> str | None:
    """Socket.IO sid of the connection behind this request, if it has one.

    Read off the raw headers rather than declared as an endpoint parameter so
    adding echo suppression doesn't change any route signature (and so the
    generated frontend client stays untouched).
    """
    return request.headers.get(CLIENT_ID_HEADER) or None


async def _broadcast(
    event: str,
    rom_ids: Iterable[int],
    actor_user_id: int,
    actor_client_id: str | None,
) -> None:
    ids = list(dict.fromkeys(rom_ids))
    if not ids:
        return

    # A dropped broadcast must never fail the request that triggered it: the
    # write already succeeded, and the worst case is other clients staying
    # stale until they refetch, which is the behaviour before this existed.
    try:
        await socket_handler.socket_server.emit(
            event,
            {
                "ids": ids,
                "actor_user_id": actor_user_id,
                "actor_client_id": actor_client_id,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"Failed to broadcast {event} for roms {ids}: {e}")


async def broadcast_roms_updated(
    rom_ids: Iterable[int], actor_user_id: int, actor_client_id: str | None = None
) -> None:
    """Tell connected clients these ROMs changed, so they can refetch them."""
    await _broadcast(ROMS_UPDATED_EVENT, rom_ids, actor_user_id, actor_client_id)


async def broadcast_roms_deleted(
    rom_ids: Iterable[int], actor_user_id: int, actor_client_id: str | None = None
) -> None:
    """Tell connected clients these ROMs are gone, so they can drop them."""
    await _broadcast(ROMS_DELETED_EVENT, rom_ids, actor_user_id, actor_client_id)
