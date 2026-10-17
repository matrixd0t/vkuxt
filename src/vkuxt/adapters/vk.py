from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, PrivateAttr
from vkx import VKClient
from vkx.models import (
    AttachmentsInput,
    Button,
    Keyboard,
    KeyboardButton,
    KeyboardButtonColor,
    Message,
)
from vkx.server import CallbackServer

from ..app import Adapter, App, BaseMetadata, Context, Input
from ..codec import KEY_NAMESPACE_VERSION, KeyLike, decode_key, encode_key
from ..interfaces import (
    DEFAULT_TTL,
    Interface,
    InterfaceHandle,
    TTLValue,
)
from ..models import InputData

logger = logging.getLogger("vkuxt")

_PEER_INTERFACE_LIMIT = 10
_VK_TEXT_LIMIT = 4096
_VK_INLINE_ROWS_LIMIT = 6
_VK_ROW_BUTTONS_LIMIT = 5
_VK_PAYLOAD_LIMIT = 255
_VK_BUTTON_COLORS = frozenset(
    {
        KeyboardButtonColor.DEFAULT.value,
        KeyboardButtonColor.POSITIVE.value,
        KeyboardButtonColor.NEGATIVE.value,
        KeyboardButtonColor.PRIMARY.value,
    }
)


class VKMessageInterfaceId(BaseModel):
    __vkuxt_key_type__: ClassVar[str] = "vkuxt.v1.vk_message_interface_id"

    cmid: int
    peer_id: int
    group_id: int


@dataclass(slots=True)
class VKMessageInterface(InterfaceHandle[VKMessageInterfaceId]):
    """Python-объект конкретного сообщения-интерфейса VK."""


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
class VKInterface(Interface[KeyboardButton]):
    """Текст сообщения VK и inline-клавиатура из моделей ``vkx``.

    Объект можно собрать один раз на уровне модуля и переиспользовать:
    адаптер не изменяет его при отправке.
    """

    attachments: AttachmentsInput | None = None

    def validate(self) -> None:
        super().validate()
        if len(self.text) > _VK_TEXT_LIMIT:
            raise ValueError(
                f"текст VK-интерфейса не должен превышать {_VK_TEXT_LIMIT} символов"
            )
        if len(self.rows) > _VK_INLINE_ROWS_LIMIT:
            raise ValueError(
                f"inline-клавиатура VK: не больше {_VK_INLINE_ROWS_LIMIT} рядов"
            )
        for index, row in enumerate(self.rows):
            if not row:
                raise ValueError(f"inline-клавиатура VK: ряд {index} пустой")
            if len(row) > _VK_ROW_BUTTONS_LIMIT:
                raise ValueError(
                    f"inline-клавиатура VK: в ряду {index} больше "
                    f"{_VK_ROW_BUTTONS_LIMIT} кнопок"
                )
            for button in row:
                if not isinstance(button, KeyboardButton):
                    raise TypeError(
                        "кнопки VK-интерфейса должны быть экземплярами KeyboardButton"
                    )
                color = button.color
                if color is not None and getattr(color, "value", color) not in _VK_BUTTON_COLORS:
                    raise ValueError(
                        f"Недопустимый цвет кнопки VK: {color!r}; допустимо: "
                        f"{', '.join(sorted(_VK_BUTTON_COLORS))}"
                    )

    @property
    def keyboard(self) -> Keyboard:
        return Keyboard.from_rows([list(row) for row in self.rows])

    @classmethod
    def callback(
        cls,
        label: str,
        data: InputData | Mapping[str, Any] | str | None = None,
        *,
        color: KeyboardButtonColor | str | None = None,
    ) -> KeyboardButton:
        """Собрать callback-кнопку; модель данных сериализуется автоматически."""
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        if color is not None and not isinstance(color, KeyboardButtonColor):
            if not isinstance(color, str) or color not in _VK_BUTTON_COLORS:
                raise ValueError(
                    f"Недопустимый цвет кнопки VK: {color!r}; допустимо: "
                    f"{', '.join(sorted(_VK_BUTTON_COLORS))}"
                )
            color = KeyboardButtonColor(color)
        elif (
            isinstance(color, KeyboardButtonColor)
            and color.value not in _VK_BUTTON_COLORS
        ):
            raise ValueError(
                f"Недопустимый цвет кнопки VK: {color!r}; допустимо: "
                f"{', '.join(sorted(_VK_BUTTON_COLORS))}"
            )
        return Button.callback(label, payload=data, color=color)


