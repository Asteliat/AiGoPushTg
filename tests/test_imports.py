from importlib import import_module


def test_application_modules_import() -> None:
    for module in (
        "app.bot.main",
        "app.bot.ui",
        "app.services.channels",
            "app.services.ai_responses",
        "app.api.main",
        "app.workers.main",
        "app.services.challenge_monitor",
        "app.services.challenge_settings",
        "app.workers.challenge_watcher",
        "app.workers.redis_keys",
        "app.workers.state",
        "app.workers.lease",
        "app.workers.connection",
        "app.workers.memory",
        "app.workers.autoresponder",
        "app.workers.account_status",
        "app.workers.learning",
        "app.workers.channel_filters",
        "app.workers.dialogs_sync",
        "app.workers.reconnect",
        "app.workers.send_policy",
            "app.workers.channel_monitor",
        "app.telegram_gateway.enroll",
        "app.telegram_gateway.proxy",
    ):
        import_module(module)
