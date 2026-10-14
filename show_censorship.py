import json
from cryptography.fernet import Fernet

fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')

with open('censorship.dat', 'rb') as f:
    config = json.loads(fernet.decrypt(f.read()))

tier1 = config['categories']['tier1_severe_violations']['words']
tier2 = config['categories']['tier2_spam_scams']['phrases']
tier3 = config['categories']['tier3_mild_profanity']['words']

print("TIER 1 (severe words):", len(tier1))
print(tier1)
print("\nTIER 2 (spam/scam phrases):", len(tier2))
print(tier2)
print("\nTIER 3 (mild profanity):", len(tier3))
print(tier3)