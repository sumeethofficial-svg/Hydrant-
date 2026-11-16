FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY hydrant ./hydrant
RUN pip install --no-cache-dir .
COPY catalog ./catalog
COPY bench ./bench
COPY web ./web
EXPOSE 8000
CMD ["uvicorn", "hydrant.api:app", "--host", "0.0.0.0", "--port", "8000"]
