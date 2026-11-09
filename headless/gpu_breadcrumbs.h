// SPDX-License-Identifier: GPL-3.0-or-later
// GPU breadcrumbs: which scheduler command the GPU was running when it stopped.
//
// Off unless EDEN_GPU_BREADCRUMBS=1 (env.txt). Every command the Vulkan scheduler records gets an
// id, and before it the command buffer gets two VK_AMD_buffer_marker writes into a small
// host-visible buffer:
// - "reached" (TOP_OF_PIPE): the command processor got to this command;
// - "finished" (BOTTOM_OF_PIPE): everything recorded before this command has completed.
// Commands that also record into the upload command buffer get the same pair there (it runs
// before the main one in each submission).
//
// When the device is lost (headless/main.cpp, terminate handler) Dump() prints the two values
// and the recording sites around them: the first command that never finished is in
// [finished, reached]. A site is the lambda's file:line from __PRETTY_FUNCTION__ in
// Scheduler::Record (tools/prepare-vulkan-port.py inserts the calls).
//
// It changes the timing (a cache flush and two writes per command), so a hang that depends on
// overlap may move or go away: that is a finding too.
#pragma once
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "video_core/vulkan_common/vulkan_device.h"
#include "video_core/vulkan_common/vulkan_wrapper.h"

namespace Eden::GpuBreadcrumbs {

inline constexpr std::uint32_t kRing = 1u << 14; // sites kept, by id
inline constexpr std::size_t kBufferBytes = 256;
// Offsets in the marker buffer (one 64-byte line each, so the CPU flushes them separately).
inline constexpr VkDeviceSize kMainReached = 0;
inline constexpr VkDeviceSize kMainFinished = 64;
inline constexpr VkDeviceSize kUploadReached = 128;
inline constexpr VkDeviceSize kUploadFinished = 192;

inline bool Requested() {
    static const bool requested = [] {
        const char* value = std::getenv("EDEN_GPU_BREADCRUMBS");
        return value != nullptr && value[0] == '1';
    }();
    return requested;
}

inline bool extension_loaded = false; // set where the device picks its extensions
inline bool enabled = false;
inline PFN_vkCmdWriteBufferMarkerAMD write_marker = nullptr;
inline VkBuffer buffer = VK_NULL_HANDLE;
inline volatile std::uint32_t* mapped = nullptr;
inline std::atomic<std::uint32_t> next_id{1};
inline std::atomic<std::uint32_t> upload_id{0}; // last id that also marked the upload buffer
inline const char* sites[kRing]{};
inline std::uint8_t kinds[kRing]{}; // 1: main only, 2: main and upload

// After the logical device exists (Scheduler constructor). Leaks its buffer on purpose: it must
// outlive everything, including a device-lost teardown.
inline void Init(const Vulkan::Device& device) {
    if (!Requested() || enabled) return;
    const auto& dld = device.GetDispatchLoader();
    const VkDevice logical = *device.GetLogical();
    if (!extension_loaded) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS unavailable: VK_AMD_buffer_marker not enabled\n");
        return;
    }
    write_marker = reinterpret_cast<PFN_vkCmdWriteBufferMarkerAMD>(
        dld.vkGetDeviceProcAddr(logical, "vkCmdWriteBufferMarkerAMD"));
    if (write_marker == nullptr) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS unavailable: no vkCmdWriteBufferMarkerAMD\n");
        return;
    }
    const VkBufferCreateInfo ci{
        .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .pNext = nullptr,
        .flags = 0,
        .size = kBufferBytes,
        .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
        .queueFamilyIndexCount = 0,
        .pQueueFamilyIndices = nullptr,
    };
    if (dld.vkCreateBuffer(logical, &ci, nullptr, &buffer) != VK_SUCCESS) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS unavailable: buffer\n");
        buffer = VK_NULL_HANDLE;
        return;
    }
    const VkMemoryRequirements requirements = device.GetLogical().GetBufferMemoryRequirements(buffer);
    const auto properties = device.GetPhysical().GetMemoryProperties().memoryProperties;
    constexpr VkMemoryPropertyFlags wanted =
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    std::uint32_t type = UINT32_MAX;
    for (std::uint32_t i = 0; i < properties.memoryTypeCount; ++i) {
        if ((requirements.memoryTypeBits & (1u << i)) == 0) continue;
        const VkMemoryPropertyFlags flags = properties.memoryTypes[i].propertyFlags;
        if ((flags & wanted) != wanted) continue;
        // Prefer an uncached type: the GPU's write reaches memory without the CPU's cache.
        if (type == UINT32_MAX || (flags & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) == 0) type = i;
        if ((flags & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) == 0) break;
    }
    if (type == UINT32_MAX) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS unavailable: memory type\n");
        return;
    }
    const VkMemoryAllocateInfo ai{
        .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = nullptr,
        .allocationSize = requirements.size,
        .memoryTypeIndex = type,
    };
    VkDeviceMemory memory = VK_NULL_HANDLE;
    void* data = nullptr;
    if (dld.vkAllocateMemory(logical, &ai, nullptr, &memory) != VK_SUCCESS ||
        dld.vkBindBufferMemory(logical, buffer, memory, 0) != VK_SUCCESS ||
        dld.vkMapMemory(logical, memory, 0, VK_WHOLE_SIZE, 0, &data) != VK_SUCCESS) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS unavailable: memory\n");
        return;
    }
    std::memset(data, 0, kBufferBytes);
    mapped = static_cast<volatile std::uint32_t*>(data);
    enabled = true;
    std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS on: memory type %u flags 0x%x\n", type,
                 properties.memoryTypes[type].propertyFlags);
}

