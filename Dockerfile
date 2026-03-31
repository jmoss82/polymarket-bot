FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs

# Switch via Railway CMD override or here:
#   Copy-trade bot:  python -u copy_trader.py
#   E2E test:        python -u e2e_test.py
CMD ["python", "-u", "consensus_tracker.py"]
