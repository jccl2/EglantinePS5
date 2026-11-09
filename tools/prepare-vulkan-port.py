"""Adapt the pinned Eden Vulkan platform boundary and required renderer fixes."""
from pathlib import Path
import struct
import subprocess
import sys
# --report: name every anchor that no longer matches (make update), instead of stopping at the first.
report = '--report' in sys.argv
source, output = map(Path, [a for a in sys.argv[1:] if a != '--report'])
port = Path(__file__).resolve().parents[1] / 'headless'
broken = []
def adapt(relative, destination, replacements):
    text = (source / relative).read_text()
    for old, new in replacements:
        if relative.endswith('/vk_rasterizer.cpp') and old.endswith('// update engine as channel may be different.') and old not in text:
            marker = '// Donkey Kong Bananza\'s terrain:'
            if marker in text:
                old = old.replace('// update engine as channel may be different.', marker)
                new = new.replace('// update engine as channel may be different.', marker)
        if relative.endswith('/vk_rasterizer.cpp') and old.startswith('    pipeline->SetEngine(maxwell3d, gpu_memory);') and old not in text:
            engine = '    pipeline->SetEngine(maxwell3d, gpu_memory);\n'
            configure = '    if (Profile::Scope scope{Profile::Zone::Configure}; !pipeline->Configure(is_indexed)) {'
            begin = text.index(engine) + len(engine)
            end = text.index(configure, begin)
            repack = text[begin:end]
            old = old.replace(engine, engine + repack)
            new = new.replace(configure, repack + configure)
        if relative.endswith('/vulkan_memory_allocator.cpp') and old.startswith('    vk::Check(vmaCreateBuffer(') and old not in text:
            if 'VkResult result = vmaCreateBuffer(' in text:
                # This core already retries outside the driver budget on allocation failure.
                if 'host_ci.flags &= ~VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT;' in text:
                    continue
        if text.count(old) != 1:
            if report:
                broken.append(f"{relative}: {old[:70]!r} (matches {text.count(old)})")
                continue
            raise RuntimeError(f"Pinned Vulkan anchor changed: {relative}: {old[:60]}")
        text = text.replace(old, new)
    path = output / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)

adapt('src/core/frontend/emu_window.h', 'include/core/frontend/emu_window.h',
      [('    Xcb,', '    Xcb,\n    PS5,')])
