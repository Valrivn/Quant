# Fix prereg.py: declared_sha256 should read from sidecar when key missing from config

with open('ingestion/harness/prereg.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Update declared_sha256 property to fall back to sidecar
old_property = '''    @property
    def declared_sha256(self) -> str:
        return str(self.get("sha256"))'''

new_property = '''    @property
    def declared_sha256(self) -> str:
        try:
            return str(self.get("sha256"))
        except PreRegistrationError:
            # Self-referential sha256 line removed per CEO ruling 1A;
            # fall back to sidecar file
            sidecar = self.path.with_suffix(self.path.suffix + ".sha256")
            if sidecar.exists():
                return sidecar.read_text().strip()
            raise'''

content = content.replace(old_property, new_property)

with open('ingestion/harness/prereg.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('prereg.py updated: declared_sha256 falls back to sidecar')