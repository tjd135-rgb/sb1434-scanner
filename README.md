# SB 1434 Qualifying Parcel Scanner

Scans DOR NAL parcels in Miami-Dade, Broward, and Palm Beach for parcels that
qualify under Florida's SB 1434 (Section 163.2525, the Infill Redevelopment
Act). Phase B implements the brownfield-area trigger + statutory exclusions;
Phases C-E layer on additional filters (UDB, military, cleanup-site proximity)
and pathway enrichment.

Backend: FastAPI + PostgreSQL/PostGIS. Deploys to Render via `backend/render.yaml`.

## Phase B scope (current)

Screening applies:
- **Gate 1** — parcel is ≥ 5 acres
- **Gate 2** — county is 23 / 16 / 60
- **Gate 3** — either **Trigger B** (centroid inside a designated FDEP
  brownfield area) OR **Trigger A** (centroid within 1,500 ft of a DEP
  cleanup site point). `env_trigger` records which matched:
  `brownfield_area`, `cleanup_site`, or `both`.
- **Gate 4** — a residential parcel (DOR 001-009) sits within 500 ft
- **Gate 5A** — not agricultural (DOR 050-069)
- **Gate 5B** — not a government-owned public park (DOR 082 + govt owner)

Deferred to later phases: **Gate 5C** (Urban Development Boundary),
**Gate 5D** (¼-mile military buffer). Their columns already exist on
`qualifying_parcels`.

## Deploy to Render

1. Create a new Render account (or use existing) and connect this GitHub repo.
2. In the Render dashboard, click **New +** → **Blueprint**, point at this
   repo, and Render will parse `backend/render.yaml`. It provisions:
   - Postgres DB `sb1434-scanner-db` (basic-256mb plan) with PostGIS included
   - Web service `sb1434-scanner-api` running FastAPI
3. First deploy runs `alembic upgrade head`, which enables PostGIS and creates
   `parcels`, `brownfield_areas`, `brownfield_sites`, `qualifying_parcels`.
4. Warm the brownfield data (few minutes):
   ```
   curl -X POST https://<your-service>.onrender.com/admin/ingest-brownfields
   ```
5. Ingest the DOR NAL parcels (Phase A — ~10-20 min per county):
   ```
   curl -X POST https://<your-service>.onrender.com/admin/ingest-nal \
        -H "content-type: application/json" -d '{"county":"all"}'
   ```
   Assets default to this repo's own GitHub release
   (`nal-2025/nal-{fips}.zip`). Override with `NAL_RELEASE_URL` /
   `NAL_ASSET_PATTERN` env vars if your release layout differs.
6. Backfill centroids (~30-60 min tri-county):
   ```
   curl -X POST https://<your-service>.onrender.com/admin/ingest-centroids \
        -H "content-type: application/json" -d '{"county":"all"}'
   ```
7. Run the SB 1434 screen:
   ```
   curl -X POST https://<your-service>.onrender.com/admin/run-screening
   ```
8. Poll job status any time:
   ```
   curl https://<your-service>.onrender.com/admin/status
   ```

All admin endpoints kick off background threads and return immediately —
watch Render's log stream for progress. You can also run any ingest as a
CLI (`python -m app.ingest --county all`, `python -m app.centroids --county 23`)
via `render ssh` if you prefer.

## Local development

```powershell
# From backend/
docker compose up -d
cp .env.example .env
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Hit `http://localhost:8000/health` to confirm PostGIS is loaded.

## Endpoints

| Verb | Path | Notes |
|------|------|-------|
| GET | /health | DB + PostGIS check |
| GET | /brownfield-areas | List, filter by `?county=` substring |
| GET | /brownfield-areas/{area_id} | Detail + GeoJSON geometry |
| GET | /qualifying-parcels | List, filters: county, pathway, env_trigger, min_acres, adjacent_only |
| GET | /qualifying-parcels/{parcel_id} | Detail |
| GET | /stats | Aggregate counts by county / pathway |
| POST | /admin/ingest-brownfields | Runs FDEP Layer 0 + Layer 1 ingest |
| POST | /admin/ingest-cleanup-sites | Runs DEP Contamination Locator ingest (Trigger A source) |
| POST | /admin/ingest-nal | Body `{"county":"all"\|"miami-dade"\|"broward"\|"palm-beach"}` |
| POST | /admin/ingest-centroids | Body `{"county":"all"\|"23"\|"16"\|"60"}` |
| POST | /admin/run-screening | Runs SB 1434 screen; body `{"update_adjacency": true}` |
| GET | /admin/status | Snapshot of in-flight/completed background jobs |

## Layout

```
backend/
  alembic.ini
  docker-compose.yml      # local Postgres+PostGIS
  render.yaml             # Render blueprint
  requirements.txt
  .env.example
  migrations/
    env.py
    versions/0001_initial.py
  app/
    db.py                 # engine + session
    models.py             # SQLAlchemy ORM (5 tables)
    brownfields.py        # FDEP ArcGIS ingest (Gate 3 Trigger B)
    cleanup_sites.py      # DEP Contamination Locator (Gate 3 Trigger A)
    ingest.py             # Phase A: DOR NAL loader (CLI + library)
    centroids.py          # Phase A: parcel-centroid backfill (CLI + library)
    screening.py          # SB 1434 gates as SQL
    main.py               # FastAPI routes + background-job runner
```
