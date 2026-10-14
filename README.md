# vkuxt

`vkuxt` — асинхронный каркас для приложений, в которых пользовательские действия приходят через адаптеры, например VK-бота. Он не навязывает FSM, схему бизнес-состояния, шаблонизатор, рендеринг или правила перерисовки. Транспорт и типизированные VK API-вызовы предоставляет `vkx`.

## Модель

- `App` передаёт сырой ввод адаптерам по очереди. Первый распознавший формат адаптер возвращает `Input` с полями `metadata`, `data` и `text`; текст и typed payload могут присутствовать одновременно.
- `metadata` содержит адаптер-специфичный экземпляр `EventMetadata`, `data` — базовый `InputData` или `None`, а `text` — строку или `None`.
- `@app.handler` принимает хендлеры с сигнатурой `(app: App, input: Input)`. Каждому хендлеру передаётся всё событие без фильтрации по аннотациям; он сам проверяет метаданные и решает, преобразовывать ли `data` в свою модель. По умолчанию хендлер запускается в `asyncio.Task`, а `@app.handler(background=False)` ожидает его завершения и возвращает непустой результат адаптеру.
- `InputData` проверяет строгую JSON-сериализуемость при Pydantic-валидации и присваивании. `handler_name` — обычное поле модели; хендлер может использовать его или валидировать данные в конкретную модель с полем `Literal[...]`.
- Ключи приложения — обычные словари или модели. `encode_key` канонически сериализует их; модели сохраняют в сериализации полное имя типа, данные сортируются по ключам. Такой же codec используется всеми storage-бэкендами и адаптерами.
- Интерфейс адресуется внешним идентификатором. Для сообщения VK это `VKMessageInterfaceId(cmid, peer_id, group_id)`; отдельного внутреннего ID нет.
- `Interface.bind()` обычно возвращает словарь typed input напрямую. Только payload длиннее лимита VK заменяется коротким token, данные которого хранятся в записи интерфейса; опция отключается через `App(tokenize_long_input=False)`.
- Повторный `register_interface()` заменяет таблицу больших token-payload. Рендеринг остаётся ответственностью приложения или адаптера.
- Handler может явно блокировать одно или несколько состояний через `async with app.lock(key, ...)`. Lock удерживается во время тела блока; читайте состояние после получения lock. Ключи блокируются в каноническом порядке, вложенное получение того же lock допустимо. Блокировки процессные и рассчитаны на один event loop/процесс.
- Изменение состояния само по себе не инициирует перерисовку. Handler решает, какие интерфейсы менять, удалять или оставлять без изменений.

## Минимальный пример

```python
from typing import Literal

from vkuxt import App, EventMetadata, Input, InputData, MemoryStorage

app = App(storage=MemoryStorage())


class GreetingMetadata(EventMetadata):
    user_id: int


class Greeting(InputData):
    handler_name: Literal["greeting-data"]
    text: str


@app.handler(background=False)
async def greeting(
    app: App,
    input: Input,
) -> None:
    if not isinstance(input.metadata, GreetingMetadata):
        return
    data = None
    if input.data is not None:
        try:
            data = Greeting.model_validate(input.data.model_dump())
        except ValueError:
            pass
    if data is not None:
        text = data.text
    elif input.text is not None:
        if input.text.casefold() != "привет":
            return
        text = input.text
    else:
        return
    key = {"kind": "greeting-count", "user_id": meta.user_id}
    async with app.lock(key):
        count = await app.get_state(key) or 0
        await app.set_state(key, count + 1)
```

Адаптер строит `GreetingMetadata` из транспортных полей. Хендлер получает объект `Input`, а при необходимости сам валидирует его базовый `InputData` в модель `Greeting`. `handler_name` — обычное поле модели: `Literal` ограничивает результат такой валидации, диспетчер это поле не интерпретирует. `background=False` ожидает выполнения и передаёт результат обратно адаптеру; для HTTP-ответа приложение само решает, как его использовать. `model_construct()` намеренно обходит обычную валидацию Pydantic.

