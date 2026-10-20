# vkuxt

`vkuxt` — асинхронный каркас для приложений, в которых пользовательские действия приходят через адаптеры, например VK-бота. Он не навязывает FSM, схему бизнес-состояния, шаблонизатор, рендеринг или правила перерисовки. Транспорт и типизированные VK API-вызовы предоставляет `vkx`.

## Установка

Нужен Python 3.13+.

```powershell
uv sync --extra dev          # разработка в репозитории
uv add vkuxt                 # в приложении, когда пакет опубликован
```

`vkx` входит в основные зависимости; отдельный extra для VK не требуется.

## Модель

- `App` передаёт сырой ввод адаптерам по очереди. Первый распознавший формат адаптер возвращает внутренний `Input` с полями `metadata`, `data`, `text` и `interface`; текст и typed payload могут присутствовать одновременно. Прикладной хендлер получает уже не `Input`.
- Контракт хендлера: `(ctx, metadata, data, text)`. `ctx` — `Context`, привязанный к текущему интерфейсу; `metadata` — экземпляр `BaseMetadata`, `data` — валидированная модель `InputData` или `None`, `text` — строка (пустая, если текста нет). В аннотациях metadata, data и второго параметра `Context` указывается одна модель или `Union` моделей через `|`. Диспетчер пропускает хендлер, если metadata не подходит по типу, payload не валидируется ни в одну из указанных data-моделей или сохранённое состояние не подходит под модель состояния `Context`. `data: None` означает событие без payload; `data: Model | None` принимает оба варианта. `BaseMetadata`, `InputData` и `StateNamespace` в аннотации работают как широкие фильтры.
- `ctx.state` — изменяемый Pydantic-namespace `StateNamespace` с доступом по атрибутам, привязанный к текущему интерфейсу. После успешного завершения хендлера он сохраняется автоматически. `ctx.redraw(view, state=...)` перерисовывает текущий интерфейс и переносит (или обновляет) состояние на новый handle; `ctx.redraw(None)` удаляет интерфейс и его состояние. Через `ctx` доступны `ctx.app`, `ctx.adapter`, `ctx.interface`, `ctx.metadata` и `ctx.locked_state(...)`. Параметризуйте `Context` типом адаптера только там, где нужен его специфичный API (`Context[VKMessagesCallbackAdapter]`); второй параметр (`Context[VKMessagesCallbackAdapter, WaitingCodeState]`) одновременно задаёт схему состояния для IDE и фильтрует вызов хендлера по текущему шагу экрана (`None` во втором параметре матчится на пустое состояние).
- По умолчанию хендлеры fire-and-forget: `App.dispatch_event` только запускает их в одной `asyncio.Task` и сразу возвращает управление. `await app.wait_background()` ждёт завершения; `App.close()` также дренирует задачи. `@app.handler(background=False)` оставляет явный foreground-режим: такие хендлеры вызываются последовательно, а первый непустой результат возвращается адаптеру. `raise StopHandlerChain` прекращает текущую фазу опроса.
- `InputData` и `StateNamespace` проверяют строгую JSON-сериализуемость при Pydantic-валидации и присваивании. `handler_name` — обычное поле данных; диспетчер не интерпретирует его. Чтобы не передавать `handler_name` вручную, задайте значение по умолчанию прямо в модели: `handler_name: Literal["ttt.close"] = "ttt.close"`.
- Ключи приложения — обычные словари, dataclass'ы или Pydantic-модели. `encode_key` канонически сериализует их; данные сортируются по ключам, а тип по умолчанию кодируется полным именем класса. Для стабильного тега можно объявить `__vkuxt_key_type__: ClassVar[str]`, чтобы перенос модуля не ломал сохранённые ключи. Storage-ключи версионированы (`vkuxt:v1:...`); ключи предыдущих alpha-версий без `v1` не читаются.
- Аннотации хендлера разрешаются при регистрации. Неразрешимое имя, неверное число аргументов, `ctx` не `Context`, второй параметр `Context` не из `StateNamespace`, metadata не из `BaseMetadata`, data не из `InputData`/`None` или `text` не `str` приводят к `TypeError`, а не к тихому превращению хендлера в wildcard. Ошибки фоновых хендлеров и запуска адаптеров пишутся в `logging.getLogger("vkuxt")`; ошибка одного фонового хендлера не останавливает остальные хендлеры события.
- `Interface` — общий контракт отрисовываемого экрана: `text` и двумерный список кнопок. Адаптер объявляет свой подкласс через `Adapter.interface_type`; для VK это `VKInterface`, который проверяет лимиты сообщения и inline-клавиатуры. `Adapter.platform` — `ClassVar` с именем платформы (у VK — `"vk"`); он входит в ключи интерфейсов и связанного состояния, поэтому одинаковые внешние ID разных платформ не пересекаются. Если платформа не задана, контекстный ключ использует полное имя класса адаптера; адаптеры со своими `InterfaceHandle` должны заполнить `platform` в handle.
- Конкретный удалённый интерфейс представлен `InterfaceHandle` — Python-объектом с непрозрачным `id`, который не меняется при перерисовках. Прикладной код передаёт handle в `Adapter.redraw()` и не работает с `cmid`/peer-id напрямую. Входящий интерфейс доступен хендлеру как `ctx.interface`; `ctx.redraw(view)` рисует именно его. Удаление интерфейса через `ctx.redraw(None)` или `adapter.delete()` очищает связанный state.
- Callback-payload'ы кладутся в кнопки обычными `InputData` или словарями. Адаптер сам сериализует их и заменяет payload длиннее 255 байт UTF-8 коротким token; token'ы хранятся в записи интерфейса. Длинный payload обязан быть JSON-объектом. `App(tokenize_long_input=False)` отключает замену, поэтому payload длиннее 255 байт отклоняется с `ValueError`. Label кнопки VK ограничен 40 символами.
- Рендеринг остаётся ответственностью приложения: оно собирает `Interface` и вызывает `ctx.redraw(view)`, а адаптер решает, отправить сообщение, отредактировать существующее или удалить его.
- Handler может явно блокировать одно или несколько состояний через `async with app.lock(key, ...)`. Lock удерживается во время тела блока; читайте состояние после получения lock. Ключи блокируются в каноническом порядке, повторное получение уже удерживаемого набора допустимо. Новые ключи при вложенном захвате должны сортироваться позже всех уже удерживаемых; иначе выбрасывается `RuntimeError`, потому что такой захват может привести к взаимоблокировке. Блокировки процессные и рассчитаны на один event loop/процесс.
- Изменение состояния само по себе не инициирует перерисовку. Handler собирает `Interface` и вызывает `ctx.redraw()`.

