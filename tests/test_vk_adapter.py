from __future__ import annotations

import asyncio
import json
from inspect import isabstract
from types import SimpleNamespace
from typing import Literal

import pytest
from vkx.models import Button, Message
from vkx.server import CallbackServer

from vkuxt import (
    App,
    BaseMetadata,
    Context,
    Input,
    InputData,
    JSONFileStorage,
    KeyboardButtonColor,
    MemoryStorage,
    StateNamespace,
    VKCallbackAdapter,
    VKInterface,
    VKMessageInterface,
    VKMessageInterfaceId,
    VKMessageMetadata,
    VKMessagesAdapter,
    VKMessagesCallbackAdapter,
)
from vkuxt.adapters.vk import KeyboardButtonColor as AdapterColor
from vkuxt.adapters.vk import _peer_index_key


def test_vk_adapter_reexports_keyboard_button_color() -> None:
    assert AdapterColor is KeyboardButtonColor


class StartInput(InputData):
    handler_name: Literal["begin-action"]
    source: str


class EditInput(InputData):
    handler_name: Literal["edit-action"]
    reason: str


class OtherStartInput(InputData):
    handler_name: Literal["other"]
    source: str


class FakeMessages:
    def __init__(self) -> None:
        self.answers: list[dict] = []
        self.edits: list[dict] = []
        self.sent: list[dict] = []
        self.deletions: list[dict] = []

    async def send(self, **kwargs) -> list[list[dict[str, int]]]:
        self.sent.append(kwargs)
        cmid = 90 + len(self.sent)
        return [[{"conversation_message_id": cmid}]]

    async def send_message_event_answer(self, **kwargs) -> None:
        self.answers.append(kwargs)

    async def edit(self, **kwargs) -> bool:
        self.edits.append(kwargs)
        return True

    async def delete(self, **kwargs) -> list[bool]:
        self.deletions.append(kwargs)
        return [True]


class FakeVK:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def add_vk_adapter(app: App, vk: FakeVK, **kwargs) -> VKMessagesCallbackAdapter:
    return app.add_adapter(VKMessagesCallbackAdapter(app, vk, group_id=71, **kwargs))


def test_vk_callback_and_messages_adapters_are_abstract() -> None:
    assert isabstract(VKCallbackAdapter)
    assert isabstract(VKMessagesAdapter)


def test_send_registers_the_vk_external_mount_id() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        assert isinstance(adapter.callback_server, CallbackServer)
        interface = await adapter.send(2000000001, VKInterface(text="hello"))
        assert interface.id == VKMessageInterfaceId(
            cmid=91,
            peer_id=2000000001,
            group_id=71,
        )
        assert (
            await app._get_interface_record(
                interface.id,
                platform=adapter.platform,
            )
            is not None
        )
        assert vk.messages.sent[0]["peer_ids"] == [interface.id.peer_id]
        await app.close()

    asyncio.run(scenario())


def test_delete_treats_already_deleted_message_as_success() -> None:
    class MissingMessages(FakeMessages):
        async def delete(self, **kwargs) -> list[SimpleNamespace]:
            self.deletions.append(kwargs)
            return [
                SimpleNamespace(
                    response=None,
                    error=SimpleNamespace(code=15),
                )
            ]

    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        vk.messages = MissingMessages()
        adapter = add_vk_adapter(app, vk)
        interface = await adapter.send(5, VKInterface(text="prompt"))

        assert await adapter.delete(interface) is True
        assert interface.deleted is True
        assert (
            await app._get_interface_record(
                interface.id,
                platform=adapter.platform,
            )
            is None
        )
        assert await app.get_state(interface.state_key()) is None
        await app.close()

    asyncio.run(scenario())