adapt('src/video_core/renderer_vulkan/vk_rasterizer.cpp', 'vulkan_rasterizer.cpp', [
    # Compute synchronization (dev_vulkan.h compute_barriers): indirect
    # dispatches returned before the pre-dispatch barrier, and no barrier made a dispatch's
    # writes visible to later indirect arguments, vertex/index fetch, shaders or copies.
    ('void RasterizerVulkan::DispatchCompute() {',
     """namespace {
constexpr VkMemoryBarrier COMPUTE_WRITE_BARRIER{
    .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
    .pNext = nullptr,
    .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
    .dstAccessMask = VK_ACCESS_INDIRECT_COMMAND_READ_BIT | VK_ACCESS_INDEX_READ_BIT |
                     VK_ACCESS_VERTEX_ATTRIBUTE_READ_BIT | VK_ACCESS_UNIFORM_READ_BIT |
                     VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT |
                     VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT,
};
constexpr VkMemoryBarrier COMPUTE_INPUT_BARRIER{
    .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
    .pNext = nullptr,
    .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT,
    .dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_INDIRECT_COMMAND_READ_BIT,
};
constexpr VkPipelineStageFlags COMPUTE_CONSUMER_STAGES =
    VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT | VK_PIPELINE_STAGE_VERTEX_INPUT_BIT |
    VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT |
    VK_PIPELINE_STAGE_TRANSFER_BIT;
} // namespace

void RasterizerVulkan::DispatchCompute() {"""),
    ('        scheduler.RequestOutsideRenderPassOperationContext();\n        scheduler.Record([pipeline, indirect_buffer = buffer->Handle(),',
     """        scheduler.RequestOutsideRenderPassOperationContext();
        if (::Eden::DevVulkan::compute_barriers) {
            scheduler.Record([](vk::CommandBuffer cmdbuf) {
                cmdbuf.PipelineBarrier(vk::PIPELINE_STAGE_GRAPHICS_COMPUTE_TRANSFER,
                                       VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT,
                                       0, COMPUTE_INPUT_BARRIER);
            });
        }
        scheduler.Record([pipeline, indirect_buffer = buffer->Handle(),"""),
    ('            cmdbuf.DispatchIndirect(indirect_buffer, indirect_offset);\n',
     """            cmdbuf.DispatchIndirect(indirect_buffer, indirect_offset);
            if (::Eden::DevVulkan::compute_barriers)
                cmdbuf.PipelineBarrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, COMPUTE_CONSUMER_STAGES, 0,
                                       COMPUTE_WRITE_BARRIER);
"""),
    ('        cmdbuf.Dispatch(dim[0], dim[1], dim[2]);\n',
     """        cmdbuf.Dispatch(dim[0], dim[1], dim[2]);
        if (::Eden::DevVulkan::compute_barriers)
            cmdbuf.PipelineBarrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, COMPUTE_CONSUMER_STAGES, 0,
                                   COMPUTE_WRITE_BARRIER);
"""),
    # CounterEnable below starts a query immediately. A deferred clear has no
    # active pass yet, so later realization would end that query in a different
    # render-pass scope (VUID-vkCmdEndQuery-None-07007). Keep the immediate path
    # when the guest enables occlusion counting.
    ('const bool can_defer_clear = ENABLE_DEFERRED_CLEAR &&',
     'const bool can_defer_clear = ENABLE_DEFERRED_CLEAR && !regs.zpass_pixel_count_enable &&'),
    ('#include <algorithm>', '#include <algorithm>\n#include "performance.h"'),
    # Guest threads take the cache locks here (tracked-page writes, flush-area reads).
    ('bool RasterizerVulkan::OnCPUWrite(DAddr addr, u64 size) {',
     'bool RasterizerVulkan::OnCPUWrite(DAddr addr, u64 size) {\n    ::Eden::Performance::Timer guest_write_timer(::Eden::Performance::guest_cpu_write);'),
    ('VideoCore::RasterizerDownloadArea RasterizerVulkan::GetFlushArea(DAddr addr, u64 size) {',
     'VideoCore::RasterizerDownloadArea RasterizerVulkan::GetFlushArea(DAddr addr, u64 size) {\n    ::Eden::Performance::Timer guest_read_timer(::Eden::Performance::guest_cpu_read);'),
    # ... and wait for those locks spinning briefly before sleeping (performance.h GuestCacheLock).
    ('        std::scoped_lock lock{texture_cache.mutex};\n        auto area = texture_cache.GetFlushArea(addr, size);',
     '        ::Eden::Performance::GuestCacheLock(texture_cache.mutex);\n'
     '        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};\n        auto area = texture_cache.GetFlushArea(addr, size);'),
    ('        std::scoped_lock lock{buffer_cache.mutex};\n        if (buffer_cache.OnCPUWrite(addr, size)) {',
     '        ::Eden::Performance::GuestCacheLock(buffer_cache.mutex);\n'
     '        std::lock_guard lock{buffer_cache.mutex, std::adopt_lock};\n        if (buffer_cache.OnCPUWrite(addr, size)) {'),
    ('        std::scoped_lock lock{texture_cache.mutex};\n        texture_cache.WriteMemory(addr, size);\n    }\n    pipeline_cache.InvalidateRegion(addr, size);',
     '        ::Eden::Performance::GuestCacheLock(texture_cache.mutex);\n'
     '        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};\n        texture_cache.WriteMemory(addr, size);\n    }\n    pipeline_cache.InvalidateRegion(addr, size);'),
    # The upstream code has an __ANDROID__ ifdef for DRAWS_TO_DISPATCH (512 vs 4096) and
    # CHECK_MASK (3 vs 7). Neither branch applies: replace the whole block. 512 draws per
    # command buffer submission: 4,096 exceeds the PS5 GPU watchdog timeout in shader-heavy
    # scenes (VK_ERROR_DEVICE_LOST in Xenoblade 2's cities). CHECK_MASK is the configurable
    # dispatch interval (dev-settings dispatch_draws=N); default 64 draws between hand-offs.
    ('#ifdef __ANDROID__\n    static constexpr u32 DRAWS_TO_DISPATCH = 512;\n    static constexpr u32 CHECK_MASK = 3;\n#else\n    // Every 32 draws (was 8): each dispatch woke the worker thread, and Switch 2 titles make\n    // ~55,000 draws a second. A full chunk is dispatched on its own (Scheduler::Record).\n    static constexpr u32 DRAWS_TO_DISPATCH = 4096;\n    static constexpr u32 CHECK_MASK = 31;\n#endif // __ANDROID__\n\n    static_assert(DRAWS_TO_DISPATCH % (CHECK_MASK + 1) == 0);\n',
     '    static constexpr u32 DRAWS_TO_DISPATCH = 512;\n    const u32 CHECK_MASK = ::Eden::Performance::dispatch_mask.load(std::memory_order_relaxed);\n\n'),
    # Where the ~4 us a draw spends goes: six phases under one running clock, each Mark taking the
    # gap since the previous one so they do not overlap. The Timer still wraps the whole function,
    # so a draw that returns early is charged for its unmarked tail and the sum of the six falls
    # short by that amount plus two clock reads per phase. control/scheduler.cpp aside, this is the
    # only place the dispatch's time can be spent - so its six numbers should add up to it.
    ('''    namespace Profile = VideoCommon::Nx2Profile;
    {
        Profile::Scope scope{Profile::Zone::FlushWork};
        FlushWork();
        gpu_memory->FlushCaching();
    }
''',
     '''    namespace Profile = VideoCommon::Nx2Profile;
    const ::Eden::Performance::Timer rasterizer_draw_timer(::Eden::Performance::rasterizer_draw);
    auto phase = ::Eden::Performance::MarkStart();
    {
        Profile::Scope scope{Profile::Zone::FlushWork};
        FlushWork();
        gpu_memory->FlushCaching();
    }
    ::Eden::Performance::Mark(::Eden::Performance::draw_flush, phase);
'''),
    ('''        return pipeline_cache.CurrentGraphicsPipeline();
    }()};
''',
     '''        return pipeline_cache.CurrentGraphicsPipeline();
    }()};
    ::Eden::Performance::Mark(::Eden::Performance::draw_pipeline, phase);
'''),
    ('''    pipeline->SetEngine(maxwell3d, gpu_memory);
    if (Profile::Scope scope{Profile::Zone::Configure}; !pipeline->Configure(is_indexed)) {
        report_failure("configure failed");
        return;
    }
''',
     '''    pipeline->SetEngine(maxwell3d, gpu_memory);
    // Waiting for both cache mutexes. SetEngine between them and here is two pointer assignments,
    // and the hold itself carries on through what follows: the phases below measure it.
    ::Eden::Performance::Mark(::Eden::Performance::draw_lock_wait, phase);
    if (Profile::Scope scope{Profile::Zone::Configure}; !pipeline->Configure(is_indexed)) {
        report_failure("configure failed");
        return;
    }
    ::Eden::Performance::Mark(::Eden::Performance::draw_config, phase);
'''),
    ('''    query_cache.CounterEnable(VideoCommon::QueryType::ZPassPixelCount64, maxwell3d->regs.zpass_pixel_count_enable);
    {
        Profile::Scope scope{Profile::Zone::DrawRecord};
        draw_func();
    }
''',
     '''    query_cache.CounterEnable(VideoCommon::QueryType::ZPassPixelCount64, maxwell3d->regs.zpass_pixel_count_enable);
    ::Eden::Performance::Mark(::Eden::Performance::draw_state, phase);
    {
        Profile::Scope scope{Profile::Zone::DrawRecord};
        draw_func();
    }
    ::Eden::Performance::Mark(::Eden::Performance::draw_record, phase);
'''),
    # A forced release blocks the GPU thread until the fence thread has retired
    # every older fence. Time only that producer-side drain.
    ('    fence_manager.WaitPendingFences(force);',
     '''    if (force) {
        ::Eden::Performance::Timer drain_timer(::Eden::Performance::gpu_fence_drain);
        fence_manager.WaitPendingFences(force);
    } else {
        fence_manager.WaitPendingFences(force);
    }'''),
    # UpdateDynamicStates asked the cache for the current pipeline a second time
    # per draw, recomputing the same fixed-state key. Reuse PrepareDraw's result;
    # other callers keep the original lookup.
    ('template <typename Func>\nvoid RasterizerVulkan::PrepareDraw(bool is_indexed, Func&& draw_func) {',
     '''namespace {
// GPU thread only: the pipeline PrepareDraw refreshed for the draw in progress.
GraphicsPipeline* prepared_pipeline = nullptr;
} // namespace

template <typename Func>
void RasterizerVulkan::PrepareDraw(bool is_indexed, Func&& draw_func) {'''),
    ('    std::scoped_lock lock{buffer_cache.mutex, texture_cache.mutex};\n    buffer_cache_runtime.BeginReadbackWriteBatch();\n    SCOPE_EXIT { buffer_cache_runtime.AbortReadbackWriteBatch(); };\n    // update engine as channel may be different.',
     '''    std::scoped_lock lock{buffer_cache.mutex, texture_cache.mutex};
    buffer_cache_runtime.BeginReadbackWriteBatch();
    SCOPE_EXIT { buffer_cache_runtime.AbortReadbackWriteBatch(); };
    prepared_pipeline = pipeline;
    SCOPE_EXIT {
        prepared_pipeline = nullptr;
    };
    // update engine as channel may be different.'''),
    ('        if (auto* gp = pipeline_cache.CurrentGraphicsPipeline(); gp && gp->HasDynamicVertexInput()) {',
     '        if (auto* gp = prepared_pipeline ? prepared_pipeline : pipeline_cache.CurrentGraphicsPipeline();\n            gp && gp->HasDynamicVertexInput()) {'),
    # Development GPU fault probe (dev_vulkan.h, pipeline_trace=on): once the runner
    # creates /app0/sync-draws.txt, report every draw/dispatch to klog and wait for the
    # GPU, so the last report before a fatal GPU fault names the operation.
    ('#include "performance.h"', '#include "performance.h"\n#include "dev_vulkan.h"\n#include "diagnostics.h"\n#include <filesystem>'),
    ('GraphicsPipeline* prepared_pipeline = nullptr;',
     'GraphicsPipeline* prepared_pipeline = nullptr;\n'
     '// The pipeline of the last prepared draw, for the development fault probe.\n'
     'GraphicsPipeline* probe_pipeline = nullptr;\n'
     'bool SyncProbeActive() {\n'
     '    static unsigned poll = 0;\n'
     '    static bool active = false;\n'
     '    if (!active && ::Eden::DevVulkan::trace_pipelines && (++poll & 255) == 0)\n'
     '        active = std::filesystem::exists("/app0/sync-draws.txt");\n'
     '    return active;\n'
     '}\n'
     'void SyncProbe(Scheduler& scheduler, const char* kind, const void* pipeline) {\n'
     '    if (!SyncProbeActive()) return;\n'
     '    static unsigned long long sequence = 0;\n'
     '    char text[96];\n'
     '    std::snprintf(text, sizeof(text), "%s #%llu p=%p", kind, ++sequence, pipeline);\n'
     '    ::Eden::Report("sync", text);\n'
     '    scheduler.Finish();\n'
     '}'),
    ('    prepared_pipeline = pipeline;\n', '    prepared_pipeline = pipeline;\n    probe_pipeline = pipeline;\n'),
    ('        }\n    });\n}\n\nvoid RasterizerVulkan::DrawIndirect() {',
     '        }\n    });\n    SyncProbe(scheduler, "draw", probe_pipeline);\n}\n\nvoid RasterizerVulkan::DrawIndirect() {'),
    ('void RasterizerVulkan::DrawTexture() {', 'void RasterizerVulkan::DrawTexture() {\n    SCOPE_EXIT { SyncProbe(scheduler, "draw_texture", nullptr); };'),
    ('void RasterizerVulkan::Clear(u32 layer_count) {', 'void RasterizerVulkan::Clear(u32 layer_count) {\n    SCOPE_EXIT { SyncProbe(scheduler, "clear", nullptr); };'),
    ('    ComputePipeline* const pipeline{pipeline_cache.CurrentComputePipeline()};\n    if (!pipeline) {\n        if (kepler_compute->IsNativeMode()) throw Shader::RuntimeError("Native compute pipeline unavailable");\n        return;\n    }',
     '    ComputePipeline* const pipeline{pipeline_cache.CurrentComputePipeline()};\n    if (!pipeline) {\n        if (kepler_compute->IsNativeMode()) throw Shader::RuntimeError("Native compute pipeline unavailable");\n        return;\n    }\n'
     '    SCOPE_EXIT { SyncProbe(scheduler, "dispatch", pipeline); };'),
    # The pages any texture cache image was ever registered in (headless/image_pages.h).
    ('#include "performance.h"\n#include "dev_vulkan.h"', '#include "performance.h"\n#include "image_pages.h"\n#include "dev_vulkan.h"'),
    # A read of memory no image was ever registered in: the texture cache would find none and
    # change nothing, so its lock (held by the GPU thread through every draw) is not taken
    # (headless/image_pages.h). Only the texture cache's part: the buffer cache's read note and
    # flush area before and after it stay as they were. With cpu_read_skip (performance.h, off by
    # default) a read whose texture cache lock is held answers "no download" instead of waiting.
    ('''    {
        ::Eden::Performance::GuestCacheLock(texture_cache.mutex);
        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};
        auto area = texture_cache.GetFlushArea(addr, size);''',
     '''    if (!::Eden::ImagePages::NeverHeld(addr, size)) {
        if (::Eden::Performance::cpu_read_skip.load(std::memory_order_relaxed)) {
            if (!texture_cache.mutex.try_lock()) {
                ::Eden::Performance::cpu_read_skipped.fetch_add(1, std::memory_order_relaxed);
                // Empty: the read does not remember it, so the next read checks again.
                return VideoCore::RasterizerDownloadArea{.start_address = addr, .end_address = addr, .preemtive = true};
            }
        } else {
            ::Eden::Performance::GuestCacheLock(texture_cache.mutex);
        }
        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};
        auto area = texture_cache.GetFlushArea(addr, size);'''),
    # Guest writes while the GPU thread holds a cache lock are queued instead of waiting
    # (performance.h cpu_write_defer); the GPU thread runs them before its next draw, clear,
    # dispatch or engine copy (MemoryManager::FlushCaching, headless/CMakeLists.txt). A write to
    # the page this core's last queued write covers joins it. Under the buffer cache lock the
    # buffer pages after the write are marked CPU-modified as their own first writes would
    # (performance.h cpu_write_ahead), up to a page no buffer covers or one the GPU modified.
    ('''    {
        ::Eden::Performance::GuestCacheLock(buffer_cache.mutex);
        std::lock_guard lock{buffer_cache.mutex, std::adopt_lock};
        if (buffer_cache.OnCPUWrite(addr, size)) {
            return true;
        }
    }
    {
        ::Eden::Performance::GuestCacheLock(texture_cache.mutex);
        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};
        texture_cache.WriteMemory(addr, size);
    }
    pipeline_cache.InvalidateRegion(addr, size);
    return false;
''',
     '''    if (::Eden::Performance::cpu_write_defer.load(std::memory_order_relaxed) &&
        !::Eden::Performance::deferred_replay &&
        ::Eden::Performance::ExtendDeferredWrite(addr, size, ::Eden::Performance::cpu_write_core)) {
        return false;
    }
    {
        if (!::Eden::Performance::GuestCacheLockOrDefer(buffer_cache.mutex)) {
            ::Eden::Performance::DeferWrite(addr, size, ::Eden::Performance::cpu_write_core);
            return false;
        }
        std::lock_guard lock{buffer_cache.mutex, std::adopt_lock};
        if (buffer_cache.OnCPUWrite(addr, size)) {
            return true;
        }
        const unsigned ahead = ::Eden::Performance::cpu_write_ahead.load(std::memory_order_relaxed);
        DAddr page = Common::AlignDown(addr + size - 1, Core::DEVICE_PAGESIZE) + Core::DEVICE_PAGESIZE;
        for (unsigned marked = 0; marked < ahead; ++marked, page += Core::DEVICE_PAGESIZE) {
            if (!buffer_cache.IsRegionRegistered(page, Core::DEVICE_PAGESIZE) ||
                buffer_cache.OnCPUWrite(page, Core::DEVICE_PAGESIZE)) {
                break;
            }
        }
    }
    {
        if (!::Eden::Performance::GuestCacheLockOrDefer(texture_cache.mutex)) {
            ::Eden::Performance::DeferWrite(addr, size, ::Eden::Performance::cpu_write_core);
            return false;
        }
        std::lock_guard lock{texture_cache.mutex, std::adopt_lock};
        texture_cache.WriteMemory(addr, size);
    }
    pipeline_cache.InvalidateRegion(addr, size);
    return false;
'''),
    # The GPU thread, before it takes both cache locks for a draw: a guest thread asleep on one
    # goes first (performance.h YieldToGuests).
    ('    std::scoped_lock lock{buffer_cache.mutex, texture_cache.mutex};\n    buffer_cache_runtime.BeginReadbackWriteBatch();\n    SCOPE_EXIT { buffer_cache_runtime.AbortReadbackWriteBatch(); };\n    prepared_pipeline = pipeline;\n',
     '    ::Eden::Performance::YieldToGuests();  // a guest thread asleep on a cache lock goes first\n'
     '    std::scoped_lock lock{buffer_cache.mutex, texture_cache.mutex};\n    buffer_cache_runtime.BeginReadbackWriteBatch();\n    SCOPE_EXIT { buffer_cache_runtime.AbortReadbackWriteBatch(); };\n    prepared_pipeline = pipeline;\n'),
    # FlushCommands submits only when nothing has been submitted since the last draw, clear or
    # dispatch was recorded (FlushWork notes the scheduler's tick at each). It asked only whether
    # a draw was counted since the counter was last cleared, and a submission by anything else (a
    # fence's own Flush, a download) does not clear it: the second submission held nothing, a
    # quarter of a millisecond of the worker thread in the driver each (a large open-world game
    # made seven of them a frame on ProsperoEden).
    ('void RasterizerVulkan::FlushCommands() {\n    if (draw_counter == 0) {\n        return;\n    }\n    draw_counter = 0;\n    scheduler.Flush();\n}',
     'namespace {\n// The scheduler\'s tick when the last draw, clear or dispatch was recorded (GPU thread).\nu64 recorded_tick = 0;\n} // namespace\n\n'
     'void RasterizerVulkan::FlushCommands() {\n    if (draw_counter == 0) {\n        return;\n    }\n    draw_counter = 0;\n'
     '    if (scheduler.CurrentTick() != recorded_tick) {\n        return; // a submission has taken every draw already\n    }\n    scheduler.Flush();\n}'),
    ('    const u32 CHECK_MASK = ::Eden::Performance::dispatch_mask.load(std::memory_order_relaxed);\n',
     '    const u32 CHECK_MASK = ::Eden::Performance::dispatch_mask.load(std::memory_order_relaxed);\n    recorded_tick = scheduler.CurrentTick();\n'),
    # Each draw counts toward the frame's busyness (headless/astc_policy.h: a CPU-converted ASTC
    # texture decodes in the background only in a busy frame). Its own anchor, apart from the
    # cache-lock hunk above, which other ports change.
    ('#include "video_core/texture_cache/texture_cache_base.h"',
     '#include "video_core/texture_cache/texture_cache_base.h"\n#include "astc_policy.h"'),
    ('    VideoCommon::LoadingProbe::gpu.Mark("draw prepare");\n    mesh_draw = pipeline->IsMeshPipeline();',
     '    VideoCommon::LoadingProbe::gpu.Mark("draw prepare");\n    ::Eden::AstcPolicy::Draw();\n'
     '    mesh_draw = pipeline->IsMeshPipeline();'),
    # Task/mesh draw diagnostics (mesh_diag.h, EDEN_MESH_DIAG): the draw's guest programs, once
    # per shape in stderr.log and in the GPU breadcrumbs; mode 2 isolates each draw on the GPU.
    ('#include "dev_vulkan.h"', '#include "dev_vulkan.h"\n#include "gpu_breadcrumbs.h"\n#include "mesh_diag.h"'),
    ('        if (mesh_draw) {\n            static u32 diagnostic_mesh_draws{};',
     '        if (mesh_draw && ::Eden::MeshDiag::Mode() != 0) {\n'
     '            const auto& mesh_regs = maxwell3d->regs;\n'
     '            const auto program = [&](size_t slot) {\n'
     '                return (u64{mesh_regs.pipelines[slot].reserved[0] & 0xff} << 32) | mesh_regs.pipelines[slot].reserved[1];\n'
     '            };\n'
     '            // The task program in the vertex slot and its mesh program in the tessellation slot;\n'
     '            // without a task program, the mesh program in the vertex slot (shader_environment.cpp).\n'
     '            const bool has_task = mesh_regs.IsShaderConfigEnabled(Maxwell::ShaderType::Tessellation);\n'
     '            const ::Eden::MeshDiag::Draw shape{has_task ? program(1) : 0, has_task ? program(3) : program(1),\n'
     '                                               program(5), draw_params.num_vertices};\n'
     '            static constexpr std::array<u64, 6> no_hashes{};\n'
     '            ::Eden::MeshDiag::Seen(shape, (prepared_pipeline ? prepared_pipeline->UniqueHashes() : no_hashes).data());\n'
     '            const bool isolate = ::Eden::MeshDiag::Mode() >= 2;\n'
     '            scheduler.Record([shape](vk::CommandBuffer cmdbuf) {\n'
     '                ::Eden::GpuBreadcrumbs::Note(shape.task, shape.mesh, shape.pixel | (u64{shape.groups} << 40));\n'
     '                cmdbuf.DrawMeshTasksEXT(shape.groups, 1, 1);\n'
     '            });\n'
     '            buffer_cache_runtime.CompleteReadbackWriteBatch();\n'
     '            // After the draw: its render pass ends with the submission (not before it, which\n'
     '            // would end the render pass PrepareDraw began).\n'
     '            if (isolate) scheduler.Finish();\n'
     '            return;\n'
     '        }\n'
     '        if (mesh_draw) {\n            static u32 diagnostic_mesh_draws{};'),
])
# Host conditional rendering (VK_EXT_conditional_rendering, dev_vulkan.h hcr_mode): count each
# decision (EDEN_DEV_GUEST cond= slots 13-19); hcr=exact sends the conditions Eden would draw
# unconditionally, or predicate from a pending host query, to the exact CPU evaluation;
# hcr=cpu evaluates every condition on the CPU and predicates from constant 0/1 buffers.
adapt('src/video_core/renderer_vulkan/vk_query_cache.cpp', 'vulkan_query_cache.cpp', [
    ('#include "video_core/vulkan_common/vulkan_wrapper.h"\n',
     '#include "video_core/vulkan_common/vulkan_wrapper.h"\n#include "dev_vulkan.h"\n#include "performance.h"\n'),
    ('    vk::Buffer hcr_resolve_buffer;\n',
     '    vk::Buffer hcr_resolve_buffer;\n    vk::Buffer hcr_constant_buffer; // hcr=cpu: predicates 0 and 1\n'),
    ('        hcr_resolve_buffer = memory_allocator.CreateBuffer(buffer_ci, MemoryUsage::DeviceLocal);\n    }\n',
     '''        hcr_resolve_buffer = memory_allocator.CreateBuffer(buffer_ci, MemoryUsage::DeviceLocal);
        if (device.IsExtConditionalRendering() && ::Eden::DevVulkan::hcr_mode == 2) {
            VkBufferCreateInfo constant_ci = buffer_ci;
            constant_ci.size = 2 * sizeof(u32);
            constant_ci.usage = VK_BUFFER_USAGE_CONDITIONAL_RENDERING_BIT_EXT;
            hcr_constant_buffer = memory_allocator.CreateBuffer(constant_ci, MemoryUsage::Upload);
            const u32 predicates[2]{0, 1};
            std::memcpy(hcr_constant_buffer.Mapped().data(), predicates, sizeof(predicates));
            hcr_constant_buffer.Flush();
        }
    }
'''),
    ('''bool QueryCacheRuntime::HostConditionalRenderingCompareValue(VideoCommon::LookupData object_1,
                                                             [[maybe_unused]] bool qc_dirty) {
    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }
    HostConditionalRenderingCompareBCImpl(object_1.address, true, true);
    return true;
}''',
     '''namespace {
// hcr=cpu: Maxwell3D::ProcessQueryCondition's exact evaluation (the read flushes pending GPU
// results), then the driver predicates the draws from a constant buffer. kind: 0 Conditional,
// 1 IfEqual, 2 IfNotEqual.
bool HostConditionalRenderingOnCpu(QueryCacheRuntime& runtime, QueryCacheRuntimeImpl& impl, DAddr address,
                                   unsigned kind) {
    if (address == 0 || !impl.hcr_constant_buffer) {
        ::Eden::Performance::CountCondition(13);
        runtime.EndHostConditionalRendering();
        return false;
    }
    u32 compare[6]{};
    impl.device_memory.ReadBlock(address, compare, sizeof(compare));
    const bool equal = compare[0] == compare[4] && compare[1] == compare[5];
    const bool execute = kind == 0 ? compare[0] != 0 && compare[1] != 0 : (kind == 1 ? equal : !equal);
    ::Eden::Performance::CountCondition(execute ? 17 : 18);
    const bool was_running = impl.is_hcr_running;
    if (was_running) {
        runtime.PauseHostConditionalRendering();
    }
    impl.hcr_setup.buffer = *impl.hcr_constant_buffer;
    impl.hcr_setup.offset = execute ? sizeof(u32) : 0;
    impl.hcr_setup.flags = 0;
    impl.hcr_is_set = true;
    impl.is_hcr_running = false;
    if (was_running) {
        runtime.ResumeHostConditionalRendering();
    }
    return true;
}
} // namespace

bool QueryCacheRuntime::HostConditionalRenderingCompareValue(VideoCommon::LookupData object_1,
                                                             [[maybe_unused]] bool qc_dirty) {
    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }
    if (::Eden::DevVulkan::hcr_mode == 2) {
        return HostConditionalRenderingOnCpu(*this, *impl, object_1.address, 0);
    }
    if (::Eden::DevVulkan::hcr_mode == 1 && qc_dirty) {
        ::Eden::Performance::CountCondition(19);
        EndHostConditionalRendering();
        return false;
    }
    ::Eden::Performance::CountCondition(16);
    HostConditionalRenderingCompareBCImpl(object_1.address, true, true);
    return true;
}'''),
    ('''    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }

    const auto check_in_bc = [&](DAddr address) {''',
     '''    if (!impl->device.IsExtConditionalRendering()) {
        return false;
    }
    if (::Eden::DevVulkan::hcr_mode == 2) {
        return HostConditionalRenderingOnCpu(*this, *impl, object_1.address, equal_check ? 1 : 2);
    }

    const auto check_in_bc = [&](DAddr address) {'''),
    ('''    if (!is_in_ac[0] && !is_in_ac[1]) {
        EndHostConditionalRendering();
        return false;
    }

    if (!qc_dirty && !is_in_bc[0] && !is_in_bc[1]) {
        EndHostConditionalRendering();
        return false;
    }
''',
     '''    if (!is_in_ac[0] && !is_in_ac[1]) {
        ::Eden::Performance::CountCondition(13);
        EndHostConditionalRendering();
        return false;
    }

    if (!qc_dirty && !is_in_bc[0] && !is_in_bc[1]) {
        ::Eden::Performance::CountCondition(13);
        EndHostConditionalRendering();
        return false;
    }
    // hcr=exact: a pending host query is not in any buffer yet; the CPU evaluation waits for it.
    if (::Eden::DevVulkan::hcr_mode == 1 && qc_dirty) {
        ::Eden::Performance::CountCondition(19);
        EndHostConditionalRendering();
        return false;
    }
'''),
    ('''            HostConditionalRenderingCompareValueImpl(*objects[j], equal_check);
            return true;''',
     '''            ::Eden::Performance::CountCondition(15);
            HostConditionalRenderingCompareValueImpl(*objects[j], equal_check);
            return true;'''),
    ('''    if (!is_gpu_high) {
        EndHostConditionalRendering();
        return true;
    }

    if (!is_in_bc[0] && !is_in_bc[1]) {
        EndHostConditionalRendering();
        return true;
    }
    HostConditionalRenderingCompareBCImpl(object_1.address, equal_check);''',
     '''    // Eden draws these unconditionally; hcr=exact evaluates them on the CPU instead.
    if (!is_gpu_high || (!is_in_bc[0] && !is_in_bc[1])) {
        ::Eden::Performance::CountCondition(14);
        EndHostConditionalRendering();
        return ::Eden::DevVulkan::hcr_mode != 1;
    }
    ::Eden::Performance::CountCondition(16);
    HostConditionalRenderingCompareBCImpl(object_1.address, equal_check);'''),
])
adapt('src/video_core/renderer_vulkan/vk_swapchain.cpp', 'vulkan_swapchain.cpp', [
    # An acquisition error has no usable image index. Forward it to the existing
    # renderer recovery path instead of indexing or presenting an invalid image.
    ('LOG_ERROR(Render_Vulkan, "vkAcquireNextImageKHR returned {}", string_VkResult(result));',
     'LOG_ERROR(Render_Vulkan, "vkAcquireNextImageKHR returned {}", string_VkResult(result));\n        vk::Check(result);'),
    ('LOG_CRITICAL(Render_Vulkan, "Failed to present with error {}", string_VkResult(result));',
     'LOG_CRITICAL(Render_Vulkan, "Failed to present with error {}", string_VkResult(result));\n        vk::Check(result);'),
])
# Keep the overlay in the existing display composition pass (also used for
# frontend screenshots), excluding guest applet captures. Cache its pipeline.
# Preserve upstream ABI, including consumers whose quoted includes resolve to
# the original header. Display/capture purpose belongs to the draw call.
adapt('src/video_core/present.h', 'include/video_core/present.h', [])
adapt('src/video_core/renderer_vulkan/vk_present_manager.h',
      'include/video_core/renderer_vulkan/vk_present_manager.h', [
    ('#include <mutex>', '#include <mutex>\n#include <exception>'),
    ('    std::jthread present_thread;',
     '    std::exception_ptr present_failure;\n    std::jthread present_thread;'),
])
adapt('src/video_core/renderer_vulkan/vk_present_manager.cpp', 'vulkan_present_manager.cpp', [
    ('#include "video_core/renderer_vulkan/vk_present_manager.h"',
     '#include "video_core/renderer_vulkan/vk_present_manager.h"\n#include "gpu_failure.h"\n#include "performance.h"'),
    # GetRenderFrame runs on the GPU thread: time its free-frame and
    # present-completion waits (FIFO back-pressure on the producer).
    ('    // Wait for free presentation frames\n    std::unique_lock lock{free_mutex};',
     '    // Wait for free presentation frames\n    ::Eden::Performance::Timer present_wait_timer(::Eden::Performance::gpu_present_wait);\n    std::unique_lock lock{free_mutex};'),
    ('PresentThread(token);', '''try {
            PresentThread(token);
        } catch (...) {
            const auto error = std::current_exception();
            {
                std::scoped_lock lock{queue_mutex, free_mutex};
                present_failure = error;
                present_queue.clear();
            }
            free_cv.notify_all();
            frame_cv.notify_all();
            Eden::RecordGpuFailure(error);
        }'''),
    ('free_cv.wait(lock, [this] { return !free_queue.empty(); });',
     '''free_cv.wait(lock, [this] { return present_failure || !free_queue.empty(); });
    if (present_failure) std::rethrow_exception(present_failure);'''),
    ('            present_queue.push_back(frame);',
     '            if (present_failure) return;\n            present_queue.push_back(frame);'),
    ('frame_cv.wait(queue_lock, [this] { return present_queue.empty(); });',
     'frame_cv.wait(queue_lock, [this] { return present_failure || present_queue.empty(); });'),
])
# Descriptor buffers: Eden's null buffer (nullDescriptor devices) has device address 0,
# but WriteDescriptorBuffer passed it with the binding's range. RADV then builds a real
# descriptor at GPU address 0; desktop kernels only log the resulting page fault, the PS5
# kills the title (TCP read at VA 0).
# The spec requires a null pAddressInfo (or VK_WHOLE_SIZE) for a null descriptor.
# The allocator's budget is the driver's, and on the console it does not say what is available: the
# driver counts each allocation twice (as VRAM and as visible VRAM), so the budget of the 8 GiB heap
# is used up at about 4 GiB, and it knows nothing of the pool the CPU shares. Eden asks for images
# and buffers within the budget and takes a refusal as final. A refused image or buffer is asked for
# again outside the budget; the driver refuses when the pool has no room, which is still the end of
# the session, and stderr.log then says what the pool had.
ALLOCATOR_REFUSED = """    VkResult created = CALL;
    if (created != VK_SUCCESS) {
        VmaAllocationCreateInfo outside_budget = alloc_ci;
        outside_budget.flags &= ~static_cast<VmaAllocationCreateFlags>(VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT);
        created = AGAIN;
        static std::atomic<unsigned long long> refused{0};
        const auto times = refused.fetch_add(1, std::memory_order_relaxed) + 1;
        if ((times & (times - 1)) == 0) { // the 1st, 2nd, 4th, 8th ... time
            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY budget refused %llu requests; asked again outside it: result %d, DETAIL\\n",
                         times, int(created), VALUES);
        }
        if (created != VK_SUCCESS) {
            VmaBudget held[VK_MAX_MEMORY_HEAPS]{};
            vmaGetHeapBudgets(allocator, held);
            unsigned long long blocks = 0, used = 0;
            for (const VmaBudget& heap : held) {
                blocks += heap.statistics.blockBytes;
                used += heap.statistics.allocationBytes;
            }
            const auto free_block = ::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed);
            std::size_t guest_span = 0, guest_held = 0;
            ::Common::GuestMemoryUsage(&guest_span, &guest_held);
            std::fprintf(stderr, "EDEN_GRAPHICS_MEMORY refused outside the budget too (result %d): the pool's largest free "
                         "block is %llu MiB; the allocator holds %llu MiB in blocks, %llu MiB of it in use; the guest's "
                         "memory holds %llu MiB, the heap %llu MiB\\n",
                         int(created), free_block ? free_block() >> 20 : 0ull, blocks >> 20, used >> 20,
                         static_cast<unsigned long long>(guest_held >> 20),
                         static_cast<unsigned long long>(eden_heap_committed() >> 20));
            ::Eden::Performance::ReportHeap("a buffer was refused");
        }
    }
    vk::Check(created);
"""
def refused(call, detail, values):
    return (ALLOCATOR_REFUSED.replace('CALL', call).replace('AGAIN', call.replace('&alloc_ci', '&outside_budget'))
            .replace('DETAIL', detail).replace('VALUES', values))
