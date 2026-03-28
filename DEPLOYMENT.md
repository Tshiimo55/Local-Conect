## Frontend + Backend Deployment

GitHub Pages can host the frontend only.
The Python backend in `server.py` must be hosted separately.

### 1. Deploy the frontend

Publish this folder to GitHub Pages.
The homepage file must remain `index.html`.

### 2. Deploy the backend

Host `server.py` on a Python-friendly service such as:

- Render
- Railway
- Fly.io
- PythonAnywhere

### 3. Render quick start

This project now includes `render.yaml`, so you can deploy the backend as a Render web service.

Set these environment variables in Render:

- `HOST=0.0.0.0`
- `FRONTEND_ORIGIN=https://tshiimo55.github.io`
- `ALLOWED_ORIGINS=https://tshiimo55.github.io`

After deploy, your backend URL will look like:

```text
https://localconnect-backend.onrender.com
```

You can test it with:

```text
https://localconnect-backend.onrender.com/api/health
```

### 4. Point the frontend at the backend

Edit `assets/localconnect.config.js` and set your backend base URL:

```js
window.LOCALCONNECT_API_BASE = "https://localconnect-backend.onrender.com";
```

Do not include a trailing slash.

### 5. Local development

If you are testing locally, leave `assets/localconnect.config.js` empty and run:

```bash
python server.py
```

The frontend will fall back to `http://127.0.0.1:8000`.
