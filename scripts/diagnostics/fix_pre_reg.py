import hashlib

# Fix line 301 in pre_registration_v4.yaml - probation_pass_rate in success_criteria
with open('config/pre_registration_v4.yaml', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the success_criteria line
content = content.replace('probation_pass_rate: "in [0.35, 0.65]"', 'probation_pass_rate: "in [0.38, 0.62]"')

with open('config/pre_registration_v4.yaml', 'w', encoding='utf-8') as f:
    f.write(content)
print('Fixed pre_reg line 301: probation_pass_rate -> [0.38, 0.62]')

# Verify
with open('config/pre_registration_v4.yaml', 'r', encoding='utf-8') as f:
    content = f.read()
if 'probation_pass_rate: "in [0.38, 0.62]"' in content:
    print('VERIFIED: Fix applied correctly')
else:
    print('ERROR: Fix not found')