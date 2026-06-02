# CLIP UI — Flask Frontend

Single-page intelligence interface for the Charter Community Landscape Intelligence Platform.

## Stack

- **Backend:** Flask (Python 3), `app/ui/server.py`
- **Frontend:** Vanilla JS + CSS, served from `app/ui/static/`
- **Template:** Single HTML shell at `app/ui/templates/index.html`
- **Map data:** GeoJSON fetched from PublicaMundi at runtime (requires internet)

## Running locally

From the project root:

```bash
cd ~/Downloads/charter-intel
python3 app/ui/server.py
```

Open: **http://localhost:5001**

Default port is 5001 (avoids macOS AirPlay conflict on 5000).

## Auth

The default access key is `CHANGEME` — set in `static/js/app.js` as:

```js
const CLIP_PASSWORD = "CHANGEME";
```

This is a client-side stub. See the TODO comment in `server.py` for the
`/api/scan` route for notes on wiring real server-side auth.

## API routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serve `index.html` |
| `/api/health` | GET | `{"status": "ok"}` |
| `/api/states` | GET | List states from `config/states.yaml` |
| `/api/cities?state=NM` | GET | Community list for a state |
| `/api/runs?state=NM` | GET | Scan run history (state optional) |
| `/api/brief?run_id=X` | GET | HTML content of a brief |
| `/api/scan` | POST | Queue a scan (stub — returns `{"status":"queued"}`) |

## Dependencies

Flask and PyYAML — both in `requirements_app.txt`.

```bash
pip install flask pyyaml
```

## File structure

```
app/ui/
  server.py              Flask app (routes only)
  static/
    css/main.css         Full theme + animations
    js/app.js            All frontend JS (vanilla)
  templates/
    index.html           Single HTML shell, all views
  README.md              This file
```
