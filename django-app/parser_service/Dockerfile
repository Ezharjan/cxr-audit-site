FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/parser
COPY parser_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && \
    groupadd --gid 1000 parser && useradd --uid 1000 --gid 1000 --no-create-home parser && \
    install -d -m 0700 -o 1000 -g 1000 /run/primer-parser
COPY upload/parser_worker.py upload/parser_client.py ./
COPY parser_service/server.py parser_service/job.py parser_service/healthcheck.py ./
USER 1000:1000
CMD ["python", "server.py"]