## Минимальный пример

```python
from typing import Literal

from vkuxt import (
    App,
    Context,
    InputData,
    MemoryStorage,
    VKInterface,
    VKMessageMetadata,
)

app = App(storage=MemoryStorage())


class Greeting(InputData):
    handler_name: Literal["greeting-data"] = "greeting-data"
    text: str


@app.handler
async def greeting(
        ctx: Context,
        metadata: VKMessageMetadata,
        data: Greeting | None,
        text: str,
) -> None:
    if data is not None:
        message = data.text
    elif text.casefold() == "привет":
        message = text
    else:
        return
    ctx.state.last = message
    await ctx.redraw(
        VKInterface(
            text=f"{message}, {metadata.user_id}",
            kb=[[VKInterface.callback("Ещё", Greeting(text="кнопка"))]],
        )
    )
```

До вызова хендлера `App` проверяет payload в `Greeting`; если он невалиден, хендлер пропускается. `None` в типе data позволяет тому же хендлеру получать текстовые события без payload. Можно объявить несколько моделей и типов metadata, например `data: StartData | StopData`, `metadata: VKMessageMetadata | OtherMetadata`. `handler_name` остаётся обычным полем данных и не интерпретируется диспетчером; значение по умолчанию избавляет от повторения в каждом конструкторе.

`ctx.state` автоматически сохраняется после хендлера. `ctx.redraw(view)` перерисовывает текущий интерфейс; вручную регистрировать внешний ID не нужно. `VKInterface.callback` принимает `InputData` или словарь и сериализует payload:

```python
view = VKInterface(
    text="Выберите действие:",
    kb=[[
        VKInterface.callback(
            "Привет",
            Greeting(text="кнопка"),
        )
    ]],
)
await ctx.redraw(view)                        # текущий интерфейс события
await adapter.redraw(view, peer_id=123)       # последний интерфейс диалога
await adapter.redraw(view, target=handle)     # конкретный интерфейс
await ctx.redraw(None)                        # удалить текущий
```

`VKInterface` можно собрать один раз на уровне модуля и переиспользовать: адаптер не изменяет объект при отправке. `target` выбирает конкретный существующий интерфейс, `peer_id` — последний интерфейс диалога.

`App.handle_input(raw)` передаёт запрос адаптерам по очереди и возвращает результат foreground-хендлера; при штатном fire-and-forget режиме возвращается `None`. VK-адаптер строит внутренний `Input` с `VKMessageMetadata`, базовым `InputData`, текстом и интерфейсом; одно событие может содержать текст и payload одновременно. В `metadata` доступны поля VK, а для `message_new` также исходная модель `vkx.models.Message` в `msg` (`None` для callback-кнопки). Текущий интерфейс события доступен как `ctx.interface`.

