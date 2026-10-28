from importlib import import_module


def test_application_modules_import() -> None:
    for module in (
        "app.bot.main",
        "app.bot.ui",
        "app.services.drafts",
        "app.api.main",
        "app.workers.main",
        "app.telegram_gateway.enroll",
        "app.telegram_gateway.proxy",
    ):
        import_module(module)
