const generateSegment = (str, seed) => {
  let h = seed;
  for (let i = 0; i < str.length; i++) {
    h = Math.imul(h ^ str.charCodeAt(i), 0x5bd1e995);
    h ^= h >>> 15;
  }
  return (h >>> 0).toString(16).padStart(8, '0');
};
const val = "HelloWorld123";
console.log(generateSegment(val, 0x12345678) + generateSegment(val, 0x9ABCDEF0) + generateSegment(val, 0x78563412) + generateSegment(val, 0xF0DEBC9A));
