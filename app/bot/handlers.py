from aiogram import BaseMiddleware
from app.bot.ui import router

class CallbackDataCompatibilityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        value = getattr(event, "data", None)
        if value:
            parts = value.split(":")
            if value.startswith("acc:setagent:") and len(parts) == 4:
                object.__setattr__(event, "data", ":".join(parts[:2] + ["x"] + parts[2:]))
            elif value.startswith("acc:setpersona:") and len(parts) == 4:
                object.__setattr__(event, "data", ":".join(parts[:2] + ["x"] + parts[2:]))
            elif value.startswith("chat:select:") and len(parts) == 4:
                object.__setattr__(event, "data", ":".join(parts[:2] + ["x"] + parts[2:]))
        return await handler(event, data)

router.callback_query.outer_middleware(CallbackDataCompatibilityMiddleware())
