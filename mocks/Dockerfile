FROM python:3.13-slim
RUN groupadd -g 10001 prqa && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin prqa && mkdir -p /data && chown 10001:10001 /data
WORKDIR /app
COPY mock_services.py /app/mock_services.py
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8090
CMD ["python3", "-I", "/app/mock_services.py"]
