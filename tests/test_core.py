from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Literal

import pytest
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette

from vkuxt import (
    DEFAULT_TTL,
    Adapter,
    App,
    BaseMetadata,
    Context,
    Input,
    InputData,
    Interface,
    InterfaceHandle,
    JSONFileStorage,
    MemoryStorage,
    StateStorage,
    StopHandlerChain,
    decode_key,
    encode_key,
    storage_key,
)


class AccountKey(BaseModel):
    user_id: int
    scope: str


class EchoMetadata(BaseMetadata):
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


class RedrawAdapter(Adapter):
    def __init__(
        self,
        app: App,
        results: list[InterfaceHandle[Any] | None],
    ) -> None:
        super().__init__(app)
        self.results = results
        self.calls: list[tuple[Interface[Any] | None, InterfaceHandle[Any] | None]] = []

    async def redraw(
        self,
        interface: Interface[Any] | None,
        *,
        target: InterfaceHandle[Any] | None = None,
    ) -> InterfaceHandle[Any] | None:
        self.calls.append((interface, target))
        return self.results.pop(0)


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


def test_handlers_receive_input_data_and_long_payload_tokens_persist(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.json"
        calls: list[EchoInput] = []
        app = App(JSONFileStorage(path))

        @app.handler
        async def echo(
            ctx: Context,
            metadata: EchoMetadata,
            data: EchoInput,
            text: str,
        ) -> None:
            assert isinstance(ctx.app, App)
            assert metadata.user_id == 4
            calls.append(data)

        interface_id = {"cmid": 80, "peer_id": 2000000001, "group_id": 71}
        old_token = "old-token"
        await app._register_interface(
            interface_id,
            {
                old_token: {
                    "data": EchoInput(
                        handler_name="echo-data",
                        text="old",
                        extra="x" * 300,
                    ).model_dump(mode="json")
                }
            },
            ttl=None,
        )
        await app.close()

        restored = App(JSONFileStorage(path))
        restored_adapter = Adapter(restored)
        restored.add_handler(echo, background=False)
        new_token = "new-token"
        await restored._register_interface(
            interface_id,
            {
                new_token: {
                    "data": EchoInput(
                        handler_name="echo-data",
                        text="current",
                        extra="y" * 300,
                    ).model_dump(mode="json")
                }
            },
            ttl=None,
        )

        record = await restored._get_interface_record(interface_id)
        assert record is not None
        assert old_token not in record.actions
        await restored.dispatch_event(
            Input(
                EchoMetadata(user_id=4),
                data=record.actions[new_token]["data"],
            ),
            restored_adapter,
        )
        assert calls[-1].text == "current"
        await restored.close()

    asyncio.run(scenario())


def test_state_storage_models_and_ttl(monkeypatch) -> None:
    class Clock:
        now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr("vkuxt.storage.time.time", clock)

    assert isinstance(MemoryStorage(), StateStorage)
    with pytest.raises(TypeError):
        StateStorage()

    async def scenario() -> None:
        app = App(MemoryStorage(), ttl=0.02)
        key = {"owner": 1, "kind": "counter"}
        await app.set_state(key, StoredState(count=3))
        assert await app.get_state(key, StoredState) == StoredState(count=3)
        clock.now += 0.03
        assert await app.get_state(key) is None

        def unused(ctx: Context, metadata: BaseMetadata, data: None, text: str) -> None:
            return None

        app.add_handler(unused)
        interface_id = {"cmid": 1, "peer_id": 2, "group_id": 3}
        token = "long-payload"
        await app._register_interface(
            interface_id,
            {token: {"data": {"handler_name": "echo-data", "text": "x" * 300}}},
            ttl=0.06,
        )
        infinite_id = {"cmid": 2, "peer_id": 2, "group_id": 3}
        await app._register_interface(infinite_id, {}, ttl=None)
        registered = await app._get_interface_record(interface_id)
        assert registered is not None
        assert registered.actions[token]
        clock.now += 0.03
        assert await app.get_state(key) is None
        assert await app._get_interface_record(interface_id) is not None
        assert await app._get_interface_record(infinite_id) is not None
        clock.now += 0.04
        assert await app._get_interface_record(interface_id) is None
        assert await app._get_interface_record(infinite_id) is not None
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
        first = app.add_adapter(RecordingAdapter(app, calls, "first"))
        second = app.add_adapter(RecordingAdapter(app, calls, "second"))
        web = Starlette(lifespan=app.lifespan)

        async with web.router.lifespan_context(web):
            assert app.adapters == (first, second)
            assert calls == ["start:first", "start:second"]
            late = app.add_adapter(RecordingAdapter(app, calls, "late"))
            assert late in app.adapters
            await app.wait_adapters_started()
            assert calls == ["start:first", "start:second", "start:late"]

        assert calls == [
            "start:first",
            "start:second",
            "start:late",
            "close:late",
            "close:second",
            "close:first",
        ]

    asyncio.run(scenario())


def test_add_adapter_accepts_an_instance_and_checks_ownership() -> None:
    async def scenario() -> None:
        app = App()
        other = App()
        adapter = Adapter(app)

        assert app.add_adapter(adapter) is adapter
        with pytest.raises(ValueError, match="уже добавлен"):
            app.add_adapter(adapter)
        with pytest.raises(ValueError, match="другому App"):
            app.add_adapter(Adapter(other))
        with pytest.raises(TypeError, match="Adapter"):
            app.add_adapter(object())  # type: ignore[arg-type]

        await app.close()
        await other.close()

    asyncio.run(scenario())


def test_raw_input_tries_adapters_in_order_and_passes_input_to_handlers() -> None:
    class OtherMetadata(BaseMetadata):
        pass

    class ParserAdapter(Adapter):
        def __init__(self, app: App, name: str, calls: list[str], handled: bool) -> None:
            super().__init__(app)
            self.name = name
            self.calls = calls
            self.handled = handled

        async def parse_input(self, raw: dict) -> Input | None:
            self.calls.append(self.name)
            if not self.handled:
                return None
            return Input(
                EchoMetadata(user_id=9),
                data={"source": "payload"},
                text=raw["text"],
            )

    async def scenario() -> None:
        app = App()
        parser_calls: list[str] = []
        received: list[tuple[int, str, str]] = []
        seen_by_echo: list[str] = []
        seen_by_other: list[tuple[BaseMetadata, InputData | None, str]] = []
        other_metadata: list[OtherMetadata] = []
        app.add_adapter(ParserAdapter(app, "first", parser_calls, False))
        second_adapter = app.add_adapter(ParserAdapter(app, "second", parser_calls, True))
        app.add_adapter(ParserAdapter(app, "third", parser_calls, True))

        @app.handler(background=False)
        async def echo(
            ctx: Context,
            metadata: EchoMetadata,
            data: InputData,
            text: str,
        ) -> str:
            received.append(
                (
                    metadata.user_id,
                    text,
                    data.model_dump()["source"],
                )
            )
            seen_by_echo.append(text)
            return "ok"

        @app.handler(background=False)
        async def other(
            ctx: Context,
            metadata: BaseMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            seen_by_other.append((metadata, data, text))
            if isinstance(metadata, OtherMetadata):
                other_metadata.append(metadata)

        assert await app.handle_input({"text": "hello"}) == "ok"
        assert parser_calls == ["first", "second"]
        assert received == [(9, "hello", "payload")]
        assert seen_by_echo == ["hello"]
        assert seen_by_other == []
        assert await app.dispatch_event(Input(OtherMetadata()), second_adapter) is None
        assert len(other_metadata) == 1
        assert seen_by_other == [(other_metadata[0], None, "")]
        await app.close()

    asyncio.run(scenario())


def test_handlers_are_indexed_by_metadata_and_validate_data_models() -> None:
    class OtherMetadata(BaseMetadata):
        source: str

    class StartData(InputData):
        action: Literal["start"]
        count: int

    class StopData(InputData):
        action: Literal["stop"]
        reason: str

    async def scenario() -> None:
        app = App()
        adapter = app.add_adapter(Adapter(app))
        received: list[tuple[InputData, BaseMetadata, str | None]] = []
        text_events: list[tuple[BaseMetadata, str]] = []

        @app.handler(background=False)
        async def actions(
            ctx: Context,
            metadata: EchoMetadata | OtherMetadata,
            data: StartData | StopData,
            text: str,
        ) -> None:
            received.append((data, metadata, text))

        @app.handler(background=False)
        async def text_only(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            text_events.append((metadata, text))

        await app.dispatch_event(
            Input(EchoMetadata(user_id=1), data={"action": "start", "count": "3"}, text="go"),
            adapter,
        )
        await app.dispatch_event(
            Input(OtherMetadata(source="other"), data={"action": "stop", "reason": "done"}),
            adapter,
        )
        await app.dispatch_event(
            Input(EchoMetadata(user_id=1), data={"action": "unknown"}),
            adapter,
        )
        await app.dispatch_event(Input(EchoMetadata(user_id=2), text="hello"), adapter)

        assert len(received) == 2
        assert isinstance(received[0][0], StartData)
        assert received[0][0].count == 3
        assert received[0][2] == "go"
        assert isinstance(received[1][0], StopData)
        assert isinstance(received[1][1], OtherMetadata)
        assert len(text_events) == 1
        assert text_events[0][0].user_id == 2
        assert text_events[0][1] == "hello"
        await app.close()

    asyncio.run(scenario())


def test_add_handler_requires_the_context_contract() -> None:
    async def scenario() -> None:
        app = App()

        def handler(*args: object) -> None:
            return None

        with pytest.raises(TypeError, match="четыре"):
            app.add_handler(handler)
        await app.close()

    asyncio.run(scenario())


def test_foreground_handlers_short_circuit_before_background_handlers() -> None:
    async def scenario() -> None:
        app = App()
        adapter = app.add_adapter(Adapter(app))
        calls: list[str] = []

        @app.handler
        async def background(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("background")

        @app.handler(background=False)
        async def first(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("first")

        @app.handler(background=False)
        async def second(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> str:
            calls.append("second")
            return "handled"

        @app.handler(background=False)
        async def third(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("third")

        assert await app.dispatch_event(Input(EchoMetadata(user_id=1)), adapter) == "handled"
        assert calls == ["first", "second"]
        await app.close()

    asyncio.run(scenario())


def test_background_handlers_stop_with_stop_handler_chain() -> None:
    async def scenario() -> None:
        app = App()
        adapter = app.add_adapter(Adapter(app))
        calls: list[str] = []

        @app.handler(background=False)
        async def foreground(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("foreground")

        @app.handler
        async def first(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("first")

        @app.handler
        async def stop(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("stop")
            raise StopHandlerChain

        @app.handler
        async def last(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            calls.append("last")

        assert await app.dispatch_event(Input(EchoMetadata(user_id=1)), adapter) is None
        assert calls == ["foreground"]
        await app.close()
        assert calls == ["foreground", "first", "stop"]

        stopped_app = App()
        stopped_adapter = stopped_app.add_adapter(Adapter(stopped_app))
        stopped_calls: list[str] = []

        @stopped_app.handler(background=False)
        async def stop_foreground(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            stopped_calls.append("stop")
            raise StopHandlerChain

        @stopped_app.handler(background=False)
        async def skipped_foreground(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            stopped_calls.append("skipped")

        @stopped_app.handler
        async def background_after_stop(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            stopped_calls.append("background")

        assert (
            await stopped_app.dispatch_event(
                Input(EchoMetadata(user_id=2)),
                stopped_adapter,
            )
            is None
        )
        assert stopped_calls == ["stop"]
        await stopped_app.close()
        assert stopped_calls == ["stop", "background"]

    asyncio.run(scenario())


def test_interface_lookup_does_not_read_legacy_mount_namespace() -> None:
    async def scenario() -> None:
        storage = MemoryStorage()
        app = App(storage)
        interface_id = {"cmid": 1, "peer_id": 2, "group_id": 3}
        await storage.set(storage_key("mount", interface_id), '{"actions":{},"ttl":null}')
        assert await app._get_interface_record(interface_id) is None
        await app.close()

    asyncio.run(scenario())


def test_nested_keyed_locks_follow_canonical_order() -> None:
    async def scenario() -> None:
        app = App()
        first = {"key": "a"}
        second = {"key": "b"}

        async with app.lock(first):  # noqa: SIM117
            async with app.lock(first, second):
                pass

        with pytest.raises(RuntimeError, match="сортируется раньше"):
            async with app.lock(second):  # noqa: SIM117
                async with app.lock(first, second):
                    pass
        await app.close()

    asyncio.run(scenario())


def test_concurrent_close_waits_for_shutdown() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        class SlowAdapter(Adapter):
            async def close(self) -> None:
                started.set()
                await release.wait()
                calls.append("closed")

        app = App()
        app.add_adapter(SlowAdapter(app))
        await app.start()
        first = asyncio.create_task(app.close())
        await started.wait()
        second = asyncio.create_task(app.close())
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)
        assert calls == ["closed"]

    asyncio.run(scenario())


def test_close_cancels_hanging_background_handler() -> None:
    async def scenario() -> None:
        app = App(close_timeout=0.01)
        adapter = app.add_adapter(Adapter(app))
        started = asyncio.Event()
        cancelled = asyncio.Event()

        @app.handler
        async def hang(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await app.dispatch_event(Input(EchoMetadata(user_id=1)), adapter)
        await started.wait()
        await app.close()
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_failed_adapter_start_is_not_closed() -> None:
    class BrokenAdapter(Adapter):
        closed = False

        async def start(self) -> None:
            raise RuntimeError("start failed")

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        app = App()
        adapter = app.add_adapter(BrokenAdapter(app))
        with pytest.raises(RuntimeError, match="start failed"):
            await app.start()
        assert not adapter.closed
        await app.close()

    asyncio.run(scenario())


def test_close_is_reentrant_from_adapter() -> None:
    class ReentrantAdapter(Adapter):
        async def close(self) -> None:
            await self.app.close()

    async def scenario() -> None:
        app = App()
        app.add_adapter(ReentrantAdapter(app))
        await app.start()
        await asyncio.wait_for(app.close(), timeout=1)

    asyncio.run(scenario())


def test_unresolved_handler_annotation_is_rejected() -> None:
    async def scenario() -> None:
        app = App()

        async def broken(
            ctx: Context,
            metadata: MissingModel,  # type: ignore[name-defined]  # noqa: F821
            data: None,
            text: str,
        ) -> None:
            return None

        with pytest.raises(TypeError, match="Не удалось разрешить"):
            app.add_handler(broken)

        async def wrong_ctx(
            ctx: App,
            metadata: BaseMetadata,
            data: None,
            text: str,
        ) -> None:
            return None

        with pytest.raises(TypeError, match="Context"):
            app.add_handler(wrong_ctx)

        async def wrong_metadata(
            ctx: Context,
            metadata: int,
            data: None,
            text: str,
        ) -> None:
            return None

        with pytest.raises(TypeError, match="BaseMetadata"):
            app.add_handler(wrong_metadata)

        async def wrong_data(
            ctx: Context,
            metadata: BaseMetadata,
            data: int,
            text: str,
        ) -> None:
            return None

        with pytest.raises(TypeError, match="InputData"):
            app.add_handler(wrong_data)

        async def wrong_text(
            ctx: Context,
            metadata: BaseMetadata,
            data: None,
            text: int,
        ) -> None:
            return None

        with pytest.raises(TypeError, match="text"):
            app.add_handler(wrong_text)
        await app.close()

    asyncio.run(scenario())


def test_interface_base_normalizes_rows_and_defaults() -> None:
    view = Interface(text="screen", rows=[[1, 2], [3]])
    assert view.rows == ((1, 2), (3,))
    assert view.ttl is DEFAULT_TTL


def test_interface_handle_state_key_is_stable_across_redraws() -> None:
    first = InterfaceHandle({"cmid": 1}, {"old-token": {"data": {"x": 1}}}, ttl=10)
    second = InterfaceHandle({"cmid": 1}, {"new-token": {"data": {"y": 2}}}, ttl=None)
    assert first.state_key() == second.state_key()
    assert first.state_key() != InterfaceHandle({"cmid": 2}).state_key()


def test_interface_state_key_can_be_used_by_app_state() -> None:
    async def scenario() -> None:
        app = App()
        handle = InterfaceHandle({"cmid": 1})
        await app.set_state(handle.state_key(), {"count": 1})
        assert await app.get_state(handle.state_key(), dict) == {"count": 1}
        await app.close()

    asyncio.run(scenario())


def test_storage_keys_are_versioned_and_support_stable_tags() -> None:
    class StableKey(BaseModel):
        __vkuxt_key_type__: ClassVar[str] = "custom-key"
        value: int

    assert storage_key("state", {"value": 1}).startswith("vkuxt:v1:state:")
    encoded = encode_key(StableKey(value=1))
    assert '"custom-key"' in encoded
    assert decode_key(encoded, StableKey) == StableKey(value=1)


def test_json_storage_rejects_corrupt_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="некорректный JSON"):
        JSONFileStorage(path)


def test_dict_state_can_be_read_explicitly() -> None:
    async def scenario() -> None:
        app = App()
        key = {"owner": 1, "kind": "raw"}
        await app.set_state(key, {"value": 1})
        assert await app.get_state(key, dict) == {"value": 1}
        await app.close()

    asyncio.run(scenario())


def test_handler_context_persists_state_and_is_background_by_default() -> None:
    async def scenario() -> None:
        app = App()
        adapter = app.add_adapter(Adapter(app))
        interface = InterfaceHandle({"cmid": 1})
        await app.set_state(interface.state_key(), {"count": 1})
        started = asyncio.Event()

        @app.handler
        async def increment(
            ctx: Context,
            metadata: EchoMetadata,
            data: None,
            text: str,
        ) -> None:
            assert ctx.app is app
            assert ctx.adapter is adapter
            started.set()
            ctx.state["count"] += 1

        await app.dispatch_event(
            Input(EchoMetadata(user_id=1), interface=interface),
            adapter,
        )
        assert not started.is_set()
        await app.wait_background()
        assert started.is_set()
        assert await app.get_state(interface.state_key(), dict) == {"count": 2}
        await app.close()

    asyncio.run(scenario())


def test_context_state_is_persisted_for_events_without_interface() -> None:
    async def scenario() -> None:
        app = App()
        adapter = app.add_adapter(Adapter(app))

        @app.handler(background=False)
        async def remember(
            ctx: Context,
            metadata: EchoMetadata,
            data: None,
            text: str,
        ) -> None:
            ctx.state["seen"] = text

        metadata = EchoMetadata(user_id=7)
        await app.dispatch_event(Input(metadata, text="hello"), adapter)
        assert await app.get_state(adapter.context_key(metadata), dict) == {"seen": "hello"}
        await app.close()

    asyncio.run(scenario())


def test_context_redraw_moves_state_to_new_interface() -> None:
    async def scenario() -> None:
        app = App()
        old = InterfaceHandle({"cmid": 1})
        new = InterfaceHandle({"cmid": 2})
        adapter = app.add_adapter(RedrawAdapter(app, [new]))
        await app.set_state(old.state_key(), {"count": 5})

        @app.handler(background=False)
        async def advance(
            ctx: Context,
            metadata: EchoMetadata,
            data: None,
            text: str,
        ) -> None:
            ctx.state["count"] = 6
            result = await ctx.redraw(Interface(text="new"))
            assert result is new
            assert ctx.interface is new

        await app.dispatch_event(
            Input(EchoMetadata(user_id=1), interface=old),
            adapter,
        )
        assert adapter.calls == [(Interface(text="new"), old)]
        assert await app.get_state(new.state_key(), dict) == {"count": 6}
        assert await app.get_state(old.state_key(), dict) is None
        await app.close()

    asyncio.run(scenario())


def test_context_redraw_moves_conversation_state_to_interface() -> None:
    async def scenario() -> None:
        app = App()
        new = InterfaceHandle({"cmid": 2})
        adapter = app.add_adapter(RedrawAdapter(app, [new]))
        metadata = EchoMetadata(user_id=3)
        context_key = adapter.context_key(metadata)
        await app.set_state(context_key, {"uid": 3})

        @app.handler(background=False)
        async def show(
            ctx: Context,
            metadata: EchoMetadata,
            data: None,
            text: str,
        ) -> None:
            assert ctx.state == {"uid": 3}
            await ctx.redraw(Interface(text="screen"))

        await app.dispatch_event(Input(metadata), adapter)
        assert await app.get_state(context_key, dict) is None
        assert await app.get_state(new.state_key(), dict) == {"uid": 3}
        await app.close()

    asyncio.run(scenario())


def test_context_redraw_none_deletes_interface_state() -> None:
    async def scenario() -> None:
        app = App()
        interface = InterfaceHandle({"cmid": 1})
        adapter = app.add_adapter(RedrawAdapter(app, [None]))
        await app.set_state(interface.state_key(), {"count": 1})

        @app.handler(background=False)
        async def close_screen(
            ctx: Context,
            metadata: EchoMetadata,
            data: None,
            text: str,
        ) -> None:
            assert await ctx.redraw(None) is None
            assert ctx.interface is None

        await app.dispatch_event(
            Input(EchoMetadata(user_id=1), interface=interface),
            adapter,
        )
        assert await app.get_state(interface.state_key(), dict) is None
        await app.close()

    asyncio.run(scenario())
