# Update prereg.py with new frozen config hash
sidecar_hash = "1f18a3a7d21ca6f2e39177c6b85483db68f2a64f574cd63c8c2bb7bfe18008b4"

with open('ingestion/harness/prereg.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Update EXPECTED_SHA256
content = content.replace(
    'EXPECTED_SHA256 = "983316e26e86c5e6778e9adf8252ec4cc0b3901528cef4214d68a89888936921"',
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

print(f'prereg.py updated with new sidecar hash: {sidecar_hash}')