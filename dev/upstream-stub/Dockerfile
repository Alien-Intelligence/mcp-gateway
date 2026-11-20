FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN useradd --system --no-create-home app
USER app
EXPOSE 4000
# Test fixture: the control endpoints are unauthenticated, bind to loopback or a compose network only.
CMD ["uvicorn", "app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "4000"]
