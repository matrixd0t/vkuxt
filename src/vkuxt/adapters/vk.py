from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, PrivateAttr
from vkx.models import AttachmentsInput, Keyboard, Message

from ..app import Adapter, App, BaseMetadata, Input
from ..codec import decode_key, encode_key
from ..interfaces import DEFAULT_TTL, Interface, TTLValue


class VKMessageInterfaceId(BaseModel):
    cmid: int
    peer_id: int
    group_id: int


class VKMessageMetadata(BaseMetadata):
    """Метаданные входящего сообщения VK."""

    event_type: Literal["message_event", "message_new"]
    group_id: int
    peer_id: int
    user_id: int
    msg: Message | None = None
    event_id: str | None = None
    _interface_cmid: int | None = PrivateAttr(default=None)
    _message_cmid: int | None = PrivateAttr(default=None)
    _reply_cmid: int | None = PrivateAttr(default=None)


type VKMessageInput = Input[Any, VKMessageMetadata]


@dataclass(slots=True)
class VKMessageTemplate(Interface):
    """Готовое состояние сообщения VK вместе с данными callback-кнопок."""  # noqa: RUF002

    attachments: AttachmentsInput | None = None
    text: str = ""
    kb: Keyboard | str | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "text":
            if not isinstance(value, str):
                raise TypeError("text шаблона VK должен быть строкой")
            value = value[:4096]
        object.__setattr__(self, name, value)


@dataclass(slots=True)
class _VKDispatchContext:
    metadata: VKMessageMetadata
    interface_id: VKMessageInterfaceId | None
    is_reply: bool
    callback_changed: bool = False


class _VKConfirmationMetadata(BaseMetadata):
    group_id: int


