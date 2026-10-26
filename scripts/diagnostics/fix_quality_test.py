# Fix quality_thresholds.py test assertion for new band [0.38, 0.62]

with open('ingestion/quality_thresholds.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the test assertion
content = content.replace(
    'assert cfg.qual_weight == 0.4 and cfg.band == (0.35, 0.65)',
    'assert cfg.qual_weight == 0.4 and cfg.band == (0.38, 0.62)'
)

with open('ingestion/quality_thresholds.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('quality_thresholds.py test assertion fixed: band -> (0.38, 0.62)')