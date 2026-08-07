from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from endpoints.sockets import roms as roms_socket
from endpoints.sockets.roms import (
    ROMS_DELETED_EVENT,
    ROMS_UPDATED_EVENT,
    acting_client_id,
    broadcast_roms_deleted,
    broadcast_roms_updated,
)


@pytest.fixture
def emit(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(roms_socket.socket_handler.socket_server, "emit", mock)
    return mock


async def test_broadcast_roms_updated_sends_ids_and_actor(emit):
    await broadcast_roms_updated([7, 9], actor_user_id=3, actor_client_id="abc")

    emit.assert_awaited_once_with(
        ROMS_UPDATED_EVENT,
        {"ids": [7, 9], "actor_user_id": 3, "actor_client_id": "abc"},
    )


async def test_broadcast_roms_deleted_uses_its_own_event(emit):
    await broadcast_roms_deleted([4], actor_user_id=1, actor_client_id="abc")

    emit.assert_awaited_once_with(
        ROMS_DELETED_EVENT,
        {"ids": [4], "actor_user_id": 1, "actor_client_id": "abc"},
    )


async def test_broadcast_without_a_client_id_suppresses_nothing(emit):
    # A caller holding no socket (API client, curl) can't be echo-matched, so
    # every connected client should act on the event.
    await broadcast_roms_updated([1], actor_user_id=1)

    assert emit.await_args.args[1]["actor_client_id"] is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-socket-id": "sid-1"}, "sid-1"),
        ({"X-Socket-Id": "sid-2"}, "sid-2"),  # header lookup is case-insensitive
        ({"x-socket-id": ""}, None),  # empty header is no client, not ""
        ({}, None),
    ],
)
def test_acting_client_id_reads_the_header(headers, expected):
    request = Request({"type": "http", "headers": Headers(headers).raw})

    assert acting_client_id(request) == expected


async def test_broadcast_dedupes_ids_and_keeps_order(emit):
    await broadcast_roms_updated([5, 2, 5, 2, 8], actor_user_id=1)

    assert emit.await_args.args[1]["ids"] == [5, 2, 8]


async def test_broadcast_skips_empty_id_list(emit):
    await broadcast_roms_updated([], actor_user_id=1)

    emit.assert_not_awaited()


async def test_broadcast_failure_never_propagates(emit):
    # The write already succeeded by this point, so a socket problem must not
    # turn a successful request into a 500.
    emit.side_effect = RuntimeError("redis is down")

    await broadcast_roms_updated([1], actor_user_id=1)

    emit.assert_awaited_once()