Для кнопки приложение создаёт `Interface` и кладёт payload в кнопку. Когда площадка отправила/обновила сообщение и получила внешний ID, `Interface` сохраняется под этим ID:

```python
draft = app.new_interface()
payload = draft.bind(Greeting(handler_name="greeting-data", text="кнопка"))
# VK получает payload с обычными полями модели, включая handler_name.
await app.register_interface(vk_message_mount_id, draft)
```

`App.handle_input(raw)` передаёт запрос адаптерам по очереди и возвращает результат синхронного (`background=False`) хендлера. VK-адаптер строит `Input` с `VKMessageMetadata`, `InputData` и текстом; одно событие может содержать текст и payload одновременно. `VKMessageMetadata` включает `group_id`, `peer_id`, `user_id` и, если известно, `interface_id`.

## Хранилища и TTL

`StateStorage` — небольшой async-протокол строковых ключей/значений и TTL (`get`, `set`, `delete`). В комплекте:

- `MemoryStorage` — для тестов и локальной разработки;
- `JSONFileStorage(path)` — однопроцессное файловое хранение с атомарной заменой файла;
- `RedisStorage(async_redis_client)` — duck-typed адаптер для async redis-py.

Pydantic-модели состояния сохраняются как JSON; тип модели передаётся в `get_state(key, Model)`. Обычные dict/list/scalars можно читать без модели. `App(ttl=...)` задаёт TTL по умолчанию; у отдельного интерфейса он переопределяется через `app.new_interface(ttl=...)`. `None` означает без срока жизни. Истечение проверяется при чтении, а `matrix-planner` периодически вызывает необязательный метод `cleanup_expired()` у бэкендов, которым нужна физическая очистка. Если backend применяет TTL сам, реализовывать cleanup не нужно.

```python
from vkuxt import App, JSONFileStorage

app = App(JSONFileStorage("state.json"), ttl=60 * 60 * 24)
```

При передаче внешнего `Planner` его жизненным циклом управляет приложение-хозяин; `App.close()` снимает только задачу очистки этого приложения.

## VK Callback API

`VKCallbackAdapter` и `VKMessagesAdapter` — абстрактные слои для Callback API и VK Messages. Готовый вариант объединяет их: `VKMessagesCallbackAdapter`. Адаптеры создаются приложением через `app.add_adapter(AdapterType, ...)`; `App.start()` запускает их lifecycle, а `App.close()` останавливает в обратном порядке. Адаптер может использовать эти hooks для приёма событий или фонового опроса.

```python
adapter = app.add_adapter(VKMessagesCallbackAdapter, vk, group_id=group_id)
```

`App.lifespan` можно передать напрямую в Starlette/FastAPI, чтобы запускать и останавливать адаптеры вместе с ASGI-приложением:

```python
web = FastAPI(lifespan=app.lifespan)
```

VK Callback адаптер использует `vkx.server.CallbackServer` для проверки входа и разбора уведомлений в модели `vkx`; установите extra `vkuxt[vk]`. Текст поступает всем обработчикам, принимающим `str`; `VKMessageMetadata.interface_id` дополнительно указывает на сообщение, на которое ответили. В личных диалогах можно включить определение последнего активного интерфейса. Если callback-кнопка не привела к edit/delete именно нажатого интерфейса, адаптер отправляет `messages.sendMessageEventAnswer`.

Полный скетч крестиков-ноликов, включая лобби с кодом, ввод кода ответом на сообщение, две синхронизируемые доски и сдачу, находится в [`examples/tictactoe.py`](examples/tictactoe.py).

## Проверка

```powershell
uv sync --extra dev
.venv\Scripts\python.exe -m pytest
uvx ruff check src tests examples
```

Для запуска примера установите `vkx[http]` и FastAPI, создайте `VKClient` с токеном группы, настройте публичный Callback URL, получите `app` из `make_callback_app()` и передайте его в `attach_fastapi(app)`.
