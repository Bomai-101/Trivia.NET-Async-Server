# questions.py
# Each function returns a short_question string (no printing).

import random

def generate_mathematics_question() -> str:
    # Build: "<n> <op> <n> [<op> <n>] ..." with 1–4 operators, numbers 0–199
    op_count = random.randint(1, 4)
    s = str(random.randint(0, 100))  # first  number
    for _ in range(op_count):
        op = random.choice(['+', '-'])
        n = random.randint(0, 100)
        s += f" {op} {n}"
    return s

def generate_roman_numerals_question() -> str:
    number = random.randint(1, 3999)

    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4, 1
    ]
    syms = [
        "M", "CM", "D", "CD",
        "C", "XC", "L", "XL",
        "X", "IX", "V", "IV", "I"
    ]

    roman_numeral = ""
    i = 0
    while number > 0:
        for _ in range(number // val[i]):
            roman_numeral += syms[i]
            number -= val[i]
        i += 1

    return roman_numeral

import random

def generate_usable_addresses_question() -> str:
    a = random.randint(0, 255)
    b = random.randint(0, 255)
    c = random.randint(0, 255)
    d = random.randint(0, 255)
    prefix = random.randint(0, 32)
    return f"{a}.{b}.{c}.{d}/{prefix}"

def generate_network_broadcast_question() -> str:
    a = random.randint(0, 255)
    b = random.randint(0, 255)
    c = random.randint(0, 255)
    d = random.randint(0, 255)
    prefix = random.randint(0, 32)
    return f"{a}.{b}.{c}.{d}/{prefix}"
  
