FROM python:3.11-slim

# Node.js is required for npx-based MCP servers (stdio transport)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# uv provides uvx, required for Python-based MCP servers (e.g. mcp-server-fetch)
RUN pip install --no-cache-dir uv

# Pre-install MCP servers so npx resolves instantly at startup (no network),
# and install headless Chromium via @playwright/mcp's OWN playwright-core so
# the browser revision always matches what the server expects.
RUN npm install -g @playwright/mcp @modelcontextprotocol/server-memory \
    && PW_CLI="$(npm root -g)/@playwright/mcp/node_modules/playwright-core/cli.js" \
    && { [ -f "$PW_CLI" ] || PW_CLI="$(npm root -g)/playwright-core/cli.js"; } \
    && node "$PW_CLI" install --with-deps chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "-u", "main.py"]
