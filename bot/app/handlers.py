def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(start_command, Command("start"))
    dp.message.register(help_command, Command("help"))
    dp.message.register(status_command, Command("status"))
    dp.message.register(chats_command, Command("chats"))
    dp.message.register(history_command, Command("history"))
    dp.message.register(reauth_sms_command, Command("reauth_sms"))
    dp.message.register(code_command, Command("code"))
    dp.message.register(reply_command, Command("reply"))
    dp.message.register(cancel_command, Command("cancel"))
    dp.message.register(upload_session_command, Command("upload_session"))
    dp.message.register(sessions_command, Command("sessions"))

    dp.message.register(button_status, F.text == "ℹ️ Статус")
    dp.message.register(button_chats, F.text == "📚 Чаты")
    dp.message.register(button_help, F.text == "🆘 Помощь")
    dp.message.register(button_listen, F.text == "📥 Слушать MAX")
    dp.message.register(
        button_upload_session, F.text == "📂 Загрузить сессию MAX"
    )

    # FSM: загрузка session-файла
    dp.message.register(
        upload_session_file_handler,
        UploadSessionState.waiting_file,
        F.content_type == "document",
    )

    dp.message.register(
        reply_text, ReplyState.waiting_text, F.content_type == "text"
    )
    dp.message.register(
        reply_media,
        ReplyState.waiting_text,
        F.content_type.in_({"photo", "video", "document"}),
    )

    dp.callback_query.register(reply_callback, F.callback_data.startswith("reply:"))
    dp.callback_query.register(showid_callback, F.callback_data.startswith("showid:"))
    dp.callback_query.register(history_callback, F.callback_data.startswith("history:"))
    dp.callback_query.register(
        session_use_callback, F.callback_data.startswith("session_use:")
    )
    dp.callback_query.register(
        auth_action_callback, AuthActionCallback.filter()
    )