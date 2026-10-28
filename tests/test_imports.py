from importlib import import_module


def test_application_modules_import() -> None:
    for module in (
        "app.bot.main",
        "app.api.main",
        "app.workers.main",
        "app.telegram_gateway.enroll",
    ):
        import_module(module)
