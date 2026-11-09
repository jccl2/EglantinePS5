// SPDX-License-Identifier: GPL-3.0-or-later
// Task/mesh draw diagnostics (EDEN_MESH_DIAG in env.txt), for the GPU hangs Mario Kart World's
// task/mesh draws cause on the console (RADV emulates the task and mesh stages there).
//
// EDEN_MESH_DIAG=1:
// - every distinct task/mesh draw (its guest programs and workgroup count) is printed once, with
//   the pipeline's shader hashes: EDEN_MESH_DRAW lines in stderr.log;
// - the SPIR-V of every task/mesh pipeline's stages goes to logs/mesh/<hash>_<stage>.spv, with an
//   EDEN_MESH_SPV line each (what the driver compiled, to reproduce a hang elsewhere);
// - with EDEN_GPU_BREADCRUMBS=1, the dump names the programs of the task/mesh draws it lists.
// EDEN_MESH_DIAG=2: also submits and waits for the GPU after every task/mesh draw, so each one
//   ends its submission and nothing after it overlaps it on the GPU (slow).
//
// The guest program addresses are what EDEN_NX2_SKIP_VS takes (the task program, or the mesh
// program of a draw without one), to leave out one kind of draw instead of all of them.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <set>
#include <span>
#include <string>
#include <sys/stat.h>
#include <tuple>

namespace Eden::MeshDiag {

inline int Mode() {
    static const int mode = [] {
        const char* value = std::getenv("EDEN_MESH_DIAG");
        return value == nullptr ? 0 : std::atoi(value);
    }();
    return mode;
}

struct Draw {
    std::uint64_t task;  // 0 without a task program
    std::uint64_t mesh;
    std::uint64_t pixel;
    std::uint32_t groups;
};

// The GPU thread, at recording: the first time each draw shape is seen.
inline void Seen(const Draw& draw, const std::uint64_t* hashes) {
    static std::mutex mutex;
    static std::set<std::tuple<std::uint64_t, std::uint64_t, std::uint64_t, std::uint32_t>> seen;
    static std::uint64_t draws = 0;
    std::scoped_lock lock{mutex};
    ++draws;
    if (seen.size() >= 512 || !seen.emplace(draw.task, draw.mesh, draw.pixel, draw.groups).second) return;
    std::fprintf(stderr,
                 "EDEN_MESH_DRAW new=%zu draws=%llu task=0x%llx mesh=0x%llx ps=0x%llx groups=%u "
                 "hashes=%016llx/%016llx/%016llx/%016llx\n",
                 seen.size(), static_cast<unsigned long long>(draws),
                 static_cast<unsigned long long>(draw.task), static_cast<unsigned long long>(draw.mesh),
                 static_cast<unsigned long long>(draw.pixel), draw.groups,
                 static_cast<unsigned long long>(hashes[1]), static_cast<unsigned long long>(hashes[2]),
                 static_cast<unsigned long long>(hashes[3]), static_cast<unsigned long long>(hashes[5]));
    std::fflush(stderr);
}

// A task/mesh pipeline stage's SPIR-V, once per hash and stage.
inline void DumpSpirv(std::uint64_t hash, const char* stage, std::span<const std::uint32_t> code) {
    if (Mode() == 0) return;
    static std::mutex mutex;
    static std::set<std::pair<std::uint64_t, std::string>> dumped;
    std::scoped_lock lock{mutex};
    if (dumped.size() >= 256 || !dumped.emplace(hash, stage).second) return;
    const char* folder = std::getenv("EDEN_MESH_DUMP_DIR");
    bool saved = false;
    if (folder != nullptr) {
        (void)mkdir(folder, 0777);
        char path[512];
        std::snprintf(path, sizeof(path), "%s/%016llx_%s.spv", folder, static_cast<unsigned long long>(hash), stage);
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file.write(reinterpret_cast<const char*>(code.data()), static_cast<std::streamsize>(code.size_bytes()));
        saved = file.good();
    }
    std::fprintf(stderr, "EDEN_MESH_SPV hash=%016llx stage=%s bytes=%zu saved=%d\n",
                 static_cast<unsigned long long>(hash), stage, code.size_bytes(), saved ? 1 : 0);
}

} // namespace Eden::MeshDiag
