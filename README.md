# vkuxt

`vkuxt` — асинхронный каркас для приложений, в которых пользовательские действия приходят через адаптеры, например VK-бота. Он не навязывает FSM, схему бизнес-состояния, шаблонизатор, рендеринг или правила перерисовки. Транспорт и типизированные VK API-вызовы предоставляет `vkx`.

## Модель

- `App` передаёт сырой ввод адаптерам по очереди. Первый распознавший формат адаптер возвращает метаданные и смысловую часть события.
- `EventMetadata` описывает данные отправителя и транспорта. Типизированные данные наследуются от `InputData`; произвольный ввод передаётся обработчику обычной строкой.
- `@app.handler` выводит допустимые модели метаданных и ввода из строгой сигнатуры `(app: App, meta: EventMetadata, input?, data?)`. Подходящие хендлеры получают событие по совместимости типов; имена функций на маршрутизацию не влияют.
- `InputData` проверяет строгую JSON-сериализуемость при Pydantic-валидации и присваивании. `handler_name` — обычное поле модели; если нужно сузить совпадение по данным, объявите его как `Literal[...]` в конкретной модели.
- Ключи приложения — обычные словари или модели. `encode_key` канонически сериализует их; модели сохраняют в сериализации полное имя типа, данные сортируются по ключам. Такой же codec используется всеми storage-бэкендами и адаптерами.
- Интерфейс адресуется внешним идентификатором. Для сообщения VK это `VKMessageMountId(cmid, peer_id, group_id)`; отдельного внутреннего ID нет.
- `InterfaceDraft.bind()` обычно возвращает словарь typed input напрямую. Только payload длиннее лимита VK заменяется коротким token, данные которого хранятся в записи интерфейса; опция отключается через `App(tokenize_long_input=False)`.
- Повторный `register_interface()` заменяет таблицу больших token-payload. Рендеринг остаётся ответственностью приложения или адаптера.
- Handler может явно блокировать одно или несколько состояний через `async with app.lock(key, ...)`. Lock удерживается во время тела блока; читайте состояние после получения lock. Ключи блокируются в каноническом порядке, вложенное получение того же lock допустимо. Блокировки процессные и рассчитаны на один event loop/процесс.
- Изменение состояния само по себе не инициирует перерисовку. Handler решает, какие интерфейсы менять, удалять или оставлять без изменений.

## Минимальный пример

```python
from typing import Literal

from vkuxt import App, EventMetadata, InputData, MemoryStorage

app = App(storage=MemoryStorage())

class GreetingMetadata(EventMetadata):
    user_id: int

class Greeting(InputData):
    handler_name: Literal["greeting-data"]
    text: str

@app.handler
async def greeting(
    app: App,
    meta: GreetingMetadata,
    input: str | None = None,
    data: Greeting | None = None,
) -> None:
    if data is not None:
        text = data.text
    elif input is not None:
        if input.casefold() != "привет":
            return
        text = input
    else:
        return
    key = {"kind": "greeting-count", "user_id": meta.user_id}
    async with app.lock(key):
        count = await app.get_state(key) or 0
        await app.set_state(key, count + 1)
```

Адаптер строит `GreetingMetadata` из транспортных полей. Для произвольного текста обработчик получает `input: str`, а для typed payload — экземпляр модели `Greeting`. `handler_name` здесь — обычное поле модели: `Literal` ограничивает, какие данные принимает эта аннотация; диспетчер не интерпретирует это поле. `model_construct()` намеренно обходит обычную валидацию Pydantic.

Для кнопки приложение создаёт draft интерфейса и кладёт payload в кнопку. Когда площадка отправила/обновила сообщение и получила внешний ID, draft сохраняется под этим ID:

```python
draft = app.new_interface()
payload = draft.bind(Greeting(handler_name="greeting-data", text="кнопка"))
# VK получает payload с обычными полями модели, включая handler_name.
await app.register_interface(vk_message_mount_id, draft)
```

`App.handle_input(raw)` передаёт запрос адаптерам по очереди. VK-адаптер возвращает `VKMessageMetadata` отдельно от строки либо словаря payload-кнопки; приложение валидирует словарь во все подходящие `data`-модели по сигнатурам хендлеров. `VKMessageMetadata` включает `group_id`, `chat_id`, `user_id` и, если известно, `interface_id`.

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
