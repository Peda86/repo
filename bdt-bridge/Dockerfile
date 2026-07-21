FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bdt_bridge.py .

CMD ["python", "bdt_bridge.py"]