@dataclass(slots=True)
class _PreparedVKInterface:
    keyboard: Keyboard
    actions: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _VKDispatchContext:
    metadata: VKMessageMetadata
    interface: VKMessageInterface | None
    is_reply: bool
    callback_changed: bool = False


class _VKConfirmationMetadata(BaseMetadata):
    group_id: int


class _VKAdapterBase(Adapter, ABC):
    """Общее состояние VK-адаптеров: группа, peer-lock и контекст события."""

    interface_type: ClassVar[type[Interface[Any]]] = VKInterface

    def __init__(self, app: App, group_id: int) -> None:
        Adapter.__init__(self, app)
        self.group_id = group_id
        self._peer_lock = asyncio.Lock()
        self._vk_event_context: contextvars.ContextVar[_VKDispatchContext | None] = (
            contextvars.ContextVar(f"vkuxt_vk_event_{id(self)}", default=None)
        )

    def context_key(self, metadata: BaseMetadata) -> KeyLike:
        if isinstance(metadata, VKMessageMetadata):
            return {
                "vk_context": {
                    "group_id": metadata.group_id,
                    "peer_id": metadata.peer_id,
                    "user_id": metadata.user_id,
                }
            }
        return super().context_key(metadata)

    @abstractmethod
    async def _latest_interface(self, peer_id: int) -> VKMessageInterfaceId | None:
        """Найти последний активный интерфейс в личном диалоге."""

    async def _handle(
        self,
        interface_id: VKMessageInterfaceId,
    ) -> VKMessageInterface:
        record = await self.app._get_interface_record(interface_id)
        if record is None:
            return VKMessageInterface(interface_id)
        return VKMessageInterface(interface_id, dict(record.actions), record.ttl)

    async def latest_interface(self, peer_id: int) -> VKMessageInterface | None:
        """Получить Python-объект последнего интерфейса в диалоге ``peer_id``."""
        interface_id = await self._latest_interface(peer_id)
        return None if interface_id is None else await self._handle(interface_id)


