from __future__ import annotations

import asyncio
import json
from inspect import isabstract
from typing import Literal

from vkx.server import CallbackServer

from vkuxt import (
    App,
    Input,
    InputData,
    MemoryStorage,
    VKCallbackAdapter,
    VKMessageInterfaceId,
    VKMessageMetadata,
    VKMessagesAdapter,
    VKMessagesCallbackAdapter,
)


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
        return [True]


class FakeVK:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def test_vk_callback_and_messages_adapters_are_abstract() -> None:
    assert isabstract(VKCallbackAdapter)
    assert isabstract(VKMessagesAdapter)


def test_send_registers_the_vk_external_mount_id() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = app.add_adapter(VKMessagesCallbackAdapter, vk, group_id=71)
        assert isinstance(adapter.callback_server, CallbackServer)
        interface_id = await adapter.send(2000000001, "hello", app.new_interface())
        assert interface_id == VKMessageInterfaceId(cmid=91, peer_id=2000000001, group_id=71)
        assert await app.get_interface(interface_id) is not None
        assert vk.messages.sent[0]["peer_ids"] == [interface_id.peer_id]
        await app.close()

    asyncio.run(scenario())


def test_manual_confirmation_is_not_rejected_for_missing_secret() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        app.add_adapter(
            VKMessagesCallbackAdapter,
            FakeVK(),
            group_id=71,
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
        app.add_adapter(VKMessagesCallbackAdapter, vk, group_id=71)
        received: list[
            tuple[VKMessageMetadata, str | None, StartInput | None, VKMessageInterfaceId | None]
        ] = []
        logged: list[tuple[str | None, InputData | None]] = []
        other_calls: list[OtherStartInput] = []

        @app.handler(background=False)
        async def start(
            app: App,
            input: Input,
        ) -> None:
            if not isinstance(input.metadata, VKMessageMetadata):
                return
            data = None
            if input.data is not None:
                try:
                    data = StartInput.model_validate(input.data.model_dump())
                except ValueError:
                    return
            received.append((input.metadata, input.text, data, input.interface_id))

        @app.handler(background=False)
        async def log_input(app: App, input: Input) -> None:
            if isinstance(input.metadata, VKMessageMetadata):
                logged.append((input.text, input.data))

        @app.handler(background=False)
        async def other(app: App, input: Input) -> None:
            if not isinstance(input.metadata, VKMessageMetadata) or input.data is None:
                return
            try:
                other_calls.append(OtherStartInput.model_validate(input.data.model_dump()))
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
        assert received[-1][1] is None
        assert isinstance(received[-1][2], StartInput)
        assert received[-1][2].source == "button"
        assert received[-1][0].peer_id == 2000000001
        assert received[-1][0].group_id == 71
        assert received[-1][0].user_id == 5
        assert received[-1][0].event_type == "message_event"
        assert received[-1][3] is None
        assert isinstance(logged[-1][1], InputData)
        assert logged[-1][1].model_dump()["source"] == "button"
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
        assert received[-1][3] is None
        assert logged[-1] == ("начать", None)
        await app.close()

    asyncio.run(scenario())


def test_only_oversized_button_payloads_are_tokenized() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = app.add_adapter(VKMessagesCallbackAdapter, vk, group_id=71)
        received: list[tuple[StartInput, VKMessageInterfaceId | None]] = []

        @app.handler(background=False)
        async def start(app: App, input: Input) -> None:
            if input.data is None:
                return
            try:
                received.append(
                    (StartInput.model_validate(input.data.model_dump()), input.interface_id)
                )
            except ValueError:
                return

        draft = app.new_interface()
        compact = draft.bind(StartInput(handler_name="begin-action", source="button"))
        unicode_compact = draft.bind(StartInput(handler_name="begin-action", source="я" * 130))
        token = draft.bind(StartInput(handler_name="begin-action", source="x" * 300))
        assert compact == {"handler_name": "begin-action", "source": "button"}
        assert isinstance(unicode_compact, dict)
        assert isinstance(token, str)
        assert draft.actions[token]["data"]["handler_name"] == "begin-action"
        interface_id = await adapter.send(5, "hello", draft)
        callback = {
            "type": "message_event",
            "group_id": 71,
            "object": {
                "user_id": 5,
                "peer_id": interface_id.peer_id,
                "event_id": "event-long",
                "conversation_message_id": interface_id.cmid,
                "payload": token,
            },
        }
        await app.handle_input(callback)
        assert received[-1][0].source == "x" * 300
        assert received[-1][1] == interface_id

        disabled = App(tokenize_long_input=False)
        assert isinstance(
            disabled.new_interface().bind({"handler_name": "begin-action", "source": "x" * 300}),
            dict,
        )
        await disabled.close()
        await app.close()

    asyncio.run(scenario())


def test_callback_ack_is_skipped_when_pressed_interface_is_edited() -> None:
    async def scenario() -> None:
        app = App(MemoryStorage())
        vk = FakeVK()
        adapter = app.add_adapter(VKMessagesCallbackAdapter, vk, group_id=71)

        @app.handler(background=False)
        async def edit(app: App, input: Input) -> None:
            if (
                not isinstance(input.metadata, VKMessageMetadata)
                or not isinstance(input.interface_id, VKMessageInterfaceId)
                or input.data is None
            ):
                return
            try:
                EditInput.model_validate(input.data.model_dump())
            except ValueError:
                return
            await adapter.edit(
                input.interface_id,
                "updated",
                app.new_interface(),
                keyboard='{"buttons":[]}',
            )

        message_id = VKMessageInterfaceId(cmid=10, peer_id=2000000001, group_id=71)
        await app.register_interface(message_id, app.new_interface())
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
