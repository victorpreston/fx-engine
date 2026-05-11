# Code Review — `planted_bugs/`

Reviewed as if a teammate had opened this as a PR and asked for my sign-off.

**My approach:** I read every file in `planted_bugs/` line by line before writing anything down. I ran the existing pytest suite (all green — the bugs survive their own test coverage). For each hypothesis I formed, I wrote a targeted test or ran the code path directly to confirm the bug reproduces before flagging it. Items I suspected but could not reproduce were not included.

**Tools used:** I used Claude Code as a second pair of eyes when checking edge cases — the same way I would use a colleague during a review. All severity rankings, production-impact reasoning, and fix proposals are my own judgment.

Issues are ordered by **production impact**, not by how obvious the bug is.

---

## Bug 1 — Race condition allows double-execution of the same quote

**Severity:** Blocker

**Location:** `fx.py:112–138`

**What's wrong:**

The check-then-act sequence is not atomic:

```python
# Outside the lock:
if row["executed"]:
    raise ValueError("quote already executed")

# Lock acquired here — too late:
with _execute_lock:
    conn.execute("UPDATE quotes SET executed = 1 ...")
```

Two threads can both pass the `if row["executed"]` check before either acquires `_execute_lock`. Both then update `executed = 1` and insert separate `transactions` rows for the same quote.

**Production impact:** In a real system with balances, this debits the customer twice per execution — once per thread that slipped through. The bank's ledger shows two transaction records for one quote. Under load (even light load with thread pool workers), this is reliably reproducible.

**Fix:** Do the check and update atomically in one SQL round-trip:

```sql
UPDATE quotes SET executed = 1, executed_at = ?
WHERE id = ? AND executed = 0
```

Check `rowcount == 0` to detect the already-executed case, all without a lock. Or use `SELECT ... FOR UPDATE` inside a transaction so the check and update are serialised at the database level.

---

## Bug 2 — Execute uses live rate instead of the rate locked in at quote time

**Severity:** Blocker

**Location:** `fx.py:126–132`

**What's wrong:**

```python
current_rate = self._effective_rate(row["from_currency"], row["to_currency"])
amount = Decimal(row["amount"])
final = (amount * current_rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)
```

`row["rate"]` and `row["final_amount"]` are ignored. The engine re-fetches the live rate at execution time and recomputes `final_amount`.

**Production impact:** The entire value of an FX quote is that the customer locks in a rate for 60 seconds. If rates move between quote generation and execution (normal market behaviour), the customer receives a different amount than shown on their quote — potentially materially worse. This is a consumer-protection violation and a financial accounting inconsistency (the quoted amount ≠ the executed amount). It also creates reconciliation failures because `quotes.final_amount` and `transactions.final_amount` will diverge.

**Fix:** Use `row["rate"]` and `row["final_amount"]` directly. Never recompute rates on the execute path.

---

## Bug 3 — No customer balance tracking

**Severity:** Blocker

**Location:** `fx.py:99–178`, `db.py:22–56`

**What's wrong:**

`execute_quote` does not debit the source balance or credit the destination balance. There is no `balances` table. The assignment requires "atomic two-leg execution" and states "debit the source-currency balance and credit the destination-currency balance."

**Production impact:** Any customer can convert any amount, regardless of funds available. Transactions are logged but no money moves. The system is a record-keeping facade with no actual financial constraint enforcement.

**Fix:** Add a `balances(customer_id, currency, amount)` table with a `CHECK (amount >= 0)` constraint. In the execute transaction: lock balances with `SELECT FOR UPDATE`, check sufficiency, `UPDATE ... amount - from_amount`, `UPDATE ... amount + to_amount`, then insert the transaction. All four writes in one transaction.

---

## Bug 4 — `_execute_lock` is process-local; provides zero protection in multi-worker deployments

**Severity:** Major

**Location:** `fx.py:21`

**What's wrong:**

```python
_execute_lock = threading.Lock()
```

This is a module-level Python lock. It guards only within one OS process. A production deployment with gunicorn `-w 4` has four independent processes, each with its own `_execute_lock`. Concurrent requests routed to different workers have zero protection.