class VKMessagesAdapter(_VKAdapterBase, ABC):
    """Абстракция операций VK Messages, общая для callback и long poll."""

    def __init__(self, app: App, vk: VKClient, group_id: int) -> None:
        _VKAdapterBase.__init__(self, app, group_id)
        self._vk = vk

    @property
    @abstractmethod
    def vk(self) -> VKClient:
        """VK-клиент с категорией messages."""

    def new_interface(
        self,
        *,
        text: str = "",
        rows: Sequence[Sequence[KeyboardButton]] = (),
        attachments: AttachmentsInput | None = None,
        ttl: TTLValue = DEFAULT_TTL,
    ) -> VKInterface:
        return VKInterface(text=text, rows=rows, ttl=ttl, attachments=attachments)

    async def send(
        self,
        peer_id: int,
        interface: VKInterface,
        *,
        reply_to: int | None = None,
    ) -> VKMessageInterface:
        return await self._send_prepared(
            peer_id,
            interface,
            self._prepare_view(interface),
            reply_to=reply_to,
        )

    async def edit(self, interface: VKMessageInterface, view: VKInterface) -> bool:
        return await self._edit_prepared(interface, view, self._prepare_view(view)) is not None

    async def delete(self, interface: VKMessageInterface) -> bool:
        if not isinstance(interface.id, VKMessageInterfaceId):
            raise TypeError("VK-адаптер ожидает InterfaceHandle с VKMessageInterfaceId")
        response = await self.vk.messages.delete(
            cmids=[interface.id.cmid],
            peer_id=interface.id.peer_id,
            group_id=interface.id.group_id,
            delete_for_all=True,
        )
        await self.app._remove_interface_record(interface.id)
        await self._forget_interface(interface.id)
        self._mark_current_interface(interface.id)
        return bool(response)

    async def redraw(
        self,
        interface: Interface[Any] | None,
        *,
        target: InterfaceHandle[Any] | None = None,
        peer_id: int | None = None,
    ) -> VKMessageInterface | None:
        """Перерисовать интерфейс одним вызовом.

        Без ``target`` и ``peer_id`` используется интерфейс текущего события.
        ``target`` выбирает конкретный существующий интерфейс, ``peer_id`` —
        последний интерфейс указанного диалога. ``interface=None`` удаляет интерфейс.
        """
        if interface is not None and not isinstance(interface, VKInterface):
            raise TypeError("VK-адаптер принимает VKInterface")
        if target is not None and not isinstance(target, VKMessageInterface):
            raise TypeError("target должен быть VKMessageInterface")
        if target is not None and peer_id is not None:
            raise ValueError("Укажите target или peer_id, но не оба")
        view = interface
        context = self._vk_event_context.get()
        explicit_target = target is not None
        if target is not None:
            current = target
            recipient = target.id.peer_id
            same_event = (
                context is not None
                and context.interface is not None
                and context.interface.id == target.id
            )
        elif peer_id is not None:
            recipient = peer_id
            same_event = context is not None and context.metadata.peer_id == recipient
            current = (
                context.interface
                if same_event and context is not None
                else await self.latest_interface(recipient)
            )
        else:
            if context is None:
                raise RuntimeError("redraw() требует VK-событие, target или peer_id")
            recipient = context.metadata.peer_id
            same_event = True
            current = context.interface

        if view is None:
            if current is not None:
                await self.delete(current)
            return None

        if (
            not explicit_target
            and same_event
            and context is not None
            and context.metadata.event_type == "message_new"
            and not context.is_reply
        ):
            current = None

        prepared = self._prepare_view(view)

        if (
            same_event
            and context is not None
            and context.metadata.event_type == "message_new"
            and context.is_reply
            and context.metadata._message_cmid is not None
            and current is not None
        ):
            new_interface = await self._send_prepared(
                recipient,
                view,
                prepared,
                reply_to=context.metadata._message_cmid,
            )
            await self.delete(current)
            return new_interface

        if current is not None and (
            not same_event
            or (context is not None and context.metadata.event_type == "message_event")
        ):
            try:
                edited = await self._edit_prepared(current, view, prepared)
            except Exception:
                logger.exception(
                    "Не удалось отредактировать сообщение %s, отправляю новое",
                    current.id,
                )
            else:
                if edited is not None:
                    return edited

        return await self._send_prepared(recipient, view, prepared)

    def _prepare_view(self, view: VKInterface) -> _PreparedVKInterface:
        view.validate()
        actions: dict[str, dict[str, Any]] = {}
        rows = [
            [self._prepare_button(button, actions) for button in row]
            for row in view.rows
        ]
        return _PreparedVKInterface(Keyboard.from_rows(rows), actions)

    def _prepare_button(
        self,
        button: KeyboardButton,
        actions: dict[str, dict[str, Any]],
    ) -> KeyboardButton:
        payload = getattr(button.action, "payload", None)
        if not isinstance(payload, str):
            return button
        payload_size = len(payload.encode("utf-8"))
        if not self.app.tokenize_long_input or payload_size <= _VK_PAYLOAD_LIMIT:
            return button
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "payload кнопки VK длиннее 255 байт и не является JSON-объектом"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(
                "payload кнопки VK длиннее 255 байт и не является JSON-объектом"
            )
        token = secrets.token_urlsafe(6)
        while token in actions:
            token = secrets.token_urlsafe(6)
        actions[token] = {"data": data}
        action = button.action.model_copy(update={"payload": token})
        return button.model_copy(update={"action": action})

    async def _send_prepared(
        self,
        peer_id: int,
        view: VKInterface,
        prepared: _PreparedVKInterface,
        *,
        reply_to: int | None = None,
    ) -> VKMessageInterface:
        response = await self.vk.messages.send(
            peer_ids=[peer_id],
            random_id=secrets.randbelow(2**31),
            group_id=self.group_id,
            message=view.text,
            keyboard=prepared.keyboard.to_json() if view.rows else None,
            attachment=view.attachments,
            reply_to=reply_to,
        )
        try:
            item = response[0][0]
            cmid = int(_field(item, "conversation_message_id"))
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "VK не вернул conversation_message_id для отправленного сообщения"
            ) from exc
        interface_id = VKMessageInterfaceId(
            cmid=cmid,
            peer_id=peer_id,
            group_id=self.group_id,
        )
        ttl = await self.app._register_interface(interface_id, prepared.actions, view.ttl)
        await self._remember_interface(interface_id, ttl)
        return VKMessageInterface(interface_id, prepared.actions, ttl)

    async def _edit_prepared(
        self,
        interface: VKMessageInterface,
        view: VKInterface,
        prepared: _PreparedVKInterface,
    ) -> VKMessageInterface | None:
        changed = await self.vk.messages.edit(
            peer_id=interface.id.peer_id,
            cmid=interface.id.cmid,
            group_id=interface.id.group_id,
            message=view.text,
            keyboard=prepared.keyboard.to_json(),
            attachment=view.attachments if view.attachments is not None else "",
        )
        if not changed:
            return None
        self._mark_current_interface(interface.id)
        ttl = await self.app._register_interface(interface.id, prepared.actions, view.ttl)
        await self._remember_interface(interface.id, ttl)
        interface.actions = prepared.actions
        interface.ttl = ttl
        return interface

    def _mark_current_interface(self, interface_id: VKMessageInterfaceId) -> None:
        return None

    async def _remember_interface(
        self,
        interface_id: VKMessageInterfaceId,
        ttl: float | None,
    ) -> None:
        index_key = _peer_index_key(interface_id.group_id, interface_id.peer_id)
        encoded = encode_key(interface_id)
        async with self._peer_lock:
            raw = await self.app.storage.get(index_key)
            interfaces = _load_index(raw)
            active = []
            for item in interfaces:
                previous_id = _decode_interface_id(item)
                if await self.app._get_interface_record(previous_id) is not None:
                    active.append(encode_key(previous_id))
            active = [item for item in active if item != encoded]
            active.append(encoded)
            active = active[-_PEER_INTERFACE_LIMIT:]
            await self.app.storage.set(index_key, _json(active), ttl=ttl)

    async def _forget_interface(self, interface_id: VKMessageInterfaceId) -> None:
        index_key = _peer_index_key(interface_id.group_id, interface_id.peer_id)
        async with self._peer_lock:
            raw = await self.app.storage.get(index_key)
            if raw is not None:
                interfaces = []
                for item in _load_index(raw):
                    previous_id = _decode_interface_id(item)
                    if previous_id != interface_id:
                        interfaces.append(encode_key(previous_id))
                if interfaces:
                    interfaces = interfaces[-_PEER_INTERFACE_LIMIT:]
                    record = await self.app._get_interface_record(
                        _decode_interface_id(interfaces[-1])
                    )
                    await self.app.storage.set(
                        index_key,
                        _json(interfaces),
                        ttl=record.ttl if record is not None else None,
                    )
                else:
                    await self.app.storage.delete(index_key)

    async def _latest_interface(self, peer_id: int) -> VKMessageInterfaceId | None:
        index_key = _peer_index_key(self.group_id, peer_id)
        async with self._peer_lock:
            raw = await self.app.storage.get(index_key)
            if raw is None:
                return None
            interfaces = _load_index(raw)
            live: list[str] = []
            latest = None
            latest_ttl = None
            for encoded in interfaces:
                interface_id = _decode_interface_id(encoded)
                record = await self.app._get_interface_record(interface_id)
                if record is not None:
                    live.append(encode_key(interface_id))
                    latest = interface_id
                    latest_ttl = record.ttl
            live = live[-_PEER_INTERFACE_LIMIT:]
            if live:
                if live != interfaces:
                    await self.app.storage.set(index_key, _json(live), ttl=latest_ttl)
            else:
                await self.app.storage.delete(index_key)
            return latest


