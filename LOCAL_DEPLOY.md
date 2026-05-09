# Local deployment

This project runs locally as two services:

- Backend: FastAPI at `http://127.0.0.1:8765`
- Frontend: Next.js at `http://localhost:3000`

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- npm

## Quick start

From the project root:

```powershell
.\start-local.ps1
```

Then open:

```text
http://localhost:3000
```

The first run installs Python and Node dependencies. Later runs can skip dependency installation:

```powershell
.\start-local.ps1 -SkipInstall
```

## AI key

AI chat needs a Moonshot/Kimi API key in:

```text
apikey.txt
```

Place this file in the project root, next to `README.md`. The dashboard and mock APIs can still run without using the AI page.

## Manual start

Backend:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Frontend:

```powershell
cd frontend
npm install
npm run dev -- --hostname 127.0.0.1 --port 3000
```

Health check:

```powershell
curl http://127.0.0.1:8765/api/health
```