IMAGE_VALUES = 'ci.extent.width, ci.extent.height, ci.extent.depth, ci.arrayLayers, ci.mipLevels, unsigned(ci.samples), int(ci.format)'
BUFFER_VALUES = 'static_cast<unsigned long long>(ci.size), unsigned(ci.usage)'
adapt('src/video_core/vulkan_common/vulkan_memory_allocator.h',
      'include/video_core/vulkan_common/vulkan_memory_allocator.h', [
    ('#pragma once', '#pragma once\n#include <array>\n#include <mutex>'),
    ('        vk::Image CreateImage(const VkImageCreateInfo &ci, bool dedicated = false) const;',
     '        vk::Image CreateImage(const VkImageCreateInfo &ci, bool dedicated = false) const;\n'
     '        vk::Image CreateTemporaryImage(const VkImageCreateInfo &ci) const;'),
    ('        u32 valid_memory_types{~0u};',
     '        u32 valid_memory_types{~0u};\n'
     '        mutable std::mutex temporary_pool_mutex;\n'
     '        mutable std::array<VmaPool, VK_MAX_MEMORY_TYPES> temporary_pools{};'),
])
adapt('src/video_core/vulkan_common/vulkan_memory_allocator.cpp', 'vulkan_memory_allocator.cpp', [
    ('            vmaCalculateStatistics(allocator, &statistics);',
     '            vmaCalculateStatistics(allocator, &statistics);\n'
     '            if (probe_rows == 160 || probe_rows == 256 || probe_rows == 512 || result != VK_SUCCESS)\n'
     '                EdenVmaSnapshot(allocator, probe_rows);'),
    ('vk::Image MemoryAllocator::CreateUnbackedImage(const VkImageCreateInfo &ci) const {',
     (port / 'transient_images.inc').read_text() + '\nvk::Image MemoryAllocator::CreateUnbackedImage(const VkImageCreateInfo &ci) const {'),
    ('MemoryAllocator::MemoryAllocator(const Device &device_)',
     r'''namespace {
std::mutex vma_probe_mutex;
VmaAllocator vma_probe_allocator{};
void EdenVmaSnapshot(VmaAllocator allocator, unsigned tag) {
    char* json = nullptr;
    vmaBuildStatsString(allocator, &json, VK_TRUE);
    if (!json) return;
    char path[192];
    std::snprintf(path, sizeof(path), "/data/eglantineps5/logs/mkw-vma-%d-%u.json", getpid(), tag);
    const auto bytes = std::strlen(json);
    bool written = false;
    if (bytes <= (8U << 20)) {
        if (FILE* file = std::fopen(path, "w")) {
            written = std::fwrite(json, 1, bytes, file) == bytes;
            std::fclose(file);
        }
    }
    std::fprintf(stderr, "MKW_VMA_MAP tag=%u bytes=%zu written=%u\n", tag, bytes, unsigned(written));
    vmaFreeStatsString(allocator, json);
}
}
extern "C" void eden_vma_probe(unsigned seconds) {
    std::lock_guard lock{vma_probe_mutex};
    if (vma_probe_allocator) EdenVmaSnapshot(vma_probe_allocator, 100000 + seconds);
}
MemoryAllocator::MemoryAllocator(const Device &device_)'''),
    ('    // Preserve the previous "RenderDoc small heap"',
     '    { std::lock_guard lock{vma_probe_mutex}; vma_probe_allocator = allocator; }\n'
     '    // Preserve the previous "RenderDoc small heap"'),
    ('MemoryAllocator::~MemoryAllocator() = default;',
     'MemoryAllocator::~MemoryAllocator() {\n'
     '    std::lock_guard lock{vma_probe_mutex};\n'
     '    if (vma_probe_allocator == allocator) vma_probe_allocator = nullptr;\n'
     '    for (auto pool : temporary_pools) if (pool) vmaDestroyPool(allocator, pool);\n' +
     ('    DestroySparsePools();\n' if 'DestroySparsePools' in (source / 'src/video_core/vulkan_common/vulkan_memory_allocator.h').read_text() else '') +
     '}'),
    ('#include "video_core/vulkan_common/vulkan_memory_allocator.h"\n',
     '#include "video_core/vulkan_common/vulkan_memory_allocator.h"\n#include <atomic>\n#include <cstddef>\n#include <cstdio>\n#include <cstring>\n#include <cstdlib>\n#include "performance.h"\nnamespace Common { void GuestMemoryUsage(std::size_t* span, std::size_t* held) noexcept; }\nextern "C" std::size_t eden_heap_committed(void);\n'),
    # What an image was that the allocator could not make at all, for stderr.log.
    ('    if (result != VK_SUCCESS) {\n        LOG_CRITICAL(Render_Vulkan, "Image {}x{}x{} ({} levels, {} layers, format {}) failed: {}",',
     """    if (result != VK_SUCCESS) {
        std::fprintf(stderr, "EDEN_IMAGE_REFUSED %ux%ux%u, %u layers, %u levels, format %d, type %d, usage 0x%x, flags 0x%x, "
                     "tiling %d, samples %d, result %d, huge %d, texels %llu\\n",
                     ci.extent.width, ci.extent.height, ci.extent.depth, ci.arrayLayers, ci.mipLevels, int(ci.format),
                     int(ci.imageType), unsigned(ci.usage), unsigned(ci.flags), int(ci.tiling), int(ci.samples),
                     int(result), int(huge), static_cast<unsigned long long>(texels));
        LOG_CRITICAL(Render_Vulkan, "Image {}x{}x{} ({} levels, {} layers, format {}) failed: {}","""),
    # An image with nothing bound (the sparse virtual texture arrays no pool could hold): when the driver refuses
    # it with residency, it is asked for again with binding alone, and what it said is written down.
    ('    if (device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &sparse_ci, nullptr,\n'
     '                                                 &sparse) != VK_SUCCESS) {\n'
     '        return vk::Image{};\n'
     '    }\n',
     """    VkResult sparse_result = device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &sparse_ci, nullptr, &sparse);
    if (sparse_result != VK_SUCCESS) {
        VkImageCreateInfo binding_only = ci;
        binding_only.flags |= VK_IMAGE_CREATE_SPARSE_BINDING_BIT;
        const VkResult second = device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &binding_only, nullptr, &sparse);
        std::fprintf(stderr, "EDEN_SPARSE_IMAGE %ux%ux%u, %u layers, %u levels, format %d, usage 0x%x, flags 0x%x, tiling %d: "
                     "with residency %d, binding alone %d\\n",
                     ci.extent.width, ci.extent.height, ci.extent.depth, ci.arrayLayers, ci.mipLevels, int(ci.format),
                     unsigned(ci.usage), unsigned(ci.flags), int(ci.tiling), int(sparse_result), int(second));
        if (second != VK_SUCCESS) {
            // Once: which smaller sizes does the driver accept? Each probe is made and destroyed at once.
            static std::atomic<bool> probed{false};
            if (!probed.exchange(true)) {
                for (u32 layers = ci.arrayLayers / 2; layers >= 1; layers /= 2) {
                    VkImageCreateInfo probe = sparse_ci;
                    probe.arrayLayers = layers;
                    VkImage probe_image{};
                    const VkResult probed_result =
                        device.GetDispatchLoader().vkCreateImage(*device.GetLogical(), &probe, nullptr, &probe_image);
                    std::fprintf(stderr, "EDEN_SPARSE_PROBE %u layers: %d\\n", layers, int(probed_result));
                    if (probed_result == VK_SUCCESS) {
                        device.GetDispatchLoader().vkDestroyImage(*device.GetLogical(), probe_image, nullptr);
                    }
                    if (layers == 1) break;
                }
            }
            return vk::Image{};
        }
    }
"""),
    ('    vk::Check(vmaCreateBuffer(allocator, &ci, &alloc_ci, &handle, &allocation, &alloc_info));\n',
     refused('vmaCreateBuffer(allocator, &ci, &alloc_ci, &handle, &allocation, &alloc_info)',
             'buffer of %llu bytes, usage 0x%x', BUFFER_VALUES)),
    ('    vk::Check(vmaCreateBufferWithAlignment(allocator, &ci, &alloc_ci, min_alignment, &handle,\n'
     '                                           &allocation, &alloc_info));\n',
     refused('vmaCreateBufferWithAlignment(allocator, &ci, &alloc_ci, min_alignment, &handle,\n'
             '                                           &allocation, &alloc_info)',
             'buffer of %llu bytes, usage 0x%x', BUFFER_VALUES)),
])
# Staging buffers (headless/staging_buffers.h): above 16 MiB a buffer the CPU writes or reads is its
# transfer rounded up to 16 MiB instead of the next power of two, a kept buffer is handed out again
# only when it holds the transfer, and a refused buffer is asked for once more after the kept ones
# the GPU is done with are let go. A Switch 2 title was refused 512 MiB for a 341 MiB transfer with
# 480 MiB free. No Finish() when releasing: a buffer handed out earlier may not have its copy recorded.
adapt('src/video_core/renderer_vulkan/vk_staging_buffer_pool.h',
      'include/video_core/renderer_vulkan/vk_staging_buffer_pool.h', [
    ('    void TickFrame();\n', '    void TickFrame();\n    void ReleaseCompleted();\n'),
])
adapt('src/video_core/renderer_vulkan/vk_staging_buffer_pool.cpp', 'vulkan_staging_buffer_pool.cpp', [
    ('StagingBufferRef StagingBufferPool::CreateStagingBuffer(size_t size, MemoryUsage usage, bool deferred) {',
     '''StagingBufferRef StagingBufferPool::CreateStagingBuffer(size_t size, MemoryUsage usage, bool deferred) {
    if (static unsigned rows{}; size >= (64ULL << 20) && rows++ < 96) {
        for (unsigned kind = 0; kind < 3; ++kind) {
            auto& cache = kind == 0 ? device_local_cache : kind == 1 ? upload_cache : download_cache;
            size_t count = 0, pinned = 0, busy = 0, free = 0;
            unsigned long long bytes = 0;
            for (const auto& level : cache) for (const auto& entry : level.entries) {
                ++count;
                bytes += ::Eden::StagingBuffers::Holds(entry.mapped_span.size(), entry.log2_level);
                if (entry.deferred) ++pinned;
                else if (scheduler.IsFree(entry.tick)) ++free;
                else ++busy;
            }
            std::fprintf(stderr, "MKW_STAGING_MISS asked=%zu usage=%u deferred=%d kind=%u count=%zu bytes=%llu pinned=%zu busy=%zu free=%zu tick=%llu completed=%llu\\n",
                         size, unsigned(usage), int(deferred), kind, count, bytes, pinned, busy, free,
                         static_cast<unsigned long long>(scheduler.CurrentTick()),
                         static_cast<unsigned long long>(scheduler.GetMasterSemaphore().KnownGpuTick()));
        }
    }
'''),
    ('#include "video_core/vulkan_common/vulkan_wrapper.h"\n',
     '#include "video_core/vulkan_common/vulkan_wrapper.h"\n#include <atomic>\n#include <cstdio>\n#include "performance.h"\n#include "staging_buffers.h"\n'),
    # While the console's pool is short, every staging buffer the GPU is done with goes back at the
    # frame's end. Eden frees them slowly - one size class a frame, 16 buffers at most, so a buffer
    # can stay 64 frames after its last use - and a course loading in Mario Kart World stages its
    # textures a layer at a time: the buffers it left behind held the pool its next request needed.
    # (ProsperoEden's TickFrame release; ReleaseCompleted is the same walk the transcode path uses.)
    ('    ReleaseCache(MemoryUsage::Download);\n}',
     '    ReleaseCache(MemoryUsage::Download);\n'
     '    if (::Eden::Performance::GraphicsMemoryIsShort()) {\n'
     '        ReleaseCompleted();\n'
     '    }\n'
     '}'),
    ('void StagingBufferPool::TickFrame() {',
     'void StagingBufferPool::ReleaseCompleted() {\n'
     '    const auto idle = [this](const StagingBuffer& entry) {\n'
     '        return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '    };\n'
     '    for (StagingBuffersCache* const cache : {&device_local_cache, &upload_cache, &download_cache}) {\n'
     '        for (StagingBuffers& level : *cache) {\n'
     '            ::Eden::StagingBuffers::ReleaseFrom(level, idle);\n'
     '        }\n'
     '    }\n'
     '}\n\nvoid StagingBufferPool::TickFrame() {'),
    ('    const auto is_free = [this](const StagingBuffer& entry) {\n'
     '        return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '    };',
     '    const auto is_free = [this, size](const StagingBuffer& entry) {\n'
     '        return !entry.deferred && scheduler.IsFree(entry.tick) &&\n'
     '               ::Eden::StagingBuffers::Holds(entry.mapped_span.size(), entry.log2_level) >= size;\n'
     '    };'),
    ('    auto const log2_size = Common::Log2Ceil<u32>(u32(size));\n',
     '    auto const log2_size = Common::Log2Ceil<u32>(u32(size));\n'
     '    const bool exact = ::Eden::StagingBuffers::Exact(size, usage != MemoryUsage::DeviceLocal);\n'
     '    const VkDeviceSize made = ::Eden::StagingBuffers::Size(size, log2_size, exact);\n'
     '    if (exact) {\n'
     '        // Kept buffers of this power of two too small for the transfer go before it is made.\n'
     '        ::Eden::StagingBuffers::ReleaseFrom(GetCache(usage)[log2_size], [this, size](const StagingBuffer& entry) {\n'
     '            return !entry.deferred && scheduler.IsFree(entry.tick) &&\n'
     '                   ::Eden::StagingBuffers::Holds(entry.mapped_span.size(), entry.log2_level) < size;\n'
     '        });\n'
     '    }\n'),
    ('        .size = 1ULL << log2_size,\n', '        .size = made,\n'),
    ('    vk::Buffer buffer = memory_allocator.CreateBuffer(buffer_ci, usage);\n',
     '    vk::Buffer buffer;\n'
     '    try {\n'
     '        buffer = memory_allocator.CreateBuffer(buffer_ci, usage);\n'
     '    } catch (const vk::Exception& refused) {\n'
     '        if (refused.GetResult() != VK_ERROR_OUT_OF_DEVICE_MEMORY) {\n'
     '            throw;\n'
     '        }\n'
     '        // Refused: the kept buffers the GPU is done with go, and the buffer is asked for once\n'
     '        // more. Refused again, the error goes on as before.\n'
     '        const auto idle = [this](const StagingBuffer& entry) {\n'
     '            return !entry.deferred && scheduler.IsFree(entry.tick);\n'
     '        };\n'
     '        ::Eden::StagingBuffers::Released released;\n'
     '        for (StagingBuffersCache* const cache : {&device_local_cache, &upload_cache, &download_cache}) {\n'
     '            for (StagingBuffers& level : *cache) {\n'
     '                released += ::Eden::StagingBuffers::ReleaseFrom(level, idle);\n'
     '            }\n'
     '        }\n'
     '        std::fprintf(stderr, "EDEN_STAGING_OOM asked=%zu made=%llu released=%zu buffers (%llu MiB)\\n", size,\n'
     '                     static_cast<unsigned long long>(made), released.buffers,\n'
     '                     static_cast<unsigned long long>(released.bytes >> 20));\n'
     '        buffer = memory_allocator.CreateBuffer(buffer_ci, usage);\n'
     '    }\n'
     '    // A large buffer smaller than the power of two: the 1st, 2nd, 4th, 8th ... in the log.\n'
     '    if (exact && made >= (VkDeviceSize{64} << 20) && made < (VkDeviceSize{1} << log2_size)) {\n'
     '        static std::atomic<unsigned long long> large{0};\n'
     '        const auto times = large.fetch_add(1, std::memory_order_relaxed) + 1;\n'
     '        if ((times & (times - 1)) == 0) {\n'
     '            std::fprintf(stderr, "EDEN_STAGING_EXACT %llu MiB made for a transfer of %llu MiB, not the %llu MiB of the "\n'
     '                         "next power of two (%llu so far)\\n",\n'
     '                         static_cast<unsigned long long>(made >> 20), static_cast<unsigned long long>(size >> 20),\n'
     '                         static_cast<unsigned long long>((VkDeviceSize{1} << log2_size) >> 20), times);\n'
     '        }\n'
     '    }\n'),
])
# The software blitter keeps its scratch at the largest blit it has done: three 64 MiB buffers in
# Mario Kart World, held for the session in the pool the guest and the GPU share. Past 16 MiB they
# go back once the blit has written its result (the scoped guest write ends with its block; the
# loading probe's writeback scope ends there too, after the write, as it did at the return).
adapt('src/video_core/engines/sw_blitter/blitter.cpp', 'sw_blitter.cpp', [
    ('    const size_t dst_size = get_surface_size(dst, dst_bytes_per_pixel);\n',
     '    {\n    const size_t dst_size = get_surface_size(dst, dst_bytes_per_pixel);\n'),
    ('    VideoCommon::LoadingProbe::writeback.Mark("scoped memory destructor", dst_size);\n    return true;\n}\n',
     '    VideoCommon::LoadingProbe::writeback.Mark("scoped memory destructor", dst_size);\n    }\n'
     '    constexpr size_t kKeepBytes = 16u << 20;\n'
     '    if (impl->tmp_buffer.capacity() > kKeepBytes) impl->tmp_buffer = Common::ScratchBuffer<u8>{};\n'
     '    if (impl->src_buffer.capacity() > kKeepBytes) impl->src_buffer = Common::ScratchBuffer<u8>{};\n'
     '    if (impl->dst_buffer.capacity() > kKeepBytes) impl->dst_buffer = Common::ScratchBuffer<u8>{};\n'
     '    if (impl->intermediate_src.capacity() * sizeof(f32) > kKeepBytes) impl->intermediate_src = Common::ScratchBuffer<f32>{};\n'
     '    if (impl->intermediate_dst.capacity() * sizeof(f32) > kKeepBytes) impl->intermediate_dst = Common::ScratchBuffer<f32>{};\n'
     '    return true;\n}\n'),
])
# Development A/B switches for device features (headless/dev_vulkan.h, dev-settings).
adapt('src/video_core/vulkan_common/vulkan_device.cpp', 'vulkan_device.cpp', [
    # 16 MiB blocks: the pool the console's GPU allocates from is the CPU's and the guest's too, and
    # what a block holds goes back to it only when the block is empty. With 64 MiB blocks Mario Kart
    # World's collector deleted 2,440 images and the allocator held 3,812 MiB before and after (the
    # 584 left were spread over every block), and the pool ran out. Anything over 8 MiB is then an
    # allocation of its own, given back when freed; the page tables are the same (one per 2 MiB).
    # EDEN_PS5_VMA_BLOCK_MIB still overrides it.
    ("std::strtoul(value, nullptr, 10) : 64;\n"
     "                return static_cast<VkDeviceSize>(mib >= 4 && mib <= 256 ? mib : 64) * 1024u * 1024u;",
     "std::strtoul(value, nullptr, 10) : 16;\n"
     "                return static_cast<VkDeviceSize>(mib >= 4 && mib <= 256 ? mib : 16) * 1024u * 1024u;"),
    # The allocator counts what it holds itself instead of asking the driver. The driver's figures
    # are wrong on the console: it counts each allocation of graphics memory twice (as VRAM and as
    # visible VRAM), so the allocator saw the budget of the 8 GiB heap used up at 4 GiB. From there
    # it could make no more of its blocks and gave every image memory of its own: 926 allocations
    # where 40 blocks had done, a third of what it held unused (1.3 of 3.9 GiB in a large open-world
    # game at 2x), and the shared pool cut into hundreds of pieces. Without the driver's budget its
    # own count stands against four fifths of the heap. Only VMA's view changes: the caches' usage
    # and budget (GetDeviceMemoryUsage/GetDeviceMemoryBudget below) still read the extension or the
    # pool, and vmaGetHeapBudgets' statistics are VMA's own either way. A request past VMA's
    # four fifths is still asked for again outside the budget (ALLOCATOR_REFUSED above and Eglantine's
    # relaxed retries); with the double count gone that retry is the rare path it was meant to be.
    ('    if (extensions.memory_budget) {\n        flags |= VMA_ALLOCATOR_CREATE_EXT_MEMORY_BUDGET_BIT;\n    }\n',
     '    // The driver\'s budget is not asked for: its count is double (tools/prepare-vulkan-port.py).\n'),
    ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "performance.h"'),
    # "Memory in use" for the texture and buffer caches: on the console, the budget less what is
    # left of the pool the CPU shares with the GPU (performance.h, graphics_memory_free).
    ('u64 Device::GetDeviceMemoryUsage() const {\n',
     'namespace {\n'
     '// The largest block of the shared pool that was free when the device was made: about the most\n'
     '// graphics can be given in this session.\n'
     'u64 graphics_capacity = 0;\n'
     '}\n\n'
     'u64 Device::GetDeviceMemoryUsage() const {\n'
     '    if (const auto free_memory = ::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed);\n'
     '        free_memory && ::Eden::Performance::graphics_usage_from_pool.load(std::memory_order_relaxed)) {\n'
     '        // The caches evict what is unused 1.6 GiB under the budget and evict hard 0.8 GiB under\n'
     '        // it. Where the pool could never give graphics 6.4 GiB, those marks become a quarter\n'
     '        // and an eighth of what it could give.\n'
     '        const double scale = graphics_capacity != 0 && graphics_capacity < 6400_MiB\n'
     '                                 ? static_cast<double>(6400_MiB) / static_cast<double>(graphics_capacity)\n'
     '                                 : 1.0;\n'
     '        const u64 free_bytes = static_cast<u64>(static_cast<double>(free_memory()) * scale);\n'
     '        return device_access_memory > free_bytes ? device_access_memory - free_bytes : 0;\n'
     '    }\n'),
    # Usage above is expressed against device_access_memory, not the driver's
    # changing heap budget. Keep both sides of the cache's pressure comparison
    # in that same domain; the shared-pool free callback already reflects CPU
    # allocations and fragmentation.
    ('u64 Device::GetDeviceMemoryBudget() const {\n',
     'u64 Device::GetDeviceMemoryBudget() const {\n'
     '    if (::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed) &&\n'
     '        ::Eden::Performance::graphics_usage_from_pool.load(std::memory_order_relaxed)) {\n'
     '        return device_access_memory;\n'
     '    }\n'),
    ('    extensions.descriptor_buffer = features.descriptor_buffer.descriptorBuffer;',
     '    extensions.descriptor_buffer = features.descriptor_buffer.descriptorBuffer &&\n'
     '        !::Eden::DevVulkan::disable_descriptor_buffer;'),
    ('    features.robustness2.robustBufferAccess2 = VK_FALSE;\n'
     '    features.robustness2.robustImageAccess2 = VK_FALSE;\n'
     '    extensions.robustness_2 = features.robustness2.nullDescriptor;',
     '    if (!::Eden::DevVulkan::robustness2) {\n'
     '        features.robustness2.robustBufferAccess2 = VK_FALSE;\n'
     '        features.robustness2.robustImageAccess2 = VK_FALSE;\n'
     '    }\n'
     '    if (::Eden::DevVulkan::disable_null_descriptor) {\n'
     '        features.robustness2.nullDescriptor = VK_FALSE;\n'
     '    }\n'
     '    extensions.robustness_2 = features.robustness2.nullDescriptor;'),
    ('            (queue_family_properties[*graphics].queueFlags & VK_QUEUE_SPARSE_BINDING_BIT) != 0;',
     '            (queue_family_properties[*graphics].queueFlags & VK_QUEUE_SPARSE_BINDING_BIT) != 0 &&\n'
     '            !::Eden::DevVulkan::disable_sparse;'),
    ('            features.custom_border_color.customBorderColorWithoutFormat;\n    }\n'
     '    RemoveExtensionFeatureIfUnsuitable(extensions.custom_border_color',
     '            features.custom_border_color.customBorderColorWithoutFormat &&\n'
     '            !::Eden::DevVulkan::disable_custom_border;\n    }\n'
     '    RemoveExtensionFeatureIfUnsuitable(extensions.custom_border_color'),
    # dev-settings conditional_rendering=off: guest render-enable conditions are resolved
    # without VK_EXT_conditional_rendering (the path Eden takes on drivers without it).
    ('    // VK_EXT_border_color_swizzle\n',
     '    if (::Eden::DevVulkan::disable_conditional_rendering) {\n'
     '        extensions.conditional_rendering = false;\n    }\n'
     '    // VK_EXT_border_color_swizzle\n'),
    # The heaps and the budget the texture/buffer caches size themselves from.
    ('            device_access_memory = std::min<u64>(device_access_memory, normal_memory + scaler_memory);\n'
     '        }\n    }\n}\n',
     '            device_access_memory = std::min<u64>(device_access_memory, normal_memory + scaler_memory);\n'
     '        }\n    }\n'
     '    if (const auto free_memory = ::Eden::Performance::graphics_memory_free.load(std::memory_order_relaxed)) {\n'
     '        graphics_capacity = free_memory();\n'
     '    }\n'
     '    std::printf("EDEN_VULKAN_MEMORY integrated=%d heaps=%zu local=%llu initial_usage=%llu device_access=%llu "\n'
     '                "capacity=%llu\\n",\n'
     '                int(is_integrated), valid_heap_memory.size(), static_cast<unsigned long long>(local_memory),\n'
     '                static_cast<unsigned long long>(device_initial_usage),\n'
     '                static_cast<unsigned long long>(device_access_memory),\n'
     '                static_cast<unsigned long long>(graphics_capacity));\n}\n'),
    # GPU breadcrumbs (gpu_breadcrumbs.h, EDEN_GPU_BREADCRUMBS=1): VK_AMD_buffer_marker.
    ('#include "dev_vulkan.h"', '#include "dev_vulkan.h"\n#include "gpu_breadcrumbs.h"'),
    ('        extensions.robustness_2 = false;\n    }\n',
     '        extensions.robustness_2 = false;\n    }\n'
     '    if (::Eden::GpuBreadcrumbs::Requested() &&\n'
     '        supported_extensions.contains(VK_AMD_BUFFER_MARKER_EXTENSION_NAME)) {\n'
     '        loaded_extensions.insert(VK_AMD_BUFFER_MARKER_EXTENSION_NAME);\n'
     '        ::Eden::GpuBreadcrumbs::extension_loaded = true;\n'
     '    }\n'),
])
# Development GPU fault probe (dev_vulkan.h, submit_sync=on): wait for every
# submission, so the CPU never records against work the GPU has not finished.
GPU_TIME_HELPERS = (port / 'gpu_time_probe.inc').read_text()
# Keep command availability separate from queue-drain completion. Only the worker
# waits on event_cv; WaitWorker waits on drain_cv under the same queue mutex.
adapt('src/video_core/renderer_vulkan/vk_scheduler.h',
      'include/video_core/renderer_vulkan/vk_scheduler.h', [
    ('    std::condition_variable_any event_cv;',
     '    std::condition_variable_any event_cv;\n    std::condition_variable drain_cv;\n    unsigned drain_waiters{};'),
    # GPU breadcrumbs (gpu_breadcrumbs.h, EDEN_GPU_BREADCRUMBS=1): every recorded command is
    # marked with its recording site, the lambda's file:line in __PRETTY_FUNCTION__.
    ('#include "video_core/renderer_vulkan/vk_master_semaphore.h"',
     '#include "video_core/renderer_vulkan/vk_master_semaphore.h"\n#include "gpu_breadcrumbs.h"'),
    ('    void RecordWithUploadBuffer(T&& command) {\n'
     '        if (VideoCommon::GpuSplit::check_owner) [[unlikely]] {\n'
     '            VideoCommon::GpuSplit::CheckOwner("Scheduler::Record");\n'
     '        }\n'
     '        if (chunk->Record(command)) {',
     '    void RecordWithUploadBuffer(T&& command) {\n'
     '        RecordAt(__PRETTY_FUNCTION__, true, std::move(command));\n'
     '    }\n\n'
     '    template <typename T>\n'
     '    void RecordAt(const char* site, bool upload, T&& command) {\n'
     '        if (::Eden::GpuBreadcrumbs::enabled) [[unlikely]] {\n'
     '            RecordChunk([site, upload, inner = std::move(command)](vk::CommandBuffer cmdbuf,\n'
     '                                                                   vk::CommandBuffer upload_cmdbuf) {\n'
     '                ::Eden::GpuBreadcrumbs::Mark(*cmdbuf, upload ? *upload_cmdbuf : VkCommandBuffer{}, site);\n'
     '                inner(cmdbuf, upload_cmdbuf);\n'
     '            });\n'
     '            return;\n'
     '        }\n'
     '        RecordChunk(std::move(command));\n'
     '    }\n\n'
     '    template <typename T>\n'
     '    void RecordChunk(T&& command) {\n'
     '        if (VideoCommon::GpuSplit::check_owner) [[unlikely]] {\n'
     '            VideoCommon::GpuSplit::CheckOwner("Scheduler::Record");\n'
     '        }\n'
     '        if (chunk->Record(command)) {'),
    ('    void Record(T&& c) {\n'
     '        this->RecordWithUploadBuffer(\n'
     '            [command = std::move(c)](vk::CommandBuffer cmdbuf, vk::CommandBuffer) {\n'
     '                command(cmdbuf);\n'
     '            });\n'
     '    }',
     '    void Record(T&& c) {\n'
     '        this->RecordAt(__PRETTY_FUNCTION__, false,\n'
     '            [command = std::move(c)](vk::CommandBuffer cmdbuf, vk::CommandBuffer) {\n'
     '                command(cmdbuf);\n'
     '            });\n'
     '    }'),
])
adapt('src/video_core/renderer_vulkan/vk_scheduler.cpp', 'vulkan_scheduler.cpp', [
    ('Scheduler::~Scheduler() = default;',
     'Scheduler::~Scheduler() {\n'
     '    worker_thread.request_stop();\n'
     '    if (worker_thread.joinable()) worker_thread.join();\n'
     '    GpuTimeReset(device);\n}'),
    ('        event_cv.wait(ql, [this] { return work_queue.empty(); });',
     '        ++drain_waiters;\n        drain_cv.wait(ql, [this] { return work_queue.empty(); });\n        --drain_waiters;'),
    ('            event_cv.notify_all();', '            if (eden_vulkan_handoff_enabled()) event_cv.notify_one();\n            else event_cv.notify_all();'),
    ('        work_queue.pop();\n        event_cv.notify_all();',
     '        work_queue.pop();\n        if (!eden_vulkan_handoff_enabled() || (work_queue.empty() && drain_waiters)) drain_cv.notify_all();'),

    ('#include <memory>', '#include <memory>\n#include <array>\n#include <chrono>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "hud_gpu_time.h"\n#include "submission_origin.h"\nextern "C" bool eden_vulkan_handoff_enabled();'),
    ('namespace Vulkan {\n', 'namespace Vulkan {\n' + GPU_TIME_HELPERS),
    ('        .pInheritanceInfo = nullptr,\n    });\n}\n\nu64 Scheduler::SubmitExecution(',
     '        .pInheritanceInfo = nullptr,\n    });\n    GpuTimeBegin(device, current_upload_cmdbuf);\n}\n\nu64 Scheduler::SubmitExecution('),
    ('u64 Scheduler::Flush(VkSemaphore signal_semaphore, VkSemaphore wait_semaphore) {',
     'u64 Scheduler::Flush(VkSemaphore signal_semaphore, VkSemaphore wait_semaphore) {\n'
     '    GpuSubmitOriginScope origin{__builtin_return_address(0), false, true};'),
    ('void Scheduler::Finish(VkSemaphore signal_semaphore, VkSemaphore wait_semaphore) {',
     'void Scheduler::Finish(VkSemaphore signal_semaphore, VkSemaphore wait_semaphore) {\n'
     '    GpuSubmitOriginScope origin{__builtin_return_address(0), true, true};'),
    ('    RecordWithUploadBuffer([signal_semaphore, wait_semaphore, signal_value,',
     '    RecordWithUploadBuffer([signal_semaphore, wait_semaphore, signal_value,\n'
     '                            submit_origin = gpu_submit_origin,'),
    ('        cmdbuf.End();\n\n        if (on_submit)',
     '        GpuTimeEnd(cmdbuf);\n        cmdbuf.End();\n\n        if (on_submit)'),
    ('            vk::Check(result);\n            break;\n        }\n    });\n    chunk->MarkSubmit();',
     '            vk::Check(result);\n            break;\n        }\n'
     '        GpuTimeCountOrigin(submit_origin);\n        GpuTimeCollect(device);\n    });\n    chunk->MarkSubmit();'),
    ('    const u64 signal_value = SubmitExecution(signal_semaphore, wait_semaphore);\n'
     '    AllocateNewContext();\n    return signal_value;',
     '    const u64 signal_value = SubmitExecution(signal_semaphore, wait_semaphore);\n'
     '    if (::Eden::DevVulkan::sync_submissions) {\n        Wait(signal_value);\n    }\n'
     '    AllocateNewContext();\n    return signal_value;'),
    # GPU breadcrumbs: the marker buffer before the worker records anything, and the dump the
    # terminate handler calls on a lost device (headless/main.cpp, weak reference).
    ('    AcquireNewChunk();\n    AllocateWorkerCommandBuffer();\n    worker_thread',
     '    ::Eden::GpuBreadcrumbs::Init(device);\n'
     '    AcquireNewChunk();\n    AllocateWorkerCommandBuffer();\n    worker_thread'),
    ('Scheduler::Scheduler(const Device& device_, StateTracker& state_tracker_)',
     'extern "C" void eden_gpu_breadcrumbs_dump() {\n    ::Eden::GpuBreadcrumbs::Dump();\n}\n\n'
     'Scheduler::Scheduler(const Device& device_, StateTracker& state_tracker_)'),
])
adapt('src/video_core/renderer_vulkan/pipeline_helper.h',
      'include/video_core/renderer_vulkan/pipeline_helper.h', [
    ('            VkDescriptorGetInfoEXT get_info{',
     '            const VkDescriptorAddressInfoEXT* const buffer_info{\n'
     '                entry.address.address != 0 ? &address_info : nullptr};\n'
     '            VkDescriptorGetInfoEXT get_info{'),
    ('get_info.data.pUniformBuffer = &address_info;', 'get_info.data.pUniformBuffer = buffer_info;'),
    ('get_info.data.pStorageBuffer = &address_info;', 'get_info.data.pStorageBuffer = buffer_info;'),
    ('get_info.data.pUniformTexelBuffer = &address_info;', 'get_info.data.pUniformTexelBuffer = buffer_info;'),
    ('get_info.data.pStorageTexelBuffer = &address_info;', 'get_info.data.pStorageTexelBuffer = buffer_info;'),
])
adapt('src/video_core/renderer_vulkan/vk_blit_screen.h',
      'include/video_core/renderer_vulkan/vk_blit_screen.h', [
    ('VkFormat current_swapchain_view_format);',
     'VkFormat current_swapchain_view_format, bool draw_hud = false);'),
])
adapt('src/video_core/renderer_vulkan/renderer_vulkan.h',
      'include/video_core/renderer_vulkan/renderer_vulkan.h', [
    ('    void Composite(', '    void PresentLoading();\n    void Composite('),
])
adapt('src/video_core/renderer_vulkan/renderer_vulkan.cpp', 'vulkan_renderer.cpp', [
    ('#include "video_core/renderer_vulkan/renderer_vulkan.h"',
     '#include "video_core/renderer_vulkan/renderer_vulkan.h"\n#include "display_refresh.h"\n'
     '#include "diagnostics.h"\n#include "hud.h"'),
    # The HUD counted the guest rate only: OnFrameDisplayed() runs once per Composite(), while the
    # frames frame generation adds are presented through PresentManager::Present() (hud.h). Count
    # every frame handed to the present manager.
    ('        present_manager.Present(generated);\n    }',
     '        present_manager.Present(generated);\n'
     '        Eden::CountPresentedFrame();\n'
     '    }'),
    # A frame the display has no refresh for is not presented: a game patched for more frames
    # than the output shows keeps its pace instead of waiting for a refresh per frame
    # (headless/display_refresh.h). The frame still ends as any other.
    ('    RenderScreenshot(framebuffers);\n    Frame* frame = present_manager.GetRenderFrame();',
     '    RenderScreenshot(framebuffers);\n'
     '    if (::Eden::Display::SkipFrame()) {\n'
     '        gpu.RendererFrameEndNotify();\n'
     '        rasterizer.TickFrame();\n'
     '        return;\n'
     '    }\n'
     '    Frame* frame = present_manager.GetRenderFrame();'),
    ('    Report();', '    PresentLoading();\n    Report();'),
    ('swapchain.GetImageViewFormat());', 'swapchain.GetImageViewFormat(), true);'),
    ('layout, 1, format);', 'layout, 1, format, true);'),
    ('void RendererVulkan::Report() const {',
     (port / 'vulkan_loading.inc').read_text() + '\nvoid RendererVulkan::Report() const {'),
    # Present() only records the hand-off into a fresh chunk. Dispatch it now so
    # the present thread gets this frame immediately instead of after the
    # producer's next 8-draw dispatch (upstream does this only with LSFG).
    ('    present_manager.Present(frame);\n#ifdef HAS_LSFG\n    scheduler.DispatchWork();\n#endif',
     '    present_manager.Present(frame);\n    Eden::CountPresentedFrame();\n'
     '    scheduler.DispatchWork();'),
    ('} // namespace Vulkan', '''} // namespace Vulkan
namespace Eden {
void PresentVulkanLoading(VideoCore::RendererBase& renderer) {
    static_cast<Vulkan::RendererVulkan&>(renderer).PresentLoading();
}
}'''),
])
adapt('src/video_core/renderer_vulkan/present/window_adapt_pass.h',
      'include/video_core/renderer_vulkan/present/window_adapt_pass.h', [
    ('const Layout::FramebufferLayout& layout, Frame* dst);',
     'const Layout::FramebufferLayout& layout, Frame* dst, bool draw_hud = false);'),
    ('    vk::Pipeline coverage_pipeline;',
     '    vk::Pipeline coverage_pipeline;\n    vk::PipelineLayout hud_layout;\n    vk::Pipeline hud_pipeline;'),
])
adapt('src/video_core/renderer_vulkan/present/window_adapt_pass.cpp', 'vulkan_window_adapt_pass.cpp', [
    ('#include "core/frontend/framebuffer_layout.h"',
     '#include "core/frontend/framebuffer_layout.h"\n#include "hud.h"\n#include "vulkan_hud_shaders.h"'),
    ('const Layout::FramebufferLayout& layout, Frame* dst) {',
     'const Layout::FramebufferLayout& layout, Frame* dst, bool draw_hud) {'),
    ('    scheduler.Record([=](vk::CommandBuffer cmdbuf) {',
     (port / 'vulkan_hud_prepare.inc').read_text() + '\n    scheduler.Record([=](vk::CommandBuffer cmdbuf) {'),
    ('        cmdbuf.EndRenderPass();',
     (port / 'vulkan_hud_draw.inc').read_text() + '\n        cmdbuf.EndRenderPass();'),
])
adapt('src/video_core/renderer_vulkan/vk_blit_screen.cpp', 'vulkan_blit_screen.cpp', [
    ('VkFormat current_swapchain_view_format) {',
     'VkFormat current_swapchain_view_format, bool draw_hud) {'),
    ('image_index, layers, framebuffers, layout, frame);',
     'image_index, layers, framebuffers, layout, frame, draw_hud);'),
])
shader_header = '#pragma once\n#include <cstdint>\n'
for stage in ('vert', 'frag'):
    binary = output / f'vulkan_hud.{stage}.spv'
    subprocess.run(['glslangValidator', '-V', '--target-env', 'vulkan1.0',
                    '-o', str(binary), str(port / f'vulkan_hud.{stage}')], check=True)
    subprocess.run(['spirv-val', '--target-env', 'vulkan1.0', str(binary)], check=True)
    data = binary.read_bytes()
    words = struct.unpack('<' + 'I' * (len(data) // 4), data)
    shader_header += f'inline constexpr uint32_t EDEN_HUD_{stage.upper()}_SPV[] = {{\n'
    shader_header += ','.join(hex(word) for word in words) + '\n};\n'
path = output / 'vulkan_hud_shaders.h'
if not path.exists() or path.read_text() != shader_header:
    path.write_text(shader_header)
adapt('src/video_core/vulkan_common/vulkan.h', 'include/video_core/vulkan_common/vulkan.h',
      [('#else\n#define VK_USE_PLATFORM_XLIB_KHR', '#elif defined(PS5_NATIVE)\n// Native display-plane presentation, no desktop window-system headers.\n#else\n#define VK_USE_PLATFORM_XLIB_KHR')])
adapt('src/video_core/vulkan_common/vulkan_instance.cpp', 'vulkan_instance.cpp', [
    ('namespace Vulkan {', 'extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance, const char*);\n\nnamespace Vulkan {'),
    ('    case Core::Frontend::WindowSystemType::Headless:', '    case Core::Frontend::WindowSystemType::PS5:\n        extensions.push_back(VK_KHR_DISPLAY_EXTENSION_NAME);\n        break;\n    case Core::Frontend::WindowSystemType::Headless:'),
    ('#else\n    case Core::Frontend::WindowSystemType::X11:', '#elif !defined(PS5_NATIVE)\n    case Core::Frontend::WindowSystemType::X11:'),
    ('''    if (!library.IsOpen()) {
        LOG_ERROR(Render_Vulkan, "Vulkan library not available");
        throw vk::Exception(VK_ERROR_INITIALIZATION_FAILED);
    }
    if (!library.GetSymbol("vkGetInstanceProcAddr", &dld.vkGetInstanceProcAddr)) {
        LOG_ERROR(Render_Vulkan, "vkGetInstanceProcAddr not present in Vulkan");
        throw vk::Exception(VK_ERROR_INITIALIZATION_FAILED);
    }''', '    (void)library;\n    dld.vkGetInstanceProcAddr = &::vkGetInstanceProcAddr;'),
])
adapt('src/video_core/vulkan_common/vulkan_library.cpp', 'vulkan_library.cpp', [
    ('#if defined(__ANDROID__) && defined(ARCHITECTURE_arm64)', '#if defined(PS5_NATIVE)\n    return std::make_shared<Common::DynamicLibrary>();\n#elif defined(__ANDROID__) && defined(ARCHITECTURE_arm64)'),
])
adapt('src/video_core/vulkan_common/vulkan_wrapper.h', 'include/video_core/vulkan_common/vulkan_wrapper.h', [
    ('#include <array>', '#include <array>\n#include <cstdio>\n#include "performance.h"'),
    ('return dld->vkQueueSubmit(queue,', 'auto timer = Eden::Performance::VulkanTimer(2);\n        return dld->vkQueueSubmit(queue,'),
    ('return dld->vkQueueSubmit2(queue,', 'auto timer = Eden::Performance::VulkanTimer(2);\n        return dld->vkQueueSubmit2(queue,'),
    ('return dld->vkWaitForFences(owner,', 'auto timer = Eden::Performance::VulkanTimer(3);\n        return dld->vkWaitForFences(owner,'),
    ('const VkResult result = dld->vkWaitSemaphores(owner,', 'auto timer = Eden::Performance::VulkanTimer(4);\n        const VkResult result = dld->vkWaitSemaphores(owner,'),
    ('return dld->vkDeviceWaitIdle(handle);', 'auto timer = Eden::Performance::VulkanTimer(5);\n        return dld->vkDeviceWaitIdle(handle);'),
    ('''inline void Check(VkResult result) {
    if (result != VK_SUCCESS) {
        throw Exception(result);''', '''inline void Check(VkResult result, const char* file = __builtin_FILE(),
                  int line = __builtin_LINE(), const char* operation = __builtin_FUNCTION()) {
    if (result != VK_SUCCESS) {
        std::fprintf(stderr, "[EglantinePS5] Vulkan failure: %s:%d (%s) VkResult=%d\\n",
                     file, line, operation, static_cast<int>(result));
        throw Exception(result);'''),
])
adapt('src/video_core/vulkan_common/vulkan_wrapper.cpp', 'vulkan_wrapper.cpp', [
    ('#include "video_core/vulkan_common/vulkan_wrapper.h"', '#include "video_core/vulkan_common/vulkan_wrapper.h"\n#include "performance.h"'),
    ('auto const result = dld->vkCreateGraphicsPipelines(', 'auto timer = Eden::Performance::VulkanTimer(0);\n    auto const result = dld->vkCreateGraphicsPipelines('),
    ('Check(dld->vkCreateComputePipelines(', 'auto timer = Eden::Performance::VulkanTimer(1);\n    Check(dld->vkCreateComputePipelines('),
])
download_source = (source / 'src/video_core/texture_cache/texture_cache.h').read_text()
download_begin = download_source.index('void TextureCache<P>::DownloadMemory(DAddr cpu_addr, size_t size)')
download_loop = download_source.index('    for (const ImageId image_id : images) {', download_begin)
download_prefix = download_source[download_begin:download_loop]
# Reuse DeleteImage's exact accounting to stop lookahead when its existing
# pressure policy changes. Fail on source drift rather than inventing a budget.
delete_begin = download_source.index('    if (image.HasScaled()) {', download_source.index('void TextureCache<P>::DeleteImage('))
delete_end = download_source.index('    const GPUVAddr gpu_addr', delete_begin)
reclaimed_bytes = (download_source[delete_begin:delete_end]
                   .replace('total_used_memory -=', 'bytes +=')
                   .replace('slot_images[image_id]', 'image'))
assert 'image_id' not in reclaimed_bytes, 'DeleteImage accounting needs adapting to an image reference'
gc_begin = download_source.index('void TextureCache<P>::RunGarbageCollector() {')
gc_end = download_source.index('\ntemplate <class P>', gc_begin)
gc_original = download_source[gc_begin:gc_end]
gc = gc_original
gc_setup = '''    const auto ReclaimedBytes = [&](const Image& image) {
        u64 bytes = 0;
''' + reclaimed_bytes + '''        return bytes;
    };
''' + (port / 'vulkan_gc_downloads.inc').read_text()
cleanup_begin = gc.index('    const auto Cleanup = [')
cleanup_end = gc.index('](ImageId image_id)', cleanup_begin) + 1
cleanup_capture = gc[cleanup_begin:cleanup_end]
# Eglantine's own block-order walk (tools/eglantine-patches/0012, Collect) gives way to
# ForEachCandidate (vulkan_gc_downloads.inc): the same order, but over up to 1,024 candidates
# instead of 8 per eviction allowed, then on oldest first, and the download lookahead walks it too.
collect_begin = gc.index('    const auto Collect = [&] {')
collect_begin = gc.rindex('    };\n', 0, collect_begin) + len('    };\n')  # with 0012's comment
collect_end = gc.index('    Configure(false);', collect_begin)
collect_original = gc[collect_begin:collect_end]
for old, new in [
    (cleanup_capture,
     gc_setup + '\n    const auto Cleanup = [&, this]'),
    (collect_original,
     '    const auto Collect = [&] { ForEachCandidate(frame_tick - ticks_to_destroy, Cleanup); };\n'),
    ('        if (must_download) {',
     '        if (must_download && KeepLargeDirty(image)) {\n            return false;\n        }\n'
     '        if (must_download && !TakePrefetched(image_id, image)) {'),
    # Time the per-image drain Cleanup still takes for an image the prefetch could not batch.
    # One GPU pipeline wait per eviction, against one wait for a whole batch in the batched path:
    # this is the stall the prefetch exists to remove, so keep it visible.
    ('            image.DownloadMemory(map, copies);\n            runtime.Finish();\n'
     '            SwizzleImage(*gpu_memory, image.gpu_addr, image.info, copies, map.mapped_span, swizzle_data_buffer);',
     '            image.DownloadMemory(map, copies);\n'
     '            {\n'
     '                const ::Eden::Performance::Timer drain_timer(\n'
     '                    ::Eden::Performance::gc_unbatched_drain, 1);\n'
     '                runtime.Finish();\n'
     '            }\n'
     '            SwizzleImage(*gpu_memory, image.gpu_addr, image.info, copies, map.mapped_span, swizzle_data_buffer);'),
    ('(!high_priority_mode && must_download)) {', '(!DirtyEvictions() && must_download)) {'),
    ('    Configure(false);', '    Configure(false);\n    PrefetchDownloads();'),
    ('        Configure(true);', '        Configure(true);\n        PrefetchDownloads();'),
    # The eviction budget counts visits, not evictions: skipped images (decoding,
    # costly, or dirty images kept below the critical mark) burn it and stop the
    # scan, so kept images shield everything behind them. Count evictions; bound
    # the scan separately. gc_scan=upstream restores visit counting.
    ('    size_t num_iterations = 0;',
     '    size_t num_iterations = 0;\n    size_t num_scanned = 0;\n    constexpr size_t max_scanned = 8192;'),
    ('        if (num_iterations == 0) {\n            return true;\n        }\n        --num_iterations;',
     '        if (num_iterations == 0 || num_scanned >= max_scanned) {\n            return true;\n        }\n        ++num_scanned;\n'
     '        if (::Eden::Performance::gc_count_visits.load(std::memory_order_relaxed)) {\n            --num_iterations;\n        }'),
    # A costly image (ASTC kept as BC3, converted on the CPU) goes only past Eden's own critical
    # mark, not when the console's pool alone is critical: rebuilding it decodes it again on the
    # CPU, in memory the pool did not have (Mario Kart World: 1,561 evicted in one window, 3.4 GB
    # of conversions and uploads in the next, and the decoder's allocation ended the session).
    ('        if ((!aggressive_mode && True(image.flags & ImageFlagBits::CostlyLoad)) || (!DirtyEvictions() && must_download)) {',
     '        const bool costly_kept = !(aggressive_mode && total_used_memory >= critical_memory) &&\n'
     '                                 !::Eden::Performance::gc_old_costly && True(image.flags & ImageFlagBits::CostlyLoad);\n'
     '        if (costly_kept || (!DirtyEvictions() && must_download)) {'),
    ('            probe_costly_skips += !aggressive_mode && True(image.flags & ImageFlagBits::CostlyLoad);\n',
     '            probe_costly_skips += costly_kept;\n'),
    ('            probe_dirty_skips += !high_priority_mode && must_download;\n            return false;\n        }\n',
     '            probe_dirty_skips += !high_priority_mode && must_download;\n            return false;\n        }\n'
     '        if (!::Eden::Performance::gc_count_visits.load(std::memory_order_relaxed)) {\n            --num_iterations;\n        }\n'),
    # The aggressive pass gets its own scan allowance: Configure resets the
    # eviction budget per pass, and the scan counter must reset with it, or a
    # first pass spent on skips would stillborn the second one. Runs after the
    # Configure(true) pair above, whose first line this matches.
    ('        Configure(true);',
     '        Configure(true);\n        num_scanned = 0;'),
    # While the console's pool is short (performance.h, GraphicsMemoryIsShort) the collector
    # works as if past its first mark, past both once the pool is critical (GraphicsMemoryIsCritical),
    # and stays so for the whole run instead of easing off after one eviction under a mark the
    # pool never reaches.
    ('    u64 ticks_to_destroy = 0;\n',
     '    u64 ticks_to_destroy = 0;\n    const bool memory_short = ::Eden::Performance::GraphicsMemoryIsShort();\n'),
    ('        num_iterations = aggressive_mode ? 40 : (high_priority_mode ? 20 : 10);\n',
     '        if (memory_short) {\n'
     '            // Short but not critical, only what 600 frames left unused goes (EDEN_NX2_TC_SHORT_AGE):\n'
     '            // at 60, and 10 whenever use passed the texture cache\'s own critical mark, a race\n'
     '            // evicted and rebuilt 1,000-2,400 textures every 600 frames, many still in use, while\n'
     '            // the pool stayed short throughout (an image freed inside a graphics block does not\n'
     '            // give the pool a larger free block). On the console the pool decides: critical\n'
     '            // (GraphicsMemoryIsCritical) collects as before.\n'
     '            static const u64 short_age = [] {\n'
     '                const char* value = std::getenv("EDEN_NX2_TC_SHORT_AGE");\n'
     '                return value != nullptr ? std::strtoull(value, nullptr, 10) : 600ULL;\n'
     '            }();\n'
     '            high_priority_mode = true;\n'
     '            aggressive_mode = allow_aggressive && ::Eden::Performance::GraphicsMemoryIsCritical();\n'
     '            ticks_to_destroy = (std::max)(aggressive_mode ? u64{10} : short_age, min_age);\n'
     '        }\n'
     '        num_iterations = aggressive_mode ? 40 : (high_priority_mode ? 20 : 10);\n'),
    ('        if (aggressive_mode && total_used_memory < critical_memory) {',
     '        if (aggressive_mode && !memory_short && total_used_memory < critical_memory) {'),
    ('        } else if (high_priority_mode && total_used_memory < expected_memory) {',
     '        } else if (high_priority_mode && !memory_short && total_used_memory < expected_memory) {'),
    ('    if (total_used_memory >= critical_memory) {\n        Configure(true);',
     '    if (total_used_memory >= critical_memory || memory_short) {\n        Configure(true);'),
    # While the pool is short, costly images 300 frames unused (a course left behind) go too, in a
    # pass of their own: kept whenever only the pool was short, Mario Kart World's next course took
    # 1.2 GB of graphics memory within five seconds with nothing the collector could give back, and
    # waiting until the pool was critical left a course load too little room (pro27: 1.37 GB usable,
    # then out of memory within one window). On a light frame (a loading screen: astc_policy.h)
    # the course left behind is unused after 10 frames already, and the next one is about to load.
    ('        num_scanned = 0;\n        PrefetchDownloads();\n        Collect();\n    }\n',
     '        num_scanned = 0;\n        PrefetchDownloads();\n        Collect();\n    }\n'
     '    const u64 old_age = ::Eden::AstcPolicy::InBackground() ? 300 : 10;\n'
     '    if (memory_short && frame_tick > old_age) {\n'
     '        Configure(true);\n'
     '        num_scanned = 0;\n'
     '        ::Eden::Performance::gc_old_costly = true;\n'
     '        PrefetchDownloads();\n'
     '        ForEachCandidate(frame_tick - old_age, Cleanup);\n'
     '        ::Eden::Performance::gc_old_costly = false;\n'
     '    }\n'),
]:
    if gc.count(old) != 1:
        raise RuntimeError(f'Pinned GC anchor changed: {old}')
    gc = gc.replace(old, new)
texture_costs = []
for signature, index in (
    ('RunGarbageCollector()', 13), ('DownloadMemory(DAddr cpu_addr, size_t size)', 14),
    ('PopAsyncFlushes()', 15),
):
    anchor = f'void TextureCache<P>::{signature} {{'
    scope = f'''
    std::optional<Eden::Performance::Timer> cost_timer;
    if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {{
        if (Eden::Performance::vulkan_cost_enabled.load(std::memory_order_relaxed))
            cost_timer.emplace(Eden::Performance::vulkan_api[{index}]);
    }}
'''
    if index == 13:
        scope += '''    if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {
        if ((cost_timer || Eden::Performance::texture_budget_log.load(std::memory_order_relaxed)) &&
            frame_tick % 300 == 0)
            std::printf("EDEN_VULKAN_TEXTURE_BUDGET frame=%llu usage=%llu expected=%llu critical=%llu "
                        "keep_dirty=%d memory_short=%d\\n",
                        static_cast<unsigned long long>(frame_tick),
                        static_cast<unsigned long long>(total_used_memory),
                        static_cast<unsigned long long>(expected_memory),
                        static_cast<unsigned long long>(critical_memory),
                        int(Eden::Performance::gc_keep_dirty.load(std::memory_order_relaxed)),
                        int(Eden::Performance::graphics_memory_short.load(std::memory_order_relaxed)));
    }
'''
    texture_costs.append((anchor, anchor + scope))
VULKAN_CACHE = 'if constexpr (std::is_same_v<Runtime, Vulkan::TextureCacheRuntime>) {'
# Large textures re-upload only the block rows guest writes changed (headless/texture_partial.h).
# The Eglantine core has the mechanism (MarkPartialWrite / RefreshPartialContents) from 16 MiB on;
# ProsperoEden's takes images from 1 MiB on (dev-settings tex_partial_kb), which is what this sets.
# tex_partial=off keeps the core's bound; tex_partial_check=on compares each partial upload with a
# whole unswizzle.
texture_partial = [
    ('#include "video_core/texture_cache/texture_cache_base.h"',
     '#include "video_core/texture_cache/texture_cache_base.h"\n#include "texture_partial.h"\n#include "astc_policy.h"'),
    ('    constexpr size_t min_size = 16_MiB;\n'
     '    constexpr ImageFlagBits excluded = ImageFlagBits::Sparse | ImageFlagBits::Converted |\n'
     '                                       ImageFlagBits::AcceleratedUpload | ImageFlagBits::Unbacked |\n'
     '                                       ImageFlagBits::TrimmedLevels | ImageFlagBits::AsynchronousDecode;\n'
     '    if (False(image.flags & ImageFlagBits::Tracked)',
     '    size_t min_size = 16_MiB;\n'
     '    ' + VULKAN_CACHE + '\n'
     '        if (Eden::Performance::tex_partial.load(std::memory_order_relaxed)) {\n'
     '            min_size = Eden::Performance::tex_partial_min.load(std::memory_order_relaxed);\n'
     '        }\n'
     '    }\n'
     '    constexpr ImageFlagBits excluded = ImageFlagBits::Sparse | ImageFlagBits::Converted |\n'
     '                                       ImageFlagBits::AcceleratedUpload | ImageFlagBits::Unbacked |\n'
     '                                       ImageFlagBits::TrimmedLevels | ImageFlagBits::AsynchronousDecode;\n'
     '    if (False(image.flags & ImageFlagBits::Tracked)'),
    ('    auto copies = UnswizzleImageRows(*gpu_memory, image.gpu_addr, image.info, ranges, data);\n'
     '    if (copies.empty()) {\n'
     '        return false;\n'
     '    }\n',
     '    auto copies = UnswizzleImageRows(*gpu_memory, image.gpu_addr, image.info, ranges, data);\n'
     '    if (copies.empty()) {\n'
     '        return false;\n'
     '    }\n'
     '    ' + VULKAN_CACHE + '\n'
     '        if (Eden::Performance::tex_partial_check.load(std::memory_order_relaxed)) {\n'
     '            Eden::TexturePartial::Check(*gpu_memory, image, copies, std::span<const u8>(data));\n'
     '        }\n'
     '        Eden::Performance::tex_partial_uploads.fetch_add(1, std::memory_order_relaxed);\n'
     '        Eden::Performance::tex_partial_bytes.fetch_add(data.size(), std::memory_order_relaxed);\n'
     '    }\n'),
]
# A large texture array (2D, several layers, 64 MiB or more) the CPU converts is uploaded a layer at
# a time, and an ASTC one converted in the background is decoded a layer at a time: Eden staged it
# whole, a heap buffer and a staging buffer each the image's size (341 and 361 MiB for Mario Kart
# World's terrain arrays) asked of the pool the guest's memory and the GPU share, at the moment a
# course loads - the requests that ended its sessions. The bytes are the same; the largest block is
# one layer. Arrays the GPU decodes keep the core's layer path (UploadsByLayer), and plain arrays its
# chunked one (UploadInChunks), so only converted ones take this (ProsperoEden: every CPU array).
LAYERED = ('image.info.type == ImageType::e2D && image.info.resources.layers > 1 && '
           'image.guest_size_bytes >= LayerUploadMinSize')
texture_layers = [
    ('    ++images_uploaded;\n'
     '    uploaded_bytes += image.guest_size_bytes;\n'
     '    largest_upload = (std::max<u64>)(largest_upload, image.guest_size_bytes);\n'
     '    if (!UploadInChunks(image)) {',
     '    ++images_uploaded;\n'
     '    uploaded_bytes += image.guest_size_bytes;\n'
     '    largest_upload = (std::max<u64>)(largest_upload, image.guest_size_bytes);\n'
     '    ' + VULKAN_CACHE + '\n'
     '        if (' + LAYERED + ' && True(image.flags & ImageFlagBits::Converted) &&\n'
     '            False(image.flags & (ImageFlagBits::AcceleratedUpload | ImageFlagBits::Sparse |\n'
     '                                 ImageFlagBits::Remapped | ImageFlagBits::Unbacked |\n'
     '                                 ImageFlagBits::TrimmedLevels))) {\n'
     '            const s32 layers = image.info.resources.layers;\n'
     '            ImageInfo layer_info = image.info;\n'
     '            layer_info.resources.layers = 1;\n'
     '            const size_t layer_map = MapSizeBytes(image) / layers;\n'
     '            for (s32 layer = 0; layer < layers; ++layer) {\n'
     '                const GPUVAddr layer_addr = image.gpu_addr + u64(layer) * image.info.layer_stride;\n'
     '                Tegra::Memory::GpuGuestMemory<u8, Tegra::Memory::GuestMemoryFlags::UnsafeRead> swizzle_data(\n'
     '                    *gpu_memory, layer_addr, image.info.layer_stride, &swizzle_data_buffer);\n'
     '                unswizzle_data_buffer.resize_destructive((std::max<size_t>)(\n'
     '                    image.unswizzled_size_bytes / layers, CalculateUnswizzledSizeBytes(layer_info)));\n'
     '                auto copies = UnswizzleImage(*gpu_memory, layer_addr, layer_info, swizzle_data,\n'
     '                                             unswizzle_data_buffer);\n'
     '                // The layer\'s converted size, where it is above the estimate (as the decode).\n'
     '                auto layout = copies;\n'
     '                ConvertedLayout(layer_info, layout);\n'
     '                const size_t layer_out = layout.empty() ? layer_map :\n'
     '                    (std::max<size_t>)(layer_map, layout.back().buffer_offset + layout.back().buffer_size);\n'
     '                auto layer_staging = runtime.UploadStagingBuffer(layer_out);\n'
     '                ConvertImage(unswizzle_data_buffer, layer_info, layer_staging.mapped_span, copies);\n'
     '                for (auto& copy : copies) copy.image_subresource.base_layer = layer;\n'
     '                image.UploadMemory(layer_staging, copies);\n'
     '            }\n'
     '            runtime.InsertUploadMemoryBarrier();\n'
     '            return;\n'
     '        }\n'
     '    }\n'
     '    if (!UploadInChunks(image)) {'),
    ('    image.flags |= ImageFlagBits::IsDecoding;\n'
     '    ++async_decodes_queued;\n'
     '    auto decode = std::make_unique<AsyncDecodeContext>();',
     '    ' + VULKAN_CACHE + '\n'
     '        if (' + LAYERED + ') {\n'
     '            image.flags |= ImageFlagBits::IsDecoding;\n'
     '            const s32 layers = image.info.resources.layers;\n'
     '            ImageInfo layer_info = image.info;\n'
     '            layer_info.resources.layers = 1;\n'
     '            const size_t layer_out = MapSizeBytes(image) / layers;\n'
     '            // A layer\'s own sizes, not a share of the whole image\'s estimates: a decode that\n'
     '            // wrote past its buffer corrupted the heap (TextureDecoder "mutex lock failed").\n'
     '            const size_t layer_in = (std::max<size_t>)(image.unswizzled_size_bytes / layers,\n'
     '                                                       CalculateUnswizzledSizeBytes(layer_info));\n'
     '            const bool zero = False(image.flags & ImageFlagBits::ZeroFilled);\n'
     '            image.flags |= ImageFlagBits::ZeroFilled;\n'
     '            for (s32 layer = 0; layer < layers; ++layer) {\n'
     '                ++async_decodes_queued;\n'
     '                auto layer_decode = std::make_unique<AsyncDecodeContext>();\n'
     '                auto* layer_ptr = layer_decode.get();\n'
     '                layer_decode->image_id = image_id;\n'
     '                async_decodes.push_back(std::move(layer_decode));\n'
     '                const GPUVAddr layer_addr = image.gpu_addr + u64(layer) * image.info.layer_stride;\n'
     '                std::vector<u8> layer_input(layer_in, 0);\n'
     '                Tegra::Memory::GpuGuestMemory<u8, Tegra::Memory::GuestMemoryFlags::UnsafeRead> layer_swizzle(\n'
     '                    *gpu_memory, layer_addr, image.info.layer_stride, &swizzle_data_buffer);\n'
     '                auto layer_copies = UnswizzleImage(*gpu_memory, layer_addr, layer_info, layer_swizzle,\n'
     '                                                   layer_input);\n'
     '                auto zero_copies = layer_copies;\n'
     '                ConvertedLayout(layer_info, zero_copies);\n'
     '                const size_t out = zero_copies.empty() ? layer_out :\n'
     '                    (std::max<size_t>)(layer_out, zero_copies.back().buffer_offset + zero_copies.back().buffer_size);\n'
     '                if (out > layer_out) {\n'
     '                    static std::atomic<u32> reported{};\n'
     '                    if (reported.fetch_add(1) < 8) {\n'
     '                        LOG_WARNING(HW_GPU, "Layer decode at {:#x} needs {} bytes, {} estimated", layer_addr, out, layer_out);\n'
     '                    }\n'
     '                }\n'
     '                if (zero) {\n'
     '                    for (auto& copy : zero_copies) copy.image_subresource.base_layer = layer;\n'
     '                    auto zero_staging = runtime.UploadStagingBuffer(out);\n'
     '                    std::memset(zero_staging.mapped_span.data(), 0, out);\n'
     '                    image.UploadMemory(zero_staging, zero_copies);\n'
     '                }\n'
     '                auto layer_func = [layer_out = out, layer, copies = layer_copies, info = layer_info,\n'
     '                                   input = std::move(layer_input), async_decode = layer_ptr]() mutable {\n'
     '                    async_decode->decoded_data.resize_destructive(layer_out);\n'
     '                    std::span copies_span{copies.data(), copies.size()};\n'
     '                    ConvertImage(input, info, async_decode->decoded_data, copies_span);\n'
     '                    for (auto& copy : copies) copy.image_subresource.base_layer = layer;\n'
     '                    async_decode->copies = std::move(copies);\n'
     '                    async_decode->complete.store(true, std::memory_order_release);\n'
     '                };\n'
     '                texture_decode_worker.QueueWork(std::move(layer_func));\n'
     '            }\n'
     '            if (zero) runtime.InsertUploadMemoryBarrier();\n'
     '            return;\n'
     '        }\n'
     '    }\n'
     '    image.flags |= ImageFlagBits::IsDecoding;\n'
     '    ++async_decodes_queued;\n'
     '    auto decode = std::make_unique<AsyncDecodeContext>();'),
    # A layer's result is staged at its own size, and the image stays decoding until its last
    # layer has landed (the collector does not take an image that is decoding).
    ('        auto staging = runtime.UploadStagingBuffer(MapSizeBytes(image));\n'
     '        std::memcpy(staging.mapped_span.data(), async_decode->decoded_data.data(),',
     '        auto staging = runtime.UploadStagingBuffer(async_decode->decoded_data.size());\n'
     '        std::memcpy(staging.mapped_span.data(), async_decode->decoded_data.data(),'),
    ('        image.UploadMemory(staging, async_decode->copies);\n'
     '        image.flags &= ~ImageFlagBits::IsDecoding;',
     '        image.UploadMemory(staging, async_decode->copies);\n'
     '        if (std::none_of(async_decodes.begin(), async_decodes.end(), [&](const auto& other) {\n'
     '                return other.get() != async_decode && other->image_id == async_decode->image_id &&\n'
     '                       !other->cancelled;\n'
     '            }))\n'
     '            image.flags &= ~ImageFlagBits::IsDecoding;'),
    # A whole-image decode too: its buffer at the converted layout's size where that is above
    # the estimate (TickAsyncDecode then drops the result with an error instead of the decode
    # writing past the buffer).
    ('    const size_t out_size = MapSizeBytes(image);\n',
     '    auto converted_layout = copies;\n'
     '    ConvertedLayout(image.info, converted_layout);\n'
     '    const size_t out_size = converted_layout.empty() ? MapSizeBytes(image) :\n'
     '        (std::max<size_t>)(MapSizeBytes(image), converted_layout.back().buffer_offset +\n'
     '                                                converted_layout.back().buffer_size);\n'),
]
# The decode thread hands a result over with complete's release store, the GPU thread takes it with
# an acquire load: no mutex per decode. Each one's std::mutex was created at its first lock, a
# texture array's course load queues one per layer (121 for a terrain array), and in 4 of ~24 races
# a decode thread's lock failed ("mutex lock failed: Invalid argument", 325-328 s into a session).
texture_decode_handoff = [
    ('        // TODO: Do we need this lock?\n'
     '        std::unique_lock lock{async_decode->mutex};\n'
     '        async_decode->copies = std::move(copies);\n'
     '        async_decode->complete = true;\n',
     '        async_decode->copies = std::move(copies);\n'
     '        async_decode->complete.store(true, std::memory_order_release);\n'),
    ('        {\n'
     '            // Release the lock before the context is erased below: unlocking it afterwards wrote\n'
     '            // into freed memory (heap corruption, crashes minutes into title 1).\n'
     '            std::unique_lock lock{async_decode->mutex};\n'
     '            if (!async_decode->complete) {\n'
     '                ++i;\n'
     '                continue;\n'
     '            }\n'
     '        }\n',
     '        // After this the context is the GPU thread\'s alone: the decode thread\'s store was\n'
     '        // its last touch of it.\n'
     '        if (!async_decode->complete.load(std::memory_order_acquire)) {\n'
     '            ++i;\n'
     '            continue;\n'
     '        }\n'),
]
# An ASTC texture kept as BC3 and converted on the CPU (accelerate_astc=CpuAsynchronous) is converted
# in the background in a busy frame only, and in place on a light screen (headless/astc_policy.h).
# The frame is counted where the texture cache ends its own; draws in RasterizerVulkan::PrepareDraw.
texture_astc_policy = [
    ('    if (True(image.flags & ImageFlagBits::AsynchronousDecode)) {\n        QueueAsyncDecode(image, image_id);\n        return;\n    }',
     '    if (True(image.flags & ImageFlagBits::AsynchronousDecode) && ::Eden::AstcPolicy::InBackground()) {\n        QueueAsyncDecode(image, image_id);\n        return;\n    }'),
    ('    TickAsyncDecode();\n    TickAsyncUnswizzle();', '    ::Eden::AstcPolicy::Frame();\n    TickAsyncDecode();\n    TickAsyncUnswizzle();'),
]
adapt('src/video_core/texture_cache/texture_cache.h',
      'vulkan-cache/video_core/texture_cache/texture_cache.h', [
    ('namespace VideoCommon {', '#include "performance.h"\n#include "common/scope_exit.h"\nnamespace Vulkan { class TextureCacheRuntime; }\n\nnamespace VideoCommon {'),
    # Every place an image is registered at is marked before the texture cache can find the
    # image there (headless/image_pages.h): the two inserts below are the only ones.
    ('#include "common/scope_exit.h"\nnamespace Vulkan { class TextureCacheRuntime; }',
     '#include "image_pages.h"\n#include "common/scope_exit.h"\nnamespace Vulkan { class TextureCacheRuntime; }'),
    ('        auto map_id =\n            slot_map_views.insert(image.gpu_addr, image.cpu_addr, image.guest_size_bytes, image_id);',
     '        ::Eden::ImagePages::Mark(image.cpu_addr, image.guest_size_bytes);\n'
     '        auto map_id =\n            slot_map_views.insert(image.gpu_addr, image.cpu_addr, image.guest_size_bytes, image_id);'),
    ('            auto map_id = slot_map_views.insert(gpu_addr, cpu_addr, size, image_id);',
     '            ::Eden::ImagePages::Mark(cpu_addr, size);\n'
     '            auto map_id = slot_map_views.insert(gpu_addr, cpu_addr, size, image_id);'),
    (download_prefix, download_prefix + (port / 'vulkan_download_batch.inc').read_text() + '\n'),
    (gc_original, gc),
    # RADV reports all Vulkan allocations, not just cached textures. A game's
    # measured ~1.9 GiB working set triggered dirty eviction at the old 1.6 GiB
    # threshold despite a 4 GiB budget. Retain 40% headroom; keep the original
    # critical threshold and minimum 1 GiB spacing. OpenGL remains unchanged.
    ('const s64 min_vacancy_expected = (6 * mem_threshold) / 10;',
     'const s64 min_vacancy_expected =\n            ((std::is_same_v<Runtime, Vulkan::TextureCacheRuntime> ? 4 : 6) * mem_threshold) / 10;'),
] + texture_costs + texture_partial + texture_layers + texture_decode_handoff + texture_astc_policy)

# The four calls inside bind_geometry and the three inside bind_update. They sit in buffer_cache.h
# rather than in Configure, and both spend their time in SynchronizeBuffer's page walk over the
# memory tracker - once for the index buffer, once per enabled vertex buffer, once per per-stage
# segment, every draw, dirty or not. Which of the seven that is cannot be decided by reading it:
# the texture-buffer loop looked like the cost of bind and measured 2.8%.
adapt('src/video_core/buffer_cache/buffer_cache.h',
      'vulkan-cache/video_core/buffer_cache/buffer_cache.h', [
    ('        if (buffer_bytes >= 1_GiB) {\n'
     '            RunGarbageCollector(buffer_bytes >= 2_GiB);\n'
     '        }\n',
     '        const auto pressure = ::Eden::Performance::graphics_memory_probe.load(std::memory_order_relaxed);\n'
     '        const bool memory_short = frame_tick >= 60 && pressure && pressure();\n'
     '        if (buffer_bytes >= 1_GiB || memory_short) {\n'
     '            RunGarbageCollector(memory_short || buffer_bytes >= 2_GiB);\n'
     '        }\n'),
    ('++frames % 600 == 0', '++frames % 60 == 0'),
    ('#include "video_core/buffer_cache/buffer_cache_base.h"',
     '#include "video_core/buffer_cache/buffer_cache_base.h"\n#include "performance.h"'),
    ('void BufferCache<P>::BindHostGeometryBuffers(bool is_indexed) {',
     'void BufferCache<P>::BindHostGeometryBuffers(bool is_indexed) {\n'
     '    long long since_geo = ::Eden::Performance::MarkStart();'),
    ('    BindHostVertexBuffers();\n'
     '    BindHostTransformFeedbackBuffers();\n'
     '    if (current_draw_indirect) {\n'
     '        BindHostDrawIndirectBuffers();\n'
     '    }\n'
     '}',
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_index, since_geo);\n'
     '    BindHostVertexBuffers();\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_vertex, since_geo);\n'
     '    BindHostTransformFeedbackBuffers();\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_xf, since_geo);\n'
     '    if (current_draw_indirect) {\n'
     '        BindHostDrawIndirectBuffers();\n'
     '    }\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::geo_indirect, since_geo);\n'
     '}'),
    ('void BufferCache<P>::DoUpdateGraphicsBuffers(bool is_indexed) {',
     'void BufferCache<P>::DoUpdateGraphicsBuffers(bool is_indexed) {\n'
     '    long long since_upd = ::Eden::Performance::MarkStart();'),
    ('        UpdateTransformFeedbackBuffers();\n'
     '        for (size_t stage = 0; stage < NUM_STAGES; ++stage) {\n'
     '            UpdateUniformBuffers(stage);\n'
     '            UpdateStorageBuffers(stage);\n'
     '            UpdateTextureBuffers(stage);\n'
     '        }\n'
     '        if (current_draw_indirect) {\n'
     '            UpdateDrawIndirect();\n'
     '        }',
     '        UpdateTransformFeedbackBuffers();\n'
     '        ::Eden::Performance::Mark(::Eden::Performance::upd_state, since_upd);\n'
     '        for (size_t stage = 0; stage < NUM_STAGES; ++stage) {\n'
     '            UpdateUniformBuffers(stage);\n'
     '            UpdateStorageBuffers(stage);\n'
     '            UpdateTextureBuffers(stage);\n'
     '        }\n'
     '        ::Eden::Performance::Mark(::Eden::Performance::upd_stage, since_upd);\n'
     '        if (current_draw_indirect) {\n'
     '            UpdateDrawIndirect();\n'
     '        }\n'
     '        ::Eden::Performance::Mark(::Eden::Performance::upd_indirect, since_upd);'),
])

# These costs sit outside vkCreateGraphicsPipelines: guest shader translation,
# worker readiness, and background serialization can block different threads.
# The six phases inside Configure. draw_config is all of Configure but SetEngine's two pointer
# assignments, and ConfigureImpl is ~210 lines; without these the 2.78 us a draw spends
# configuring arrives as one number with nothing to aim at. One running clock, marked at the
# boundaries the source already has.
configure_phases = [
    ('bool GraphicsPipeline::ConfigureImpl(bool is_indexed) {',
     'bool GraphicsPipeline::ConfigureImpl(bool is_indexed) {\n'
     '    long long since = ::Eden::Performance::MarkStart();'),
    ('    buffer_cache.SetUniformBuffersState(enabled_uniform_buffer_masks, &uniform_buffer_sizes);',
     '    buffer_cache.SetUniformBuffersState(enabled_uniform_buffer_masks, &uniform_buffer_sizes);\n'
     '    // Sampler and image tables synced, uniform-buffer state set.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_sync, since);'),
    ('        texture_cache.FillImageViews(std::span(views.data(), views.size()), false,\n'
     '                                     Spec::has_images);\n    }\n',
     '        texture_cache.FillImageViews(std::span(views.data(), views.size()), false,\n'
     '                                     Spec::has_images);\n    }\n'
     '    // Every texture handle read out of guest memory, every sampler and view filled.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_read, since);\n'
     '    long long since_bind = ::Eden::Performance::MarkStart();'),
    ('    if (regs.transform_feedback_enabled != 0) {',
     '    // The texture-buffer loop over every enabled stage.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::bind_tbo, since_bind);\n'
     '    if (regs.transform_feedback_enabled != 0) {'),
    ('    buffer_cache.UpdateGraphicsBuffers(is_indexed);',
     '    buffer_cache.UpdateGraphicsBuffers(is_indexed);\n'
     '    // Index, vertex, transform-feedback and per-stage buffer updates done.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::bind_update, since_bind);'),
    ('    buffer_cache.BindHostGeometryBuffers(is_indexed);',
     '    buffer_cache.BindHostGeometryBuffers(is_indexed);\n'
     '    // Storage, texture and geometry buffers bound.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::bind_geometry, since_bind);\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_bind, since);'),
    ('    texture_cache.UpdateRenderTargets(false);',
     '    // Descriptor queue acquired, image descriptors pushed, uploads barriered.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_desc, since);\n'
     '    texture_cache.UpdateRenderTargets(false);'),
    ('    if (IsBuilt() && !pipeline) {',
     '    // Render targets current, feedback loops checked.\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_rt, since);\n'
     '    if (IsBuilt() && !pipeline) {'),
    ('    return ConfigureDraw(rescaling, render_area);',
     '    // Compare the descriptor payload against last draw\'s and record the bindings.\n'
     '    const bool configured = ConfigureDraw(rescaling, render_area);\n'
     '    ::Eden::Performance::Mark(::Eden::Performance::cfg_draw, since);\n'
     '    return configured;'),
]
for name in ('vk_graphics_pipeline', 'vk_compute_pipeline'):
    anchors = [
        ('#include <algorithm>', '#include <algorithm>\n#include "performance.h"'),
        ('            std::unique_lock lock{build_mutex};',
         '            auto timer = Eden::Performance::VulkanTimer(16);\n            std::unique_lock lock{build_mutex};'),
    ]
    if name == 'vk_graphics_pipeline':
        anchors += configure_phases
    adapt(f'src/video_core/renderer_vulkan/{name}.cpp', name + '_cost.cpp', anchors)
shader_costs = [
    ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include <unordered_set>\n'
     '#include "performance.h"\n#include "dev_vulkan.h"\n#include "diagnostics.h"'),
    # Development crash probes: report each pipeline's first use (stage hashes) to klog,
    # which survives a GPU fault that kills the title.
    ('    current_pipeline = pipeline.get();\n    return BuiltPipeline(current_pipeline);\n}',
     '    current_pipeline = pipeline.get();\n'
     '    if (::Eden::DevVulkan::trace_pipelines) {\n'
     '        static std::unordered_set<const void*> traced;\n'
     '        if (traced.insert(current_pipeline).second) {\n'
     '            const auto& h = graphics_key.unique_hashes;\n'
     '            char text[256];\n'
     '            std::snprintf(text, sizeof(text), "G%zu p=%p new=%d va=%llx vb=%llx tc=%llx te=%llx gs=%llx fs=%llx",\n'
     '                          traced.size(), (const void*)current_pipeline, int(is_new), (unsigned long long)h[0], (unsigned long long)h[1],\n'
     '                          (unsigned long long)h[2], (unsigned long long)h[3], (unsigned long long)h[4],\n'
     '                          (unsigned long long)h[5]);\n'
     '            ::Eden::Report("pipeline", text);\n'
     '            LOG_INFO(Render_Vulkan, "EDEN_PIPELINE_FIRST_USE {}", text);\n'
     '        }\n'
     '    }\n'
     '    return BuiltPipeline(current_pipeline);\n}'),
    ('    if (!is_new) {\n        return pipeline.get();\n    }\n'
     '    try {\n        pipeline = CreateComputePipeline(key, shader);\n'
     '    } catch (const Shader::TensorRejected&) {\n        compute_cache.erase(pair);\n'
     '        rejected_tensor_pipelines.emplace(key, std::current_exception());\n        throw;\n    }\n'
     '    return pipeline.get();\n}',
     '    if (is_new) {\n'
     '        try {\n            pipeline = CreateComputePipeline(key, shader);\n'
     '        } catch (const Shader::TensorRejected&) {\n            compute_cache.erase(pair);\n'
     '            rejected_tensor_pipelines.emplace(key, std::current_exception());\n            throw;\n        }\n'
     '    }\n'
     '    if (::Eden::DevVulkan::trace_pipelines && pipeline) {\n'
     '        static std::unordered_set<const void*> traced;\n'
     '        if (traced.insert(pipeline.get()).second) {\n'
     '            char text[160];\n'
     '            std::snprintf(text, sizeof(text), "C%zu p=%p new=%d cs=%llx shared=%u wg=%u,%u,%u", traced.size(),\n'
     '                          (const void*)pipeline.get(), int(is_new), (unsigned long long)key.unique_hash, unsigned(key.shared_memory_size),\n'
     '                          unsigned(key.workgroup_size[0]), unsigned(key.workgroup_size[1]),\n'
     '                          unsigned(key.workgroup_size[2]));\n'
     '            ::Eden::Report("pipeline", text);\n'
     '            LOG_INFO(Render_Vulkan, "EDEN_PIPELINE_FIRST_USE {}", text);\n'
     '        }\n'
     '    }\n'
     '    return pipeline.get();\n}'),
    ('std::unique_ptr<GraphicsPipeline> PipelineCache::CreateGraphicsPipeline() {',
     'std::unique_ptr<GraphicsPipeline> PipelineCache::CreateGraphicsPipeline() {\n    auto timer = Eden::Performance::VulkanTimer(17);'),
    ('    const ComputePipelineCacheKey& key, const ShaderInfo* shader) {',
     '    const ComputePipelineCacheKey& key, const ShaderInfo* shader) {\n    auto timer = Eden::Performance::VulkanTimer(17);'),
    ('                                                 u32 cache_version) try {',
     '                                                 u32 cache_version) try {\n    auto timer = Eden::Performance::VulkanTimer(18);'),
    ('}\n\nPipelineCache::~PipelineCache()',
     '    if (Eden::Performance::vulkan_cost_enabled.load(std::memory_order_relaxed))\n'
     '        std::printf("EDEN_VULKAN_DYNAMIC blend=%u enables=%u vertex=%u\\n",\n'
     '                    unsigned(dynamic_features.has_extended_dynamic_state_3_blend),\n'
     '                    unsigned(dynamic_features.has_extended_dynamic_state_3_enables),\n'
     '                    unsigned(dynamic_features.has_dynamic_vertex_input));\n'
     '}\n\nPipelineCache::~PipelineCache()'),
    # Static vertex input (no attribute) for pipelines whose vertex shader reads no vertex attribute
    # (dev_vulkan.h static_vertex_input): with VK_EXT_vertex_input_dynamic_state the console's
    # driver drew them wrong (a full-screen triangle from the vertex index covering a game's
    # cutscenes with a flat colour). Building a pipeline records whether its vertex shader reads
    # attributes; the static key has no attribute, so the disk cache keeps loading it.
    ('#include "video_core/engines/maxwell_3d.h"',
     '#include "video_core/engines/maxwell_3d.h"\n#include "video_core/dirty_flags.h"\n'
     '#include <atomic>\n#include <mutex>\n#include <set>\n#include <unordered_set>\n'
     '#include "video_core/renderer_vulkan/vk_state_tracker.h"'),
    ('GraphicsPipeline* PipelineCache::CurrentGraphicsPipeline() {',
     """namespace {
std::mutex eden_attributeless_mutex;
using EdenVertexIdentity = std::array<u64, 2>;
std::set<EdenVertexIdentity> eden_attributeless_vertex;
std::atomic<u32> eden_attributeless_generation{0};
bool eden_static_vertex_input_bound = false;

void EdenRecordAttributeless(EdenVertexIdentity vertex_hash, const Shader::Info& info) {
    for (size_t index = 0; index < Shader::IR::NUM_GENERICS; ++index) {
        if (info.loads.Generic(index)) {
            return;
        }
    }
    std::scoped_lock lock{eden_attributeless_mutex};
    if (eden_attributeless_vertex.insert(vertex_hash).second) {
        eden_attributeless_generation.fetch_add(1, std::memory_order_release);
    }
}

// GPU thread: whether the vertex shader reads no attribute, as far as built pipelines tell.
bool EdenAttributeless(EdenVertexIdentity vertex_hash) {
    static EdenVertexIdentity last_hash{};
    static u32 last_generation = ~0u;
    static bool last_result = false;
    const u32 generation = eden_attributeless_generation.load(std::memory_order_acquire);
    if (vertex_hash != last_hash || generation != last_generation) {
        std::scoped_lock lock{eden_attributeless_mutex};
        last_hash = vertex_hash;
        last_generation = generation;
        last_result = eden_attributeless_vertex.contains(vertex_hash);
    }
    return last_result;
}

// A static key without attributes (what the loader takes back from the disk cache).
bool EdenAttributelessStaticKey(const FixedPipelineState& state) {
    return state.dynamic_vertex_input == 0 &&
           std::ranges::none_of(state.attributes,
                                [](const auto& attribute) { return attribute.raw != 0; });
}
} // Anonymous namespace

GraphicsPipeline* PipelineCache::CurrentGraphicsPipeline() {"""),
    ('    graphics_key.state.Refresh(*maxwell3d, dynamic_features);\n',
     """    graphics_key.state.Refresh(*maxwell3d, dynamic_features);
    const bool static_vertex_input = graphics_key.state.dynamic_vertex_input != 0 &&
                                     ::Eden::DevVulkan::static_vertex_input &&
                                     EdenAttributeless({graphics_key.unique_hashes[0], graphics_key.unique_hashes[1]});
    if (static_vertex_input) {
        // Instance-rate streams as the registers set them (with every stream per vertex the
        // driver drew these wrong too); no attribute, so the key does not depend on what the
        // registers hold for attributes the shader does not read.
        const auto& regs = maxwell3d->regs;
        graphics_key.state.dynamic_vertex_input.Assign(0);
        graphics_key.state.enabled_divisors = 0;
        graphics_key.state.attributes = {};
        graphics_key.state.binding_divisors = {};
        for (size_t index = 0; index < Maxwell::NumVertexArrays; ++index) {
            const bool is_enabled = regs.vertex_stream_instances.IsInstancingEnabled(index);
            graphics_key.state.binding_divisors[index] =
                is_enabled ? regs.vertex_streams[index].frequency : 0;
            graphics_key.state.enabled_divisors |= (is_enabled ? u64{1} : 0) << index;
        }
        // The union's attribute_types now holds divisors: the next dynamic draw recomputes them
        // and sets its vertex input again (a static pipeline replaced it).
        maxwell3d->dirty.flags[Dirty::VertexInput] = true;
    }
    if (static_vertex_input && !eden_static_vertex_input_bound) {
        // Rebind the vertex buffers with their strides, dynamic state of the static pipeline.
        auto& flags = maxwell3d->dirty.flags;
        flags[VideoCommon::Dirty::VertexBuffers] = true;
        for (size_t index = 0; index < Maxwell::NumVertexArrays; ++index) {
            flags[VideoCommon::Dirty::VertexBuffer0 + index] = true;
        }
    }
    eden_static_vertex_input_bound = static_vertex_input;
"""),
    ('            (key.state.dynamic_vertex_input != 0) !=\n                dynamic_features.has_dynamic_vertex_input) {',
     '            ((key.state.dynamic_vertex_input != 0) !=\n'
     '                 dynamic_features.has_dynamic_vertex_input &&\n'
     '             !(dynamic_features.has_dynamic_vertex_input &&\n'
     '               ::Eden::DevVulkan::static_vertex_input &&\n'
     '               EdenAttributelessStaticKey(key.state)))) {'),
    ('    std::array<const Shader::Info*, Maxwell::MaxShaderStage> infos{};\n',
     '    if (uses_vertex_b) {\n'
     '        EdenRecordAttributeless({key.unique_hashes[0], key.unique_hashes[1]}, programs[1].info);\n'
     '    }\n'
     '    std::array<const Shader::Info*, Maxwell::MaxShaderStage> infos{};\n'),
]
# Time both graphics and compute preparation, without modifying the compiler or
# reading guest state on another thread. These intervals overlap shader_prepare.
shader_source = (source / 'src/video_core/renderer_vulkan/vk_pipeline_cache.cpp').read_text()
shader_source = shader_source.replace('    auto it{graphics_cache.find(graphics_key)};\n    if (it == graphics_cache.end()) {',
                                      '    auto it{graphics_cache.find(graphics_key)};\n    const bool is_new = it == graphics_cache.end();\n    if (is_new) {')
