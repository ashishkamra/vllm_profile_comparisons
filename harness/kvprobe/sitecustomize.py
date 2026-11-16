# SPDX-License-Identifier: Apache-2.0
"""KV-cache probe: log what vLLM's KV-cache manager decides, one line per step.

The torch profiler shows *when* the scheduler allocates blocks and how long it
takes, but not *which* blocks: tensor values are not recorded. This probe writes
the block-level state the scheduler holds after every `Scheduler.schedule()`:
each running request's block table, which blocks were newly allocated, prefix
cache hits, preemptions, finished requests, evictions from the prefix cache,
and the block pool's free count.

It is loaded through `sitecustomize` (harness/journey_run.sh puts this directory
on PYTHONPATH when KV_PROBE=1) and does nothing unless VLLM_KV_PROBE names an
output file. It does not import vLLM itself: an import hook patches the two
modules when vLLM imports them, in whichever process runs the scheduler.

Cost: one JSON line per engine step, ~50-150 us on the engine's main thread.
"""

import importlib.abc
import importlib.machinery
import os
import sys

_OUT = os.environ.get("VLLM_KV_PROBE")
_TARGETS = {"vllm.v1.core.sched.scheduler", "vllm.v1.core.block_pool"}


def _patch_block_pool(mod):
    import time

    pool_cls = mod.BlockPool
    orig = pool_cls._maybe_evict_cached_block

    def _maybe_evict_cached_block(self, block):
        evicted = orig(self, block)
        if evicted:
            _STATE["evicted"].append(block.block_id)
        return evicted

    pool_cls._maybe_evict_cached_block = _maybe_evict_cached_block
    _STATE["t_import"] = time.time()


def _patch_scheduler(mod):
    import json
    import time

    sched_cls = mod.Scheduler
    orig = sched_cls.schedule
    f = open(_OUT, "a", buffering=1)

    def schedule(self, *args, **kwargs):
        out = orig(self, *args, **kwargs)
        try:
            _STATE["step"] += 1
            kvm = self.kv_cache_manager
            pool = kvm.block_pool
            tables = {}
            refs = {}
            reqs = {}
            for req in self.running:
                rid = req.request_id
                ids = list(kvm.get_block_ids(rid)[0])
                tables[rid] = ids
                for b in ids:
                    rc = pool.blocks[b].ref_cnt
                    if rc > 1:
                        refs[b] = rc
                reqs[rid] = [req.num_prompt_tokens, req.num_tokens, req.num_computed_tokens]
            cached = out.scheduled_cached_reqs
            rec = {
                "t": time.time(),
                "step": _STATE["step"],
                "sched": out.num_scheduled_tokens,
                "new": [
                    {"rid": n.req_id, "blocks": list(n.block_ids[0]), "computed": n.num_computed_tokens}
                    for n in out.scheduled_new_reqs
                ],
                "cached": [
                    {
                        "rid": rid,
                        "new_blocks": list(nb[0]) if nb else [],
                        "computed": nc,
                        "resumed": rid in cached.resumed_req_ids,
                    }
                    for rid, nb, nc in zip(cached.req_ids, cached.new_block_ids, cached.num_computed_tokens)
                ],
                "preempted": sorted(out.preempted_req_ids or ()),
                "finished": sorted(out.finished_req_ids),
                "waiting": [r.request_id for r in self.waiting],
                "free_blocks": pool.get_num_free_blocks(),
                "num_blocks": pool.num_gpu_blocks,
                "cached_hashes": len(pool.cached_block_hash_to_block),
                "evicted": _STATE["evicted"],
                "tables": tables,
                "reqs": reqs,  # rid -> [prompt tokens, tokens so far, computed tokens]
                "shared": refs,  # block id -> ref count, for blocks held by >1 request
            }
            _STATE["evicted"] = []
            if out.total_num_scheduled_tokens or rec["finished"] or rec["preempted"]:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # never break the engine
            f.write(json.dumps({"probe_error": repr(e)}) + "\n")
        return out

    sched_cls.schedule = schedule
    f.write(json.dumps({"probe": "start", "pid": os.getpid(), "t": time.time()}) + "\n")


_STATE = {"step": 0, "evicted": []}
_PATCHERS = {"vllm.v1.core.sched.scheduler": _patch_scheduler, "vllm.v1.core.block_pool": _patch_block_pool}


class _Loader(importlib.abc.Loader):
    def __init__(self, inner, name):
        self.inner, self.name = inner, name

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        _PATCHERS[self.name](module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name not in _TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _Loader(spec.loader, name)
        return spec


if _OUT:
    sys.meta_path.insert(0, _Finder())
