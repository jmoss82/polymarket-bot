FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs

# Switch between test and live:
#   Test:  python -u e2e_test.py
#   Live:  python -u live_trader.py
#   Audit: python -u live_trader.py --audit
CMD ["python", "-u", "live_trader.py", "--audit"]