for expression, index, count in (
    ('main_pools.ReleaseContents()', 19, 2),
    ('TranslateProgram(pools.inst, pools.block, env, cfg, host_info)', 21, 3),
    ('EmitSPIRV(profile, runtime_info, program, binding)', 22, 1),
    ('EmitSPIRV(profile, program)', 22, 1),
    ('BuildShader(device, code)', 23, 2),
):
    if shader_source.count(expression) != count:
        raise RuntimeError(f'Shader cost anchor changed: {expression}')
    shader_source = shader_source.replace(expression,
        f'([&] {{ auto cost = Eden::Performance::VulkanTimer({index}); '
        f'return {expression}; }}())')
for statement in (
    'Shader::Maxwell::Flow::CFG cfg(env, pools.flow_block, cfg_offset, index == 0);',
    'Shader::Maxwell::Flow::CFG cfg{env, pools.flow_block, env.StartAddress()};',
):
    expected = 2 if 'cfg_offset' in statement else 1  # graphics exception dump too
    if shader_source.count(statement) != expected:
        raise RuntimeError(f'Shader CFG anchor changed: {statement}')
    shader_source = shader_source.replace(statement,
        'auto cfg_cost = Eden::Performance::VulkanTimer(20);\n        ' + statement +
        '\n        cfg_cost.reset();')
