import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pathlib import Path
import polars as pl

qual_dir = Path("data/qual")
print("CWD:", Path.cwd())
print("qual_dir:", qual_dir)
print("qual_dir exists:", qual_dir.exists())
print("qual_dir absolute:", qual_dir.absolute())

for f in qual_dir.glob("*.parquet"):
    print(f"  {f.name}: exists={f.exists()}")
    if f.exists():
        q_df = pl.read_parquet(f).to_pandas()
        q_df.columns = [c.lower() for c in q_df.columns]
        print(f"    cols: {q_df.columns.tolist()}")