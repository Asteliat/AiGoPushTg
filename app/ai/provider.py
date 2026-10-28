from openai import AsyncOpenAI
from app.storage.models import AIAgent

class AIUnavailableError(RuntimeError):
    pass

async def generate_reply(*, agent: AIAgent, incoming_text: str, style_instructions: str, examples: list[tuple[str, str]]) -> str:
    if not agent.enabled or not agent.api_key.strip():
        raise AIUnavailableError("ИИ-агент выключен или у него не заполнен API-ключ.")
    if not agent.base_url.strip() or not agent.model.strip():
        raise AIUnavailableError("Заполните адрес API и модель в настройках ИИ-агента.")
    client = AsyncOpenAI(api_key=agent.api_key, base_url=agent.base_url, timeout=agent.timeout_seconds)
    sample = "\n\n".join(f"Сообщение: {a}\nПредпочтительный ответ: {b}" for a, b in examples[:8]) or "Примеров нет."
    try:
        response = await client.chat.completions.create(
            model=agent.model, temperature=0.7,
            messages=[
                {"role": "system", "content": "Составь черновик ответа. Не выдумывай факты, не помогай со спамом, преследованием или скоординированным обманом. Не копируй персональные данные. Стиль: " + (style_instructions or "кратко, уважительно и естественно") + "\nПримеры:\n" + sample},
                {"role": "user", "content": "Подготовь ответ на сообщение:\n" + incoming_text},
            ],
        )
    except Exception as exc:
        raise AIUnavailableError(f"Ошибка провайдера ИИ: {type(exc).__name__}: {str(exc)[:300]}") from exc
    result = response.choices[0].message.content
    if not result or not result.strip():
        raise AIUnavailableError("Провайдер ИИ вернул пустой ответ.")
    return result.strip()