for old, new in shader_costs:
    if old == '}\n\nPipelineCache::~PipelineCache()' and old not in shader_source:
        boundary = '}\n\nnamespace {\n// <title>/vulkan_used.bin'
        new = new.replace(old, boundary)
        old = boundary
    if old.startswith('    current_pipeline = pipeline.get();') and old not in shader_source:
        mark_used = '    current_pipeline->MarkUsed(frame_count);\n'
        old = old.replace('    return BuiltPipeline', mark_used + '    return BuiltPipeline')
        new = new.replace('    current_pipeline = pipeline.get();\n',
                          '    current_pipeline = pipeline.get();\n' + mark_used)
    if 'Shader::TensorRejected' in old and 'Shader::TensorRejected' not in shader_source:
        old = ('    if (!is_new) {\n        return pipeline.get();\n    }\n'
               '    pipeline = CreateComputePipeline(key, shader);\n    return pipeline.get();\n}')
        trace = new[new.index('    if (::Eden::DevVulkan::trace_pipelines && pipeline)'):]
        new = ('    if (is_new) {\n        pipeline = CreateComputePipeline(key, shader);\n    }\n' + trace)
    if shader_source.count(old) != 1:
        raise RuntimeError(f'Shader pipeline anchor changed: {old[:60]}')
    shader_source = shader_source.replace(old, new)
