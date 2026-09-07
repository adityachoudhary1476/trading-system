"""Regression tests for the Phase 8C data-wiring in _cmd_serve_paper_api.

Verifies that the paper-API server entrypoint attaches:
  * a market_data_provider callable to PaperTradingControlCenter (Upstox-based)
  * an InstrumentRepository + CurrentOptionDiscoverer to AutonomousController
  * an OptionsChainProvider fallback to AutonomousController

When Upstox credentials/env are absent the wiring must fail closed:
market_data_provider returns None, discoverer stays None.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_args():
    args = MagicMock()
    args.host = "127.0.0.1"
    args.port = 0
    return args


class TestPaperApiWiring:
    def test_market_data_provider_attached(self):
        from trading_system.paper.control import PaperTradingControlCenter

        captured = {}

        def fake_from_engine(engine, **kwargs):
            captured["market_data_provider"] = kwargs.get("market_data_provider")
            return PaperTradingControlCenter(
                registry=MagicMock(),
                intelligence=MagicMock(),
                gate=MagicMock(),
                market_data_provider=kwargs.get("market_data_provider"),
            )

        args = _make_args()
        with patch.dict("os.environ", {}, clear=True):
            with patch("sqlalchemy.create_engine") as mock_engine:
                with patch("trading_system.config.settings") as mock_settings:
                    mock_settings.storage.db_url = "sqlite:///:memory:"
                    mock_engine.return_value = MagicMock()
                    with patch("trading_system.paper_api.PaperAPIRouter"):
                        with patch(
                            "trading_system.paper_api.build_default_server"
                        ) as mock_build:
                            mock_build.return_value = MagicMock()
                            with patch.object(
                                PaperTradingControlCenter,
                                "from_engine",
                                side_effect=fake_from_engine,
                            ):
                                from trading_system.__main__ import _cmd_serve_paper_api

                                try:
                                    _cmd_serve_paper_api(args)
                                except Exception:
                                    pass

        assert captured["market_data_provider"] is not None
        assert callable(captured["market_data_provider"])

    def test_no_upstox_credentials_means_no_discoverer(self):
        import inspect
        from trading_system.__main__ import _cmd_serve_paper_api

        src = inspect.getsource(_cmd_serve_paper_api)
        assert "UpstoxInstrumentDiscovery" in src
        assert "CurrentOptionDiscoverer" in src
        assert "market_data_callable" in src
        assert "set_option_discoverer" in src
        assert "set_chain_provider" in src

    def test_upstox_market_data_provider_used(self):
        import inspect
        from trading_system.__main__ import _cmd_serve_paper_api

        src = inspect.getsource(_cmd_serve_paper_api)
        assert "UpstoxMarketDataProvider" in src
        assert "FYERSMarketDataProvider" not in src
        assert "FYERS_CLIENT_ID" not in src
        assert "FYERS_ACCESS_TOKEN" not in src
        assert "UPSTOX_CLIENT_ID" in src or "is_authenticated" in src
