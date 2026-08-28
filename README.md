# Nash Chat API

Backend for a multi-tenant platform that lets anyone build, configure, and deploy custom AI chat
agents — persona, guardrails, knowledge base, and a choice of LLM provider — then publish them to a
web client or WhatsApp through a generated API key.

FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL · Redis

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # set DATABASE_URL
python main.py
```

The API listens on `http://127.0.0.1:8000` and `GET /health` reports status.

## API documentation

| | |
|---|---|
| Swagger UI | <http://127.0.0.1:8000/docs> |
| ReDoc | <http://127.0.0.1:8000/redoc> |
| OpenAPI schema | <http://127.0.0.1:8000/openapi.json> |

Paths and availability are configurable under the `docs` section of `application.yaml`
(`DOCS_ENABLED`, `DOCS_SWAGGER_PATH`, `DOCS_REDOC_PATH`, `DOCS_OPENAPI_PATH`). Every endpoint
carries a tag, summary, description, and explicit response model — CI fails if a route is
undocumented.

Dependencies via containers instead:

```bash
docker compose up -d postgres redis
```

## Configuration

`src/configs/application.yaml` is the source of truth for shape and defaults. Every value is written
as `"${ENV_VAR:default} | type"`, and an environment variable (or a key in a local `.env`) overrides
the default. Read config through `src.configs` — never `os.environ`:

```python
from src.configs import DATABASE_URL, SERVER_PORT
```

After adding or renaming a key, regenerate the type stub with `python -m src.configs.generate` and
commit it.

## Project structure

```
src/
├── configs/          # application.yaml + loader
├── core/             # app factory, lifespan, middleware — wiring only
├── shared/           # database, exceptions, responses, llm — infrastructure
└── modules/<name>/
    ├── domain/       # models.py, repositories.py, services.py
    ├── internal/     # module-private helpers
    └── presentation/ # dtos/ + api/
```

Routers are thin (HTTP → one service call), services own business logic, repositories own every
`select(...)` and `flush()` rather than `commit()`. No module imports another module's `internal/`;
cross-module calls go service → service.

Every endpoint returns the `ApiResponse` envelope, with camelCase JSON:

```json
{ "success": true, "value": { }, "message": null }
```

## Development

```bash
ruff check .
ruff format --check .
mypy
pytest
```

See `CONTRIBUTING.md` for the branch/PR workflow and commit message format.
