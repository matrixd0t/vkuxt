# vkuxt

`vkuxt` — асинхронный каркас для приложений, в которых пользовательские действия приходят через адаптеры, например VK-бота. Он не навязывает FSM, схему бизнес-состояния, шаблонизатор, рендеринг или правила перерисовки. Транспорт и типизированные VK API-вызовы предоставляет `vkx`.

## Модель

- `App` передаёт сырой ввод адаптерам по очереди. Первый распознавший формат адаптер возвращает `Input` с полями `metadata`, `data` и `text`; текст и typed payload могут присутствовать одновременно.
- `metadata` содержит адаптер-специфичный экземпляр `BaseMetadata`, `data` — базовый `InputData` или `None`, `text` — строку или `None`. При регистрации `App` индексирует хендлеры по типам metadata из `Input[DataType, MetadataType]`; объединения задаются через `|`. Для подходящего типа metadata адаптерский `data` проверяется по очереди против моделей, объявленных хендлером; при первом успешном варианте хендлер получает уже валидированную модель. `Input[None, MetadataType]` предназначен для событий без typed data; голый `Input` или `Input[Any, BaseMetadata]` подходит для наблюдателей без фильтра.
- Хендлер получает `(app, input)`. Сначала последовательно вызываются подходящие `background=False` хендлеры в порядке регистрации; первый непустой результат возвращается адаптеру. Если они ничего не вернули, подходящие `background=True` хендлеры последовательно запускаются одной `asyncio.Task`. `raise StopHandlerChain` прекращает текущую фазу опроса.
- `InputData` проверяет строгую JSON-сериализуемость при Pydantic-валидации и присваивании. `handler_name` — обычное поле данных; `Literal[...]` можно использовать для разделения моделей, но диспетчер не интерпретирует это поле.
- Ключи приложения — обычные словари или модели. `encode_key` канонически сериализует их; модели сохраняют в сериализации полное имя типа, данные сортируются по ключам. Такой же codec используется всеми storage-бэкендами и адаптерами.
- Сохранённый интерфейс адресуется внешним идентификатором. Для сообщения VK это `VKMessageInterfaceId(cmid, peer_id, group_id)`, но VK-хендлеры не получают его: адаптер хранит текущий контекст события и сам находит интерфейс через `App.get_interface()`.
- `Interface.bind()` обычно возвращает словарь typed input напрямую. Только payload длиннее лимита VK заменяется коротким token, данные которого хранятся в записи интерфейса; опция отключается через `App(tokenize_long_input=False)`.
- Повторный `register_interface()` заменяет таблицу больших token-payload. Рендеринг остаётся ответственностью приложения или адаптера.
- Handler может явно блокировать одно или несколько состояний через `async with app.lock(key, ...)`. Lock удерживается во время тела блока; читайте состояние после получения lock. Ключи блокируются в каноническом порядке, вложенное получение того же lock допустимо. Блокировки процессные и рассчитаны на один event loop/процесс.
- Изменение состояния само по себе не инициирует перерисовку. Handler задаёт следующее состояние шаблоном интерфейса адаптера; адаптер решает, отправить, отредактировать или заменить сообщение.

## Минимальный пример

```python
from typing import Literal

from vkuxt import App, BaseMetadata, Input, InputData, MemoryStorage

app = App(storage=MemoryStorage())


class GreetingMetadata(BaseMetadata):
    user_id: int


class Greeting(InputData):
    handler_name: Literal["greeting-data"]
    text: str


@app.handler(background=False)
async def greeting(
    app: App,
    input: Input[Greeting | None, GreetingMetadata],
) -> None:
    data = input.data
    if data is not None:
        text = data.text
    elif input.text is not None:
        if input.text.casefold() != "привет":
            return
        text = input.text
    else:
        return
    key = {"kind": "greeting-count", "user_id": input.metadata.user_id}
    async with app.lock(key):
        count = await app.get_state(key) or 0
        await app.set_state(key, count + 1)
```

Адаптер строит `GreetingMetadata` из транспортных полей. До вызова хендлера `App` проверяет payload в `Greeting`; если он невалиден, хендлер пропускается. `None` в типе data позволяет тому же хендлеру получать текстовые события без payload. Можно объявить несколько моделей и типов metadata, например `Input[StartData | StopData, VKMessageMetadata | OtherMetadata]`. `handler_name` остаётся обычным полем данных и не интерпретируется диспетчером.

Для кнопки приложение создаёт `Interface` и кладёт payload в кнопку. Когда площадка отправила/обновила сообщение и получила внешний ID, `Interface` сохраняется под этим ID:

