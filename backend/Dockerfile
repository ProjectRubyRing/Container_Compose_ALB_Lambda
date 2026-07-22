FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3 waitress==3.0.0

COPY app.py .

ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
