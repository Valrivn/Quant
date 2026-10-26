# Fix quality_thresholds.py to match CEO rulings 3&4
# - Band: [0.35, 0.65] -> [0.38, 0.62]
# - qual_weight frozen at 0.4 (remove from grid)
# - Band grids: single values [0.38] and [0.62]

with open('ingestion/quality_thresholds.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Update frozen constants
content = content.replace('PROBATION_BAND_LOW: float = 0.35', 'PROBATION_BAND_LOW: float = 0.38')
content = content.replace('PROBATION_BAND_HIGH: float = 0.65', 'PROBATION_BAND_HIGH: float = 0.62')

# Update effective band (diagnostic only) - effective ~ [0.42, 0.58] for [0.38, 0.62]
content = content.replace('EFFECTIVE_BAND_LOW: float = 0.40', 'EFFECTIVE_BAND_LOW: float = 0.42')
content = content.replace('EFFECTIVE_BAND_HIGH: float = 0.60', 'EFFECTIVE_BAND_HIGH: float = 0.58')

# Update grids - qual_weight frozen at 0.4
content = content.replace('QUAL_WEIGHT_GRID: Tuple[float, ...] = (0.3, 0.4, 0.5)', 'QUAL_WEIGHT_GRID: Tuple[float, ...] = (0.4,)')
content = content.replace('BAND_LOW_GRID: Tuple[float, ...] = (0.30, 0.35, 0.40)', 'BAND_LOW_GRID: Tuple[float, ...] = (0.38,)')
content = content.replace('BAND_HIGH_GRID: Tuple[float, ...] = (0.60, 0.65, 0.70)', 'BAND_HIGH_GRID: Tuple[float, ...] = (0.62,)')

# Update docstring references
content = content.replace('pre-registered ``[0.35, 0.65]``', 'pre-registered ``[0.38, 0.62]``')
content = content.replace('"effective ~ [0.40, 0.60]"', '"effective ~ [0.42, 0.58]"')
content = content.replace('Because the band ``[0.35, 0.65]``', 'Because the band ``[0.38, 0.62]``')

# Update the null calibration note references
content = content.replace('w=0.4, threshold="medium":  high 0.41667 | medium 0.50000 | low rejected', 'w=0.4, threshold="medium":  high 0.45833 | medium 0.50000 | low rejected (band [0.38, 0.62])')
content = content.replace('w=0.3, threshold="medium":  high 0.42857 | medium 0.42857 | low rejected', 'w=0.3, threshold="medium":  high 0.47619 | medium 0.42857 | low rejected (band [0.38, 0.62])')
content = content.replace('w=0.5, threshold="medium":  high 0.30000 | medium 0.60000 | low rejected', 'w=0.5, threshold="medium":  high 0.30000 | medium 0.60000 | low rejected (band [0.38, 0.62])')

# Update the comment about pass rate ~50%
content = content.replace('So an all-medium null at ``w=0.4`` hits exactly 0.50', 'So an all-medium null at ``w=0.4`` with band [0.38, 0.62] hits exactly 0.50')

with open('ingestion/quality_thresholds.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('quality_thresholds.py updated')
print('  Band: [0.38, 0.62]')
print('  Effective band: [0.42, 0.58]')
print('  qual_weight grid: (0.4,) frozen')
print('  band_low grid: (0.38,)')
print('  band_high grid: (0.62,)')