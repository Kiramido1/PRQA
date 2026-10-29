# PR QA Pilot worker — stdlib HTTP server + PyYAML + jsonschema. No shell access needed at runtime.
FROM python:3.13-slim

# git: only for the optional worker-side pinned checkout (PRQA_WORKER_CHECKOUT_ENABLED, Phase 2). Hooks are disabled by the plan.
# Deliberately NO docker CLI and NO socket: the worker builds the sandbox plan (opencode_runner.build_sandbox_plan) but
# launching containers is a separate, privileged component decided in Phase 2 (docs/SANDBOX_TRUST_BOUNDARY.md).
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "PyYAML==6.0.2" "jsonschema==4.25.1" \
    && groupadd -g 10001 prqa && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin prqa

WORKDIR /app
COPY prqa_worker /app/prqa_worker
RUN mkdir -p /data && chown 10001:10001 /data

USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
# Not -I: isolated mode drops /app from sys.path, and this is our own code, not downloaded input.
CMD ["python3", "-m", "prqa_worker.server"]