**Production impact:** Even ignoring Bug 1 (the lock is too late anyway), this design fails completely the moment you run more than one worker. Every concurrent pair of requests to different workers can double-execute the same quote.

**Fix:** Database-level locking (`SELECT FOR UPDATE` in a transaction) is the only correct solution — it works across any number of processes and servers.

---

## Bug 5 — Idempotency check is outside the execute lock/transaction (TOCTOU race)

**Severity:** Major

**Location:** `fx.py:102–110`

**What's wrong:**

```python
# Check idempotency — outside any lock:
row = conn.execute("SELECT response FROM idempotency WHERE key = ?", ...).fetchone()
if row:
    return json.loads(row["response"])

# Execution happens after, with a separate lock:
with _execute_lock:
    conn.execute("UPDATE quotes SET executed = 1 ...")
    conn.execute("INSERT INTO idempotency ...")
```

Two concurrent retries with the same key can both find no cached row at line 106, both fall through to the execute path, and both attempt to insert into `idempotency`. The second insert fails on the UNIQUE constraint and the client receives a 500 — the opposite of the idempotency guarantee.

**Production impact:** Under concurrent retry (standard client behaviour in any payment system), idempotency fails with a server error. The customer's retry logic may then issue another request with a new key, causing a double charge.

**Fix:** The idempotency key lookup must run as the first statement inside the same database transaction as the `SELECT FOR UPDATE` on the quote row:

```python
with conn.transaction():
    existing = conn.execute("SELECT ... FROM idempotency WHERE key = ?").fetchone()
    if existing: return ...          # safe early return
    quote = conn.execute("SELECT ... FOR UPDATE WHERE id = ?").fetchone()
    ...
```

---

## Bug 6 — Float arithmetic in `generate_quote` introduces precision errors

**Severity:** Major

**Location:** `fx.py:60–63`

**What's wrong:**

```python
final = float(amount) * float(rate)
final_decimal = Decimal(str(final)).quantize(QUANTUM, rounding=ROUND_HALF_UP)
```

Both `amount` and `rate` are already `Decimal`. Converting to `float` loses precision — IEEE 754 doubles have ~15-16 significant digits; a large NGN amount (e.g., `Decimal("1234567.89")`) converted to float and back will have rounding errors in the lower digits before `quantize` is applied.

**Production impact:** For small amounts, the error is sub-kobo and rounds away cleanly. For large amounts or accumulated over many transactions, the quoted `to_amount` diverges from what pure-Decimal arithmetic would give. In an audit or reconciliation, the bank's numbers don't add up.

**Fix:** `final = amount * rate` — both are `Decimal`, multiply directly. One line.

---

## Bug 7 — Inverse rate calculation discards the spread

**Severity:** Major

**Location:** `fx.py:188–191`

**What's wrong:**

```python
inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
if inverse is not None:
    mid = (inverse["buy"] + inverse["sell"]) / 2  # averages the spread away
    return Decimal("1") / mid
```

For a conversion of KES → USD (no direct KES/USD pair stored), the code computes `mid = avg(buy=129, sell=130) = 129.5` and returns `1/129.5 ≈ 0.007722`. The mid-rate is `1/130 ≈ 0.007692`. The customer receives **a better rate than mid** — the bank loses money on every inverse-pair conversion.

**Production impact:** Revenue leakage on every inverse pair. For KES→USD on a 1,000,000 KES transaction, the bank loses approximately (0.007722 - 0.007692) × 1,000,000 ≈ USD 3 per transaction. At scale this is material.

**Fix:** For the inverse direction, use the sell rate (not the mid):

```python
return Decimal("1") / inverse["sell"]
```

The customer converting FROM KES TO USD is buying USD from the bank; the bank sells USD at the sell rate.

---

## Bug 8 — Cross-pair routing computes a wildly wrong rate

**Severity:** Major

**Location:** `fx.py:193–200`

**What's wrong:**

```python
leg1 = self.rates.get(f"{from_ccy}/USD") or self.rates.get(f"USD/{from_ccy}")
leg2 = self.rates.get(f"USD/{to_ccy}") or self.rates.get(f"{to_ccy}/USD")
if leg1 and leg2:
    return leg1["sell"] * leg2["sell"]
```