# The driver's pipeline cache file (vulkan_pipelines.bin). Eden rewrote it in place (the source
# now writes a temporary file and renames it; this keeps a failed copy from removing the cache): opening it
# emptied it, and a large game's cache takes seconds to write, so a session that ended during the
# write (the ten-second limit on leaving a game, a crash, rest mode) left it empty or cut off, and
# every pipeline was compiled again at the next start (ProsperoEden #170). It is now written beside the old
# file and renamed over it once whole, and leaving a game rewrites it only when pipelines were
# added since it was last written.
# The anchors include the timer the cost patches above add to the function.
pipeline_cache_writes = [
    ('PipelineCache::~PipelineCache() {\n'
     '    if (use_vulkan_pipeline_cache && !vulkan_pipeline_cache_filename.empty()) {\n'
     '        SerializeVulkanPipelineCache(vulkan_pipeline_cache_filename, vulkan_pipeline_cache,\n'
     '                                     CACHE_VERSION);\n'
     '    }\n'
     '}\n',
     'PipelineCache::~PipelineCache() {\n'
     '    if (use_vulkan_pipeline_cache && !vulkan_pipeline_cache_filename.empty()) {\n'
     '        // A flush still being written would race this one for the same file.\n'
     '        serialization_thread.WaitForRequests();\n'
     '        // Written after the game loaded and at each flush: with no pipeline added since,\n'
     '        // the file already holds everything, and writing it again only delays leaving.\n'
     '        const bool written = last_flush.time_since_epoch().count() != 0;\n'
     '        if (!written || pipelines_since_flush != 0) {\n'
     '            SerializeVulkanPipelineCache(vulkan_pipeline_cache_filename, vulkan_pipeline_cache,\n'
     '                                         CACHE_VERSION);\n'
     '        }\n'
     '    }\n'
     '}\n'),
    ('''void PipelineCache::SerializeVulkanPipelineCache(const std::filesystem::path& filename,
                                                 const vk::PipelineCache& pipeline_cache,
                                                 u32 cache_version) try {
    auto timer = Eden::Performance::VulkanTimer(18);
    // Written beside the cache and renamed over it: the emulator stopped mid-write left the cache
    // empty, and every later boot failed to read it.
    auto temporary = filename;
    temporary += ".tmp";
    std::ofstream file(temporary, std::ios::binary);
    file.exceptions(std::ifstream::failbit);
    if (!file.is_open()) {
        LOG_ERROR(Common_Filesystem, "Failed to open Vulkan driver pipeline cache file {}",
                  Common::FS::PathToUTF8String(filename));
        return;
    }
    file.write(VULKAN_CACHE_MAGIC_NUMBER.data(), VULKAN_CACHE_MAGIC_NUMBER.size())
        .write(reinterpret_cast<const char*>(&cache_version), sizeof(cache_version));

    size_t cache_size = 0;
    std::vector<char> cache_data;
    if (pipeline_cache) {
        pipeline_cache.Read(&cache_size, nullptr);
        cache_data.resize(cache_size);
        pipeline_cache.Read(&cache_size, cache_data.data());
    }
    file.write(cache_data.data(), cache_size);
    file.close();
    std::error_code error;
    std::filesystem::rename(temporary, filename, error);
    if (error) {
        LOG_ERROR(Common_Filesystem, "Failed to replace Vulkan driver pipeline cache file {}: {}",
                  Common::FS::PathToUTF8String(filename), error.message());
        std::filesystem::remove(temporary, error);
        return;
    }

    LOG_INFO(Render_Vulkan, "Vulkan driver pipelines cached at: {}",
             Common::FS::PathToUTF8String(filename));

} catch (const std::ios_base::failure& e) {
    LOG_ERROR(Common_Filesystem, "{}", e.what());
    if (!Common::FS::RemoveFile(filename)) {
        LOG_ERROR(Common_Filesystem, "Failed to delete Vulkan driver pipeline cache file {}",
                  Common::FS::PathToUTF8String(filename));
    }
}
''',
'''void PipelineCache::SerializeVulkanPipelineCache(const std::filesystem::path& filename,
                                                 const vk::PipelineCache& pipeline_cache,
                                                 u32 cache_version) {
    auto timer = Eden::Performance::VulkanTimer(18);  // as the port times the write it replaces
    // Written whole beside the cache, then renamed over it: until the rename the previous cache
    // stays as it was, and a write that fails or is cut short leaves it alone.
    auto partial = filename;
    partial += ".part";
    std::error_code error;
    try {
        size_t cache_size = 0;
        std::vector<char> cache_data;
        if (pipeline_cache) {
            pipeline_cache.Read(&cache_size, nullptr);
            cache_data.resize(cache_size);
            pipeline_cache.Read(&cache_size, cache_data.data());
        }
        {
            std::ofstream file(partial, std::ios::binary | std::ios::trunc);
            if (!file.is_open()) {
                LOG_ERROR(Common_Filesystem, "Failed to open Vulkan driver pipeline cache file {}",
                          Common::FS::PathToUTF8String(partial));
                return;
            }
            file.exceptions(std::ofstream::failbit | std::ofstream::badbit);
            file.write(VULKAN_CACHE_MAGIC_NUMBER.data(), VULKAN_CACHE_MAGIC_NUMBER.size())
                .write(reinterpret_cast<const char*>(&cache_version), sizeof(cache_version))
                .write(cache_data.data(), static_cast<std::streamsize>(cache_size));
            file.flush();
        }
        std::filesystem::rename(partial, filename, error);
        if (error) {
            LOG_ERROR(Common_Filesystem, "Failed to replace Vulkan driver pipeline cache file {}: {}",
                      Common::FS::PathToUTF8String(filename), error.message());
            std::filesystem::remove(partial, error);
            return;
        }
        LOG_INFO(Render_Vulkan, "Vulkan driver pipelines cached at: {}",
                 Common::FS::PathToUTF8String(filename));
    } catch (const std::ios_base::failure& e) {
        LOG_ERROR(Common_Filesystem, "{}", e.what());
        std::filesystem::remove(partial, error);
    } catch (const std::bad_alloc&) {
        // A copy of a large cache did not fit in memory: keep the one on disk.
        LOG_ERROR(Render_Vulkan, "No memory to copy the Vulkan driver pipeline cache; kept the previous file");
        std::filesystem::remove(partial, error);
    }
}
'''),
]
for old, new in pipeline_cache_writes:
    if shader_source.count(old) != 1:
        raise RuntimeError(f'Pipeline cache write anchor changed: {old[:60]}')
    shader_source = shader_source.replace(old, new)
