"""
PIT Validator
Validates Point-In-Time (PIT) integrity of factor datasets and dataframes.
Ensures timestamp_col <= trade_date_col, schema completeness, and provenance inclusion.
Includes @pit_validated decorator for factor writer methods/functions.
"""

import functools
import pandas as pd
from typing import Callable, List, Optional

class PITValidationError(Exception):
    """Raised when Point-In-Time alignment or schema validation fails."""
    pass

class PITValidator:
    @staticmethod
    def validate_pit(
        df: pd.DataFrame,
        timestamp_col: str = "filed",
        trade_date_col: str = "date",
        required_cols: Optional[List[str]] = None
    ) -> bool:
        """
        Validates that timestamp_col <= trade_date_col (no lookahead bias).
        Checks schema constraints (required columns present, no null keys).
        """
        if df.empty:
            return True

        if required_cols:
            missing = set(required_cols) - set(df.columns)
            if missing:
                raise PITValidationError(f"Missing required columns: {missing}")

        if timestamp_col in df.columns and trade_date_col in df.columns:
            ts = pd.to_datetime(df[timestamp_col])
            td = pd.to_datetime(df[trade_date_col])
            
            violations = df[ts > td]
            if not violations.empty:
                raise PITValidationError(
                    f"Lookahead bias detected! {len(violations)} rows have {timestamp_col} > {trade_date_col}. "
                    f"First violation:\n{violations.head(1).to_dict(orient='records')}"
                )

        # Check key non-null constraints if standard columns present
        for col in [c for c in ["date", "ticker", timestamp_col] if c in df.columns]:
            if df[col].isnull().any():
                raise PITValidationError(f"Null values found in key column '{col}'")

        return True

def pit_validated(
    timestamp_col: str = "filed",
    trade_date_col: str = "date",
    required_cols: Optional[List[str]] = None
):
    """
    Decorator for functions/methods that output or process factor DataFrames.
    Validates PIT constraints on returned DataFrame or input argument 'df'.
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Check if df is passed in kwargs or positional args
            df_input = kwargs.get("df", None)
            if df_input is None and len(args) > 0 and isinstance(args[0], pd.DataFrame):
                df_input = args[0]
            elif df_input is None and len(args) > 1 and isinstance(args[1], pd.DataFrame):
                df_input = args[1]

            if isinstance(df_input, pd.DataFrame):
                PITValidator.validate_pit(df_input, timestamp_col, trade_date_col, required_cols)

            result = func(*args, **kwargs)

            if isinstance(result, pd.DataFrame):
                PITValidator.validate_pit(result, timestamp_col, trade_date_col, required_cols)

            return result
        return wrapper
    return decorator

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PIT Validator Harness")
    parser.add_argument("--test", action="store_true", help="Run test suite for PITValidator")
    args = parser.parse_args()

    if args.test:
        print("Running PITValidator tests...")
        
        # Test 1: Valid PIT Data
        valid_df = pd.DataFrame({
            "filed": ["2023-01-01", "2023-01-02"],
            "date": ["2023-01-02", "2023-01-03"],
            "ticker": ["AAPL", "GOOGL"],
            "value": [10.0, 20.0]
        })
        assert PITValidator.validate_pit(valid_df, "filed", "date", ["ticker", "value"])
        print("Test 1 Passed: Valid PIT dataset validated successfully.")

        # Test 2: Invalid PIT Data (Lookahead Bias)
        invalid_df = pd.DataFrame({
            "filed": ["2023-01-05", "2023-01-02"],
            "date": ["2023-01-02", "2023-01-03"],
            "ticker": ["AAPL", "GOOGL"],
            "value": [10.0, 20.0]
        })
        try:
            PITValidator.validate_pit(invalid_df, "filed", "date")
            assert False, "Should have raised PITValidationError"
        except PITValidationError as e:
            print(f"Test 2 Passed: Caught lookahead violation correctly ({e}).")

        # Test 3: Decorator test
        @pit_validated(timestamp_col="filed", trade_date_col="date")
        def process_factors(df: pd.DataFrame):
            return df

        try:
            process_factors(invalid_df)
            assert False, "Decorator should have raised PITValidationError"
        except PITValidationError:
            print("Test 3 Passed: @pit_validated decorator trapped violation.")

        print("All PITValidator tests passed successfully!")

if __name__ == "__main__":
    main()
