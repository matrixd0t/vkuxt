"""Анкета: пошаговое заполнение формы в диалоге VK.

Пользователь присылает текстовые значения ответом на сообщение-подсказку,
опыт работы выбирает кнопками. Шаг и введённые значения живут в ``ctx.state``
текущего интерфейса, готовая анкета сохраняется в storage приложения. Через
меню можно посмотреть сохранённую анкету или заполнить её заново.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from vkx import VKClient
from vkx.models import KeyboardButton

from vkuxt import (
    App,
    Context,
    InputData,
    JSONFileStorage,
    KeyboardButtonColor,
    StateNamespace,
    VKInterface,
    VKMessageMetadata,
    VKMessagesCallbackAdapter,
)

FORM_TTL = 30 * 60
MIN_AGE = 14
MAX_AGE = 120
START_WORDS = frozenset({"/form", "анкета", "форма"})

type FormStep = Literal[
    "idle",
    "name",
    "age",
    "experience",
    "city",
    "confirm",
    "done",
    "profile",
]
type Experience = Literal["none", "junior", "middle", "senior"]

EXPERIENCE_LABELS: dict[Experience, str] = {
    "none": "Нет опыта",
    "junior": "До 1 года",
    "middle": "1–3 года",
    "senior": "Более 3 лет",
}

_STEPS: tuple[FormStep, ...] = ("name", "age", "experience", "city", "confirm")
_PREVIOUS_STEP: dict[FormStep, FormStep] = {
    "age": "name",
    "experience": "age",
    "city": "experience",
    "confirm": "city",
}


class FormState(StateNamespace):
    step: FormStep = "idle"
    uid: int = 0
    name: str = ""
    age: int = 0
    experience: Experience | None = None
    city: str = ""
    error: str = ""


class Profile(BaseModel):
    name: str
    age: int
    experience: Experience
    city: str


type FormContext = Context[VKMessagesCallbackAdapter, FormState]


class FormInput(InputData):
    uid: int


class StartInput(FormInput):
    handler_name: Literal["form.start"] = "form.start"


class BackInput(FormInput):
    handler_name: Literal["form.back"] = "form.back"


class SubmitInput(FormInput):
    handler_name: Literal["form.submit"] = "form.submit"


class CancelInput(FormInput):
    handler_name: Literal["form.cancel"] = "form.cancel"


class ExperienceInput(FormInput):
    handler_name: Literal["form.experience"] = "form.experience"
    value: Experience


class ViewProfileInput(FormInput):
    handler_name: Literal["form.view"] = "form.view"


def normalize_name(raw: str) -> str | None:
    value = " ".join(raw.split())
    if not 2 <= len(value) <= 40:
        return None
    if not value[0].isalpha() or not value[-1].isalpha():
        return None
    if any(not (character.isalpha() or character in " -") for character in value):
        return None
    return value


def parse_age(raw: str) -> int | None:
    value = raw.strip()
    if not (value.isascii() and value.isdigit()):
        return None
    age = int(value)
    if not MIN_AGE <= age <= MAX_AGE:
        return None
    return age


def normalize_city(raw: str) -> str | None:
    value = " ".join(raw.split())
    if not 2 <= len(value) <= 60:
        return None
    if not value[0].isalpha():
        return None
    if any(not (character.isalpha() or character in " -.") for character in value):
        return None
    return value


def profile_key(metadata: VKMessageMetadata) -> dict[str, int | str]:
    return {
        "app": "profile-form",
        "kind": "profile",
        "group_id": metadata.group_id,
        "user_id": metadata.user_id,
    }


def is_owner(state: FormState, metadata: VKMessageMetadata) -> bool:
    return state.uid == metadata.user_id


def progress(step: FormStep) -> str:
    return f"Шаг {_STEPS.index(step) + 1} из {len(_STEPS)}"


def prompt_for(step: FormStep) -> str:
    match step:
        case "name":
            return "Как вас зовут? Напишите имя ответом на это сообщение."
        case "age":
            return (
                f"Сколько вам лет? Напишите число от {MIN_AGE} до {MAX_AGE} "
                "ответом на это сообщение."
            )
        case "city":
            return "В каком городе вы живёте? Напишите город ответом на это сообщение."
        case _:
            return ""


def experience_label(value: Experience | None) -> str:
    return "не выбрано" if value is None else EXPERIENCE_LABELS[value]


def filled_lines(state: FormState) -> list[str]:
    lines: list[str] = []
    if state.name:
        lines.append(f"Имя: {state.name}")
    if state.age:
        lines.append(f"Возраст: {state.age}")
    if state.experience is not None:
        lines.append(f"Опыт: {EXPERIENCE_LABELS[state.experience]}")
    if state.city:
        lines.append(f"Город: {state.city}")
    return lines


def profile_lines(profile: Profile) -> list[str]:
    return [
        f"Имя: {profile.name}",
        f"Возраст: {profile.age}",
        f"Опыт: {EXPERIENCE_LABELS[profile.experience]}",
        f"Город: {profile.city}",
    ]


def profile_from_state(state: FormState) -> Profile | None:
    if not state.name or not state.city or not state.age or state.experience is None:
        return None
    return Profile(
        name=state.name,
        age=state.age,
        experience=state.experience,
        city=state.city,
    )


def with_error(lines: list[str], error: str) -> str:
    if error:
        lines.extend(["", f"⚠ {error}"])
    return "\n".join(lines)


def menu_view(state: FormState, profile: Profile | None) -> VKInterface:
    lines = ["Анкета", ""]
    if profile is None:
        lines.append("Сохранённой анкеты пока нет.")
    else:
        lines.append(
            f"Сохранённая анкета: {profile.name}, возраст {profile.age}, "
            f"{EXPERIENCE_LABELS[profile.experience]}, {profile.city}."
        )
    actions = []
    if profile is not None:
        actions.append(
            VKInterface.callback(
                "Моя анкета",
                ViewProfileInput(uid=state.uid),
                color=KeyboardButtonColor.PRIMARY,
            )
        )
    actions.append(
        VKInterface.callback(
            "Заполнить заново" if profile is not None else "Заполнить анкету",
            StartInput(uid=state.uid),
            color=KeyboardButtonColor.POSITIVE,
        )
    )
    rows = [
        actions,
        [
            VKInterface.callback(
                "Закрыть",
                CancelInput(uid=state.uid),
                color=KeyboardButtonColor.NEGATIVE,
            )
        ],
    ]
    return VKInterface(text="\n".join(lines), kb=rows, ttl=FORM_TTL)


def saved_view(profile: Profile, uid: int) -> VKInterface:
    lines = ["Ваша анкета", "", *profile_lines(profile)]
    rows = [
        [
            VKInterface.callback(
                "Заполнить заново",
                StartInput(uid=uid),
                color=KeyboardButtonColor.POSITIVE,
            ),
            VKInterface.callback(
                "Закрыть",
                CancelInput(uid=uid),
                color=KeyboardButtonColor.NEGATIVE,
            ),
        ]
    ]
    return VKInterface(text="\n".join(lines), kb=rows, ttl=FORM_TTL)


def prompt_view(state: FormState) -> VKInterface:
    lines = [progress(state.step), "", prompt_for(state.step)]
    values = filled_lines(state)
    if values:
        lines.extend(["", *values])
    buttons = [
        VKInterface.callback(
            "Отмена",
            CancelInput(uid=state.uid),
            color=KeyboardButtonColor.NEGATIVE,
        )
    ]
    previous = _PREVIOUS_STEP.get(state.step)
    if previous is not None:
        buttons.insert(
            0,
            VKInterface.callback(
                "Назад",
                BackInput(uid=state.uid),
                color=KeyboardButtonColor.PRIMARY,
            ),
        )
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[buttons],
        ttl=FORM_TTL,
    )


def experience_button(state: FormState, value: Experience) -> KeyboardButton:
    return VKInterface.callback(
        EXPERIENCE_LABELS[value],
        ExperienceInput(uid=state.uid, value=value),
        color=(
            KeyboardButtonColor.POSITIVE
            if state.experience == value
            else KeyboardButtonColor.PRIMARY
        ),
    )


def experience_view(state: FormState) -> VKInterface:
    lines = [progress("experience"), "", "Выберите опыт работы:"]
    values = filled_lines(state)
    if values:
        lines.extend(["", *values])
    rows = [
        [experience_button(state, "none"), experience_button(state, "junior")],
        [experience_button(state, "middle"), experience_button(state, "senior")],
        [
            VKInterface.callback(
                "Назад",
                BackInput(uid=state.uid),
                color=KeyboardButtonColor.PRIMARY,
            ),
            VKInterface.callback(
                "Отмена",
                CancelInput(uid=state.uid),
                color=KeyboardButtonColor.NEGATIVE,
            ),
        ],
    ]
    return VKInterface(
        text=with_error(lines, state.error),
        kb=rows,
        ttl=FORM_TTL,
    )


def confirm_view(state: FormState) -> VKInterface:
    lines = [
        progress("confirm"),
        "",
        "Проверьте данные:",
        "",
        f"Имя: {state.name}",
        f"Возраст: {state.age}",
        f"Опыт: {experience_label(state.experience)}",
        f"Город: {state.city}",
    ]
    buttons = [
        VKInterface.callback(
            "Отправить",
            SubmitInput(uid=state.uid),
            color=KeyboardButtonColor.POSITIVE,
        ),
        VKInterface.callback(
            "Назад",
            BackInput(uid=state.uid),
            color=KeyboardButtonColor.PRIMARY,
        ),
        VKInterface.callback(
            "Отмена",
            CancelInput(uid=state.uid),
            color=KeyboardButtonColor.NEGATIVE,
        ),
    ]
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[buttons],
        ttl=FORM_TTL,
    )


def done_view(state: FormState) -> VKInterface:
    profile = profile_from_state(state)
    lines = ["Анкета сохранена.", ""]
    if profile is not None:
        lines.extend(profile_lines(profile))
    rows = [
        [
            VKInterface.callback(
                "Заполнить заново",
                StartInput(uid=state.uid),
                color=KeyboardButtonColor.POSITIVE,
            ),
            VKInterface.callback(
                "Закрыть",
                CancelInput(uid=state.uid),
                color=KeyboardButtonColor.NEGATIVE,
            ),
        ]
    ]
    return VKInterface(text="\n".join(lines), kb=rows, ttl=FORM_TTL)


def active_view(state: FormState) -> VKInterface:
    if state.step == "experience":
        return experience_view(state)
    if state.step == "confirm":
        return confirm_view(state)
    if state.step == "done":
        return done_view(state)
    return prompt_view(state)


async def form_text(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    """Принять текст: команду запуска или значение текущего шага."""
    if metadata.event_type != "message_new":
        return
    value = " ".join(text.split())
    if not value:
        return

    state = ctx.state
    if state.step == "idle":
        if value.casefold() in START_WORDS:
            profile = await ctx.app.get_state(profile_key(metadata), Profile)
            ctx.state = FormState(step="idle", uid=metadata.user_id)
            await ctx.redraw(menu_view(ctx.state, profile))
        return
    if not is_owner(state, metadata):
        return
    if state.step in {"done", "profile"}:
        if value.casefold() in START_WORDS:
            profile = await ctx.app.get_state(profile_key(metadata), Profile)
            ctx.state = FormState(step="idle", uid=metadata.user_id)
            await ctx.redraw(menu_view(ctx.state, profile))
        return
    if state.step == "confirm":
        state.error = "Проверьте данные и нажмите «Отправить»."
        await ctx.redraw(confirm_view(state))
        return
    if state.step == "experience":
        state.error = "Выберите опыт работы кнопкой."
        await ctx.redraw(experience_view(state))
        return

    match state.step:
        case "name":
            name = normalize_name(value)
            if name is None:
                state.error = "Имя — от 2 до 40 букв, пробелов или дефисов."
                await ctx.redraw(prompt_view(state))
                return
            state.name = name
            state.error = ""
            state.step = "age"
            await ctx.redraw(prompt_view(state))
        case "age":
            age = parse_age(value)
            if age is None:
                state.error = f"Возраст — целое число от {MIN_AGE} до {MAX_AGE}."
                await ctx.redraw(prompt_view(state))
                return
            state.age = age
            state.error = ""
            state.step = "experience"
            await ctx.redraw(experience_view(state))
        case "city":
            city = normalize_city(value)
            if city is None:
                state.error = "Город — от 2 до 60 букв, пробелов, дефисов или точек."
                await ctx.redraw(prompt_view(state))
                return
            state.city = city
            state.error = ""
            state.step = "confirm"
            await ctx.redraw(confirm_view(state))


async def start(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: StartInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    ctx.state = FormState(step="name", uid=metadata.user_id)
    await ctx.redraw(prompt_view(ctx.state))


async def choose_experience(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: ExperienceInput,
    text: str,
) -> None:
    state = ctx.state
    if data.uid != metadata.user_id or not is_owner(state, metadata):
        return
    if state.step != "experience":
        return
    state.experience = data.value
    state.error = ""
    state.step = "city"
    await ctx.redraw(prompt_view(state))


async def show_profile(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: ViewProfileInput,
    text: str,
) -> None:
    state = ctx.state
    if data.uid != metadata.user_id or not is_owner(state, metadata):
        return
    profile = await ctx.app.get_state(profile_key(metadata), Profile)
    if profile is None:
        state.step = "idle"
        state.error = ""
        await ctx.redraw(menu_view(state, None))
        return
    state.step = "profile"
    state.error = ""
    await ctx.redraw(saved_view(profile, state.uid))


async def go_back(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: BackInput,
    text: str,
) -> None:
    state = ctx.state
    if data.uid != metadata.user_id or not is_owner(state, metadata):
        return
    previous = _PREVIOUS_STEP.get(state.step)
    if previous is None:
        return
    state.step = previous
    state.error = ""
    await ctx.redraw(active_view(state))


async def submit(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: SubmitInput,
    text: str,
) -> None:
    state = ctx.state
    if data.uid != metadata.user_id or not is_owner(state, metadata):
        return
    profile = profile_from_state(state)
    if state.step != "confirm" or profile is None:
        return
    await ctx.app.set_state(profile_key(metadata), profile)
    state.step = "done"
    state.error = ""
    await ctx.redraw(done_view(state))


async def cancel(
    ctx: FormContext,
    metadata: VKMessageMetadata,
    data: CancelInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id:
        return
    await ctx.redraw(None)


def register_handlers(app: App) -> None:
    for handler in (form_text, start, choose_experience, show_profile, go_back, submit, cancel):
        app.add_handler(handler)


def build_app(
    vk: VKClient,
    group_id: int,
    *,
    storage_path: str = "profile-form-state.json",
) -> App:
    """Собрать приложение; HTTP-обвязка — как в examples/tictactoe.py."""
    app = App(JSONFileStorage(storage_path))
    app.add_adapter(VKMessagesCallbackAdapter(app, vk, group_id))
    register_handlers(app)
    return app
