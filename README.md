# RequestLab 🔶

A full-featured Postman alternative built with Python + Flask.

## Quick Start

```bash
# 1. Install dependencies
pip install flask requests

# 2. Run the app
python postman.py

# 3. Open in browser
open http://localhost:5000
```

## Features

| Feature | Details |
|---|---|
| **HTTP Methods** | GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS |
| **Request Builder** | URL bar, query params, custom headers, request body |
| **Body Types** | JSON, raw text, form-data, x-www-form-urlencoded |
| **Authentication** | No auth, Basic Auth, Bearer Token, API Key (header or query) |
| **Response Viewer** | Pretty-printed JSON, raw body, response headers, cookies |
| **Response Stats** | Status code, response time (ms), body size |
| **Collections** | Save, organize, and reload requests |
| **History** | Last 80 requests with status codes |
| **Environments** | Variable substitution with `{{variable}}` syntax |
| **Persistent Storage** | SQLite database (`pypostman.db`) |

## Using Environments

1. Go to the **Environments** tab
2. Create a new environment and add variables like `base_url = https://api.example.com`
3. Set it as Active
4. In your requests, use `{{base_url}}/endpoint` — variables are substituted automatically

## Data Storage

All data is stored in `RequestLab.db` (SQLite) in the same directory.
To reset, just delete that file.
