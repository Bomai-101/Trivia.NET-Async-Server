# questions.py
# Each function returns a short_question string (no printing).

import random

def generate_mathematics_question() -> str:
    # Build: "<n> <op> <n> [<op> <n>]..." with 1–4 operators, numbers 0–199
    op_count = random.randint(1, 4)
    s = str(random.randint(0, 100))  # first number
    for _ in range(op_count):
        op = random.choice(['+', '-'])
        n = random.randint(0, 100)
        s += f" {op} {n}"
    return s

def generate_roman_numerals_question() -> str:
    # Return a Roman numeral string (e.g., "XIV")
    n = random.randint(1, 3999)
    return _int_to_roman(n)

def generate_usable_addresses_question() -> str:
    # Return a valid IPv4 CIDR for usable-address question
    bases = ["192.168.0.0", "192.168.1.0", "172.16.0.0", "10.0.0.0"]
    base = random.choice(bases)
    prefix = random.choice([24, 25, 26, 27, 28, 29, 30])
    return f"{base}/{prefix}"

def generate_network_broadcast_question() -> str:
    # Return a valid IPv4 CIDR for network/broadcast question
    third_octet = random.randint(0, 254)
    return f"10.0.{third_octet}.0/24"
  
# ---- helpers ----
def _int_to_roman(num: int) -> str:
    vals = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"),  (90, "XC"),  (50, "L"),  (40, "XL"),
        (10, "X"),   (9, "IX"),   (5, "V"),   (4, "IV"), (1, "I")
    ]
    res = []
    n = num
    for v, s in vals:
        if n == 0:
            break
        count, n = divmod(n, v)
        if count:
            res.append(s * count)
    return "".join(res)
