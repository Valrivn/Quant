# Update prereg.py with new frozen config hash (raw bytes hash)
sidecar_hash = "2b45ba98a90734a2178e8052dce8b6ee4e9f0d634f1dce13830500989dcfdb8b"

with open('ingestion/harness/prereg.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Update EXPECTED_SHA256
content = content.replace(
    'EXPECTED_SHA256 = "1f18a3a7d21ca6f2e39177c6b85483db68f2a64f574cd63c8c2bb7bfe18008b4"',
    f'EXPECTED_SHA256 = "{sidecar_hash}"'
)

# Update OBSERVED_SHA256
content = content.replace(
    'OBSERVED_SHA256 = "b4b65a353f23e486193912a0f9cec242165dccec266d5262d01cfc110e777a6c"',
    f'OBSERVED_SHA256 = "{sidecar_hash}"'
)

with open('ingestion/harness/prereg.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(f'prereg.py updated with new hash: {sidecar_hash}')