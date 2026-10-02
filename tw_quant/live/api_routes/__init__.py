from .admin import build_admin_router
from .market import build_market_router
from .paper import build_paper_router
from .research import build_research_router
from .strategies import build_strategy_router
from .system import build_system_router
from .trading_runtimes import build_trading_runtime_router
from .live_canary import build_live_canary_router
from .live_auto import build_live_auto_router

__all__ = [
    "build_admin_router",
    "build_market_router",
    "build_paper_router",
    "build_research_router",
    "build_strategy_router",
    "build_system_router",
    "build_trading_runtime_router",
    "build_live_canary_router",
    "build_live_auto_router",
]
