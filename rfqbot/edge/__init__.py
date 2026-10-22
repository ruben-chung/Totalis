"""Margin optimiser: counterfactual fills, fill curves, winner's curse, markouts, wallet scores
(claude-briefs/margin-optimizer-brief.md). Quoting integration point: ``margin_for``."""
from .config import EdgeConfig, load_edge_config
from .segments import Segment, n_legs_bucket, sigma_bucket
from .table import MarginTable, MarginTerms, load_table, margin_for

__all__ = ["EdgeConfig", "load_edge_config", "Segment", "n_legs_bucket", "sigma_bucket", "MarginTable", "MarginTerms",
           "load_table", "margin_for"]