# Task/mesh draw diagnostics (mesh_diag.h, EDEN_MESH_DIAG): the SPIR-V of every stage of a
# task/mesh pipeline, as the driver receives it.
for old, new in (
    ('#include <algorithm>', '#include <algorithm>\n#include "mesh_diag.h"'),
    ('        device.SaveShader(code);\n',
     '        device.SaveShader(code);\n'
     '        if (infos[0] != nullptr && (infos[0]->is_task || infos[0]->is_mesh)) {\n'
     '            static constexpr std::array mesh_stage_names{"vertex_or_task", "tess_control", "tess_eval_or_mesh",\n'
     '                                                         "geometry", "fragment"};\n'
     '            ::Eden::MeshDiag::DumpSpirv(key.unique_hashes[index],\n'
     '                                        program.info.is_task ? "task" : program.info.is_mesh ? "mesh"\n'
     '                                                                                     : mesh_stage_names[stage_index],\n'
     '                                        std::span<const u32>(code.data(), code.size()));\n'
     '        }\n'),
):
    if shader_source.count(old) != 1:
        raise RuntimeError(f'Mesh diagnostic anchor changed: {old[:60]}')
    shader_source = shader_source.replace(old, new, 1)
shader_output = output / 'vk_pipeline_cache_cost.cpp'
if not shader_output.exists() or shader_output.read_text() != shader_source:
    shader_output.write_text(shader_source)

