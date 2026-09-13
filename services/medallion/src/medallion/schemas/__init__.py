"""Wire and state contracts for the medallion services.

`events` builds the OpenLineage run events; `tier` is the governed row contract every tier carries;
`promotion` is the held-promotion payload a stage runner publishes and the review workflow resumes
from. All three are plain models with no engine and no workload in them, which is what lets a door
import the contract without importing whatever happens to process it.
"""
