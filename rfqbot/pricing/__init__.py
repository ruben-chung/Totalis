"""Market-implied joint distribution engine for same-game dependence (NFL v1).

Public API: ``joint.build_game(quotes) -> GameModel``; ``GameModel.joint(specs) -> JointQuote``.
See docs/joint-engine-brief.md and the module docstrings of ladders_fit, joint_nfl, indicators.
"""
from .joint import GameModel, JointQuote, build_game  # noqa: F401