def test_duplicate_message_new_is_ignored_by_vkx_server() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        add_vk_adapter(app, vk)
        received: list[VKMessageMetadata] = []

        @app.handler(background=False)
        async def handler(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            received.append(metadata)

        payload = {
            "type": "message_new",
            "group_id": 71,
            "object": {
                "message": {
                    "from_id": 5,
                    "peer_id": 5,
                    "conversation_message_id": 10,
                    "date": 1,
                    "id": 10,
                    "version": 10000000,
                    "out": 0,
                    "text": "duplicate",
                }
            },
        }
        await app.handle_input(payload)
        await app.handle_input(payload)

        assert len(received) == 1
        assert vk.messages.answers == []
        await app.close()

    asyncio.run(scenario())


def test_duplicate_message_event_is_ignored_by_vkx_server() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        add_vk_adapter(app, vk)
        received: list[VKMessageMetadata] = []

        @app.handler(background=False)
        async def handler(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            received.append(metadata)

        payload = {
            "type": "message_event",
            "group_id": 71,
            "object": {
                "user_id": 5,
                "peer_id": 2000000001,
                "event_id": "event-duplicate",
                "conversation_message_id": 10,
                "payload": json.dumps({"handler_name": "begin-action", "source": "button"}),
            },
        }
        await app.handle_input(payload)
        await app.handle_input(payload)

        assert len(received) == 1
        assert len(vk.messages.answers) == 1
        await app.close()

    asyncio.run(scenario())


def test_ack_failure_does_not_mask_handler_error() -> None:
    class FailingAnswerMessages(FakeMessages):
        async def send_message_event_answer(self, **kwargs) -> None:
            raise RuntimeError("ack failed")

    class FailingAnswerVK(FakeVK):
        def __init__(self) -> None:
            self.messages = FailingAnswerMessages()

    async def scenario() -> None:
        app = App(MemoryStorage())
        adapter = add_vk_adapter(app, FailingAnswerVK())

        @app.handler(background=False)
        async def fail(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            raise ValueError("handler failed")

        metadata = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=5,
            user_id=5,
            event_id="event-error",
        )
        metadata._interface_cmid = None
        with pytest.raises(ValueError, match="handler failed"):
            await adapter.dispatch_input(Input(metadata))
        await app.close()

    asyncio.run(scenario())


def test_failed_edit_falls_back_to_new_message() -> None:
    class FailingEditMessages(FakeMessages):
        async def edit(self, **kwargs) -> bool:
            self.edits.append(kwargs)
            raise RuntimeError("edit failed")

    class FailingEditVK(FakeVK):
        def __init__(self) -> None:
            self.messages = FailingEditMessages()

    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FailingEditVK()
        adapter = add_vk_adapter(app, vk)

        @app.handler(background=False)
        async def advance(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            await ctx.redraw(VKInterface(text="replacement"))

        old = await adapter.send(5, VKInterface(text="old"))
        metadata = VKMessageMetadata(
            event_type="message_event",
            group_id=71,
            peer_id=5,
            user_id=5,
            event_id="event-edit-failed",
        )
        metadata._interface_cmid = old.id.cmid
        await adapter.dispatch_input(Input(metadata))

        assert len(vk.messages.edits) == 1
        assert len(vk.messages.sent) == 2
        assert vk.messages.sent[-1]["message"] == "replacement"
        assert len(vk.messages.answers) == 1
        await app.close()

    asyncio.run(scenario())


def test_peer_interface_index_is_bounded() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        for _ in range(15):
            await adapter.send(5, VKInterface(text="x"))

        raw = await app.storage.get(_peer_index_key(71, 5))
        assert raw is not None
        assert len(json.loads(raw)) == 10
        await app.close()

    asyncio.run(scenario())


def test_manual_confirmation_is_not_rejected_for_missing_secret() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        add_vk_adapter(
            app,
            FakeVK(),
            confirmation_code="confirm-me",
            secret="callback-secret",
        )
        assert await app.handle_input({"type": "confirmation", "group_id": 71}) == "confirm-me"
        await app.close()

    asyncio.run(scenario())


def test_same_handler_receives_typed_callback_and_plain_text() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        add_vk_adapter(app, vk)
        received: list[tuple[VKMessageMetadata, str | None, StartInput | None]] = []
        logged: list[tuple[str | None, InputData | None]] = []
        other_calls: list[OtherStartInput] = []

        @app.handler(background=False)
        async def start(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: StartInput | None,
            text: str,
        ) -> None:
            received.append((metadata, text, data))

        @app.handler(background=False)
        async def log_input(
            ctx: Context,
            metadata: BaseMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            logged.append((text, data))

        @app.handler(background=False)
        async def other(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            if data is None:
                return
            try:
                other_calls.append(OtherStartInput.model_validate(data.model_dump()))
            except ValueError:
                return

        click = {
            "type": "message_event",
            "group_id": 71,
            "object": {
                "user_id": 5,
                "peer_id": 2000000001,
                "event_id": "event-1",
                "conversation_message_id": 10,
                "payload": json.dumps({"handler_name": "begin-action", "source": "button"}),
            },
        }
        assert await app.handle_input(click) is None
        assert received[-1][1] == ""
        assert isinstance(received[-1][2], StartInput)
        assert received[-1][2].source == "button"
        assert received[-1][0].peer_id == 2000000001
        assert received[-1][0].group_id == 71
        assert received[-1][0].user_id == 5
        assert received[-1][0].event_type == "message_event"
        assert received[-1][0].msg is None
        assert "cmid" not in received[-1][0].model_dump()
        assert isinstance(logged[-1][1], InputData)
        assert logged[-1][1].model_dump()["source"] == "button"
        assert logged[-1][0] == ""
        assert other_calls == []
        assert len(vk.messages.answers) == 1

        message = {
            "type": "message_new",
            "group_id": 71,
            "object": {
                "message": {
                    "from_id": 5,
                    "peer_id": 5,
                    "conversation_message_id": 11,
                    "date": 1,
                    "id": 11,
                    "version": 10000000,
                    "out": 0,
                    "text": "начать",
                }
            },
        }
        assert await app.handle_input(message) is None
        assert received[-1][1] == "начать"
        assert received[-1][2] is None
        assert received[-1][0].peer_id == 5
        assert received[-1][0].event_type == "message_new"
        assert isinstance(received[-1][0].msg, Message)
        assert received[-1][0].msg.text == "начать"
        assert "cmid" not in received[-1][0].model_dump()
        assert logged[-1] == ("начать", None)
        await app.close()

    asyncio.run(scenario())


def test_only_oversized_button_payloads_are_tokenized() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        received: list[StartInput] = []

        @app.handler(background=False)
        async def start(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: StartInput,
            text: str,
        ) -> None:
            received.append(data)

        compact = StartInput(handler_name="begin-action", source="button")
        unicode_compact = StartInput(handler_name="begin-action", source="я" * 100)
        unicode_long = StartInput(handler_name="begin-action", source="я" * 130)
        long = StartInput(handler_name="begin-action", source="x" * 300)
        interface = await adapter.send(
            5,
            VKInterface(
                text="hello",
                kb=[
                    [VKInterface.callback("compact", compact)],
                    [VKInterface.callback("unicode-compact", unicode_compact)],
                    [VKInterface.callback("unicode-long", unicode_long)],
                    [VKInterface.callback("long", long)],
                ],
            ),
        )
        assert sorted(action["data"]["source"] for action in interface.actions.values()) == sorted(
            ["я" * 130, "x" * 300]
        )
        keyboard = json.loads(vk.messages.sent[0]["keyboard"])
        payloads = [row[0]["action"]["payload"] for row in keyboard["buttons"]]
        assert json.loads(payloads[0]) == compact.model_dump(mode="json")
        assert json.loads(payloads[1]) == unicode_compact.model_dump(mode="json")
        assert payloads[2] in interface.actions
        assert payloads[3] in interface.actions
        token = payloads[3]
        callback = {
            "type": "message_event",
            "group_id": 71,
            "object": {
                "user_id": 5,
                "peer_id": interface.id.peer_id,
                "event_id": "event-long",
                "conversation_message_id": interface.id.cmid,
                "payload": token,
            },
        }
        await app.handle_input(callback)
        assert received[-1].source == "x" * 300

        disabled = App(tokenize_long_input=False)
        disabled_vk = FakeVK()
        disabled_adapter = add_vk_adapter(disabled, disabled_vk)
        with pytest.raises(ValueError, match="tokenize_long_input"):
            await disabled_adapter.send(
                5,
                VKInterface(
                    text="hello",
                    kb=[[VKInterface.callback("long", long)]],
                ),
            )
        await disabled.close()
        await app.close()

    asyncio.run(scenario())


def test_auto_tokenized_payload_survives_restart(tmp_path) -> None:
    path = tmp_path / "state.json"

    async def first_process() -> VKMessageInterface:
        app = App(JSONFileStorage(path))
        adapter = add_vk_adapter(app, FakeVK())
        interface = await adapter.send(
            5,
            VKInterface(
                text="press",
                kb=[
                    [
                        VKInterface.callback(
                            "go",
                            StartInput(handler_name="begin-action", source="x" * 300),
                        )
                    ]
                ],
            ),
        )
        await app.close()
        return interface

    interface = asyncio.run(first_process())
    token = next(iter(interface.actions))

    async def second_process() -> None:
        app = App(JSONFileStorage(path))
        vk = FakeVK()
        add_vk_adapter(app, vk)
        received: list[StartInput] = []

        @app.handler(background=False)
        async def start(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: StartInput,
            text: str,
        ) -> None:
            received.append(data)

        await app.handle_input(
            {
                "type": "message_event",
                "group_id": 71,
                "object": {
                    "user_id": 5,
                    "peer_id": 5,
                    "event_id": "event-restart",
                    "conversation_message_id": interface.id.cmid,
                    "payload": token,
                },
            }
        )
        assert received[0].source == "x" * 300
        assert len(vk.messages.answers) == 1
        await app.close()

    asyncio.run(second_process())


def test_callback_ack_is_skipped_when_pressed_interface_is_edited() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)

        @app.handler(background=False)
        async def edit(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: EditInput,
            text: str,
        ) -> None:
            await ctx.redraw(VKInterface(text="updated"))

        message = await adapter.send(2000000001, VKInterface(text="initial"))
        message_id = message.id
        payload = {
            "type": "message_event",
            "group_id": 71,
            "object": {
                "user_id": 5,
                "peer_id": message_id.peer_id,
                "event_id": "event-1",
                "conversation_message_id": message_id.cmid,
                "payload": json.dumps({"handler_name": "edit-action", "reason": "button"}),
            },
        }
        await app.handle_input(payload)
        assert len(vk.messages.edits) == 1
        assert vk.messages.answers == []

        payload["object"]["payload"] = "stale-token"
        payload["object"]["event_id"] = "event-2"
        await app.handle_input(payload)
        assert [answer["event_id"] for answer in vk.messages.answers] == ["event-2"]
        await app.close()

    asyncio.run(scenario())


def test_vk_adapter_handler_filters_non_vk_metadata() -> None:
    class OtherMetadata(BaseMetadata):
        pass

    async def scenario() -> None:
        app = App()
        adapter = add_vk_adapter(app, FakeVK())
        received: list[VKMessageMetadata] = []

        @app.handler(background=False)
        async def handler(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            received.append(metadata)

        await app.dispatch_event(Input(OtherMetadata()), adapter)
        assert received == []

        await adapter.dispatch_input(
            Input(
                VKMessageMetadata(
                    event_type="message_new",
                    group_id=71,
                    peer_id=5,
                    user_id=5,
                ),
                text="hello",
            )
        )
        assert len(received) == 1
        assert isinstance(received[0], VKMessageMetadata)
        assert not hasattr(received[0], "cmid")
        await app.close()

    asyncio.run(scenario())


def test_redraw_replies_to_text_and_replaces_replied_interface() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        old = await adapter.send(5, VKInterface(text="prompt"))

        @app.handler(background=False)
        async def advance(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            await ctx.redraw(
                VKInterface(
                    text="replacement",
                    attachments=["photo1_2", "doc3_4"],
                )
            )

        metadata = VKMessageMetadata(
            event_type="message_new",
            group_id=71,
            peer_id=5,
            user_id=5,
        )
        metadata._message_cmid = 44
        metadata._reply_cmid = old.id.cmid
        await adapter.dispatch_input(Input(metadata, text="next"))

        sent = vk.messages.sent[-1]
        assert sent["message"] == "replacement"
        assert sent["attachment"] == ["photo1_2", "doc3_4"]
        assert sent["keyboard"] is None
        assert json.loads(sent["forward"]) == {
            "peer_id": 5,
            "conversation_message_ids": [44],
            "is_reply": True,
        }
        assert "reply_to" not in sent
        assert vk.messages.deletions[-1]["cmids"] == [old.id.cmid]
        assert (
            await app._get_interface_record(
                old.id,
                platform=adapter.platform,
            )
            is None
        )
        assert (
            await app._get_interface_record(
                VKMessageInterfaceId(cmid=92, peer_id=5, group_id=71),
                platform=adapter.platform,
            )
            is not None
        )
        await app.close()

    asyncio.run(scenario())


def test_redraw_without_existing_interface_sends_new_message() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)

        @app.handler(background=False)
        async def show(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            await ctx.redraw(VKInterface(text="new screen"))

        metadata = VKMessageMetadata(
            event_type="message_new",
            group_id=71,
            peer_id=5,
            user_id=5,
        )
        metadata._message_cmid = 22
        await adapter.dispatch_input(Input(metadata, text="start"))

        assert len(vk.messages.sent) == 1
        assert vk.messages.sent[0]["message"] == "new screen"
        assert vk.messages.sent[0]["forward"] is None
        assert vk.messages.edits == []
        assert vk.messages.deletions == []
        await app.close()

    asyncio.run(scenario())


def test_redraw_replaces_latest_interface_attached_to_plain_dm_text() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        old = await adapter.send(5, VKInterface(text="prompt"))

        @app.handler(background=False)
        async def advance(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: InputData | None,
            text: str,
        ) -> None:
            assert ctx.interface is not None
            assert ctx.interface.id == old.id
            await ctx.redraw(VKInterface(text="replacement"))

        metadata = VKMessageMetadata(
            event_type="message_new",
            group_id=71,
            peer_id=5,
            user_id=5,
        )
        metadata._message_cmid = 44
        await adapter.dispatch_input(Input(metadata, text="13"))

        sent = vk.messages.sent[-1]
        assert sent["message"] == "replacement"
        assert sent["forward"] is None
        assert vk.messages.deletions[-1]["cmids"] == [old.id.cmid]
        assert (
            await app._get_interface_record(
                old.id,
                platform=adapter.platform,
            )
            is None
        )
        new_id = VKMessageInterfaceId(cmid=92, peer_id=5, group_id=71)
        assert (
            await app._get_interface_record(
                new_id,
                platform=adapter.platform,
            )
            is not None
        )
        assert await app.get_state(old.state_key()) is None
        assert (
            await app.get_state(
                VKMessageInterface(new_id, platform=adapter.platform).state_key()
            )
            is not None
        )
        await app.close()

    asyncio.run(scenario())


def test_redraw_targets_a_concrete_interface_and_returns_handle() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        first = await adapter.send(5, VKInterface(text="first"))
        second = await adapter.send(6, VKInterface(text="second"))

        with pytest.raises(ValueError, match="target или peer_id"):
            await adapter.redraw(VKInterface(text="ambiguous"), target=second, peer_id=5)

        updated = await adapter.redraw(
            VKInterface(text="updated second"),
            target=second,
        )

        assert updated is second
        assert vk.messages.edits[-1]["peer_id"] == 6
        assert vk.messages.edits[-1]["cmid"] == second.id.cmid
        assert vk.messages.edits[-1]["message"] == "updated second"
        assert vk.messages.deletions == []
        assert (
            await app._get_interface_record(
                first.id,
                platform=adapter.platform,
            )
            is not None
        )
        await app.close()

    asyncio.run(scenario())


def test_redraw_uses_latest_interface_for_given_peer() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        await adapter.send(5, VKInterface(text="first"))
        latest = await adapter.send(5, VKInterface(text="second"))

        found = await adapter.latest_interface(5)
        assert found is not None
        assert found.id == latest.id

        await adapter.redraw(VKInterface(text="third"), peer_id=5)
        assert vk.messages.edits[-1]["cmid"] == latest.id.cmid
        assert vk.messages.sent[-1]["message"] == "second"
        await app.close()

    asyncio.run(scenario())


def test_handler_receives_source_interface_object() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        source = await adapter.send(
            5,
            VKInterface(
                text="choose",
                kb=[[VKInterface.callback("edit", StartInput(
                    handler_name="begin-action",
                    source="button",
                ))]],
            ),
        )
        received: list[VKMessageInterface] = []

        @app.handler(background=False)
        async def edit(
            ctx: Context,
            metadata: VKMessageMetadata,
            data: StartInput,
            text: str,
        ) -> None:
            assert ctx.interface is not None
            received.append(ctx.interface)
            await ctx.redraw(VKInterface(text="edited"))

        await app.handle_input(
            {
                "type": "message_event",
                "group_id": 71,
                "object": {
                    "user_id": 5,
                    "peer_id": source.id.peer_id,
                    "event_id": "event-source",
                    "conversation_message_id": source.id.cmid,
                    "payload": json.dumps(
                        {"handler_name": "begin-action", "source": "button"}
                    ),
                },
            }
        )
        assert received[0].id == source.id
        assert vk.messages.edits[-1]["cmid"] == source.id.cmid
        assert vk.messages.answers == []
        await app.close()

    asyncio.run(scenario())


def test_prebuilt_interface_is_not_mutated_and_can_be_reused() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)
        view = VKInterface(
            text="static",
            kb=[[VKInterface.callback("go", {"handler_name": "begin-action", "source": "x" * 300})]],
        )
        original_payload = view.kb[0][0].action.payload

        first = await adapter.send(5, view)
        second = await adapter.send(6, view)

        assert view.kb[0][0].action.payload == original_payload
        assert first.actions != second.actions
        first_keyboard = json.loads(vk.messages.sent[0]["keyboard"])
        second_keyboard = json.loads(vk.messages.sent[1]["keyboard"])
        first_token = first_keyboard["buttons"][0][0]["action"]["payload"]
        second_token = second_keyboard["buttons"][0][0]["action"]["payload"]
        assert first_token in first.actions
        assert second_token in second.actions
        await app.close()

    asyncio.run(scenario())


def test_vk_interface_validates_transport_limits() -> None:
    with pytest.raises(ValueError, match="4096"):
        VKInterface(text="x" * 4097)
    with pytest.raises(ValueError, match="6 рядов"):
        VKInterface(kb=[[VKInterface.callback(str(i))] for i in range(7)])
    with pytest.raises(ValueError, match="пустой"):
        VKInterface(kb=[[]])
    with pytest.raises(ValueError, match="5 кнопок"):
        VKInterface(kb=[[VKInterface.callback(str(i)) for i in range(6)]])
    with pytest.raises(TypeError, match="KeyboardButton"):
        VKInterface(kb=[["not-a-button"]])  # type: ignore[list-item]


def test_vk_callback_accepts_only_known_colors() -> None:
    positive = VKInterface.callback("ok", color="positive")
    assert positive.color is KeyboardButtonColor.POSITIVE
    negative = VKInterface.callback("no", color=KeyboardButtonColor.NEGATIVE)
    assert negative.color is KeyboardButtonColor.NEGATIVE
    with pytest.raises(ValueError, match="допустимо"):
        VKInterface.callback("bad", color="red")
    with pytest.raises(ValueError, match="допустимо"):
        VKInterface.callback("bad", color="NOT_SUPPORTED")
    with pytest.raises(ValueError, match="допустимо"):
        VKInterface(kb=[[Button.callback("bad", color="red")]])


def test_redraw_revalidates_a_mutated_interface() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        adapter = add_vk_adapter(app, FakeVK())
        view = VKInterface(text="ok")
        view.text = "x" * 4097
        with pytest.raises(ValueError, match="4096"):
            await adapter.redraw(view, peer_id=5)
        assert await app.storage.get(_peer_index_key(71, 5)) is None
        await app.close()

    asyncio.run(scenario())


def test_send_and_redraw_persist_passed_state() -> None:
    class ScreenState(StateNamespace):
        phase: Literal["board"] = "board"
        seat: int

    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = add_vk_adapter(app, vk)

        sent = await adapter.send(
            5,
            VKInterface(text="initial", ttl=120),
            state=ScreenState(seat=0),
        )
        assert await app.get_state(sent.state_key(), ScreenState) == ScreenState(seat=0)

        updated = await adapter.redraw(
            VKInterface(text="updated", ttl=120),
            peer_id=5,
            state=ScreenState(seat=1),
        )
        assert updated is not None
        assert updated.id == sent.id
        assert await app.get_state(updated.state_key(), ScreenState) == ScreenState(seat=1)

        with pytest.raises(ValueError, match="удалении интерфейса"):
            await adapter.redraw(None, peer_id=5, state=ScreenState(seat=0))

        await app.close()

    asyncio.run(scenario())
