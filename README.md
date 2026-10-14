# vkuxt

`vkuxt` — асинхронный каркас для приложений, в которых пользовательские действия приходят через адаптеры, например VK-бота. Он не навязывает FSM, схему бизнес-состояния, шаблонизатор, рендеринг или правила перерисовки. Транспорт и типизированные VK API-вызовы предоставляет `vkx`.

## Модель

- `App` передаёт сырой ввод адаптерам по очереди. Первый распознавший формат адаптер возвращает `Input` с полями `metadata`, `data`, `text` и `interface_id`; текст и typed payload могут присутствовать одновременно.
- `metadata` содержит адаптер-специфичный экземпляр `InputMetadata`, `data` — базовый `InputData` или `None`, `text` — строку или `None`, а `interface_id` — ID найденного сохранённого интерфейса или `None`.
- `App.add_handler()` не проверяет сигнатуру; при вызове хендлер получает `(app, input)` и сам проверяет метаданные и данные. Сначала последовательно вызываются `background=False` хендлеры в порядке регистрации; первый непустой результат возвращается адаптеру. Если они ничего не вернули, все `background=True` хендлеры последовательно запускаются одной `asyncio.Task`. `StopIteration` прекращает текущую фазу опроса.
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

from vkuxt import App, InputMetadata, Input, InputData, MemoryStorage

app = App(storage=MemoryStorage())


class GreetingMetadata(InputMetadata):
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
    key = {"kind": "greeting-count", "user_id": input.metadata.user_id}
    async with app.lock(key):
        count = await app.get_state(key) or 0
        await app.set_state(key, count + 1)
```

Адаптер строит `GreetingMetadata` из транспортных полей. Хендлер получает объект `Input`, а при необходимости сам валидирует его базовый `InputData` в модель `Greeting`. `handler_name` — обычное поле модели: `Literal` ограничивает результат такой валидации, диспетчер это поле не интерпретирует. Результат foreground-хендлера возвращается адаптеру; если результата нет, адаптер выбирает свой стандартный ответ. `model_construct()` намеренно обходит обычную валидацию Pydantic.

Для кнопки приложение создаёт `Interface` и кладёт payload в кнопку. Когда площадка отправила/обновила сообщение и получила внешний ID, `Interface` сохраняется под этим ID:

```python
draft = app.new_interface()
payload = draft.bind(Greeting(handler_name="greeting-data", text="кнопка"))
# VK получает payload с обычными полями модели, включая handler_name.
await app.register_interface(vk_message_mount_id, draft)
```

`App.handle_input(raw)` передаёт запрос адаптерам по очереди и возвращает результат foreground-хендлера. VK-адаптер строит `Input` с `VKMessageMetadata`, `InputData`, текстом и `interface_id`; одно событие может содержать текст и payload одновременно. `Input.interface_id` задан, только если адаптер нашёл в приложении сохранённый интерфейс для этого события.

## Хранилища и TTL

`StateStorage` — async-протокол строковых ключей/значений, TTL и `cleanup_expired()`. В комплекте:

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

`App.lifespan` можно передать напрямую в Starlette/FastAPI, чтобы запускать и останавливать адаптеры вместе с ASGI-приложением:

```python
web = FastAPI(lifespan=app.lifespan)
```

VK Callback адаптер использует `vkx.server.CallbackServer` для проверки входа и разбора уведомлений в модели `vkx`; установите extra `vkuxt[vk]`. Текст поступает всем хендлерам; `Input.interface_id` указывает на найденный сохранённый интерфейс. В личных диалогах можно включить определение последнего активного интерфейса. Если callback-кнопка не привела к edit/delete именно нажатого интерфейса, адаптер отправляет `messages.sendMessageEventAnswer`.

Полный скетч крестиков-ноликов, включая лобби с кодом, ввод кода ответом на сообщение, две синхронизируемые доски и сдачу, находится в [`examples/tictactoe.py`](examples/tictactoe.py).

## Проверка

```powershell
uv sync --extra dev
.venv\Scripts\python.exe -m pytest
uvx ruff check src tests examples
```

Для запуска примера установите `vkx[http]` и FastAPI, создайте `VKClient` с токеном группы, настройте публичный Callback URL, получите `app` из `make_callback_app()` и передайте его в `attach_fastapi(app)`.
