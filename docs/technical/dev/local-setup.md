# Local Development Setup

Run the BigRAE portal locally for development.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Node 20.19 or later, with npm

## First run

1. Install the platform dependencies:

   ```bash
   cd shifter/shifter_platform
   uv sync --group dev
   ```

2. Build the SPA so that Django can serve it:

   ```bash
   (cd frontend && npm ci && npm run build)
   ```

3. Prepare the local database and start the server:

   ```bash
   TESTING=1 uv run python manage.py migrate
   TESTING=1 uv run python manage.py runserver
   ```

The portal runs at `http://localhost:8000`. `TESTING=1` selects the test posture:
`DEBUG` on and a SQLite database in `shifter/shifter_platform/db.sqlite3`.

Sign in at `http://localhost:8000/dev-login/`. Development login works only in a
development posture and only from a loopback or allowed admin address.

## Tests and checks

Use the [command matrix](../../dev/testing.md#command-matrix) for every test,
lint and policy command. It names the local command and the CI job for each
lane.

## Troubleshooting

- Port in use: check with `lsof -i :8000`, or start on another port with
  `TESTING=1 uv run python manage.py runserver 8001`.
- Missing dependencies: run `uv sync --group dev` again from
  `shifter/shifter_platform`.
