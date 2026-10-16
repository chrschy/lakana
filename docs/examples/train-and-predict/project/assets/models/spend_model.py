from pathlib import Path

import joblib
import polars as pl
from sklearn.linear_model import LinearRegression

import lakana
from lakana import asset


@asset(inputs={"visits": "bronze.visits"}, output="artifact", keep_versions=2)
def spend_model(visits: pl.DataFrame) -> Path:
    """A straight line through spend by number of visits."""
    model = LinearRegression()
    model.fit(visits.select("visits").to_numpy(), visits["spend"].to_numpy())
    path = lakana.output_dir() / "model.joblib"
    joblib.dump(model, path)
    return path
