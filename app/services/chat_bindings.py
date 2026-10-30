"""Helpers for keeping newly discovered Telegram dialogs opt-in."""
def discovered_binding_values(title: str) -> dict[str, object]:
    return {
        "title": (title or "").strip()[:255],
        "enabled": False,
        "auto_discovered": True,
    }