For KES → NGN:
- `rates.get("KES/USD")` → `None`
- `rates.get("USD/KES")` → `{"buy": 129, "sell": 130}` ← rate object for the **wrong pair direction**
- `rates.get("USD/NGN")` → `{"buy": 1472, "sell": 1488}`
- Returns `130 × 1488 = 193,440`

Correct rate: KES/NGN ≈ 1480 / 129.5 ≈ 11.43 NGN per KES.

The computed rate is off by **~16,900×**.

**Production impact:** Any cross-pair conversion (any pair not in the six stored pairs) produces a catastrophically wrong exchange rate. A customer sending 1,000 KES would be quoted 193,440,000 NGN instead of ~11,430 NGN. The bank or customer suffers a massive loss depending on which side the error falls.

**Fix:** When the fallback pair direction is used (e.g., `USD/{from_ccy}` instead of `{from_ccy}/USD`), invert the rate:

```python
def _leg_rate(self, from_ccy, to_ccy):
    direct = self.rates.get(f"{from_ccy}/{to_ccy}")
    if direct:
        return direct["buy"]  # customer sells from_ccy
    inv = self.rates.get(f"{to_ccy}/{from_ccy}")
    if inv:
        return Decimal("1") / inv["sell"]
    raise ValueError(f"No rate for {from_ccy}/{to_ccy}")
```

---

## Bug 9 — `get_db()` has no explicit rollback on exception

**Severity:** Minor

**Location:** `db.py:12–19`

**What's wrong:**

```python
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
```

On exception, `conn.close()` is called without `conn.rollback()`. SQLite implicitly rolls back uncommitted transactions when a connection closes, so this works today. But:
1. It is not obvious to a reader — the absence of rollback looks like an oversight.
2. If this layer is ever migrated to PostgreSQL with connection pooling, connections are returned to the pool, not closed. An uncommitted transaction without an explicit rollback will block or corrupt the next user of that connection.

**Fix:**

```python
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

---

## Bug 10 — `SPREAD_BPS` name implies integer basis-point count

**Severity:** Nit

**Location:** `rates.py:19`

**What's wrong:**

```python
SPREAD_BPS = Decimal("0.005")  # 50 bps each side
```

`BPS` conventionally denotes an integer count of basis points (50, 100, etc.). The value `0.005` is the decimal fraction (0.5%). A future developer reading `SPREAD_BPS` might write `SPREAD_BPS += 50` intending to widen the spread by 50 bps, which would instead set it to `50.005` — a 5,000% spread.

**Fix:** Rename to `SPREAD_RATE` or `SPREAD_FRACTION` and add an inline comment:
```python
SPREAD_RATE = Decimal("0.005")  # 0.5% = 50 bps each side
```

---

## Summary Table

| # | Location | Severity | Category |
|---|----------|----------|----------|
| 1 | `fx.py:112` | Blocker | Race condition — double execute |
| 2 | `fx.py:126` | Blocker | Rate re-fetched at execute time |
| 3 | `fx.py:99` | Blocker | No balance tracking |
| 4 | `fx.py:21` | Major | Process-local lock |
| 5 | `fx.py:102` | Major | Idempotency TOCTOU race |
| 6 | `fx.py:60` | Major | Float arithmetic |
| 7 | `fx.py:188` | Major | Inverse rate discards spread |
| 8 | `fx.py:193` | Major | Cross-pair rate wrong by ~17,000× |
| 9 | `db.py:12` | Minor | Missing explicit rollback |
| 10 | `rates.py:19` | Nit | Misleading variable name |

Bugs 1, 2, and 3 are blockers in any order — they must all be fixed before this code handles real money. Bugs 4 and 5 become blockers the moment the service is deployed with more than one worker. Bugs 6, 7, and 8 cause financial calculation errors ranging from sub-cent rounding (Bug 6) to catastrophic wrong-rate output (Bug 8).

I did not flag style choices (the use of `dataclass` for `Quote`, the structuring of `to_dict`) as bugs — they are reasonable implementation choices that do not affect correctness.
