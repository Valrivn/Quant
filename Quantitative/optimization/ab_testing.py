import json
import sqlite3
import yaml
import os
import time
from db.connection import get_db_connection

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "reddit_weights.yaml"))

def get_champion_weights() -> dict:
    """Retrieves the currently active (Champion) weights from the database or YAML fallback."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT config_yaml, version_id FROM weight_versions WHERE is_active = 1 ORDER BY version_id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return yaml.safe_load(row[0]), row[1]
        
    # Fallback to YAML file if none set in DB yet
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f), None

def get_latest_challenger() -> dict:
    """Retrieves the latest optimized (Challenger) weights that are not active."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT config_yaml, version_id, ic_score, sharpe_ratio, hit_rate FROM weight_versions WHERE is_active = 0 ORDER BY version_id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "version_id": row[1],
            "weights": yaml.safe_load(row[0]),
            "ic": row[2],
            "sharpe": row[3],
            "hit_rate": row[4]
        }
    return None

def promote_challenger_to_champion(version_id: int):
    """Promotes the specified challenger version to active champion and updates the config YAML file."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Deactivate current champion
    cursor.execute("UPDATE weight_versions SET is_active = 0 WHERE is_active = 1")
    
    # Activate new champion
    now = int(time.time())
    cursor.execute("UPDATE weight_versions SET is_active = 1, promoted_at = ? WHERE version_id = ?", (now, version_id))
    
    # Fetch content to write back to local config YAML
    cursor.execute("SELECT config_yaml FROM weight_versions WHERE version_id = ?", (version_id,))
    row = cursor.fetchone()
    
    conn.commit()
    conn.close()
    
    if row:
        new_config = yaml.safe_load(row[0])
        # Preserving other fields if needed, or overwrite weights
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(new_config, f, default_flow_style=False)
        print(f"Version {version_id} promoted to Champion and written to {CONFIG_PATH}")
        return True
    return False

def compare_champion_vs_challenger() -> dict:
    """Compares metrics of the active champion against the latest challenger."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT version_id, ic_score, sharpe_ratio, hit_rate, created_at 
        FROM weight_versions 
        WHERE is_active = 1 
        ORDER BY version_id DESC LIMIT 1
    """)
    champ = cursor.fetchone()
    
    cursor.execute("""
        SELECT version_id, ic_score, sharpe_ratio, hit_rate, created_at 
        FROM weight_versions 
        WHERE is_active = 0 
        ORDER BY version_id DESC LIMIT 1
    """)
    chall = cursor.fetchone()
    conn.close()
    
    res = {"champion": None, "challenger": None}
    if champ:
        res["champion"] = {"version_id": champ[0], "ic": champ[1], "sharpe": champ[2], "hit_rate": champ[3], "date": champ[4]}
    if chall:
        res["challenger"] = {"version_id": chall[0], "ic": chall[1], "sharpe": chall[2], "hit_rate": chall[3], "date": chall[4]}
    return res
