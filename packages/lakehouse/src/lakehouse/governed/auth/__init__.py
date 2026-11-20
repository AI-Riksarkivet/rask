"""The authorization model and its writer: `model.fga`, the `model.json` every service loads, `model.fga.yaml`'s tests,
and `write_model`, which the chart's hook runs to write the model the image carries.

A regular package, so the import graph `.importlinter` judges holds `write_model` too. Nothing is exported here.
"""
