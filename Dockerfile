FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
EXPOSE 8000
# Serves the website and runs the refresh every day at DAILY_RUN_HOUR (New York time).
CMD ["python", "-m", "offmarket", "serve"]
