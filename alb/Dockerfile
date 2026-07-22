FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alb_sim.py .

ENV PYTHONUNBUFFERED=1 \
    LISTENER_PORT=80 \
    ADMIN_PORT=9000 \
    STATE_DIR=/state

EXPOSE 80 9000
CMD ["python", "alb_sim.py"]
