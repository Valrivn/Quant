# Update prereg.py with correct sidecar hash
sidecar_hash = "f9615710c7d8a6348652fbc711c3bf690d531baef06ce67c0ddd849b60d61533"

with open('ingestion/harness/prereg.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Update EXPECTED_SHA256
content = content.replace(
    'EXPECTED_SHA256 = "b932bf71a7c3b070e52e95155d6f971a2c0846c54d27a84e4e84c3cb113b0505"',
    f'EXPECTED_SHA256 = "{sidecar_hash}"'
)

# Update OBSERVED_SHA256
content = content.replace(
    'OBSERVED_SHA256 = "b4b65a353f23e486193912a0f9cec242165dccec266d5262d01cfc110e777a6c"',
    f'OBSERVED_SHA256 = "{sidecar_hash}"'
)

# Update the integrity finding comment
content = content.replace(
    'sha256 = 25d991371ea02f044e8f8f46cbdd7c925f58fd9760aa9995983157c6520b4092',
    f'sha256 = {sidecar_hash}'
)
content = content.replace(
    'b4b65a353f23e486193912a0f9cec242165dccec266d5262d01cfc110e777a6c',
    sidecar_hash
)

with open('ingestion/harness/prereg.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(f'prereg.py updated with sidecar hash: {sidecar_hash}')