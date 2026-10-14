from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette

from vkuxt import (
    Adapter,
    AdapterInput,
    App,
    EventMetadata,
    InputData,
    JSONFileStorage,
    MemoryStorage,
    decode_key,
    encode_key,
    storage_key,
)


class AccountKey(BaseModel):
    user_id: int
    scope: str


class EchoMetadata(EventMetadata):
    user_id: int


class EchoInput(InputData):
    handler_name: Literal["echo-data"]
    text: str


class StoredState(BaseModel):
    count: int


class RecordingAdapter(Adapter):
    def __init__(self, app: App, calls: list[str], name: str) -> None:
        super().__init__(app)
        self.calls = calls
        self.name = name

    async def start(self) -> None:
        self.calls.append(f"start:{self.name}")

    async def close(self) -> None:
        self.calls.append(f"close:{self.name}")


def test_key_codec_is_canonical_and_type_aware() -> None:
    key = AccountKey(user_id=12, scope="chat")
    encoded = encode_key(key)
    assert encoded == encode_key(AccountKey(scope="chat", user_id=12))
    assert decode_key(encoded, AccountKey) == key
    assert encode_key({"scope": "chat", "user_id": 12}) != encoded
    assert decode_key(encode_key({"b": 2, "a": 1}), dict) == {"a": 1, "b": 2}


def test_key_codec_rejects_ambiguous_nested_keys() -> None:
    with pytest.raises(TypeError, match="строками"):
        encode_key({"nested": {1: "integer", "1": "string"}})


def test_input_data_rejects_non_json_values() -> None:
    class ArbitraryInput(InputData):
        value: Any

    with pytest.raises(ValidationError, match="строгий JSON"):
        ArbitraryInput(value=object())
    value = ArbitraryInput(value="json")
    with pytest.raises(ValidationError, match="строгий JSON"):
        value.value = object()


def test_handler_types_are_inferred_and_long_payload_tokens_persist(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.json"
        calls: list[EchoInput] = []
        app = App(JSONFileStorage(path))

        @app.handler
        async def echo(app: App, meta: EchoMetadata, data: EchoInput) -> None:
            assert isinstance(app, App)
            assert meta.user_id == 4
            calls.append(data)

        interface_id = {"cmid": 80, "peer_id": 2000000001, "group_id": 71}
        first = app.new_interface()
        direct_payload = first.bind(EchoInput(handler_name="echo-data", text="bound"))
        assert direct_payload == {"handler_name": "echo-data", "text": "bound"}
        old_token = first.bind(
            EchoInput(handler_name="echo-data", text="old", extra="x" * 300)
        )
        assert isinstance(old_token, str)
        await app.register_interface(interface_id, first)
        await app.close()

        restored = App(JSONFileStorage(path))
        restored.add_handler(echo)
        current = restored.new_interface()
        new_token = current.bind(
            EchoInput(handler_name="echo-data", text="current", extra="y" * 300)
        )
        assert isinstance(new_token, str)
        await restored.register_interface(interface_id, current)

        record = await restored.get_interface(interface_id)
        assert record is not None
        assert old_token not in record.actions
        await restored.dispatch_event(
            EchoMetadata(user_id=4),
            data=record.actions[new_token]["data"],
        )
        assert calls[-1].text == "current"
        await restored.close()

    asyncio.run(scenario())


def test_state_storage_models_and_ttl() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage(), ttl=0.02)
        key = {"owner": 1, "kind": "counter"}
        await app.set_state(key, StoredState(count=3))
        assert await app.get_state(key, StoredState) == StoredState(count=3)
        await asyncio.sleep(0.03)
        assert await app.get_state(key) is None

        def unused(app: App, meta: EchoMetadata, data: EchoInput) -> None:
            return None

        app.add_handler(unused)
        interface_id = {"cmid": 1, "peer_id": 2, "group_id": 3}
        draft = app.new_interface(ttl=0.06)
        token = draft.bind(EchoInput(handler_name="echo-data", text="x" * 300))
        await app.register_interface(interface_id, draft)
        infinite_id = {"cmid": 2, "peer_id": 2, "group_id": 3}
        infinite = app.new_interface(ttl=None)
        await app.register_interface(infinite_id, infinite)
        registered = await app.get_interface(interface_id)
        assert registered is not None
        assert registered.actions[token]
        await asyncio.sleep(0.03)
        assert await app.get_state(key) is None
        assert await app.get_interface(interface_id) is not None
        assert await app.get_interface(infinite_id) is not None
        await asyncio.sleep(0.04)
        assert await app.get_interface(interface_id) is None
        assert await app.get_interface(infinite_id) is not None
        await app.close()

    asyncio.run(scenario())


