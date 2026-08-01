FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_server.py .
ENV MCP_TRANSPORT=streamable-http \
    MEM0_URL=http://localhost:8000 \
    MEM0_USER_ID=default_user
EXPOSE 8001
CMD ["python3", "mcp_server.py"]
