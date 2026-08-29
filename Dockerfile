FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.9 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim AS runtime

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN apt-get update \
	&& apt-get install --no-install-recommends --yes gosu \
	&& rm -rf /var/lib/apt/lists/* \
	&& mkdir -p /data \
	&& useradd --create-home --uid 10001 appuser \
	&& chown -R appuser:appuser /app \
	&& chmod 755 /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
