import asyncio
from openai import AsyncOpenAI
from app.storage.models import AIAgent

class AIUnavailableError(RuntimeError):
    pass

class NoReplyNeeded(Exception):
    """Raised when the model decides a proactive group message would be unnatural."""
    pass

async def chat_completion_with_retry(client, **kwargs):
    """Retry only transient provider failures; do not repeat permanent request errors."""
    for attempt in range(3):
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            transient = (
                status in {408, 409, 429}
                or (isinstance(status, int) and status >= 500)
                or type(exc).__name__ in {"APIConnectionError", "APITimeoutError", "TimeoutError", "ConnectionError"}
            )
            if not transient or attempt == 2:
                raise
            await asyncio.sleep(min(2 ** attempt, 8))

TRIGGER_CONTEXT_POLICY = (
    "РЕЖИМ ОТВЕТА ПО ТРИГГЕРУ. Текущее сообщение — главная и единственная реплика, на которую нужно подготовить ответ. "
    "Предыдущие сообщения того же участника из этого же чата — только вспомогательный контекст. "
    "Используй из истории исключительно те сведения, которые относятся к теме сработавшего триггера "
    "и помогают понять намерение, вопрос или ситуацию участника. Все остальные темы игнорируй. "
    "Не отвечай на отдельные старые вопросы, не повторяй уже данные ответы и не воспринимай историю "
    "как новые запросы. Если история не связана с темой триггера, не используй её. "
    "Если история противоречит текущему сообщению, ориентируйся на текущее сообщение. "
    "Не додумывай отсутствующие факты. Индивидуальная инструкция триггера задаёт предмет и цель ответа, "
    "а это правило определяет только то, как использовать историю переписки. "
)


async def generate_reply(*, agent: AIAgent, incoming_text: str, style_instructions: str, examples: list[tuple[str, str]], proactive: bool = False, conversation_context: str = "", trigger_mode: bool = False) -> str:
    if not agent.enabled or not agent.api_key.strip():
        raise AIUnavailableError("ИИ-агент выключен или у него не заполнен API-ключ.")
    if not agent.base_url.strip() or not agent.model.strip():
        raise AIUnavailableError("Заполните адрес API и модель в настройках ИИ-агента.")
    client = AsyncOpenAI(api_key=agent.api_key, base_url=agent.base_url, timeout=agent.timeout_seconds)
    sample = "\n\n".join(f"Сообщение: {a}\nПредпочтительный ответ: {b}" for a, b in [(a[:700], b[:700]) for a, b in examples[:8]]) or "Примеров нет."
    try:
        response = await chat_completion_with_retry(client,
            model=agent.model, temperature=0.7,
            messages=[
                {"role": "system", "content": ("Ты участник живого группового чата. Решение о том, отвечать ли на это сообщение, уже принято настройкой вероятности до вызова ИИ. Твоя задача — всегда подготовить короткий, содержательный ответ именно на последнее сообщение с учётом контекста. Никогда не возвращай [[SKIP]], не отказывайся от ответа и не оценивай, стоит ли вступать в разговор. Если сообщение не содержит вопроса, отреагируй коротко и естественно по его теме; не выдумывай факты. Не помогай со спамом, преследованием или скоординированным обманом. Не копируй персональные данные. Стиль: " + ((style_instructions or "кратко, уважительно и естественно")[:8000]) + "\nПримеры:\n" + sample) if proactive else ("Составь черновик ответа. Не выдумывай факты, не помогай со спамом, преследованием или скоординированным обманом. Не копируй персональные данные. Стиль: " + (style_instructions or "кратко, уважительно и естественно")[:8000] + ("\n\n" + TRIGGER_CONTEXT_POLICY if trigger_mode else "") + "\nПримеры:\n" + sample)},
                {"role": "user", "content": (("Оцени, стоит ли самому вступить в разговор. Контекст предыдущих сообщений:\n" + (conversation_context[:8000] + "\n\n" if conversation_context else "") + "Последнее сообщение:\n") if proactive else (("История предыдущих сообщений этого же участника в этом же чате (используй только как дополнительный контекст по теме триггера; не отвечай на старые вопросы):\n" + (conversation_context[:8000] + "\n\n" if conversation_context else "") + "ТЕКУЩЕЕ СООБЩЕНИЕ — подготовь ответ именно на него:\n") if trigger_mode else ("Контекст предыдущей переписки (используй для связности, не раскрывай лишнее):\n" + (conversation_context[:8000] + "\n\n" if conversation_context else "") + "Подготовь ответ на сообщение:\n"))) + incoming_text[:8000]},
            ],
        )
    except Exception as exc:
        raise AIUnavailableError(f"Ошибка провайдера ИИ: {type(exc).__name__}: {str(exc)[:300]}") from exc
    result = response.choices[0].message.content
    if not result or not result.strip() or (proactive and result.strip() == "[[SKIP]]"):
        raise AIUnavailableError("Провайдер ИИ вернул пустой ответ.")
    return result.strip()


