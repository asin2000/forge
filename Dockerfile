# FORGE dashboard / approval surface (HUM-1). Built by Cloud Run
# --source deploys; agent worker services reuse this image with a
# different entrypoint (Day 7 / Lane 2).
FROM python:3.13-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
COPY services ./services
COPY contracts ./contracts
COPY prompts ./prompts
COPY data ./data
COPY agents ./agents
COPY infra/residency.yaml ./infra/residency.yaml
RUN pip install --no-cache-dir -e . --no-deps
ENV PORT=8080
CMD ["sh", "-c", "uvicorn services.dashboard.app:production_app --factory --host 0.0.0.0 --port ${PORT}"]