class VKCallbackAdapter(_VKAdapterBase, ABC):
    """Абстрактная обработка Callback API на типизированных событиях vkx."""

    def __init__(
        self,
        app: App,
        group_id: int,
        *,
        callback_server: CallbackServer | None = None,
        confirmation_code: str | None = None,
        secret: str | None = None,
        dm_latest_interface: bool = True,
    ) -> None:
        if callback_server is None:
            callback_server = CallbackServer(
                group_id,
                secret=secret,
                confirmation_code=confirmation_code,
            )
        _VKAdapterBase.__init__(self, app, group_id)
        self.callback_server = callback_server
        self.confirmation_code = confirmation_code
        self.dm_latest_interface = dm_latest_interface

        async def confirmation(
            ctx: Context,
            metadata: _VKConfirmationMetadata,
            data: None,
            text: str,
        ) -> str | None:
            if metadata.group_id != self.group_id:
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
        # vkx дедуплицирует конвертный event_id и повторный message_new по
        # peer_id + conversation_message_id; object.event_id у message_event
        # в vkx 0.1.12 пока не учитывается.
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
        if getattr(event, "event_id", None) is None:
            is_duplicate = getattr(self.callback_server, "is_duplicate", None)
            # Отдельный префикс не конфликтует с будущей нативной
            # дедупликацией вложенного event_id в vkx.
            if callable(is_duplicate) and is_duplicate(f"vkuxt:{event_id}"):
                return None

        interface_id = (
            VKMessageInterfaceId(
                cmid=int(cmid),
                peer_id=int(peer_id),
                group_id=self.group_id,
            )
            if cmid is not None
            else None
        )
        interface = await self._handle(interface_id) if interface_id is not None else None
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
        elif isinstance(payload, str) and interface is not None:
            action = interface.actions.get(payload)
            if action is not None:
                action_data = action.get("data")
        return Input(
            metadata,
            data=action_data,
            interface=interface,
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
        is_reply = metadata.event_type == "message_new" and metadata._reply_cmid is not None
        event = await self._attach_interface(event)
        context = _VKDispatchContext(
            metadata,
            cast(VKMessageInterface | None, event.interface),
            is_reply,
        )
        context_token = self._vk_event_context.set(context)
        try:
            try:
                result = await super().dispatch_input(event)
            except BaseException:
                await self._answer_callback_if_needed(context, metadata, safe=True)
                raise
            else:
                await self._answer_callback_if_needed(context, metadata, safe=False)
                return result
        finally:
            self._vk_event_context.reset(context_token)

    async def _attach_interface(self, event: Input) -> Input:
        metadata = event.metadata
        if not isinstance(metadata, VKMessageMetadata) or event.interface is not None:
            return event
        interface: VKMessageInterface | None = None
        if metadata.event_type == "message_event":
            if metadata._interface_cmid is not None:
                interface = await self._handle(
                    VKMessageInterfaceId(
                        cmid=metadata._interface_cmid,
                        peer_id=metadata.peer_id,
                        group_id=metadata.group_id,
                    )
                )
        elif metadata._reply_cmid is not None:
            candidate = VKMessageInterfaceId(
                cmid=metadata._reply_cmid,
                peer_id=metadata.peer_id,
                group_id=metadata.group_id,
            )
            if await self.app._get_interface_record(candidate) is not None:
                interface = await self._handle(candidate)
        elif self.dm_latest_interface and metadata.peer_id == metadata.user_id:
            interface = await self.latest_interface(metadata.peer_id)
        if interface is None:
            return event
        return Input(event.metadata, event.data, event.text, interface=interface)

    async def _answer_callback_if_needed(
        self,
        context: _VKDispatchContext,
        metadata: VKMessageMetadata,
        *,
        safe: bool,
    ) -> None:
        if (
            metadata.event_type != "message_event"
            or context.callback_changed
            or metadata.event_id is None
        ):
            return
        if not safe:
            await self._answer_callback(
                metadata.event_id,
                metadata.peer_id,
                metadata.user_id,
            )
            return
        try:
            await self._answer_callback(
                metadata.event_id,
                metadata.peer_id,
                metadata.user_id,
            )
        except Exception:
            logger.exception("Не удалось ответить на callback-событие %s", metadata.event_id)

    def _mark_current_interface(self, interface_id: VKMessageInterfaceId) -> None:
        context = self._vk_event_context.get()
        if (
            context is not None
            and context.interface is not None
            and context.interface.id == interface_id
        ):
            context.callback_changed = True

    @abstractmethod
    async def _answer_callback(self, event_id: str, peer_id: int, user_id: int) -> None:
        """Завершить обработку callback-события средствами VK Messages."""


class VKMessagesCallbackAdapter(VKCallbackAdapter, VKMessagesAdapter):
    """Готовое сочетание Callback API и VK Messages."""

    def __init__(
        self,
        app: App,
        vk: VKClient,
        group_id: int,
        *,
        callback_server: CallbackServer | None = None,
        confirmation_code: str | None = None,
        secret: str | None = None,
        dm_latest_interface: bool = True,
    ) -> None:
        # Callback-инициализация создаёт общее состояние _VKAdapterBase;
        # второй __init__ из VKMessagesAdapter здесь не нужен.
        VKCallbackAdapter.__init__(
            self,
            app,
            group_id,
            callback_server=callback_server,
            confirmation_code=confirmation_code,
            secret=secret,
            dm_latest_interface=dm_latest_interface,
        )
        self._vk = vk

    @property
    def vk(self) -> VKClient:
        return self._vk

    async def _answer_callback(self, event_id: str, peer_id: int, user_id: int) -> None:
        await self.vk.messages.send_message_event_answer(
            event_id=event_id,
            peer_id=peer_id,
            user_id=user_id,
        )


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


def _peer_index_key(group_id: int, peer_id: int) -> str:
    return f"vkuxt:{KEY_NAMESPACE_VERSION}:vk-peer-interfaces:{group_id}:{peer_id}"


def _load_index(raw: str | None) -> list[str]:
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str)]


def _decode_interface_id(encoded: str) -> VKMessageInterfaceId:
    return decode_key(encoded, VKMessageInterfaceId)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
