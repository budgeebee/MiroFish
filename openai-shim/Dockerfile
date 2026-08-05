FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV SHIM_LISTEN_PORT=9401
EXPOSE 9401

CMD ["python", "main.py"]