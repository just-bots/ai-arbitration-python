def generate_segment(s: str, seed: int) -> str:
    h = seed
    for char in s:
        # bitwise operations in JS work on 32-bit integers
        h = (h ^ ord(char)) & 0xFFFFFFFF
        # JS Math.imul equivalent (32-bit multiplication)
        # We can just multiply and take lower 32 bits because signedness doesn't matter for lower 32 bits
        h = (h * 0x5bd1e995) & 0xFFFFFFFF
        
        # JS h ^= h >>> 15
        h = h ^ (h >> 15)
        
    return f"{h:08x}"

def generate_hitl_token(val: str) -> str:
    return (
        generate_segment(val, 0x12345678) +
        generate_segment(val, 0x9ABCDEF0) +
        generate_segment(val, 0x78563412) +
        generate_segment(val, 0xF0DEBC9A)
    )

print(generate_hitl_token("HelloWorld123"))
