import pytest
import tempfile
import os
from unittest.mock import patch
from datetime import datetime, timezone
from psychological.dcf_floor import (
    DCFFloorStub, DCFFloorClient, create_dcf_floor_stub, 
    create_dcf_floor_client, DCFFloorOutput
)
from db.schema import create_phase1_tables


class TestDCFFloorStub:
    @pytest.fixture
    def mock_config(self):
        return {
            "quantitative_dcf": {
                "default_wacc": 0.10,
                "default_terminal_growth": 0.03,
                "projection_years": 5,
                "margin_of_safety": 0.25,
                "model_version": "stub_v1"
            }
        }

    @pytest.fixture
    def stub(self, mock_config):
        with patch('psychological.dcf_floor.load_hybrid_config') as mock_load:
            mock_load.return_value = {"quantitative_dcf": mock_config["quantitative_dcf"]}
            stub = DCFFloorStub(config_dict=mock_config["quantitative_dcf"])
            yield stub

    def test_init(self, stub):
        assert stub is not None
        assert stub.default_wacc == 0.10
        assert stub.default_terminal_growth == 0.03
        assert stub.margin_of_safety == 0.25

    def test_calculate_dcf_floor_basic(self, stub):
        result = stub.calculate_dcf_floor(
            ticker="AAPL",
            current_price=150.0,
            fcf_projection=100.0,
            wacc=0.10,
            terminal_growth=0.03,
            projection_years=5
        )
        
        assert isinstance(result, DCFFloorOutput)
        assert result.ticker == "AAPL"
        assert result.current_price == 150.0
        assert result.fcf_projection == 100.0
        assert result.wacc == 0.10
        assert result.intrinsic_floor > 0
        assert result.intrinsic_ceiling > result.intrinsic_floor
        assert 0.0 <= result.upside_ratio <= 1.0
        assert 0.0 <= result.risk_adjusted_upside <= 1.0

    def test_calculate_dcf_floor_wacc_greater_than_growth(self, stub):
        with pytest.raises(ValueError, match="WACC must be greater than terminal growth rate"):
            stub.calculate_dcf_floor(
                ticker="AAPL",
                current_price=150.0,
                fcf_projection=100.0,
                wacc=0.05,
                terminal_growth=0.10
            )

    def test_calculate_dcf_floor_cache(self, stub):
        result1 = stub.calculate_dcf_floor("AAPL", 150.0, 100.0, 0.10, 0.03)
        result2 = stub.get_dcf_floor("AAPL")
        
        assert result2 is not None
        assert result2.ticker == "AAPL"
        assert result2.intrinsic_floor == result1.intrinsic_floor

    def test_calculate_dcf_floor_different_tickers(self, stub):
        result_aapl = stub.calculate_dcf_floor("AAPL", 150.0, 100.0, 0.10, 0.03)
        result_msft = stub.calculate_dcf_floor("MSFT", 300.0, 200.0, 0.10, 0.03)
        
        assert stub.get_dcf_floor("AAPL") == result_aapl
        assert stub.get_dcf_floor("MSFT") == result_msft

    def test_calculate_dcf_floor_projection_years(self, stub):
        result_5 = stub.calculate_dcf_floor("AAPL", 150.0, 100.0, 0.10, 0.03, 5)
        result_10 = stub.calculate_dcf_floor("AAPL", 150.0, 100.0, 0.10, 0.03, 10)
        
        # Terminal value formula is independent of projection_years, but PV of terminal differs
        assert result_10.intrinsic_floor != result_5.intrinsic_floor


class TestDCFFloorClient:
    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        import sqlite3
        conn = sqlite3.connect(db_path)
        create_phase1_tables(conn)
        conn.close()
        
        yield db_path
        
        os.unlink(db_path)

    @pytest.fixture
    def mock_config(self):
        return {
            "quantitative_dcf": {
                "default_wacc": 0.10,
                "default_terminal_growth": 0.03,
                "projection_years": 5,
                "margin_of_safety": 0.25,
                "model_version": "stub_v1"
            }
        }

    @pytest.fixture
    def client(self, temp_db, mock_config):
        with patch('psychological.dcf_floor.load_hybrid_config') as mock_load:
            mock_load.return_value = {"quantitative_dcf": mock_config["quantitative_dcf"]}
            client = DCFFloorClient(db_path=temp_db, config_dict=mock_config["quantitative_dcf"])
            yield client

    def test_init(self, client):
        assert client is not None
        assert client.db_path is not None
        assert client.stub is not None

    def test_fetch_and_store(self, client):
        result = client.fetch_and_store(
            ticker="AAPL",
            current_price=150.0,
            fcf_projection=100.0,
            wacc=0.10,
            terminal_growth=0.03
        )
        
        assert isinstance(result, DCFFloorOutput)
        assert result.ticker == "AAPL"
        assert result.current_price == 150.0
        assert result.intrinsic_floor > 0

    def test_fetch_and_store_defaults(self, client):
        result = client.fetch_and_store(
            ticker="MSFT",
            current_price=300.0,
            fcf_projection=200.0
        )
        
        assert result.wacc == 0.10  # default
        assert result.terminal_value > 0

    def test_get_latest(self, client):
        client.fetch_and_store("AAPL", 150.0, 100.0, 0.10, 0.03)
        latest = client.get_latest("AAPL")
        
        assert latest is not None
        assert "intrinsic_floor" in latest
        assert "intrinsic_ceiling" in latest
        assert "current_price" in latest
        assert "wacc" in latest

    def test_get_latest_not_found(self, client):
        latest = client.get_latest("NONEXISTENT")
        assert latest is None


class TestCreateFunctions:
    def test_create_dcf_floor_stub(self):
        with patch('psychological.dcf_floor.load_hybrid_config') as mock_load:
            mock_load.return_value = {"quantitative_dcf": {}}
            stub = create_dcf_floor_stub()
            assert isinstance(stub, DCFFloorStub)

    def test_create_dcf_floor_client(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            create_phase1_tables(conn)
            conn.close()
            
            with patch('psychological.dcf_floor.load_hybrid_config') as mock_load:
                mock_load.return_value = {"quantitative_dcf": {}}
                client = create_dcf_floor_client(db_path)
                assert isinstance(client, DCFFloorClient)
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])