async def summarize_chat_context(*, agent: AIAgent, messages: list[dict]) -> str:
    """Create a compact, non-verbatim context summary from temporary chat samples."""
    if not agent.enabled or not agent.api_key.strip():
        raise AIUnavailableError("ИИ-агент выключен или у него не заполнен API-ключ.")
    if not agent.base_url.strip() or not agent.model.strip():
        raise AIUnavailableError("Заполните адрес API и модель в настройках ИИ-агента.")
    if not messages:
        raise AIUnavailableError("Недостаточно сообщений для обучения.")
    client = AsyncOpenAI(api_key=agent.api_key, base_url=agent.base_url, timeout=agent.timeout_seconds)

    def render(batch: list[dict]) -> str:
        lines = []
        for item in batch:
            direction = "сообщение аккаунта" if item.get("outgoing") else "сообщение собеседника"
            text = str(item.get("text", "")).replace("\x00", "")[:450]
            chat = str(item.get("chat", "чат"))[:80]
            if text.strip():
                lines.append(f"[{chat}; {direction}] {text}")
        return "\n".join(lines)

    summaries: list[str] = []
    try:
        # Process every sampled message in small batches to avoid sending a huge prompt.
        for offset in range(0, min(len(messages), 1000), 20):
            batch = render(messages[offset:offset + 20])
            if not batch:
                continue
            response = await chat_completion_with_retry(client,
                model=agent.model,
                temperature=0.2,
                max_tokens=450,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты анализируешь выбранный чат для помощи оператору в подготовке черновиков. "
                            "Сделай краткую сводку тем, терминов, контекста и общих норм общения. "
                            "Не копируй сообщения дословно, не сохраняй имена, usernames, контакты, "
                            "адреса, секреты или другие персональные данные. Не имитируй конкретного "
                            "участника и не делай выводов о его чувствительных свойствах. "
                            "Не предлагай спам, манипуляции или обход правил платформы."
                        ),
                    },
                    {"role": "user", "content": "Обобщи эти сообщения без цитирования:\n" + batch},
                ],
            )
            part = response.choices[0].message.content
            if part and part.strip():
                summaries.append(part.strip()[:2500])
        if not summaries:
            raise AIUnavailableError("ИИ не смог подготовить сводку по выбранным сообщениям.")
        response = await chat_completion_with_retry(client,
            model=agent.model,
            temperature=0.2,
            max_tokens=700,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Объедини заметки в мини-контекст для генерации черновиков ответов. "
                        "Верни только компактные инструкции на русском: основные темы, термины, "
                        "общий тон и уместные границы общения. Не включай личные данные, имена, "
                        "дословные цитаты и имитацию конкретного человека. Максимум 1800 символов."
                    ),
                },
                {"role": "user", "content": "\n\n".join(summaries)[:12000]},
            ],
        )
    except AIUnavailableError:
        raise
    except Exception as exc:
        raise AIUnavailableError(f"Не удалось обобщить контекст: {type(exc).__name__}: {str(exc)[:220]}") from exc
    result = response.choices[0].message.content
    if not result or not result.strip():
        raise AIUnavailableError("Провайдер ИИ вернул пустую сводку.")
    return result.strip()[:2500]
