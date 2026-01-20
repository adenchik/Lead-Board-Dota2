FROM python:3.11-slim

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY templates templates/
COPY static static/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8066

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8066"]
