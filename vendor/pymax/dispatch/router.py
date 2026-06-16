from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeAlias, TypeVar

from pymax.types import MessageDeleteEvent

from .enums import EventType

if TYPE_CHECKING:
    from pymax.client import Client
    from pymax.protocol import InboundFrame
    from pymax.types import Chat
    from pymax.types.domain import Message
    from pymax.types.events import (
        MessageReadEvent,
        PresenceEvent,
        ReactionUpdateEvent,
        TypingEvent,
    )


_EventT = TypeVar("_EventT")
ClientT = TypeVar("ClientT")

HandlerCallback: TypeAlias = Callable[
    [_EventT, ClientT],
    Awaitable[Any] | Any,
]
HandlerDecorator: TypeAlias = Callable[
    [HandlerCallback[_EventT, ClientT]],
    HandlerCallback[_EventT, ClientT],
]
FilterCallback: TypeAlias = Callable[
    [_EventT],
    Awaitable[bool] | bool,
]
StartCallback: TypeAlias = Callable[
    [ClientT],
    Awaitable[Any] | Any,
]
StartDecorator: TypeAlias = Callable[
    [StartCallback[ClientT]],
    StartCallback[ClientT],
]


@dataclass(slots=True)
class HandlerEntry(Generic[_EventT, ClientT]):
    callback: HandlerCallback[_EventT, ClientT]
    filters: tuple[FilterCallback[_EventT], ...] = ()


class Router(Generic[ClientT]):
    """Контейнер обработчиков событий PyMax.

    Роутер хранит обработчики и фильтры. Когда приходит событие, dispatcher
    проходит по root router и его дочерним роутерам, проверяет фильтры и
    вызывает подходящие callbacks как ``handler(event, client)``.

    Example:
        .. code-block:: python

           from pymax import Client, Message, Router

           router = Router[Client]()

           def is_start(message: Message) -> bool:
               return message.text == "/start"

           @router.on_message(is_start)
           async def start(message: Message, client: Client) -> None:
               await message.answer("Привет")

           client = Client(phone="+79990000000")
           client.include_router(router)
    """

    def __init__(self) -> None:
        self.handlers: dict[
            EventType,
            list[HandlerEntry[Any, ClientT]],
        ] = defaultdict(list)

        self.children: list[Router[ClientT]] = []
        self.on_start_handler: StartCallback[ClientT] | None = None

    def on(
        self,
        event: EventType,
        /,
        *filters: FilterCallback[_EventT],
    ) -> HandlerDecorator[_EventT, ClientT]:
        """Регистрирует обработчик события по ``EventType``.

        Args:
            event: Тип события.
            *filters: Фильтры, которые получают событие и возвращают bool.

        Returns:
            Декоратор для ``handler(event, client)``.

        Example:
            .. code-block:: python

               from pymax import Client
               from pymax.protocol import InboundFrame

               @router.on(EventType.RAW)
               async def raw(frame: InboundFrame, client: Client) -> None:
                   print(frame.payload)
        """

        def decorator(
            handler: HandlerCallback[_EventT, ClientT],
        ) -> HandlerCallback[_EventT, ClientT]:
            self.handlers[event].append(
                HandlerEntry(
                    callback=handler,
                    filters=filters,
                )
            )
            return handler

        return decorator

    def include_router(self, router: Router[ClientT]) -> None:
        """Подключает дочерний роутер.

        Args:
            router: Router, обработчики которого нужно добавить в дерево.

        Returns:
            ``None``.
        """
        self.children.append(router)

    def on_start(self) -> StartDecorator:
        """Регистрирует обработчик старта клиента.

        Returns:
            Декоратор для ``handler(client)``.
        """

        def decorator(handler: StartCallback) -> StartCallback:
            self.on_start_handler = handler
            return handler

        return decorator

    def on_message(
        self,
        *filters: FilterCallback[Message],
    ) -> HandlerDecorator[Message, ClientT]:
        """Регистрирует обработчик новых сообщений.

        Args:
            *filters: Фильтры для ``Message``.

        Returns:
            Декоратор для ``handler(message, client)``.
        """
        return self.on(EventType.MESSAGE_NEW, *filters)

    def on_message_edit(
        self,
        *filters: FilterCallback[Message],
    ) -> HandlerDecorator[Message, ClientT]:
        """Регистрирует обработчик редактирования сообщений.

        Args:
            *filters: Фильтры для ``Message``.

        Returns:
            Декоратор для ``handler(message, client)``.
        """
        return self.on(EventType.MESSAGE_EDIT, *filters)

    def on_message_delete(
        self,
        *filters: FilterCallback[MessageDeleteEvent],
    ) -> HandlerDecorator[MessageDeleteEvent, ClientT]:
        """Регистрирует обработчик удаления сообщений.

        Args:
            *filters: Фильтры для ``MessageDeleteEvent``.

        Returns:
            Декоратор для ``handler(event, client)``.
        """
        return self.on(EventType.MESSAGE_DELETE, *filters)

    def on_message_read(
        self,
        *filters: FilterCallback[MessageReadEvent],
    ) -> HandlerDecorator[MessageReadEvent, ClientT]:
        """Регистрирует обработчик изменения отметки прочтения."""
        return self.on(EventType.MESSAGE_READ, *filters)

    def on_typing(
        self,
        *filters: FilterCallback[TypingEvent],
    ) -> HandlerDecorator[TypingEvent, ClientT]:
        """Регистрирует обработчик набора текста."""
        return self.on(EventType.TYPING, *filters)

    def on_presence(
        self,
        *filters: FilterCallback[PresenceEvent],
    ) -> HandlerDecorator[PresenceEvent, ClientT]:
        """Регистрирует обработчик изменения присутствия пользователя."""
        return self.on(EventType.PRESENCE, *filters)

    def on_reaction_update(
        self,
        *filters: FilterCallback[ReactionUpdateEvent],
    ) -> HandlerDecorator[ReactionUpdateEvent, ClientT]:
        """Регистрирует обработчик обновления реакций сообщения."""
        return self.on(EventType.REACTION_UPDATE, *filters)

    def on_chat_update(
        self,
        *filters: FilterCallback[Chat],
    ) -> HandlerDecorator[Chat, ClientT]:
        """Регистрирует обработчик обновления чата.

        Args:
            *filters: Фильтры для ``Chat``.

        Returns:
            Декоратор для ``handler(chat, client)``.
        """
        return self.on(EventType.CHAT_UPDATE, *filters)

    def on_raw(
        self,
        *filters: FilterCallback[InboundFrame],
    ) -> HandlerDecorator[InboundFrame, ClientT]:
        """Регистрирует обработчик исходных frame-ов.

        Args:
            *filters: Фильтры для ``InboundFrame``.

        Returns:
            Декоратор для ``handler(frame, client)``.
        """
        return self.on(EventType.RAW, *filters)


ClientRouter: TypeAlias = Router["Client"]