## Хранилища и TTL

`StateStorage` — абстрактный родительский класс async-хранилищ строковых ключей/значений, TTL и `cleanup_expired()`. В комплекте:

- `MemoryStorage` — для тестов и локальной разработки;
- `JSONFileStorage(path)` — однопроцессное файловое хранение с атомарной заменой файла. Запись выполняется в отдельном потоке (`asyncio.to_thread`) и не блокирует event loop.

Redis-бэкенд намеренно не входит в библиотеку: прикладной слой может реализовать `StateStorage` под свой клиент. Pydantic-модели состояния сохраняются как JSON; тип модели передаётся в `get_state(key, Model)`. Обычные dict/list/scalars можно читать без модели, для словаря явно передайте `dict`. `App(ttl=...)` задаёт TTL по умолчанию; у отдельного интерфейса он переопределяется через `VKInterface(ttl=...)` или `adapter.new_interface(ttl=...)`. `None` означает без срока жизни. `matrix-planner` периодически вызывает обязательный `cleanup_expired()`.

`ctx.state` — изменяемый Pydantic-namespace `StateNamespace`, а не словарь. Поля читаются и пишутся как атрибуты, объявленные в подклассе поля видны IDE и валидируются при присваивании, а необъявленные атрибуты разрешены и сохраняются как extra-поля. Значения обязаны сериализоваться в строгий JSON; проверка выполняется при каждом присваивании. Состояние автоматически сохраняется после успешного хендлера. При наличии интерфейса оно хранится под стабильным `InterfaceHandle.state_key()`; при событии без интерфейса используется ключ `Adapter.context_key(metadata)`. `ctx.redraw(view)` переносит состояние на новый handle, а `ctx.redraw(None)` удаляет его вместе с интерфейсом. Для общего состояния, разделяемого несколькими интерфейсами (например, партии на двух досках), по-прежнему используйте `app.get_state`/`app.set_state`; это необязательное удобство, прикладной слой может хранить такие данные где угодно. Сам handle как ключ использовать нельзя: его `actions` и `ttl` меняются. Внутренние записи интерфейсов vkuxt хранит под непрозрачными ID адаптера, чтобы переживать перерисовки и длинные callback-payload'ы.

Схему и фильтр состояния экрана можно объявить вторым параметром `Context`:

```python
from typing import Literal
from vkuxt import (
    Context,
    StateNamespace,
    VKInterface,
    VKMessageMetadata,
    VKMessagesCallbackAdapter,
)


class WaitingCodeState(StateNamespace):
    phase: Literal["waiting_code"] = "waiting_code"
    uid: int


view = VKInterface(text="Введите код игры:")


@app.handler
async def ask_code(
    ctx: Context[VKMessagesCallbackAdapter],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if text.casefold() == "код":
        await ctx.redraw(view, state=WaitingCodeState(uid=metadata.user_id))


@app.handler
async def on_code_entered(
    ctx: Context[VKMessagesCallbackAdapter, WaitingCodeState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    # Вызывается только когда интерфейс находится в состоянии WaitingCodeState
    ...
```

Второй параметр необязателен: голый `Context` использует базовый `StateNamespace`, который подходит под любое состояние. Специализированные подклассы `StateNamespace` (с обязательными полями или `Literal`-тегом шага) работают как типобезопасные состояния экрана: хендлер вызывается только тогда, когда сохранённое состояние проходит валидацию указанной модели (или `Union` моделей). Для перехода между состояниями присвойте `ctx.state = NextState(...)` или передайте `state=NextState(...)` в `ctx.redraw(view, state=...)` / `adapter.redraw(view, peer_id=..., state=...)`. Если у модели все поля имеют обычные значения по умолчанию без `Literal`-дискриминаторов, она создаётся автоматически и при пустом состоянии; `Context[Adapter, None]` матчится только тогда, когда состояния ещё нет.

Бизнес-состояние можно хранить Pydantic-моделью и атомарно обновлять под блокировкой через `ctx.locked_state` (или `app.locked_state`):

```python
class GameState(BaseModel):
    status: Literal["active", "finished"]
    game_id: str
    board: list[str | None]
    turn: int


async with ctx.locked_state(game_key, GameState, ttl=GAME_TTL) as game:
    if game is None:
        return
    game.turn = 1 - game.turn
```

`locked_state` захватывает `lock(game_key)`, читает `get_state(game_key, GameState)` (или создаёт начальное значение из `default=...`) и при выходе из блока без ошибки автоматически сохраняет изменённую модель в storage. `set_state` и `get_state(key, GameState)` остаются доступны для явных операций. С `JSONFileStorage` состояние переживает перезапуск приложения и снова приходит типизированным объектом; отдельного общего `app.state`-словаря нет, потому что ключ и TTL задаются явно.

