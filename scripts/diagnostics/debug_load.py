import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ingestion.harness.three_arm_runner import load_data_from_store

print("Testing load_data_from_store...")
data = load_data_from_store('2018-01-01', '2024-12-31')
print('Qual shape:', data['qual'].shape)
print('Qual columns:', data['qual'].columns.tolist())
if not data['qual'].empty:
    print('Qual date range:', data['qual']['date'].min(), 'to', data['qual']['date'].max())
    print('Qual tickers:', data['qual']['ticker'].nunique())
else:
    print("QUAL IS EMPTY!")