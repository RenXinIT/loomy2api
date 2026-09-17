FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DISPLAY=:99
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*
COPY app.py config.py database.py models.py loomy_client.py auth_browser.py accounts.py credit.py ./
RUN mkdir -p /app/data
EXPOSE 7865 6080
CMD ["bash","-lc","Xvfb :99 -screen 0 1440x900x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 & x11vnc -display :99 -forever -shared -rfbport 5900 -nopw >/tmp/x11vnc.log 2>&1 & websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/websockify.log 2>&1 & uvicorn app:app --host 0.0.0.0 --port 7865"]