// Before a command runs on the worker thread: its id and the two markers.
inline void Mark(VkCommandBuffer cmdbuf, VkCommandBuffer upload, const char* site) {
    const std::uint32_t id = next_id.fetch_add(1, std::memory_order_relaxed);
    sites[id % kRing] = site;
    kinds[id % kRing] = upload != VK_NULL_HANDLE ? 2 : 1;
    write_marker(cmdbuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, buffer, kMainReached, id);
    write_marker(cmdbuf, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, buffer, kMainFinished, id);
    if (upload != VK_NULL_HANDLE) {
        write_marker(upload, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, buffer, kUploadReached, id);
        write_marker(upload, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, buffer, kUploadFinished, id);
        upload_id.store(id, std::memory_order_relaxed);
    }
}

// "void Vulkan::Scheduler::Record(T &&) [T = (lambda at .../vk_rasterizer.cpp:330:22)]" -> the
// part after the last "src/" or "build/" (the file and line).
inline const char* ShortSite(const char* site) {
    if (site == nullptr) return "(none)";
    const char* at = std::strstr(site, "lambda at ");
    const char* text = at != nullptr ? at + 10 : site;
    for (const char* prefix : {"/src/", "/headless/", "/build/"}) {
        const char* found = nullptr;
        for (const char* p = std::strstr(text, prefix); p != nullptr; p = std::strstr(p + 1, prefix)) found = p;
        if (found != nullptr) return found + 1;
    }
    return text;
}

inline std::uint32_t Read(VkDeviceSize offset) {
    volatile std::uint32_t* const word = mapped + offset / sizeof(std::uint32_t);
#if defined(__x86_64__)
    __builtin_ia32_clflush(const_cast<std::uint32_t*>(word));
    __builtin_ia32_mfence();
#endif
    return *word;
}

inline void PrintRange(const char* label, std::uint32_t from, std::uint32_t to) {
    for (std::uint32_t id = from; id != to + 1; ++id) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMB %s id=%u %s%s\n", label, id,
                     kinds[id % kRing] == 2 ? "[+upload] " : "", ShortSite(sites[id % kRing]));
    }
}

// From the terminate handler: no allocation, stderr only.
inline void Dump() {
    if (!enabled) return;
    const std::uint32_t recorded = next_id.load(std::memory_order_relaxed) - 1;
    const std::uint32_t reached = Read(kMainReached);
    const std::uint32_t finished = Read(kMainFinished);
    const std::uint32_t upload_reached = Read(kUploadReached);
    const std::uint32_t upload_finished = Read(kUploadFinished);
    std::fprintf(stderr,
                 "EDEN_GPU_BREADCRUMBS dump: recorded=%u reached=%u finished=%u upload_reached=%u "
                 "upload_finished=%u last_upload_id=%u\n",
                 recorded, reached, finished, upload_reached, upload_finished,
                 upload_id.load(std::memory_order_relaxed));
    if (finished == 0 || reached == 0) {
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMBS no marker reached memory\n");
        return;
    }
    // Context before the first command that never finished.
    const std::uint32_t before = finished > 8 ? finished - 8 : 1;
    PrintRange("done", before, finished - 1);
    // The unfinished commands the command processor reached (a stuck command is among them).
    const std::uint32_t last = reached >= finished ? reached : finished;
    if (last - finished <= 96) {
        PrintRange("open", finished, last);
    } else {
        PrintRange("open", finished, finished + 47);
        std::fprintf(stderr, "EDEN_GPU_BREADCRUMB ... %u more ...\n", last - finished - 96);
        PrintRange("open", last - 47, last);
    }
    // What was recorded but never reached.
    if (recorded > last) {
        const std::uint32_t until = recorded - last > 16 ? last + 16 : recorded;
        PrintRange("queued", last + 1, until);
    }
    std::fflush(stderr);
}

} // namespace Eden::GpuBreadcrumbs