```python
draft = app.new_interface()
payload = draft.bind(Greeting(handler_name="greeting-data", text="кнопка"))
# VK получает payload с обычными полями модели, включая handler_name.
await app.register_interface(vk_message_mount_id, draft)
```

`App.handle_input(raw)` передаёт запрос адаптерам по очереди и возвращает результат foreground-хендлера. VK-адаптер строит `Input` с `VKMessageMetadata`, базовым `InputData` и текстом; одно событие может содержать текст и payload одновременно. В metadata доступны поля VK, а для `message_new` также исходная модель `vkx.models.Message` в `msg` (`None` для callback-кнопки). Это позволяет фильтровать сообщения, например по `input.metadata.msg.peer_id` после проверки `msg is not None`. Контекст интерфейса, используемый `transition()`, остаётся внутри адаптера.

## Хранилища и TTL

`StateStorage` — абстрактный родительский класс async-хранилищ строковых ключей/значений, TTL и `cleanup_expired()`. В комплекте:

- `MemoryStorage` — для тестов и локальной разработки;
- `JSONFileStorage(path)` — однопроцессное файловое хранение с атомарной заменой файла;
- `RedisStorage(async_redis_client)` — duck-typed адаптер для async redis-py.

Pydantic-модели состояния сохраняются как JSON; тип модели передаётся в `get_state(key, Model)`. Обычные dict/list/scalars можно читать без модели. `App(ttl=...)` задаёт TTL по умолчанию; у отдельного интерфейса он переопределяется через `app.new_interface(ttl=...)`. `None` означает без срока жизни. `matrix-planner` периодически вызывает обязательный `cleanup_expired()`; у Redis метод ничего не делает, потому что Redis применяет TTL самостоятельно.

```python
from vkuxt import App, JSONFileStorage

app = App(JSONFileStorage("state.json"), ttl=60 * 60 * 24)
```

При передаче внешнего `Planner` его жизненным циклом управляет приложение-хозяин; `App.close()` снимает только задачу очистки этого приложения.

## VK Callback API

`VKCallbackAdapter` и `VKMessagesAdapter` — абстрактные слои для Callback API и VK Messages. Готовый вариант объединяет их: `VKMessagesCallbackAdapter`. Адаптеры создаются приложением через `app.add_adapter(AdapterType, ...)`; `App.start()` запускает зарегистрированные адаптеры, а новые адаптеры, добавленные во время работы, запускаются асинхронно в текущем event loop. `App.close()` останавливает запущенные адаптеры в обратном порядке. Адаптер может использовать эти hooks для приёма событий или фонового опроса.

```python
adapter = app.add_adapter(VKMessagesCallbackAdapter, vk, group_id=group_id)
```

Хендлеры регистрируются через `app.handler`; фильтрация определяется аннотацией handler input, например `Input[StartData, VKMessageMetadata]`. `VKMessageInput` — сокращение для `Input[Any, VKMessageMetadata]`, если нужно принимать любой payload. `adapter.new_interface(text=..., attachments=..., kb=...)` создаёт типизированный `VKMessageTemplate`; текст ограничивается 4096 символами, вложения принимают типы VKX или VK-строки, клавиатура — модель `vkx.Keyboard` или JSON-строку. `await adapter.transition(template)` поручает адаптеру выбрать действие: редактировать сообщение callback-кнопки, отправить ответ на текст и удалить старый интерфейс либо отправить новое сообщение. `await adapter.transition(None)` удаляет текущий интерфейс.

`App.lifespan` можно передать напрямую в Starlette/FastAPI, чтобы запускать и останавливать адаптеры вместе с ASGI-приложением:

```python
web = FastAPI(lifespan=app.lifespan)
```

VK Callback адаптер использует `vkx.server.CallbackServer` для проверки входа и разбора уведомлений в модели `vkx`; установите extra `vkuxt[vk]`. Текст поступает VK-хендлерам как `Input.text`; адаптер сам определяет, было ли сообщение ответом на сохранённый интерфейс. В личных диалогах можно включить определение последнего активного интерфейса. Если callback-кнопка не привела к edit/delete именно нажатого интерфейса, адаптер отправляет `messages.sendMessageEventAnswer`.

Полный скетч крестиков-ноликов, включая лобби с кодом, ввод кода ответом на сообщение, две синхронизируемые доски и сдачу, находится в [`examples/tictactoe.py`](examples/tictactoe.py).

## Проверка

```powershell
uv sync --extra dev
.venv\Scripts\python.exe -m pytest
uvx ruff check src tests examples
```

Для запуска примера установите `vkx[http]` и FastAPI, создайте `VKClient` с токеном группы, настройте публичный Callback URL, получите `app` из `make_callback_app()` и передайте его в `attach_fastapi(app)`.