def test_keyed_locks_serialize_same_key_and_allow_reentrance() -> None:
    async def scenario() -> None:
        app = App()
        key = {"game_id": "one"}
        active = 0
        peak = 0

        async def turn() -> None:
            nonlocal active, peak
            async with app.lock(key):
                active += 1
                peak = max(peak, active)
                async with app.lock(key):
                    await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(turn() for _ in range(4)))
        assert peak == 1
        await app.close()

    asyncio.run(scenario())


def test_multikey_locks_use_consistent_order() -> None:
    async def scenario() -> None:
        app = App()
        first = {"key": "a"}
        second = {"key": "b"}

        async def acquire(left, right) -> None:
            async with app.lock(left, right):
                await asyncio.sleep(0)

        await asyncio.wait_for(
            asyncio.gather(acquire(first, second), acquire(second, first)),
            timeout=1,
        )
        await app.close()

    asyncio.run(scenario())


def test_app_owns_adapter_lifecycle_through_starlette_lifespan() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        calls: list[str] = []
        first = app.add_adapter(RecordingAdapter, calls, "first")
        second = app.add_adapter(RecordingAdapter, calls, "second")
        web = Starlette(lifespan=app.lifespan)

        async with web.router.lifespan_context(web):
            assert app.adapters == (first, second)
            assert calls == ["start:first", "start:second"]
            with pytest.raises(RuntimeError, match="после запуска"):
                app.add_adapter(RecordingAdapter, calls, "late")

        assert calls == [
            "start:first",
            "start:second",
            "close:second",
            "close:first",
        ]

    asyncio.run(scenario())


def test_raw_input_tries_adapters_in_order_and_dispatches_by_annotations() -> None:
    class OtherMetadata(EventMetadata):
        pass

    class ParserAdapter(Adapter):
        def __init__(self, app: App, name: str, calls: list[str], handled: bool) -> None:
            super().__init__(app)
            self.name = name
            self.calls = calls
            self.handled = handled

        async def parse_input(self, raw: dict) -> AdapterInput | None:
            self.calls.append(self.name)
            if not self.handled:
                return None
            return AdapterInput(EchoMetadata(user_id=9), input=raw["text"])

    async def scenario() -> None:
        app = App()
        parser_calls: list[str] = []
        received: list[tuple[int, str]] = []
        app.add_adapter(ParserAdapter, "first", parser_calls, False)
        app.add_adapter(ParserAdapter, "second", parser_calls, True)
        app.add_adapter(ParserAdapter, "third", parser_calls, True)

        @app.handler
        async def echo(app: App, meta: EchoMetadata, input: str) -> None:
            received.append((meta.user_id, input))

        @app.handler
        async def other(app: App, meta: OtherMetadata, input: str) -> None:
            raise AssertionError("несовместимая модель метаданных не должна вызываться")

        assert await app.handle_input({"text": "hello"}) == "ok"
        assert parser_calls == ["first", "second"]
        assert received == [(9, "hello")]
        await app.close()

    asyncio.run(scenario())


def test_interface_lookup_does_not_read_legacy_mount_namespace() -> None:
    async def scenario() -> None:
        storage = MemoryStorage()
        app = App(storage)
        interface_id = {"cmid": 1, "peer_id": 2, "group_id": 3}
        await storage.set(storage_key("mount", interface_id), '{"actions":{},"ttl":null}')
        assert await app.get_interface(interface_id) is None
        await app.close()

    asyncio.run(scenario())
