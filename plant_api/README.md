# Private Plant Diary API

This service is the HTTPS backend for a private Custom GPT. It uses the current
Airtable schema and does not add service-only fields or change existing Plant IDs.

## Supported actions

- find a plant by permanent ID or name;
- add phone photos and a comment to an existing plant;
- create one new physical specimen with `max(Plant ID) + 1`;
- rename a plant or refine its Latin name/cultivar without changing Plant ID;
- select an existing uploaded photo as the card cover;
- add a diary event.

Every mutation has two gates: `confirmed=true` after an exact user-facing preview,
and server-side `PLANT_API_WRITE_ENABLED=true`. New-plant creation additionally
requires the previewed `proposed_plant_id` as `expected_plant_id`, so a retry cannot
silently create the next number.

## Local check

```bash
python3 -m venv .venv
.venv/bin/pip install -r plant_api/requirements.txt -r plant_api/requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests -v
PLANT_API_KEY=local-test .venv/bin/uvicorn plant_api.app:app --reload
```

With write mode off, reads use live Airtable when `AIRTABLE_WRITE_TOKEN` is set and
the checked-in JSON snapshot otherwise. Confirmed mutations return `dry_run` and do
not change Airtable or Object Storage.

## Deployment requirements

- public HTTPS URL reachable by GPT Actions;
- exactly one application replica/process for serial Plant ID allocation;
- Docker build context at the repository root and Dockerfile
  `plant_api/Dockerfile`;
- all variables from `plant_api/.env.example` stored as host secrets;
- health check `GET /health`;
- `PLANT_API_WRITE_ENABLED=false` for the first end-to-end test.

The Custom GPT action schema is served from `/openapi-action.yaml`. Set
`PLANT_API_PUBLIC_URL` so its `servers` entry points back to the deployed service.
Use API-key authentication in the GPT editor with header `X-Plant-API-Key`, then
paste the contents of `GPT_INSTRUCTIONS.md` into the GPT instructions.

GitHub Pages cannot host this API because Pages serves static files only. The API
must run on a separate HTTPS container host; it can still trigger the existing
Pages workflow after a successful write when `PLANT_GITHUB_TOKEN` is configured.
