# Playwright's official image ships Chromium + all system deps preinstalled,
# which saves a lot of pain vs. installing them by hand on Railway.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

# Default command; Railway cron services override this with the mode arg.
CMD ["python", "main.py", "scrape"]
