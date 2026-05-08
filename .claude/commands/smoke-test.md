# API Smoke Test

Runs a full end-to-end flow against the running API: create customer → credit balance → get quote → execute → verify balances.

Requires the API to be running on port 8000.

```bash
#!/usr/bin/env bash
set -e
BASE="http://localhost:8000"

echo "=== 1. Health check ==="
curl -sf $BASE/healthz | python3 -c "import sys,json; d=json.load(sys.stdin); print('Status:', d['status'])"

echo -e "\n=== 2. Create customer ==="
CUSTOMER=$(curl -sf -X POST $BASE/customers \
  -H "Content-Type: application/json" \
  -d '{"name":"Smoke Test","email":"smoke@fx.test"}')
CID=$(echo $CUSTOMER | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Customer ID: $CID"

echo -e "\n=== 3. Credit 1000 USD ==="
curl -sf -X POST $BASE/customers/$CID/balances/credit \
  -H "Content-Type: application/json" \
  -d '{"currency":"USD","amount":"1000.00"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print('Balance:', d['amount'], d['currency'])"

echo -e "\n=== 4. Get USD→KES quote ==="
QUOTE=$(curl -sf -X POST $BASE/quotes \
  -H "Content-Type: application/json" \
  -d "{\"customer_id\":\"$CID\",\"from_currency\":\"USD\",\"to_currency\":\"KES\",\"amount\":\"100.00\"}")
QID=$(echo $QUOTE | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['quote_id'])")
echo $QUOTE | python3 -c "import sys,json; d=json.load(sys.stdin); print('Rate:', d['rate'], '| You get:', d['to_amount'], 'KES')"

echo -e "\n=== 5. Execute quote ==="
TX=$(curl -sf -X POST $BASE/quotes/$QID/execute \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: smoke-$(date +%s)" \
  -d "{\"customer_id\":\"$CID\"}")
echo $TX | python3 -c "import sys,json; d=json.load(sys.stdin); print('TX:', d['transaction_id'], '| Status:', d['status'])"

echo -e "\n=== 6. Final balances ==="
curl -sf $BASE/customers/$CID/balances | python3 -c "
import sys,json
d=json.load(sys.stdin)
for b in d['balances']:
    print(f\"  {b['currency']}: {b['amount']}\")"

echo -e "\n✅ Smoke test complete"
```
