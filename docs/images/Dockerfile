FROM python:3.12-slim
WORKDIR /app
COPY collector.py /app/collector.py
CMD ["python", "collector.py", "--mode", "real", "--interval", "300"]
