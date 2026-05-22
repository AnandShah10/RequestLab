# RequestLab 🔶

A full-featured Postman alternative built as a lightweight, lightning-fast Python application. With zero heavy client-side frameworks, RequestLab runs cleanly in your browser and is perfect for individuals or teams needing a powerful, self-hosted API client.

## Quick Start

```bash
# 1. Install dependencies
pip install flask requests

# 2. Run the app
python postman.py

# 3. Open in browser
open http://localhost:5000
```

## Core Features

| Feature | Details |
|---|---|
| **Multi-User Authentication** | Secure user accounts, login/signup flows, and SMTP-based password reset links. |
| **Workspace Persistence** | Your open tabs, request bodies, and active tab states survive page reloads instantly via `localStorage`. |
| **Dark / Light Themes** | Toggle instantly between a sleek Dark Mode and a clean Light Mode. Preference is remembered per device. |
| **Drag & Drop Reordering** | Easily organize your Collections: drag and drop individual requests into folders and collections. |
| **File Uploads** | First-class support for `multipart/form-data` with native file attachments. |
| **HTTP Methods** | GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS |
| **Request Builder** | URL bar, query params, custom headers, and request body building. |
| **Body Types** | JSON, raw text, form-data (w/ files), x-www-form-urlencoded |
| **Authentication Profiles**| No auth, Basic Auth, Bearer Token, API Key (header or query) |
| **Response Viewer** | Pretty-printed JSON, raw body, HTML preview, response headers, cookies |
| **Response Stats** | Status code, response time (ms), body size |
| **Collections & Folders** | Save, organize, and reload requests in nested folder structures. |
| **History** | Keeps track of your latest requests with status codes and methods. |
| **Environments** | Environment-specific variables with real-time `{{variable}}` substitution syntax across URLs, params, and bodies. |
| **Import / Export** | Bring your existing Postman collections into RequestLab or backup your own setups. |

## Using Environments

1. Go to the **Environments** tab on the top-right toolbar.
2. Create a new environment and add key-value variables like `base_url = https://api.example.com`.
3. Set the environment as Active.
4. In your requests, use `{{base_url}}/endpoint` — variables are highlighted and substituted automatically.

## Data Storage & Deployment

All user data, collections, folders, and history are stored securely in `RequestLab.db` (SQLite) generated in the root directory.

To customize SMTP configuration for password resets, copy `.env.example` to `.env` and fill out your email credentials.
