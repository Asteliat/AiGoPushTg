FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY pyproject.toml .
COPY app ./app
RUN pip install --upgrade pip && pip install . && mkdir -p /data/sessions && chown -R app:app /app /data
USER app
CMD ["python", "-m", "app.bot.main"]
