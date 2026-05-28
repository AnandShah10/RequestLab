# RequestLab 🔶

> A lightweight, lightning-fast, self-hosted Postman alternative built with Python. Zero heavy frameworks, complete privacy, and powerful features for modern API development.

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)](https://flask.palletsprojects.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![SQLite](https://img.shields.io/badge/SQLite-3.0-orange.svg)](https://sqlite.org)

---

## ✨ Why RequestLab?

- 🚀 **Lightning Fast**: 2-second startup, <100MB RAM usage
- 🔒 **100% Private**: Self-hosted, no cloud, complete data ownership
- 🐍 **Python Scripting**: Pre-request and test scripts in Python (not just JavaScript)
- 📱 **Fully Responsive**: Works on desktop, tablet, and mobile devices
- 💰 **Completely Free**: No premium tiers, no feature gates, no limits
- 🎯 **Postman-Like UI**: Familiar interface with zero learning curve

---

## 📋 Quick Start

```bash
# 1. Clone or download the repository
cd RequestLab

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python postman.py

# 4. Open in browser
# → http://localhost:5000
```

**That's it!** You're ready to test APIs. 🎉

---

## 🎯 Core Features

### 🔌 Multi-Protocol Support
| Protocol | Status | Description |
|----------|--------|-------------|
| **HTTP/REST** | ✅ Stable | GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS |
| **GraphQL** | ✅ Stable | Query and variables with dedicated editor |
| **WebSockets** | ✅ Stable | Real-time bidirectional communication |
| **Socket.io** | ✅ Stable | Event-based real-time messaging |
| **MQTT** | ✅ Stable | IoT and pub/sub messaging |
| **gRPC** | 🚧 Planned | Protocol Buffers support coming soon |

### 🛠 Request Builder
- **URL Bar**: Smart variable highlighting and substitution
- **Query Params**: Key-value table with enable/disable toggles
- **Custom Headers**: Full header customization
- **Body Types**: JSON, GraphQL, raw text, form-data (with files), x-www-form-urlencoded, SOAP, XML
- **Authentication**: No auth, Basic Auth, Bearer Token, OAuth 2.0, API Key, AWS Signature V4
- **File Uploads**: Native `multipart/form-data` support with file picker

### 📊 Response Viewer
- **Pretty Mode**: Syntax-highlighted, formatted JSON/XML
- **Tree View**: Collapsible JSON tree structure
- **Raw Mode**: Unformatted response body
- **Preview Mode**: HTML rendering for web responses
- **Response Headers**: Complete header table
- **Response Stats**: Status code, response time (ms), body size

### 📁 Organization
- **Collections & Folders**: Nested folder structure for requests
- **Drag & Drop**: Reorder requests intuitively
- **Multi-Tab Interface**: Work on multiple requests simultaneously
- **Request History**: Track your latest 80 requests
- **Search**: Quick search through collections and requests
- **Workspace Persistence**: Tabs and states survive page reloads via `localStorage`

---

## 🧪 Advanced Features

### 🌍 Environments & Variables

Manage environment-specific variables with real-time substitution:

```bash
# Example: Environment variables
base_url = https://api.example.com
api_key = your-secret-key
timeout = 5000

# Usage in requests
{{base_url}}/users/{{user_id}}
Authorization: Bearer {{api_key}}
```

**Features:**
- ✅ Multiple environments (Development, Staging, Production)
- ✅ Global variables (always active across all environments)
- ✅ Real-time variable highlighting with color-coded badges:
  - 🟢 **Green**: Resolved variable
  - 🔴 **Red**: Unresolved variable
  - 🟠 **Orange**: Variable with value
- ✅ Variable autocomplete dropdown
- ✅ Variable tooltips on hover

### 🎭 Mock Server

Create mock API endpoints for testing without a backend:

```bash
# Mock endpoint example
Path: /api/users
Method: GET
Status: 200
Response Body: {"users": [{"id": 1, "name": "John"}]}
Delay: 500ms (optional)
```

**Features:**
- ✅ Static JSON/text responses
- ✅ Configurable HTTP methods (GET, POST, PUT, PATCH, DELETE)
- ✅ Custom status codes
- ✅ Response delay simulation
- ✅ Enable/disable toggles
- ✅ Instant testing with 🧪 button

**Base URL:** `http://localhost:5000/mock/`

### 🏃 Collection Runner

Automate API testing by running entire collections:

```bash
# Runner configuration
Collection: My API Tests
Iterations: 3
Delay between requests: 100ms
```

**Features:**
- ✅ Sequential request execution
- ✅ Multiple iterations support
- ✅ Configurable delays between requests
- ✅ Pass/fail tracking with assertions
- ✅ Detailed results summary
- ✅ Execution time tracking

### 💻 Scripting Engine

Write pre-request and test scripts in **Python** or **JavaScript**:

#### Python Example (Server-Side)
```python
# Pre-request Script
pm.environment.set("timestamp", str(int(time.time())))
pm.globals.set("request_count", pm.globals.get("request_count", 0) + 1)

# Test Script
status_code = pm.response.get("status_code")
pm.test("Status code is 200", lambda: pm.expect("Status Check", status_code == 200))
pm.test("Response time < 500ms", lambda: pm.expect("Response Time", pm.response.get("time") < 500))

body = pm.response.get("body")
pm.test("Has users array", lambda: pm.expect("Users Field", "users" in body))
```

#### JavaScript Example (Client-Side)
```javascript
// Pre-request Script
pm.environment.set("random_id", Math.random().toString(36));

// Test Script
pm.test("Status code is 200", function() {
    pm.expect(pm.response.status).to.equal(200);
});

pm.test("Response has data", function() {
    const body = pm.response.json();
    pm.expect(body).to.have.property('data');
});
```

**Postman-Compatible API:**
- `pm.environment.get/set/unset()`
- `pm.globals.get/set/unset()`
- `pm.request` - Request object
- `pm.response` - Response object
- `pm.expect(name, value)` - Assertion builder
- `pm.test(name, testFn)` - Test execution

**Assertion Methods:**
- `.to_equal(expected)` - Equality check
- `.to_be_truthy()` - Truthiness check
- `.to_contain(value)` - Contains check
- Chain multiple assertions

### 🔐 Multi-User Authentication

Secure user management system:

- ✅ User registration and login
- ✅ Session-based authentication
- ✅ SMTP-based password reset
- ✅ User-specific data isolation
- ✅ Avatar with customizable colors

### 🎨 UI/UX Features

#### Themes
- **Dark Mode**: Sleek, eye-friendly dark theme
- **Light Mode**: Clean, professional light theme
- **Auto-save**: Preference remembered per device

#### Responsive Design
Works perfectly on all devices:
- 🖥 **Desktop** (1024px+): Full sidebar, multi-column layout
- 📱 **Tablet** (768-1024px): Compact sidebar, optimized spacing
- 📱 **Mobile Landscape** (480-768px): Hidden sidebar, wrapped URL bar
- 📱 **Mobile Portrait** (<480px): Ultra-compact, touch-friendly

#### Collapsible Cards
- ✅ Environment cards with expand/collapse
- ✅ Mock endpoint cards with expand/collapse
- ✅ Smooth animations
- ✅ State persistence during session

#### Keyboard Shortcuts
- `Ctrl + /` - Toggle comment in body editor
- `Ctrl + B` - Beautify JSON body
- `Tab` - Indent in body editor
- `Enter` - Send request from URL bar

---

## 📥 Import / Export

### Supported Formats
- ✅ **Postman Collections** (v2.1)
- ✅ **OpenAPI 3.0** specifications
- ✅ **Swagger 2.0** specifications
- ✅ **RequestLab Backup** (JSON)

### Import Features
- Automatic folder generation from OpenAPI paths
- Parameter extraction (path, query, header)
- Request body schema import
- Security scheme conversion to auth profiles
- Example response import

---

## 🗄 Data Storage & Deployment

### Local Storage
All data is stored in `RequestLab.db` (SQLite):
- Users and sessions
- Collections and folders
- Requests and history
- Environments and variables
- Mock endpoints
- Collection runs

### Database Schema
```
RequestLab.db
├── users
├── collections
├── folders
├── requests
├── environments
├── environment_variables
├── history
├── mock_endpoints
└── collection_runs
```

### Deployment Options

#### Local Development
```bash
python postman.py
```

#### Production (with Gunicorn)
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 postman:app
```

#### Docker (Coming Soon)
```bash
docker run -p 5000:5000 requestlab:latest
```

---

## ⚙️ Configuration

### Environment Variables

Create a `.env` file in the root directory:

```env
# SMTP Configuration (for password reset)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_USE_TLS=true

# Application Settings
SECRET_KEY=your-secret-key-here
DATABASE_URL=sqlite:///RequestLab.db
```

Copy `.env.example` to `.env` and customize:
```bash
cp .env.example .env
```

---

## 📚 Documentation

### Using Environments

1. Navigate to **Environments** tab
2. Click **"+ New Environment"**
3. Add variables (key-value pairs)
4. Set environment as **Active**
5. Use `{{variable}}` syntax in requests

### Creating Mock Endpoints

1. Go to **Mock Server** tab
2. Click **"+ New Mock"**
3. Configure:
   - Path (e.g., `/api/users`)
   - HTTP Method
   - Response Body (JSON/text)
   - Status Code
   - Delay (optional)
4. Click **Save Changes**
5. Test with 🧪 button

### Running Collections

1. Open **Collection Runner** tab
2. Select a collection
3. Configure:
   - Number of iterations
   - Delay between requests (ms)
4. Click **Run Collection**
5. View results with pass/fail status

### Writing Test Scripts

1. Open a request
2. Scroll to **Scripts** section (if available)
3. Write pre-request or test script
4. Use `pm.*` API (Postman-compatible)
5. Send request and view test results in response

---

## 🆚 Comparison with Other Tools

See our [detailed comparison](COMPARISON.md) with Postman, OpenAPI, and Swagger.

### Quick Comparison

| Feature | RequestLab | Postman | Swagger UI |
|---------|-----------|---------|------------|
| **Cost** | 💰 Free | $14-84/user/mo | Free |
| **Setup** | ⚡ 2 minutes | 5 minutes | 5 minutes |
| **RAM Usage** | <100MB | 500-800MB | ~50MB |
| **Privacy** | 🔒 100% local | ☁️ Cloud | 🔒 Your server |
| **Mobile** | 📱 Full support | ❌ No | ✅ Responsive |
| **Scripting** | 🐍 Python + JS | JavaScript only | ❌ No |
| **Offline** | ✅ Full | ⚠️ Limited | ✅ Full |

---

## 🛠 Tech Stack

- **Backend**: Python 3.8+ with Flask
- **Database**: SQLite 3.0
- **Frontend**: Vanilla HTML, CSS, JavaScript
- **No Frameworks**: Zero React, Vue, Angular, or build tools
- **Authentication**: Session-based with Flask sessions
- **HTTP Client**: `requests` library

---

## 🤝 Contributing

We welcome contributions! Here's how you can help:

1. **Report Bugs**: Open an issue with detailed steps to reproduce
2. **Feature Requests**: Share your ideas for new features
3. **Code Contributions**: Submit pull requests
4. **Documentation**: Improve README, add tutorials
5. **Testing**: Test on different platforms and browsers

### Development Setup

```bash
# Clone repository
git clone https://github.com/yourusername/requestlab.git
cd requestlab

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run in development mode
python postman.py
```

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **Postman**: For inspiring the UI/UX design
- **Flask**: For the lightweight web framework
- **SQLite**: For the zero-configuration database
- **Community**: For feedback and feature requests

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/requestlab/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/requestlab/discussions)
- **Email**: your-email@example.com

---

## 🗺 Roadmap

### ✅ Completed
- [x] HTTP REST API testing
- [x] GraphQL support
- [x] WebSocket, Socket.io, MQTT
- [x] Environments & variables
- [x] Collections & folders
- [x] Mock server
- [x] Collection runner
- [x] Python & JavaScript scripting
- [x] Assertions framework
- [x] Multi-user authentication
- [x] Dark/Light themes
- [x] Responsive design
- [x] Collapsible UI cards
- [x] OpenAPI/Swagger import

### 🚧 In Progress
- [ ] gRPC support
- [ ] Code generation (cURL, Python, JavaScript)
- [ ] API documentation generator
- [ ] Request chaining

### 📋 Planned
- [ ] Team collaboration (WebSocket sync)
- [ ] Cloud backup/restore
- [ ] Plugin system
- [ ] CI/CD integration (CLI mode)
- [ ] API monitoring
- [ ] Data-driven testing (CSV/JSON)
- [ ] Mobile app (React Native)

---

<div align="center">

**Made with ❤️ by the RequestLab Team**

⭐ Star this repo if you find it helpful!

</div>