class VKMessagesAdapter(Adapter, ABC):
    """Абстракция операций VK Messages, общая для callback и long poll."""

    def __init__(self, app: App, vk: Any, group_id: int) -> None:
        Adapter.__init__(self, app)
        self._vk = vk
        self.group_id = group_id
        self._peer_lock = asyncio.Lock()
        self._vk_event_context: contextvars.ContextVar[_VKDispatchContext | None] = (
            contextvars.ContextVar(f"vkuxt_vk_event_{id(self)}", default=None)
        )

    @property
    @abstractmethod
    def vk(self) -> Any:
        """VK-клиент с категорией messages."""  # noqa: RUF002

    async def send(
        self,
        peer_id: int,
        text: str,
        interface: Interface,
        *,
        keyboard: Any = None,
        attachments: AttachmentsInput | None = None,
        reply_to: int | None = None,
    ) -> VKMessageInterfaceId:
        response = await self.vk.messages.send(
            peer_ids=[peer_id],
            random_id=secrets.randbelow(2**31),
            group_id=self.group_id,
            message=text,
            keyboard=_keyboard_json(keyboard),
            attachment=attachments,
            reply_to=reply_to,
        )
        try:
            item = response[0][0]
            cmid = _field(item, "conversation_message_id")
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(
                "VK не вернул conversation_message_id для отправленного сообщения"
            ) from exc
        if cmid is None:
            raise RuntimeError("VK вернул ответ без conversation_message_id")
        interface_id = VKMessageInterfaceId(cmid=int(cmid), peer_id=peer_id, group_id=self.group_id)
        await self.app.register_interface(interface_id, interface)
        await self._remember_interface(interface_id)
        return interface_id

    async def edit(
        self,
        interface_id: VKMessageInterfaceId,
        text: str,
        interface: Interface,
        *,
        keyboard: Any = None,
        attachments: AttachmentsInput | None = None,
    ) -> bool:
        changed = await self.vk.messages.edit(
            peer_id=interface_id.peer_id,
            cmid=interface_id.cmid,
            group_id=interface_id.group_id,
            message=text,
            keyboard=_keyboard_json(keyboard, clear_none=True),
            attachment=attachments if attachments is not None else "",
        )
        if changed:
            self._mark_current_interface(interface_id)
            await self.app.register_interface(interface_id, interface)
            await self._remember_interface(interface_id)
        return bool(changed)

    async def delete(self, interface_id: VKMessageInterfaceId) -> bool:
        response = await self.vk.messages.delete(
            cmids=[interface_id.cmid],
            peer_id=interface_id.peer_id,
            group_id=interface_id.group_id,
            delete_for_all=True,
        )
        await self.app.remove_interface(interface_id)
        await self._forget_interface(interface_id)
        self._mark_current_interface(interface_id)
        return bool(response)

    def new_interface(
        self,
        *,
        text: str = "",
        attachments: AttachmentsInput | None = None,
        kb: Keyboard | str | None = None,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> VKMessageTemplate:
        return VKMessageTemplate(
            ttl=ttl,
            tokenize_long_input=self.app.tokenize_long_input,
            attachments=attachments,
            text=text,
            kb=kb,
        )

    def _mark_current_interface(self, interface_id: VKMessageInterfaceId) -> None:
        return None

    async def transition(
        self,
        interface: VKMessageTemplate | None,
        *,
        peer_id: int | None = None,
    ) -> None:
        """Перевести интерфейс пользователя в новое состояние."""
        context = self._vk_event_context.get()
        if context is None:
            if peer_id is None:
                raise RuntimeError("transition() требует VK-событие или peer_id")
            recipient = peer_id
            same_event = False
            current = await self._latest_interface(recipient)
        else:
            recipient = context.metadata.peer_id if peer_id is None else peer_id
            same_event = context.metadata.peer_id == recipient
            current = (
                context.interface_id if same_event else await self._latest_interface(recipient)
            )

        if interface is None:
            if current is not None:
                await self.delete(current)
            return
        if (
            same_event
            and context is not None
            and context.metadata.event_type == "message_new"
            and not context.is_reply
        ):
            current = None

        if (
            same_event
            and context is not None
            and context.metadata.event_type == "message_new"
            and context.is_reply
            and context.metadata._message_cmid is not None
            and current is not None
        ):
            await self.send(
                recipient,
                interface.text,
                interface,
                keyboard=interface.kb,
                attachments=interface.attachments,
                reply_to=context.metadata._message_cmid,
            )
            await self.delete(current)
            return

        if current is not None and (
            not same_event
            or (context is not None and context.metadata.event_type == "message_event")
        ):
            await self.edit(
                current,
                interface.text,
                interface,
                keyboard=interface.kb,
                attachments=interface.attachments,
            )
            return

        await self.send(
            recipient,
            interface.text,
            interface,
            keyboard=interface.kb,
            attachments=interface.attachments,
        )

    async def _remember_interface(self, interface_id: VKMessageInterfaceId) -> None:
        index_key = _peer_index_key(interface_id.group_id, interface_id.peer_id)
        encoded = encode_key(interface_id)
        async with self._peer_lock:
            raw = await self.app.storage.get(index_key)
            interfaces = json.loads(raw) if raw is not None else []
            active = []
            for item in interfaces:
                previous_id = _decode_interface_id(item)
                if await self.app.get_interface(previous_id) is not None:
                    active.append(encode_key(previous_id))
            active = [item for item in active if item != encoded]
            active.append(encoded)
            await self.app.storage.set(index_key, _json(active))

    async def _forget_interface(self, interface_id: VKMessageInterfaceId) -> None:
        index_key = _peer_index_key(interface_id.group_id, interface_id.peer_id)
        async with self._peer_lock:
            raw = await self.app.storage.get(index_key)
            if raw is not None:
                interfaces = []
                for item in json.loads(raw):
                    previous_id = _decode_interface_id(item)
                    if previous_id != interface_id:
                        interfaces.append(encode_key(previous_id))
                if interfaces:
                    await self.app.storage.set(index_key, _json(interfaces))
                else:
                    await self.app.storage.delete(index_key)

    async def _latest_interface(self, peer_id: int) -> VKMessageInterfaceId | None:
        index_key = _peer_index_key(self.group_id, peer_id)
        async with self._peer_lock:
            raw = await self.app.storage.get(index_key)
            if raw is None:
                return None
            interfaces = json.loads(raw)
            live: list[str] = []
            latest = None
            for encoded in interfaces:
                interface_id = _decode_interface_id(encoded)
                if await self.app.get_interface(interface_id) is not None:
                    live.append(encode_key(interface_id))
                    latest = interface_id
            if live:
                await self.app.storage.set(index_key, _json(live))
            else:
                await self.app.storage.delete(index_key)
            return latest


class VKCallbackAdapter(Adapter, ABC):
    """Абстрактная обработка Callback API на типизированных событиях vkx."""

    def __init__(
        self,
        app: App,
        group_id: int,
        *,
        callback_server: Any | None = None,
        confirmation_code: str | None = None,
        secret: str | None = None,
        dm_latest_interface: bool = True,
    ) -> None:
        if callback_server is None:
            try:
                from vkx.server import CallbackServer
            except ImportError as exc:
                raise RuntimeError("Установите vkuxt[vk] для обработки VK Callback API") from exc
            callback_server = CallbackServer(
                group_id,
                secret=secret,
                confirmation_code=confirmation_code,
            )
        Adapter.__init__(self, app)
        self.group_id = group_id
        self.callback_server = callback_server
        self.confirmation_code = confirmation_code
        self.dm_latest_interface = dm_latest_interface
        self._vk_event_context: contextvars.ContextVar[_VKDispatchContext | None] = (
            contextvars.ContextVar(f"vkuxt_vk_event_{id(self)}", default=None)
        )

        async def confirmation(
            app: App,
            input: Input[None, _VKConfirmationMetadata],
        ) -> str | None:
            if input.metadata.group_id != self.group_id:
                return None
            return self.confirmation_code or getattr(
                self.callback_server,
                "confirmation_code",
                "",
            )

        self.app.add_handler(confirmation, background=False)

    async def parse_input(
        self,
        raw: str | bytes | bytearray | Mapping[str, Any],
    ) -> Input | None:
        if _raw_event_type(raw) not in {"confirmation", "message_event", "message_new"}:
            return None
        callback_raw = dict(raw) if isinstance(raw, Mapping) else raw
        if isinstance(callback_raw, bytearray):
            callback_raw = bytes(callback_raw)
        event = self.callback_server.handle(callback_raw)
        if event is None:
            confirmation_code = self.confirmation_code or getattr(
                self.callback_server,
                "confirmation_code",
                "",
            )
            if confirmation_code:
                from vkx.server import parse_callback

                candidate = parse_callback(callback_raw)
                if _event_type(candidate) == "confirmation" and candidate.group_id == self.group_id:
                    return Input(_VKConfirmationMetadata(group_id=self.group_id))
            return None
        event_type = _event_type(event)
        if event_type == "confirmation":
            if event.group_id != self.group_id:
                return None
            return Input(_VKConfirmationMetadata(group_id=self.group_id))
        if event.group_id != self.group_id:
            return None
        if event_type == "message_event":
            return await self._parse_message_event(event)
        if event_type == "message_new":
            return await self._parse_message_new(event)
        return None

    async def _parse_message_event(self, event: Any) -> Input | None:
        obj = event.object
        payload = obj.payload
        cmid = obj.conversation_message_id
        peer_id = obj.peer_id
        user_id = obj.user_id
        event_id = obj.event_id
        if peer_id is None or user_id is None or event_id is None:
            return None

        candidate_interface_id = (
            VKMessageInterfaceId(cmid=int(cmid), peer_id=int(peer_id), group_id=self.group_id)
            if cmid is not None
            else None
        )
        interface_record = (
            await self.app.get_interface(candidate_interface_id)
            if candidate_interface_id is not None
            else None
        )
        metadata = VKMessageMetadata(
            event_type="message_event",
            group_id=self.group_id,
            peer_id=int(peer_id),
            user_id=int(user_id),
            event_id=str(event_id),
        )
        metadata._interface_cmid = int(cmid) if cmid is not None else None
        action_data = None
        if isinstance(payload, str):
            try:
                decoded_payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(decoded_payload, Mapping):
                    payload = decoded_payload
        if isinstance(payload, Mapping):
            action_data = payload
        elif isinstance(payload, str) and interface_record is not None:
            action = interface_record.actions.get(payload)
            if action is not None:
                action_data = action.get("data")
        return Input(
            metadata,
            data=action_data,
        )

    async def _parse_message_new(self, event: Any) -> Input | None:
        message = event.object.message
        if message is None or message.from_id is None or message.peer_id is None:
            return None
        user_id = int(message.from_id)
        peer_id = int(message.peer_id)
        cmid = message.conversation_message_id
        reply = message.reply_message
        reply_cmid = getattr(reply, "conversation_message_id", None)
        metadata = VKMessageMetadata(
            event_type="message_new",
            group_id=self.group_id,
            peer_id=peer_id,
            user_id=user_id,
            msg=message,
        )
        metadata._message_cmid = int(cmid) if cmid is not None else None
        metadata._reply_cmid = int(reply_cmid) if reply_cmid is not None else None
        return Input(metadata, text=message.text or "")

    async def dispatch_input(self, event: Input) -> Any:
        if not isinstance(event.metadata, VKMessageMetadata):
            return await super().dispatch_input(event)
        metadata = event.metadata
        interface_id = None
        is_reply = metadata.event_type == "message_new" and metadata._reply_cmid is not None
        target_cmid = (
            metadata._interface_cmid
            if metadata.event_type == "message_event"
            else metadata._reply_cmid
        )
        if target_cmid is not None:
            candidate = VKMessageInterfaceId(
                cmid=target_cmid,
                peer_id=metadata.peer_id,
                group_id=metadata.group_id,
            )
            if await self.app.get_interface(candidate) is not None:
                interface_id = candidate
        elif (
            metadata.event_type == "message_new"
            and self.dm_latest_interface
            and metadata.peer_id == metadata.user_id
        ):
            interface_id = await self._latest_interface(metadata.peer_id)

        context = _VKDispatchContext(metadata, interface_id, is_reply)
        context_token = self._vk_event_context.set(context)
        try:
            return await super().dispatch_input(event)
        finally:
            try:
                if (
                    metadata.event_type == "message_event"
                    and not context.callback_changed
                    and metadata.event_id is not None
                ):
                    await self._answer_callback(
                        metadata.event_id,
                        metadata.peer_id,
                        metadata.user_id,
                    )
            finally:
                self._vk_event_context.reset(context_token)

    def _mark_current_interface(self, interface_id: VKMessageInterfaceId) -> None:
        context = self._vk_event_context.get()
        if context is not None and context.interface_id == interface_id:
            context.callback_changed = True

    @abstractmethod
    async def _answer_callback(self, event_id: str, peer_id: int, user_id: int) -> None:
        """Завершить обработку callback-события средствами VK Messages."""

    @abstractmethod
    async def _latest_interface(self, peer_id: int) -> VKMessageInterfaceId | None:
        """Найти последний активный интерфейс в личном диалоге."""


class VKMessagesCallbackAdapter(VKCallbackAdapter, VKMessagesAdapter):
    """Готовое сочетание Callback API и VK Messages."""

    def __init__(
        self,
        app: App,
        vk: Any,
        group_id: int,
        *,
        callback_server: Any | None = None,
        confirmation_code: str | None = None,
        secret: str | None = None,
        dm_latest_interface: bool = True,
    ) -> None:
        VKMessagesAdapter.__init__(self, app, vk, group_id)
        VKCallbackAdapter.__init__(
            self,
            app,
            group_id,
            callback_server=callback_server,
            confirmation_code=confirmation_code,
            secret=secret,
            dm_latest_interface=dm_latest_interface,
        )

    @property
    def vk(self) -> Any:
        return self._vk

    async def _answer_callback(self, event_id: str, peer_id: int, user_id: int) -> None:
        await self.vk.messages.send_message_event_answer(
            event_id=event_id,
            peer_id=peer_id,
            user_id=user_id,
        )

    async def _latest_interface(self, peer_id: int) -> VKMessageInterfaceId | None:
        return await VKMessagesAdapter._latest_interface(self, peer_id)


def _event_type(event: Any) -> str:
    value = event.type
    return str(getattr(value, "value", value))


def _raw_event_type(raw: Any) -> str | None:
    try:
        if isinstance(raw, (bytes, bytearray, str)):
            raw = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("type")
    return str(value) if isinstance(value, str) else None


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _keyboard_json(keyboard: Any, *, clear_none: bool = False) -> str | None:
    if keyboard is None:
        return '{"buttons":[]}' if clear_none else None
    if isinstance(keyboard, str):
        return keyboard
    to_json = getattr(keyboard, "to_json", None)
    if to_json is None:
        raise TypeError(
            "keyboard должен быть JSON-строкой или объектом с to_json()"  # noqa: RUF001
        )
    return to_json()


def _peer_index_key(group_id: int, peer_id: int) -> str:
    return f"vkuxt:vk-peer-interfaces:{group_id}:{peer_id}"


def _decode_interface_id(encoded: str) -> VKMessageInterfaceId:
    return decode_key(encoded, VKMessageInterfaceId)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
