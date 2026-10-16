from pathlib import Path

import joblib
import polars as pl

from lakana import asset


@asset(inputs={"model": "models.spend_model", "visits": "bronze.visits"})
def spend_predictions(model: Path, visits: pl.DataFrame) -> pl.DataFrame:
    """What the model expects each customer to spend."""
    fitted = joblib.load(model)
    spend = fitted.predict(visits.select("visits").to_numpy())
    return visits.select("customer_id").with_columns(predicted_spend=pl.Series(spend))
