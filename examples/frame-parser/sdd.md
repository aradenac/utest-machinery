# SDD — Small binary frame parser

- **FRM-001**: A valid frame SHALL have exactly four bytes: 0xA5, length value 1, command byte, and checksum byte.
- **FRM-002**: For a valid frame, checksum SHALL equal the XOR of the first three bytes. The parser SHALL return 0 and write the command to the output pointer.
- **FRM-003**: For null pointers, wrong size, wrong preamble, wrong length field or incorrect checksum, the parser SHALL return nonzero and SHALL NOT modify the command output.
- **FRM-004**: The parser SHALL NOT read beyond the supplied input length.

This fixture intentionally contains missing checks and out-of-bounds access. Its expected outcome is detection rather than an all-passing suite. Generate valid and invalid seeds for libFuzzer and verify ASan diagnostics on short buffers.
