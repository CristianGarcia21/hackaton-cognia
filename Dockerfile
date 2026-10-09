# Imagen del Agente Vocal Cognitivo (Railway / cualquier host con Docker + WebSockets).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1

# Dependencias primero (capa cacheada mientras no cambie el lock). Sin los grupos dev ni ui
# (Streamlit es de la UI vieja, no del reto): imagen más liviana y despliegues más rápidos.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

COPY . .

# Railway define $PORT. Un solo worker: las sesiones de voz viven en memoria (spec §5, §14).
CMD ["sh", "-c", "uv run --no-sync uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
