# prqa-launcher — Phase 2 only, built on demand (compose profile "phase2"). The Docker CLI talks to the POLICY PROXY
# socket (DOCKER_HOST=unix:///run/prqa/docker-policy.sock), never to the daemon directly. No secret besides the hand-off token.
# Build from the repository root with an additional context: docker build -f launcher/Dockerfile --build-context src=. -t prqa/launcher:0.1.0 launcher
FROM python:3.13-slim
# docker-cli only: since Debian 13 `docker.io` is the daemon (dockerd/containerd/runc) and merely recommends the CLI,
# which --no-install-recommends skipped. The launcher runs `docker` against DOCKER_HOST; it never runs a daemon.
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli ca-certificates && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10002 launcher && useradd -u 10002 -g 10002 -M -s /usr/sbin/nologin launcher
# /work is the compose volume prqa_launcher_work: an empty named volume takes this directory's owner on first mount, so
# without it the volume is root-owned and the launcher (uid 10002) cannot stage checkout artifacts.
RUN mkdir -p /work && chown 10002:10002 /work && chmod 0700 /work
WORKDIR /app
COPY --from=src worker/prqa_worker /app/prqa_worker
COPY --from=src scripts/services/prqa_launcher.py /app/prqa_launcher.py
USER 10002:10002
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "/app/prqa_launcher.py", "--interval", "15"]
