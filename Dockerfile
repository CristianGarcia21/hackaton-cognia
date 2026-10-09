# Imagen del Agente Vocal Cognitivo (Railway / cualquier host con Docker + WebSockets).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencias primero (capa cacheada mientras no cambie el lock). Sin los grupos dev ni ui
# (Streamlit es de la UI vieja, no del reto): imagen más liviana y despliegues más rápidos.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

COPY . .

# Railway define $PORT. `exec`: uvicorn queda como PID 1 y recibe SIGTERM (apagado ordenado en cada
# redeploy). Un solo worker: las sesiones de voz viven en memoria (spec §5, §14). Mensajes WS de
# máximo 1 MB: el audio llega en trozos de 20-40 ms.
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*' --ws-max-size 1048576"]
