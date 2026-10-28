from openai import AsyncOpenAI
from app.settings import get_settings

class AIUnavailableError(RuntimeError):
    pass

async def generate_reply(*, incoming_text: str, style_instructions: str, examples: list[tuple[str, str]]) -> str:
    cfg = get_settings()
    if not cfg.ai_enabled or not cfg.ai_api_key:
        raise AIUnavailableError("ИИ отключён: настройте AI_ENABLED=true и AI_API_KEY в .env.")
    client = AsyncOpenAI(api_key=cfg.ai_api_key, base_url=cfg.ai_base_url, timeout=cfg.ai_timeout_seconds)
    sample = "\n\n".join(f"Сообщение: {a}\nПредпочтительный ответ: {b}" for a, b in examples[:8]) or "Примеров нет."
    response = await client.chat.completions.create(
        model=cfg.ai_model, temperature=0.7,
        messages=[
            {"role": "system", "content": "Составь только черновик ответа для проверки оператором; никогда не отправляй его сам. Не выдавай ИИ за человека, не выдумывай факты, не помогай со спамом, преследованием или скоординированным обманом. Не копируй персональные данные. Стиль: " + (style_instructions or "кратко, уважительно и естественно") + "\nПримеры:\n" + sample},
            {"role": "user", "content": "Подготовь черновик ответа на сообщение:\n" + incoming_text},
        ],
    )
    result = response.choices[0].message.content
    if not result or not result.strip():
        raise AIUnavailableError("Провайдер ИИ вернул пустой ответ.")
    return result.strip()
