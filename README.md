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
- **Gate 3B** — parcel centroid lies inside a designated FDEP brownfield area
- **Gate 4** — a residential parcel (DOR 001-009) sits within 500 ft
- **Gate 5A** — not agricultural (DOR 050-069)
- **Gate 5B** — not a government-owned public park (DOR 082 + govt owner)

Deferred to Phase C: **Trigger A** (DEP cleanup-site proximity), **Gate 5C**
(Urban Development Boundary), **Gate 5D** (¼-mile military buffer). Their
columns already exist on `qualifying_parcels`.

## Deploy to Render

1. Create a new Render account (or use existing) and connect this GitHub repo.
2. In the Render dashboard, click **New +** → **Blueprint**, point at this
   repo, and Render will parse `backend/render.yaml`. It provisions:
   - Postgres DB `sb1434-scanner-db` (basic-256mb plan) with PostGIS included
   - Web service `sb1434-scanner-api` running FastAPI
3. First deploy runs `alembic upgrade head`, which enables PostGIS and creates
   `parcels`, `brownfield_areas`, `brownfield_sites`, `qualifying_parcels`.
4. Once the service is up, warm the brownfield data:
   ```
   curl -X POST https://<your-service>.onrender.com/admin/ingest-brownfields
   ```
   (Takes ~2-5 minutes, ~531 areas + all sites statewide.)
5. Load the parcels table (Phase A ingest — separate step, not in this repo yet).
6. Fire the screen:
   ```
   curl -X POST https://<your-service>.onrender.com/admin/run-screening
   ```

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
| POST | /admin/run-screening | Runs SB 1434 screen; body `{"update_adjacency": true}` |

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
    models.py             # SQLAlchemy ORM (4 tables)
    brownfields.py        # FDEP ArcGIS ingest
    screening.py          # SB 1434 gates as SQL
    main.py               # FastAPI routes
```