`JSONFileStorage` рассчитан на один процесс и один экземпляр на файл: параллельная запись из нескольких процессов или экземпляров не поддерживается. Повреждённый JSON при старте приводит к `RuntimeError` с путём файла, а не к тихой перезаписи.

```python
from vkuxt import App, JSONFileStorage

app = App(JSONFileStorage("state.json"), ttl=60 * 60 * 24)
```

При передаче внешнего `Planner` его жизненным циклом управляет приложение-хозяин; `App.close()` снимает только задачу очистки этого приложения. `App(close_timeout=...)` ограничивает ожидание фоновых хендлеров при остановке (по умолчанию 10 секунд); по истечении они отменяются. `App(adapter_close_timeout=...)` ограничивает закрытие каждого адаптера (по умолчанию 10 секунд); если адаптер не закрылся, приложение остаётся незакрытым, и повторный `close()` попробует снова. `App.start()` и `App.close()` сериализованы, повторный `close()` дожидается уже идущей остановки. `await app.wait_background()` ждёт завершения фоновых хендлеров, `await app.wait_adapters_started()` — запуска адаптеров, добавленных во время работы. Контейнер `app.services` свободен для сервисов приложения.

## VK Callback API

`VKCallbackAdapter` и `VKMessagesAdapter` — абстрактные слои для Callback API и VK Messages. Готовый вариант объединяет их: `VKMessagesCallbackAdapter`. Адаптер создаётся вручную и передаётся готовым инстансом в `app.add_adapter(adapter)`; `App.start()` запускает зарегистрированные адаптеры, а новые адаптеры, добавленные во время работы, запускаются асинхронно в текущем event loop. `App.close()` останавливает запущенные адаптеры в обратном порядке. Если `messages.edit` завершился ошибкой, `redraw()` логирует её и отправляет новое сообщение вместо падения webhook'а.

```python
adapter = app.add_adapter(VKMessagesCallbackAdapter(app, vk, group_id=group_id))
```

Вложения принимают типы VKX или VK-строки, кнопки — модели `vkx.KeyboardButton`. `VKInterface.callback(..., color=...)` принимает `KeyboardButtonColor` или строку и проверяет допустимые значения. `KeyboardButtonColor` реэкспортируется из `vkuxt` и `vkuxt.adapters.vk`, поэтому отдельный импорт из `vkx.models` не нужен.

`await ctx.redraw(view)` выбирает интерфейс текущего события: редактирует сообщение callback-кнопки; для ответа на текст отправляет ответ и удаляет старый интерфейс; если интерфейс был определён как последний для обычного текста в личке, отправляет новое сообщение и также удаляет прежний. В остальных случаях отправляет новое сообщение. Для чужого или явно выбранного интерфейса используйте `await adapter.redraw(view, target=handle)`; `peer_id=...` выбирает последний интерфейс диалога. `await ctx.redraw(None)` удаляет текущий интерфейс. `await adapter.send(peer_id, view)` и `await adapter.edit(handle, view)` доступны для явных операций.

`App.lifespan` можно передать напрямую в Starlette/FastAPI, чтобы запускать и останавливать адаптеры вместе с ASGI-приложением:

```python
web = FastAPI(lifespan=app.lifespan)
```

VK Callback адаптер использует `vkx.server.CallbackServer` для проверки входа и разбора уведомлений в модели `vkx`. Дедупликация доставленных событий — ответственность `vkx`: `CallbackServer` отбрасывает повторный `event_id` и повторную доставку `message_new` с той же парой `peer_id` + `conversation_message_id`. У `message_event` идентификатор лежит внутри `object.event_id`, поэтому адаптер дополнительно передаёт его в `CallbackServer.is_duplicate`. Текст приходит VK-хендлеру аргументом `text`; адаптер сам определяет, было ли сообщение ответом на сохранённый интерфейс. В личных диалогах можно включить определение последнего активного интерфейса; если обработчик затем вызывает `redraw()`, прежний интерфейс заменяется новым сообщением. Если foreground-хендлер не привёл к edit/delete именно нажатого интерфейса, адаптер отправляет `messages.sendMessageEventAnswer`. Индекс последних интерфейсов peer'а ограничен 10 записями и получает TTL интерфейса.

При fire-and-forget обработке `sendMessageEventAnswer` отправляется сразу после запуска фоновой задачи, не дожидаясь её завершения. Это укладывается в окно ответа VK; если нужен результат хендлера до ответа, используйте `background=False`, но держите такой путь коротким.

Полный скетч крестиков-ноликов, включая лобби с кодом, ввод кода ответом на сообщение, две синхронизируемые доски и сдачу, находится в [`examples/tictactoe.py`](examples/tictactoe.py).

