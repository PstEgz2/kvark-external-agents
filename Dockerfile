FROM python:3.12-slim

WORKDIR /srv

# Dependencies first, so editing the application does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The registration receipt lives here. Mount a volume over it — the API key is shown once,
# and a container rebuild that loses it means registering afresh under a new name.
VOLUME ["/data"]
ENV AGENT_STATE_PATH=/data/agent.json

EXPOSE 8099

# --reload is deliberately not set: this is the thing under test, and a reload mid-turn
# would drop the in-memory session and read as a gateway fault.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
