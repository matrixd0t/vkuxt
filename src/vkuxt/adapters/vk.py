from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from ..app import Adapter, AdapterInput, App, EventMetadata
from ..codec import decode_key, encode_key
from ..mounts import InterfaceDraft


class VKMessageMountId(BaseModel):
    cmid: int
    peer_id: int
    group_id: int


class VKMessageMetadata(EventMetadata):
    """Метаданные входящего сообщения VK."""

    group_id: int
    chat_id: int
    user_id: int
    cmid: int | None = None
    event_id: str | None = None
    interface_id: VKMessageMountId | None = None

    @property
    def peer_id(self) -> int:
        return self.chat_id


class VKMessagesAdapter(Adapter, ABC):
    """Абстракция операций VK Messages, общая для callback и long poll."""

    def __init__(self, app: App, vk: Any, group_id: int) -> None:
        Adapter.__init__(self, app)
        self._vk = vk
        self.group_id = group_id
        self._peer_lock = asyncio.Lock()

    @property
    @abstractmethod
    def vk(self) -> Any:
        """VK-клиент с категорией messages."""  # noqa: RUF002

    async def send(
        self,
        peer_id: int,
        text: str,
        interface: InterfaceDraft,
        *,
        keyboard: Any = None,
    ) -> VKMessageMountId:
        response = await self.vk.messages.send(
            peer_ids=[peer_id],
            random_id=secrets.randbelow(2**31),
            group_id=self.group_id,
            message=text,
            keyboard=_keyboard_json(keyboard),
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
        interface_id = VKMessageMountId(cmid=int(cmid), peer_id=peer_id, group_id=self.group_id)
        await self.app.register_interface(interface_id, interface)
        await self._remember_interface(interface_id)
        return interface_id

    async def edit(
        self,
        interface_id: VKMessageMountId,
        text: str,
        interface: InterfaceDraft,
        *,
        keyboard: Any = None,
    ) -> bool:
        changed = await self.vk.messages.edit(
            peer_id=interface_id.peer_id,
            cmid=interface_id.cmid,
            group_id=interface_id.group_id,
            message=text,
            keyboard=_keyboard_json(keyboard, clear_none=True),
        )
        if changed:
            self._mark_current_interface(interface_id)
            await self.app.register_interface(interface_id, interface)
            await self._remember_interface(interface_id)
        return bool(changed)

    async def delete(self, interface_id: VKMessageMountId) -> bool:
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

    async def _remember_interface(self, interface_id: VKMessageMountId) -> None:
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

    async def _forget_interface(self, interface_id: VKMessageMountId) -> None:
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

    async def _latest_interface(self, peer_id: int) -> VKMessageMountId | None:
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

    def _mark_current_interface(self, interface_id: VKMessageMountId) -> None:
        return None


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
        dm_latest_mount: bool = True,
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
        self.dm_latest_mount = dm_latest_mount
        self._callback_mount: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"vkuxt_vk_mount_{id(self)}", default=None
        )
        self._callback_changed: contextvars.ContextVar[bool] = contextvars.ContextVar(
            f"vkuxt_vk_changed_{id(self)}", default=False
        )

    async def handle(self, raw: bytes | bytearray | str | dict[str, Any]) -> str:
        response = await self.app.handle_input(bytes(raw) if isinstance(raw, bytearray) else raw)
        return response if isinstance(response, str) else "ok"

    async def parse_input(
        self,
        raw: str | bytes | bytearray | Mapping[str, Any],
    ) -> AdapterInput | None:
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
                    return AdapterInput(None, None, response=confirmation_code)
            return None
        event_type = _event_type(event)
        if event_type == "confirmation":
            confirmation_code = (
                self.confirmation_code or getattr(self.callback_server, "confirmation_code", "")
            )
            return AdapterInput(None, None, response=confirmation_code or "")
        if event.group_id != self.group_id:
            return None
        if event_type == "message_event":
            return await self._parse_message_event(event)
        if event_type == "message_new":
            return await self._parse_message_new(event)
        return AdapterInput(None, None)

    async def _parse_message_event(self, event: Any) -> AdapterInput:
        obj = event.object
        payload = obj.payload
        cmid = obj.conversation_message_id
        peer_id = obj.peer_id
        user_id = obj.user_id
        event_id = obj.event_id
        if peer_id is None or user_id is None or event_id is None:
            return AdapterInput(None, None)

        interface_id = (
            VKMessageMountId(cmid=int(cmid), peer_id=int(peer_id), group_id=self.group_id)
            if cmid is not None
            else None
        )
        metadata = VKMessageMetadata(
            group_id=self.group_id,
            chat_id=int(peer_id),
            user_id=int(user_id),
            cmid=int(cmid) if cmid is not None else None,
            event_id=str(event_id),
            interface_id=interface_id,
        )
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
        elif isinstance(payload, str) and interface_id is not None:
            record = await self.app.get_interface(interface_id)
            action = record.actions.get(payload) if record is not None else None
            if action is not None:
                action_data = action.get("data")
        return AdapterInput(
            metadata,
            data=action_data,
            acknowledge=True,
        )

    async def _parse_message_new(self, event: Any) -> AdapterInput:
        message = event.object.message
        if message is None or message.from_id is None or message.peer_id is None:
            return AdapterInput(None, None)
        user_id = int(message.from_id)
        peer_id = int(message.peer_id)
        cmid = message.conversation_message_id
        reply = message.reply_message
        reply_cmid = getattr(reply, "conversation_message_id", None)
        interface_id = None
        if reply_cmid is not None:
            interface_id = VKMessageMountId(
                cmid=int(reply_cmid), peer_id=peer_id, group_id=self.group_id
            )
        elif self.dm_latest_mount and peer_id == user_id:
            interface_id = await self._latest_interface(peer_id)
        metadata = VKMessageMetadata(
            group_id=self.group_id,
            chat_id=peer_id,
            user_id=user_id,
            cmid=int(cmid) if cmid is not None else None,
            interface_id=interface_id,
        )
        return AdapterInput(metadata, input=message.text or "")

    async def dispatch_input(self, event: AdapterInput) -> Any:
        if not event.acknowledge or not isinstance(event.metadata, VKMessageMetadata):
            return await super().dispatch_input(event)
        metadata = event.metadata
        mount_token = self._callback_mount.set(
            encode_key(metadata.interface_id) if metadata.interface_id is not None else None
        )
        changed_token = self._callback_changed.set(False)
        try:
            return await super().dispatch_input(event)
        finally:
            changed = self._callback_changed.get()
            try:
                if not changed and metadata.event_id is not None:
                    await self._answer_callback(
                        metadata.event_id,
                        metadata.chat_id,
                        metadata.user_id,
                    )
            finally:
                self._callback_changed.reset(changed_token)
                self._callback_mount.reset(mount_token)

    def _mark_current_interface(self, interface_id: VKMessageMountId) -> None:
        if self._callback_mount.get() == encode_key(interface_id):
            self._callback_changed.set(True)

    @abstractmethod
    async def _answer_callback(self, event_id: str, peer_id: int, user_id: int) -> None:
        """Завершить обработку callback-события средствами VK Messages."""

    @abstractmethod
    async def _latest_interface(self, peer_id: int) -> VKMessageMountId | None:
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
        dm_latest_mount: bool = True,
    ) -> None:
        VKMessagesAdapter.__init__(self, app, vk, group_id)
        VKCallbackAdapter.__init__(
            self,
            app,
            group_id,
            callback_server=callback_server,
            confirmation_code=confirmation_code,
            secret=secret,
            dm_latest_mount=dm_latest_mount,
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

    async def _latest_interface(self, peer_id: int) -> VKMessageMountId | None:
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


def _decode_interface_id(encoded: str) -> VKMessageMountId:
    return decode_key(encoded, VKMessageMountId)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