# Scope only the existing synchronous operations; never alter their ordering.
# Report these caller costs separately from the overlapping API totals.
import re
# Development GPU fault probes (dev_vulkan.h): multi-range storage buffers are
# the one path that binds sparse ranges of the console's device memory.
probes = {
    'vk_multi_range_buffer': [
        ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "diagnostics.h"'),
        ('    if (bind_result != VK_SUCCESS) {\n        return SparseBuffer{};\n    }',
         '    if (bind_result != VK_SUCCESS) {\n'
         '        if (::Eden::DevVulkan::trace_pipelines) ::Eden::Report("multirange", "sparse bind failed");\n'
         '        return SparseBuffer{};\n    }'),
        ('    retired.push_back(Retired{',
         '    if (::Eden::DevVulkan::trace_pipelines) {\n'
         '        static unsigned retires = 0;\n'
         '        if (++retires <= 64) {\n'
         '            char text[96];\n'
         '            std::snprintf(text, sizeof(text), "retire #%u sparse=%d tick=%llu", retires,\n'
         '                          int(bool(entry.sparse_handle)), (unsigned long long)scheduler.CurrentTick());\n'
         '            ::Eden::Report("multirange", text);\n'
         '        }\n'
         '    }\n'
         '    retired.push_back(Retired{'),
        ('    entries.emplace(key, std::move(entry));',
         '    if (::Eden::DevVulkan::trace_pipelines) {\n'
         '        static unsigned created = 0;\n'
         '        if (++created <= 256) {\n'
         '            char text[192];\n'
         '            std::snprintf(text, sizeof(text), "new #%u key=%llx total=%llu sources=%zu sparse=%d addr=%llx",\n'
         '                          created, (unsigned long long)key, (unsigned long long)total, sources.size(),\n'
         '                          int(bool(entry.sparse_handle)), (unsigned long long)entry.address);\n'
         '            ::Eden::Report("multirange", text);\n'
         '        }\n'
         '    }\n'
         '    entries.emplace(key, std::move(entry));'),
    ],
    'vk_buffer_cache': [
        ('#include <algorithm>', '#include <algorithm>\n#include "dev_vulkan.h"\n#include "submission_origin.h"'),
        ('void BufferCacheRuntime::Finish() {',
         'void BufferCacheRuntime::Finish() {\n'
         '    ::Eden::VulkanProbe::GpuSubmitOriginScope origin{__builtin_return_address(0), true};'),
        ('bool BufferCacheRuntime::BindMultiRangeStorageBuffer(u64 key, bool is_written) {\n',
         'bool BufferCacheRuntime::BindMultiRangeStorageBuffer(u64 key, bool is_written) {\n'
         '    if (::Eden::DevVulkan::disable_multi_range) {\n        return false;\n    }\n'),
    ],
    # With custom border colours off (the console default), report which clamp-to-border
    # samplers ask for a colour the three standard ones cannot give exactly.
    'vk_texture_cache': [
        ('void TextureCacheRuntime::Finish() {',
         'void TextureCacheRuntime::Finish() {\n'
         '    ::Eden::VulkanProbe::GpuSubmitOriginScope origin{__builtin_return_address(0), true};'),
        ('void TextureCacheRuntime::TranscodeAstcToBcn(\n'
         '    Image& image, const StagingBufferRef& map,\n'
         '    std::span<const VideoCommon::SwizzleParameters> swizzles) {\n',
         'void TextureCacheRuntime::TranscodeAstcToBcn(\n'
         '    Image& image, const StagingBufferRef& map,\n'
         '    std::span<const VideoCommon::SwizzleParameters> swizzles) {\n'
         '    ::Eden::Performance::Timer mkw_timer(::Eden::Performance::mkw_transcode);\n'),
        # Decode images can otherwise survive a long loading burst until TickFrame. At this
        # boundary every use of the input and output staging buffers has been recorded, so a
        # wait is safe (unlike a wait inside an arbitrary staging-buffer allocation).
        ('        .image = memory_allocator.CreateImage(VkImageCreateInfo{\n            .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,',
         '        .image = memory_allocator.CreateTemporaryImage(VkImageCreateInfo{\n            .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,'),
        # An ASTC-to-BC transcode decodes into an RGBA copy (4 bytes a pixel, every level) that
        # lives until the GPU has used it. A course load makes hundreds in a frame, which held
        # 6.7 GB of the pool the console's GPU shares with the CPU (ProsperoEden). So the copies in
        # flight are bounded, always: by bytes (256 MiB, or EDEN_PS5_TRANSCODE_PENDING_MIB),
        # waiting for the oldest copy only, and what is recorded is submitted every 32 MiB, so the
        # GPU works through the copies while more are recorded and the oldest is usually done
        # before the bound is reached. The tighter bound below still applies while memory is short.
        ('    PendingTranscode pending{\n        .tick = 0,\n        .bytes = decode_bytes,\n',
         '    {\n'
         '        using namespace Common::Literals;\n'
         '        static const u64 held_limit = [] {\n'
         '            const char* value = std::getenv("EDEN_PS5_TRANSCODE_PENDING_MIB");\n'
         '            return value != nullptr ? std::strtoull(value, nullptr, 10) << 20 : u64{256_MiB};\n'
         '        }();\n'
         '        while (!pending_transcodes.empty() && scheduler.IsFree(pending_transcodes.front().tick)) {\n'
         '            pending_transcodes.pop_front();\n'
         '        }\n'
         '        u64 held_bytes = 0, recorded_bytes = 0;\n'
         '        for (const PendingTranscode& held : pending_transcodes) {\n'
         '            held_bytes += held.bytes;\n'
         '            recorded_bytes += held.tick >= scheduler.CurrentTick() ? held.bytes : 0;\n'
         '        }\n'
         '        if (recorded_bytes >= 32_MiB) {\n'
         '            scheduler.Flush();\n'
         '        }\n'
         '        while (!pending_transcodes.empty() && held_bytes + decode_bytes > held_limit) {\n'
         '            scheduler.Wait(pending_transcodes.front().tick);\n'
         '            while (!pending_transcodes.empty() && scheduler.IsFree(pending_transcodes.front().tick)) {\n'
         '                held_bytes -= pending_transcodes.front().bytes;\n'
         '                pending_transcodes.pop_front();\n'
         '            }\n'
         '        }\n'
         '    }\n'
         '    PendingTranscode pending{\n        .tick = 0,\n        .bytes = decode_bytes,\n'),
        # A full GPU wait per texture was 5% of the split's back thread at Mario Kart World's
        # title (one streamed texture a frame, the pool always short): the decode images are now
        # reclaimed once the ones in flight exceed EDEN_PS5_TRANSCODE_PENDING_MIB (64), waiting
        # for the oldest only. EDEN_PS5_TRANSCODE_FINISH=1 restores the wait after each.
        ('    pending_transcodes.push_back(std::move(pending));\n',
         '    pending_transcodes.push_back(std::move(pending));\n'
         '    const auto memory_short = ::Eden::Performance::graphics_memory_probe.load(std::memory_order_relaxed);\n'
         '    if (memory_short && memory_short()) {\n'
         '        static const bool finish_each = [] {\n'
         '            const char* value = std::getenv("EDEN_PS5_TRANSCODE_FINISH");\n'
         '            return value != nullptr && value[0] == \'1\';\n'
         '        }();\n'
         '        static const u64 pending_limit = [] {\n'
         '            const char* value = std::getenv("EDEN_PS5_TRANSCODE_PENDING_MIB");\n'
         '            return (value != nullptr ? std::strtoull(value, nullptr, 10) : u64{64}) << 20;\n'
         '        }();\n'
         '        ::Eden::Performance::Timer finish_timer(::Eden::Performance::mkw_transcode_finish);\n'
         '        if (finish_each) {\n'
         '            scheduler.Finish();\n'
         '        }\n'
         '        while (!pending_transcodes.empty() && scheduler.IsFree(pending_transcodes.front().tick)) {\n'
         '            pending_transcodes.pop_front();\n'
         '        }\n'
         '        u64 pending_bytes = 0;\n'
         '        for (const auto& entry : pending_transcodes) {\n'
         '            pending_bytes += entry.bytes;\n'
         '        }\n'
         '        while (pending_bytes > pending_limit && pending_transcodes.size() > 1) {\n'
         '            scheduler.Wait(pending_transcodes.front().tick);\n'
         '            pending_bytes -= pending_transcodes.front().bytes;\n'
         '            pending_transcodes.pop_front();\n'
         '        }\n'
         '        staging_buffer_pool.ReleaseCompleted();\n'
         '    }\n'),
        ('#include <algorithm>', '#include <algorithm>\n#include <cstdio>\n#include "dev_vulkan.h"\n#include "diagnostics.h"\n#include "submission_origin.h"'),
        ('    has_custom_border_colors = samples_border && device.IsCustomBorderColorUsable();',
         '    if (::Eden::DevVulkan::trace_pipelines && samples_border) {\n'
         '        static std::vector<std::array<float, 4>> reported;\n'
         '        const auto report = [](const std::array<float, 4>& c, const char* kind) {\n'
         '            if (c == std::array<float, 4>{0, 0, 0, 0} || c == std::array<float, 4>{0, 0, 0, 1} ||\n'
         '                c == std::array<float, 4>{1, 1, 1, 1} || reported.size() >= 32 ||\n'
         '                std::find(reported.begin(), reported.end(), c) != reported.end()) return;\n'
         '            reported.push_back(c);\n'
         '            char text[128];\n'
         '            std::snprintf(text, sizeof(text), "non-standard %s border %g %g %g %g", kind, c[0], c[1],\n'
         '                          c[2], c[3]);\n'
         '            ::Eden::Report("sampler", text);\n'
         '        };\n'
         '        report(border_color, "linear");\n'
         '        if (tsc.srgb_conversion) report(srgb_border_color, "srgb");\n'
         '    }\n'
         '    has_custom_border_colors = samples_border && device.IsCustomBorderColorUsable();'),
    ],
}
for name, index, expected in (
    ('vk_fence_manager', 6, 1), ('vk_buffer_cache', 7, 6),  # three diagnostic mapped-readback probe waits
    ('vk_texture_cache', 8, 1), ('vk_swapchain', 9, 6),
    ('vk_descriptor_buffer', 10, 2), ('vk_multi_range_buffer', 11, 1),
    ('vk_compute_pass', 12, 1),
):
    relative = f'src/video_core/renderer_vulkan/{name}.cpp'
    destination = 'vulkan_swapchain.cpp' if name == 'vk_swapchain' else name + '_cost.cpp'
    # Preserve the earlier swapchain error-recovery adaptation.
    path = output / destination
    text = path.read_text() if name == 'vk_swapchain' else (source / relative).read_text()
    pattern = r'(?m)^( +)(scheduler\.(?:Wait|Finish)\([^\n]*\);)$'
    matches = list(re.finditer(pattern, text))
    if len(matches) != expected:
        raise RuntimeError(f'Wait-site anchors changed: {name}: {len(matches)} != {expected}')
    text = re.sub(pattern, lambda m: m[1] + '{ auto timer = Eden::Performance::VulkanTimer('
                  + str(index) + '); ' + m[2] + ' }', text)
    if name == 'vk_compute_pass':
        # ASTC decode has no CPU consumer. Its existing image barrier orders
        # later shaders, and staging/descriptor reuse is protected by ticks.
        text = text.replace('VulkanTimer(12); scheduler.Finish();',
                            'VulkanTimer(12); scheduler.Flush();')
    for probe_old, probe_new in probes.get(name, []):
        if text.count(probe_old) != 1:
            raise RuntimeError(f'Probe anchor changed: {name}: {probe_old[:60]}')
        text = text.replace(probe_old, probe_new)
    text = '#include "performance.h"\n' + text
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
# Eden loads these shaders when the renderer starts, whether frames are to be generated or not:
# the reasons are only written when the setting is on, or every session without the user's
# Lossless.dll would say so.
adapt('src/video_core/renderer_vulkan/present/lsfg_shaders.cpp', 'vulkan_lsfg_shaders.cpp', [
    ('#include "video_core/vulkan_common/vulkan_device.h"',
     '#include <string>\n#include "common/settings.h"\n#include "video_core/vulkan_common/vulkan_device.h"\n#include "diagnostics.h"'),
    ("""    if (!device.IsVulkanMemoryModelSupported() || !device.HasNullDescriptor() ||
        !device.IsFloat16Supported()) {
        return;
    }""",
     """    const bool wanted = Settings::values.frame_gen.GetValue();
    if (!device.IsVulkanMemoryModelSupported() || !device.HasNullDescriptor() ||
        !device.IsFloat16Supported()) {
        if (wanted)
            Eden::Report("framegen",
                         ("device features: vulkanMemoryModel=" +
                          std::to_string(device.IsVulkanMemoryModelSupported()) + " nullDescriptor=" +
                          std::to_string(device.HasNullDescriptor()) + " float16=" +
                          std::to_string(device.IsFloat16Supported()) +
                          " -- all are required, and dev-settings.txt null_descriptor=off turns "
                          "the second one off").c_str());
        return;
    }"""),
    ("""    VideoCore::FrameGen::ShaderModules code;
    if (VideoCore::FrameGen::LoadShaderModules(code) != VideoCore::FrameGen::LosslessStatus::Ok) {
        return;
    }""",
     """    VideoCore::FrameGen::ShaderModules code;
    const VideoCore::FrameGen::LosslessStatus status = VideoCore::FrameGen::LoadShaderModules(code);
    if (status != VideoCore::FrameGen::LosslessStatus::Ok) {
        if (wanted)
            Eden::Report("framegen",
                         ("Lossless.dll " + VideoCore::FrameGen::GetLosslessDllPath().string() +
                          " status=" + std::to_string(static_cast<u32>(status)) +
                          " (0 ok, 1 not installed, 2 unreadable, 3 not a PE, 4 no shaders in it, "
                          "5 translation failed, 6 cache unusable)").c_str());
        return;
    }"""),
    ("""    valid = true;""",
     """    valid = true;
    if (wanted)
        Eden::Report("framegen", ("shaders ready: " + std::to_string(code.size()) + " modules").c_str());"""),
])
# Two members, so that "frames are being generated" and "this image cannot take them" are said once
# per session rather than once per process or once per frame: this object is built per renderer,
# and the renderer is built per game session.
adapt('src/video_core/renderer_vulkan/present/frame_gen.h',
      'include/video_core/renderer_vulkan/present/frame_gen.h', [
    ('    bool dumped{};', '    bool dumped{};\n    bool generation_reported{};\n    bool storage_reported{};'),
])
adapt('src/video_core/renderer_vulkan/present/frame_gen.cpp', 'vulkan_frame_gen.cpp', [
    ('#include "video_core/vulkan_common/vulkan_device.h"',
     '#include "video_core/vulkan_common/vulkan_device.h"\n#include "diagnostics.h"'),
    ("""    if (!shaders->IsValid() && Settings::values.frame_gen_type.GetValue() != Settings::FrameGenType::Fsr3) {
        unavailable = true;
        return;
    }""",
     """    if (!shaders->IsValid() && Settings::values.frame_gen_type.GetValue() != Settings::FrameGenType::Fsr3) {
        Eden::Report("framegen", "LsfgShaders is not valid; see the lines above");
        unavailable = true;
        return;
    }"""),
    ("""    if (!frame->storage_view) {
        warm_streak = 0;
        return;
    }""",
     """    if (!frame->storage_view) {
        if (!storage_reported) {
            storage_reported = true;
            Eden::Report("framegen",
                         "the frame image has no storage view: the swapchain format lacks "
                         "VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT, so frames cannot be generated");
        }
        warm_streak = 0;
        return;
    }"""),
    # The gates above only say why it is not running; this says that it is.
    ("""    generated = warm && warm_streak >= LSFG_RECURRENCE_FRAMES && plan.generations > 0;""",
     """    generated = warm && warm_streak >= LSFG_RECURRENCE_FRAMES && plan.generations > 0;
    if (generated && !generation_reported) {
        generation_reported = true;
        Eden::Report("framegen",
                     ("generating " + std::to_string(plan.generations) +
                      " extra frame(s) per guest frame").c_str());
    }"""),
])
# Split the dispatch into its three phases (Tegra::Control::Scheduler::Push): the channel lock,
# queueing the submit list, and executing the guest's methods. The dispatch timer wraps the whole
# call from gpu_thread.cpp, so these say which of the three it is spent in.
adapt('src/video_core/control/scheduler.cpp', 'control_scheduler.cpp', [
    ('#include <memory>', '#include <memory>\n#include "performance.h"'),
    ('        std::unique_lock lk(scheduling_guard);\n'
     '        auto it = channels.find(channel);\n'
     '        ASSERT(it != channels.end());\n'
     '        channel_state = it->second;\n'
     '    }\n'
     '    BindChannel(gpu, channel_state->bind_id);\n'
     '    // Process commands outside the lock to reduce contention.\n'
     '    // Multiple channels can prepare their commands in parallel.\n'
     '    channel_state->payload->dma_pusher.Push(std::move(entries));\n'
     '    channel_state->payload->dma_pusher.DispatchCalls();',
     '        const ::Eden::Performance::Timer sched_lock_timer(::Eden::Performance::gpu_sched_lock);\n'
     '        std::unique_lock lk(scheduling_guard);\n'
     '        auto it = channels.find(channel);\n'
     '        ASSERT(it != channels.end());\n'
     '        channel_state = it->second;\n'
     '    }\n'
     '    BindChannel(gpu, channel_state->bind_id);\n'
     '    // Process commands outside the lock to reduce contention.\n'
     '    // Multiple channels can prepare their commands in parallel.\n'
     '    {\n'
     '        const ::Eden::Performance::Timer dma_push_timer(::Eden::Performance::gpu_dma_push);\n'
     '        channel_state->payload->dma_pusher.Push(std::move(entries));\n'
     '    }\n'
     '    {\n'
     '        const ::Eden::Performance::Timer dma_dispatch_timer(::Eden::Performance::gpu_dma_dispatch);\n'
     '        channel_state->payload->dma_pusher.DispatchCalls();\n'
     '    }'),
])
shader_header = '#pragma once\n#include <cstdint>\n'
for stage in ('vert', 'frag'):
    binary = output / f'vulkan_hud.{stage}.spv'
    subprocess.run(['glslangValidator', '-V', '--target-env', 'vulkan1.0',
                    '-o', str(binary), str(port / f'vulkan_hud.{stage}')], check=True)
    subprocess.run(['spirv-val', '--target-env', 'vulkan1.0', str(binary)], check=True)
    data = binary.read_bytes()
    words = struct.unpack('<' + 'I' * (len(data) // 4), data)
    shader_header += f'inline constexpr uint32_t EDEN_HUD_{stage.upper()}_SPV[] = {{\n'
    shader_header += ','.join(hex(word) for word in words) + '\n};\n'
path = output / 'vulkan_hud_shaders.h'
if not path.exists() or path.read_text() != shader_header:
    path.write_text(shader_header)
if broken:
    print(f"{len(broken)} anchor(s) of the Eden source changed; adapt them in tools/prepare-vulkan-port.py:", file=sys.stderr)
    for line in broken:
        print("  " + line, file=sys.stderr)
    sys.exit(1)
