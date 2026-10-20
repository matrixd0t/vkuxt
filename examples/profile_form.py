"""Анкета: пошаговое заполнение формы в диалоге VK.

Экранные состояния объявлены отдельными моделями: хендлеры получают только
события своего шага, а переходы выполняются через ``ctx.redraw(view, state=...)``.
Готовая анкета обновляется атомарно через ``ctx.locked_state`` и переживает
перезапуск приложения.
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
START_WORDS = frozenset({"/form", "/start", "анкета", "форма", "начать", "заполнить"})

type Experience = Literal["none", "junior", "middle", "senior"]

EXPERIENCE_LABELS: dict[Experience, str] = {
    "none": "Нет опыта",
    "junior": "До 1 года",
    "middle": "1–3 года",
    "senior": "Более 3 лет",
}

_STEP_NUMBERS: dict[str, int] = {
    "name": 1,
    "age": 2,
    "experience": 3,
    "city": 4,
    "confirm": 5,
}


class Profile(BaseModel):
    name: str
    age: int
    experience: Experience
    city: str
    revision: int = 0


class FormState(StateNamespace):
    uid: int
    name: str = ""
    age: int = 0
    experience: Experience | None = None
    city: str = ""
    error: str = ""


class MenuState(FormState):
    phase: Literal["menu"] = "menu"


class NameState(FormState):
    phase: Literal["name"] = "name"


class AgeState(FormState):
    phase: Literal["age"] = "age"
    name: str


class ExperienceState(FormState):
    phase: Literal["experience"] = "experience"
    name: str
    age: int


class CityState(FormState):
    phase: Literal["city"] = "city"
    name: str
    age: int
    experience: Experience


class ConfirmState(FormState):
    phase: Literal["confirm"] = "confirm"
    name: str
    age: int
    experience: Experience
    city: str


class DoneState(FormState):
    phase: Literal["done"] = "done"
    name: str
    age: int
    experience: Experience
    city: str


class ProfileState(FormState):
    phase: Literal["profile"] = "profile"


type FormContext[StateT: StateNamespace | None = StateNamespace] = Context[
    VKMessagesCallbackAdapter,
    StateT,
]
type MenuLikeState = MenuState | DoneState | ProfileState
type BackState = AgeState | ExperienceState | CityState | ConfirmState


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


def is_start(text: str) -> bool:
    return " ".join(text.split()).casefold() in START_WORDS


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


def progress(phase: str) -> str:
    return f"Шаг {_STEP_NUMBERS[phase]} из {len(_STEP_NUMBERS)}"


def progress_lines(step: str, prompt: str) -> list[str]:
    return [progress(step), "", prompt]


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


def menu_view(uid: int, profile: Profile | None) -> VKInterface:
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
                ViewProfileInput(uid=uid),
                color=KeyboardButtonColor.PRIMARY,
            )
        )
    actions.append(
        VKInterface.callback(
            "Заполнить заново" if profile is not None else "Заполнить анкету",
            StartInput(uid=uid),
            color=KeyboardButtonColor.POSITIVE,
        )
    )
    return VKInterface(
        text="\n".join(lines),
        kb=[
            actions,
            [
                VKInterface.callback(
                    "Закрыть",
                    CancelInput(uid=uid),
                    color=KeyboardButtonColor.NEGATIVE,
                )
            ],
        ],
        ttl=FORM_TTL,
    )


def saved_view(profile: Profile, uid: int) -> VKInterface:
    return VKInterface(
        text="\n".join(["Ваша анкета", "", *profile_lines(profile)]),
        kb=[
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
        ],
        ttl=FORM_TTL,
    )


def name_view(state: NameState) -> VKInterface:
    lines = progress_lines(
        "name",
        "Как вас зовут? Напишите имя ответом на это сообщение.",
    )
    values = filled_lines(state)
    if values:
        lines.extend(["", *values])
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[
            [
                VKInterface.callback(
                    "Отмена",
                    CancelInput(uid=state.uid),
                    color=KeyboardButtonColor.NEGATIVE,
                )
            ]
        ],
        ttl=FORM_TTL,
    )


def age_view(state: AgeState) -> VKInterface:
    lines = progress_lines(
        "age",
        f"Сколько вам лет? Напишите число от {MIN_AGE} до {MAX_AGE} "
        "ответом на это сообщение.",
    )
    values = filled_lines(state)
    if values:
        lines.extend(["", *values])
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[
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
            ]
        ],
        ttl=FORM_TTL,
    )


def experience_button(state: ExperienceState, value: Experience) -> KeyboardButton:
    return VKInterface.callback(
        EXPERIENCE_LABELS[value],
        ExperienceInput(uid=state.uid, value=value),
        color=(
            KeyboardButtonColor.POSITIVE
            if state.experience == value
            else KeyboardButtonColor.PRIMARY
        ),
    )


def experience_view(state: ExperienceState) -> VKInterface:
    lines = progress_lines("experience", "Выберите опыт работы:")
    values = filled_lines(state)
    if values:
        lines.extend(["", *values])
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[
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
        ],
        ttl=FORM_TTL,
    )


def city_view(state: CityState) -> VKInterface:
    lines = progress_lines(
        "city",
        "В каком городе вы живёте? Напишите город ответом на это сообщение.",
    )
    values = filled_lines(state)
    if values:
        lines.extend(["", *values])
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[
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
            ]
        ],
        ttl=FORM_TTL,
    )


def confirm_view(state: ConfirmState) -> VKInterface:
    lines = [
        progress("confirm"),
        "",
        "Проверьте данные:",
        "",
        f"Имя: {state.name}",
        f"Возраст: {state.age}",
        f"Опыт: {EXPERIENCE_LABELS[state.experience]}",
        f"Город: {state.city}",
    ]
    return VKInterface(
        text=with_error(lines, state.error),
        kb=[
            [
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
        ],
        ttl=FORM_TTL,
    )


def done_view(state: DoneState) -> VKInterface:
    profile = profile_from_state(state)
    lines = ["Анкета сохранена.", ""]
    if profile is not None:
        lines.extend(profile_lines(profile))
    return VKInterface(
        text="\n".join(lines),
        kb=[
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
        ],
        ttl=FORM_TTL,
    )


async def start_from_empty(
    ctx: FormContext[None],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    """Начать с пустого состояния: у пользователя ещё нет экрана."""
    if metadata.event_type != "message_new" or not is_start(text):
        return
    profile = await ctx.app.get_state(profile_key(metadata), Profile)
    state = MenuState(uid=metadata.user_id)
    await ctx.redraw(menu_view(metadata.user_id, profile), state=state)


async def open_menu(
    ctx: FormContext[MenuLikeState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new" or not is_start(text):
        return
    if ctx.state.uid != metadata.user_id:
        return
    profile = await ctx.app.get_state(profile_key(metadata), Profile)
    state = MenuState(uid=metadata.user_id)
    await ctx.redraw(menu_view(metadata.user_id, profile), state=state)


async def begin_form(
    ctx: FormContext[MenuLikeState],
    metadata: VKMessageMetadata,
    data: StartInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id or ctx.state.uid != metadata.user_id:
        return
    state = NameState(uid=metadata.user_id)
    await ctx.redraw(name_view(state), state=state)


async def enter_name(
    ctx: FormContext[NameState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new":
        return
    if ctx.state.uid != metadata.user_id:
        return
    name = normalize_name(text)
    if name is None:
        ctx.state.error = "Имя — от 2 до 40 букв, пробелов или дефисов."
        await ctx.redraw(name_view(ctx.state))
        return
    state = AgeState(uid=ctx.state.uid, name=name)
    await ctx.redraw(age_view(state), state=state)


async def enter_age(
    ctx: FormContext[AgeState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new":
        return
    if ctx.state.uid != metadata.user_id:
        return
    age = parse_age(text)
    if age is None:
        ctx.state.error = f"Возраст — целое число от {MIN_AGE} до {MAX_AGE}."
        await ctx.redraw(age_view(ctx.state))
        return
    state = ExperienceState(uid=ctx.state.uid, name=ctx.state.name, age=age)
    await ctx.redraw(experience_view(state), state=state)


async def experience_text(
    ctx: FormContext[ExperienceState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new":
        return
    if ctx.state.uid != metadata.user_id:
        return
    ctx.state.error = "Выберите опыт работы кнопкой."
    await ctx.redraw(experience_view(ctx.state))


async def choose_experience(
    ctx: FormContext[ExperienceState],
    metadata: VKMessageMetadata,
    data: ExperienceInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id or ctx.state.uid != metadata.user_id:
        return
    state = CityState(
        uid=ctx.state.uid,
        name=ctx.state.name,
        age=ctx.state.age,
        experience=data.value,
    )
    await ctx.redraw(city_view(state), state=state)


async def enter_city(
    ctx: FormContext[CityState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new":
        return
    if ctx.state.uid != metadata.user_id:
        return
    city = normalize_city(text)
    if city is None:
        ctx.state.error = "Город — от 2 до 60 букв, пробелов, дефисов или точек."
        await ctx.redraw(city_view(ctx.state))
        return
    state = ConfirmState(
        uid=ctx.state.uid,
        name=ctx.state.name,
        age=ctx.state.age,
        experience=ctx.state.experience,
        city=city,
    )
    await ctx.redraw(confirm_view(state), state=state)


async def confirm_text(
    ctx: FormContext[ConfirmState],
    metadata: VKMessageMetadata,
    data: None,
    text: str,
) -> None:
    if metadata.event_type != "message_new":
        return
    if ctx.state.uid != metadata.user_id:
        return
    ctx.state.error = "Проверьте данные и нажмите «Отправить»."
    await ctx.redraw(confirm_view(ctx.state))


async def go_back(
    ctx: FormContext[BackState],
    metadata: VKMessageMetadata,
    data: BackInput,
    text: str,
) -> None:
    state = ctx.state
    if data.uid != metadata.user_id or state.uid != metadata.user_id:
        return
    if isinstance(state, AgeState):
        target = NameState(uid=state.uid, name=state.name)
        await ctx.redraw(name_view(target), state=target)
    elif isinstance(state, ExperienceState):
        target = AgeState(uid=state.uid, name=state.name, age=state.age)
        await ctx.redraw(age_view(target), state=target)
    elif isinstance(state, CityState):
        target = ExperienceState(
            uid=state.uid,
            name=state.name,
            age=state.age,
            experience=state.experience,
        )
        await ctx.redraw(experience_view(target), state=target)
    else:
        target = CityState(
            uid=state.uid,
            name=state.name,
            age=state.age,
            experience=state.experience,
            city=state.city,
        )
        await ctx.redraw(city_view(target), state=target)


async def show_profile(
    ctx: FormContext[MenuState],
    metadata: VKMessageMetadata,
    data: ViewProfileInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id or ctx.state.uid != metadata.user_id:
        return
    profile = await ctx.app.get_state(profile_key(metadata), Profile)
    if profile is None:
        state = MenuState(uid=metadata.user_id)
        await ctx.redraw(menu_view(metadata.user_id, None), state=state)
        return
    state = ProfileState(uid=metadata.user_id)
    await ctx.redraw(saved_view(profile, metadata.user_id), state=state)


async def submit(
    ctx: FormContext[ConfirmState],
    metadata: VKMessageMetadata,
    data: SubmitInput,
    text: str,
) -> None:
    state = ctx.state
    if data.uid != metadata.user_id or state.uid != metadata.user_id:
        return
    profile = Profile(
        name=state.name,
        age=state.age,
        experience=state.experience,
        city=state.city,
    )
    async with ctx.locked_state(
        profile_key(metadata),
        Profile,
        default=profile,
    ) as saved:
        saved.name = state.name
        saved.age = state.age
        saved.experience = state.experience
        saved.city = state.city
        saved.revision += 1
    done = DoneState(
        uid=state.uid,
        name=state.name,
        age=state.age,
        experience=state.experience,
        city=state.city,
    )
    await ctx.redraw(done_view(done), state=done)


async def cancel(
    ctx: FormContext[FormState],
    metadata: VKMessageMetadata,
    data: CancelInput,
    text: str,
) -> None:
    if data.uid != metadata.user_id or ctx.state.uid != metadata.user_id:
        return
    await ctx.redraw(None)


def register_handlers(app: App) -> None:
    for handler in (
        start_from_empty,
        open_menu,
        begin_form,
        enter_name,
        enter_age,
        experience_text,
        choose_experience,
        enter_city,
        confirm_text,
        go_back,
        show_profile,
        submit,
        cancel,
    ):
        app.add_handler(handler)


def build_app(
    vk: VKClient,
    group_id: int,
    *,
    storage_path: str = "profile-form-state.json",
    dm_latest_interface: bool = True,
) -> App:
    """Собрать приложение; HTTP-обвязка — как в examples/tictactoe.py."""
    app = App(JSONFileStorage(storage_path))
    app.add_adapter(
        VKMessagesCallbackAdapter(
            app,
            vk,
            group_id,
            dm_latest_interface=dm_latest_interface,
        )
    )
    register_handlers(app)
    return app
