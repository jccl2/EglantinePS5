# Ampere shader lowering tools (host)

Offline tools for the Ampere → Maxwell shader lowering (`Eglantine/src/shader_recompiler/frontend/ampere/lower_to_maxwell.h`).
Build them in the `prospero-win` container from the repository root (the lowering is header-only):

    clang++-18 -std=c++20 -O1 -I Eglantine/src -o lowering_stats tools/ampere/lowering_stats.cpp
    clang++-18 -std=c++20 -O1 -I Eglantine/src -o expand_examples tools/ampere/expand_examples.cpp
    clang++-18 -std=c++20 -O1 -o hmma_sim tools/ampere/hmma_sim.cpp

- `lowering_stats DIR...` — runs the lowering over dumped native programs and lists, per Ampere opcode,
  how many instructions it becomes (HMMA programs are included; `EDEN_NX2_SOFT_HMMA=0` leaves them out).
- `expand_examples DIR OPCODE...` — the Maxwell instruction patterns (top 16 bits) an opcode becomes.
- `hmma_sim` — checks the native HMMA lane mapping (PTX m16n8k16 / m16n8k8 f16 fragments) against a
  reference product with the same f32 accumulation and f16 rounding.

Dump a game's programs on the console with `EDEN_NX2_SHADER_DUMP=<dir>` and `EDEN_NX2_SHADER_DUMP_ALL=1`
(env.txt), with a fresh shader cache. `EDEN_NX2_IR_STATS=1` logs the optimized IR size, lane shuffles and
FMAs of every program with 16 or more shuffles (`NX2_IR_STATS` in eden_log.txt).
