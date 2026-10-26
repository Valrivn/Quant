# Fix verify_preregistration_hash to properly verify against sidecar

with open('research/phase9_full_pipeline.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the verify_preregistration_hash function
old_func = '''def verify_preregistration_hash() -> bool:
    """Verifies pre-registration YAML integrity against frozen SHA-256 hash."""
    config_path = Path(FROZEN_CONFIG_PATH)
    if not config_path.exists():
        logger.error(f"Pre-registration file not found at {FROZEN_CONFIG_PATH}")
        return False

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
        if cfg.get("sha256") == EXPECTED_SHA256:
            return True
        if cfg.get("status") == "APPROVED":
            return True
    return False'''

new_func = '''def verify_preregistration_hash() -> bool:
    """Verifies pre-registration YAML integrity against frozen SHA-256 sidecar."""
    import hashlib
    config_path = Path(FROZEN_CONFIG_PATH)
    sidecar_path = config_path.with_suffix(config_path.suffix + ".sha256")
    
    if not config_path.exists():
        logger.error(f"Pre-registration file not found at {FROZEN_CONFIG_PATH}")
        return False
    if not sidecar_path.exists():
        logger.error(f"Sidecar file not found at {sidecar_path}")
        return False
    
    # Read sidecar hash
    with open(sidecar_path, "r") as f:
        expected_hash = f.read().strip()
    
    # Compute hash of config file (canonical: exclude self-referential sha256 lines)
    with open(config_path, "rb") as f:
        content = f.read()
    text = content.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    canonical_lines = []
    for i, line in enumerate(lines):
        if "sha256:" in line and not line.strip().startswith("#"):
            continue
        if "pre_registration_sha256:" in line:
            continue
        canonical_lines.append(line)
    canonical_content = "".join(canonical_lines)
    
    # Compute hash
    h = hashlib.sha256()
    h.update(canonical_content.encode("utf-8"))
    actual_hash = h.hexdigest()
    
    if actual_hash == expected_hash:
        return True
    logger.error(f"Pre-registration hash mismatch: expected {expected_hash}, got {actual_hash}")
    return False'''

content = content.replace(old_func, new_func)

with open('research/phase9_full_pipeline.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('verify_preregistration_hash() fixed - now verifies against sidecar with canonical hash')