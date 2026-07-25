# Confirmed-at historical backfill

Migration `2c4e6a7b8d9e` only adds the nullable `media_buys.confirmed_at`
column. After deploying that migration, run the backfill separately:

```bash
uv run python scripts/ops/backfill_media_buy_confirmed_at.py --batch-rows 1000
```

The job commits each batch independently and waits for conflicting row locks,
so it can be safely rerun after interruption. Monitor its reported row count;
a second run should report zero updates. Eligibility is derived from the
application's canonical unconfirmed-status set, including case normalization.
