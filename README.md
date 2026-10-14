# Employee Attendance & Analytics API

Backend for the HROne Software Engineer Trainee assignment. The implementation uses FastAPI, PyMongo and MongoDB 6.0+.

## Run locally

1. Use Python 3.11 or newer and make MongoDB reachable (local MongoDB or MongoDB Atlas).
2. Install dependencies: `python -m pip install -r requirements.txt`
3. Set `MONGO_URI` and `MONGO_DB` in your shell. A local `.env` file is also supported; existing environment variables take precedence.
4. From the repository root, run: `uvicorn app.main:app --port 8000`
5. Check readiness at `GET /health`. API docs are available at `/docs` while the service is running.

Indexes are created idempotently at startup. Do not commit credentials, `.env` files, database dumps, virtual environments, or secrets.

## Implementation notes

- All application code is in `app/main.py`.
- Analytics are computed with MongoDB aggregation pipelines; time instants cross the API boundary as epoch milliseconds.
- The optional seed script and `sample_data/` documents belong to the original candidate kit and are not part of this submission.
