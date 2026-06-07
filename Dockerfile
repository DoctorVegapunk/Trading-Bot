FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ctrader-open-api pins protobuf==3.20.1 which conflicts with firebase-admin's google-api-core
RUN pip install --no-cache-dir --no-deps ctrader-open-api==0.9.2

COPY . .

CMD ["python", "main.py"]
