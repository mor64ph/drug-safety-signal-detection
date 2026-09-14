# rxsignal -- Flask/Waitress app serving a precomputed signal table.
#
# The scored data is baked into the image rather than mounted. It is a fixed
# snapshot of roughly 20 MB that changes only when the pipeline is re-run, so
# there is no writable state at runtime and nothing to back up: a container can
# be destroyed and replaced with no loss.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Dependencies first, so code edits do not invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY templates/ ./templates/
COPY static/ ./static/
COPY config/ ./config/
COPY alembic/ ./alembic/
COPY scripts/ ./scripts/
COPY serve.py run.py alembic.ini docker-entrypoint.sh ./

# CRLF in a shell script makes the kernel look for an interpreter named
# "/bin/sh\r", which fails with a "not found" naming a path that plainly exists.
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

# Only the scored table is needed to serve. data/raw/ holds the API response
# cache and 35 MB of label text, which are build inputs, not runtime ones.
COPY data/results/scored_pairs.parquet ./data/results/scored_pairs.parquet
COPY data/results/label_interaction_pairs.json ./data/results/label_interaction_pairs.json
COPY data/results/drug_facts.json ./data/results/drug_facts.json

# Run unprivileged: a web process has no reason to be able to write its own code.
RUN useradd --create-home --shell /usr/sbin/nologin rxsignal \
    && chown -R rxsignal:rxsignal /app
USER rxsignal

EXPOSE 8000

# Fails the container if the data file is missing or unreadable, rather than
# serving empty results.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz')); \
sys.exit(0 if r.get('status')=='ok' else 1)"

CMD ["./docker-entrypoint.sh"]
