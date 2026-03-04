"""
Agent Mesh v1.0 — Spec Refiner

Breaks design changes into implementable chunks, ordered by dependency.
Each chunk gets a self-contained partial spec for the Implementation Pipeline.
After each chunk completes, remaining chunks can be adjusted based on results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .spec_analyzer import DesignChange, _parse_json_array

logger = logging.getLogger("agent-mesh")


@dataclass
class DesignChunk:
    """A batch of changes that can be implemented together."""
    chunk_id: str                     # e.g. "chunk-1-schema"
    title: str
    changes: list[DesignChange] = field(default_factory=list)
    partial_spec: str = ""            # self-contained spec for this chunk
    wave_order: int = 0               # execution order
    depends_on_chunks: list[str] = field(default_factory=list)
    estimated_tasks: int = 0          # expected number of plan tasks
    status: str = "pending"           # pending | in_progress | completed | needs_redesign

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "title": self.title,
            "changes": [c.to_dict() for c in self.changes],
            "partial_spec": self.partial_spec,
            "wave_order": self.wave_order,
            "depends_on_chunks": self.depends_on_chunks,
            "estimated_tasks": self.estimated_tasks,
            "status": self.status,
        }

    @staticmethod
    def from_dict(d: dict) -> DesignChunk:
        return DesignChunk(
            chunk_id=d["chunk_id"],
            title=d["title"],
            changes=[DesignChange.from_dict(c) for c in d.get("changes", [])],
            partial_spec=d.get("partial_spec", ""),
            wave_order=d.get("wave_order", 0),
            depends_on_chunks=d.get("depends_on_chunks", []),
            estimated_tasks=d.get("estimated_tasks", 0),
            status=d.get("status", "pending"),
        )


class SpecRefiner:
    """Breaks changes into implementable chunks."""

    def __init__(self, config: dict):
        design_cfg = config.get("design", {})
        self.model = design_cfg.get("refiner_model", "claude-sonnet-4-6")
        self.max_tasks_per_chunk = design_cfg.get("max_tasks_per_chunk", 10)

    async def plan_chunks(
        self, changes: list[DesignChange], spec_content: str
    ) -> list[DesignChunk]:
        """
        Group changes into implementable chunks, ordered by dependency.

        Chunking rules:
        1. Schema changes (ALTER/CREATE) → always chunk-1 (foundation)
        2. Backend CRUD for new modules → one chunk per module
        3. Modifications to existing modules → group by module
        4. Frontend → after backend, group by page
        5. Independent apps (PWA) → separate chunk, can run last

        Each chunk gets a self-contained partial_spec from full spec.
        """
        prompt = self._build_chunking_prompt(changes, spec_content)
        raw = await self._call_claude(prompt)

        chunks = self._parse_chunks(raw)
        if not chunks:
            # Fallback: one chunk with all changes
            logger.warning("[SpecRefiner] LLM chunking failed, using single chunk fallback")
            chunks = [DesignChunk(
                chunk_id="chunk-1-all",
                title="All changes",
                changes=changes,
                wave_order=1,
                estimated_tasks=len(changes),
            )]

        # Extract partial specs for each chunk
        for chunk in chunks:
            if not chunk.partial_spec:
                chunk.partial_spec = await self._extract_partial_spec(
                    chunk, spec_content, changes
                )

        # Sort by wave_order
        chunks.sort(key=lambda c: c.wave_order)

        logger.info(
            f"[SpecRefiner] Planned {len(chunks)} chunks: "
            + ", ".join(f"{c.chunk_id}(wave={c.wave_order})" for c in chunks)
        )
        return chunks

    async def _extract_partial_spec(
        self, chunk: DesignChunk, full_spec: str, all_changes: list[DesignChange]
    ) -> str:
        """
        Extract relevant sections from full spec for this chunk.
        Includes: relevant data models, API endpoints, acceptance criteria.
        Adds context about what already exists (from previous chunks).
        """
        # chunk.changes may not be populated yet (only _change_ids from LLM)
        # Use _change_ids to look up from all_changes
        change_ids = getattr(chunk, '_change_ids', [c.change_id for c in chunk.changes])
        change_map = {c.change_id: c for c in all_changes}
        relevant_changes = [change_map[cid] for cid in change_ids if cid in change_map]

        if not relevant_changes:
            # Fallback: use chunk title for context
            change_summaries = f"- {chunk.chunk_id}: {chunk.title}"
        else:
            change_summaries = "\n".join(
                f"- {c.change_id}: {c.title} ({c.change_type}) — {c.description[:100]}"
                for c in relevant_changes
            )

        prompt = f"""You are a technical writer extracting a self-contained partial specification.

## Task
From the FULL SPEC below, extract ONLY the sections relevant to these changes:

{change_summaries}

## Rules
1. Include ALL relevant data model definitions (CREATE TABLE, field descriptions)
2. Include ALL relevant API endpoint definitions (routes, request/response)
3. Include ALL relevant acceptance criteria
4. Include shared context (auth, conventions) that these changes depend on
5. The partial spec must be SELF-CONTAINED — someone reading it should understand
   what to build without reading the full spec
6. Do NOT include sections for unrelated modules
7. Keep the same markdown structure as the original spec

## Output
Respond with the partial spec in markdown. No JSON wrapping.

## FULL SPEC
{full_spec}
"""
        result = await self._call_claude(prompt)
        # If result looks like JSON-wrapped, unwrap it
        if result.startswith('{') or result.startswith('['):
            return full_spec  # fallback to full spec
        return result

    async def adjust_remaining_chunks(
        self,
        completed_chunk: DesignChunk,
        validation_result: dict,
        remaining_chunks: list[DesignChunk],
        full_spec: str,
    ) -> list[DesignChunk]:
        """
        After a chunk is implemented, adjust remaining chunks if needed.
        E.g., if Contract module implementation revealed Invoice needs
        different schema than planned, update subsequent chunks' specs.
        """
        design_issues = validation_result.get("design_issues", [])
        drift_notes = validation_result.get("drift_notes", "")

        if not design_issues and not drift_notes:
            return remaining_chunks

        prompt = self._build_adjustment_prompt(
            completed_chunk, design_issues, drift_notes, remaining_chunks
        )
        raw = await self._call_claude(prompt)
        adjustments = _parse_json_array(raw)

        # Apply adjustments
        chunk_map = {c.chunk_id: c for c in remaining_chunks}
        adjusted_count = 0

        for adj in adjustments:
            if not isinstance(adj, dict):
                continue
            chunk_id = adj.get("chunk_id")
            if chunk_id not in chunk_map:
                continue

            chunk = chunk_map[chunk_id]
            if adj.get("spec_additions"):
                chunk.partial_spec += f"\n\n## Adjustments from {completed_chunk.chunk_id}\n"
                chunk.partial_spec += adj["spec_additions"]
                adjusted_count += 1
            if adj.get("new_estimated_tasks"):
                chunk.estimated_tasks = adj["new_estimated_tasks"]
            if adj.get("status") == "needs_redesign":
                chunk.status = "needs_redesign"
                adjusted_count += 1

        if adjusted_count:
            logger.info(
                f"[SpecRefiner] Adjusted {adjusted_count} remaining chunks "
                f"based on {completed_chunk.chunk_id} results"
            )

        return remaining_chunks

    def _build_chunking_prompt(
        self, changes: list[DesignChange], spec_content: str
    ) -> str:
        changes_json = json.dumps(
            [c.to_dict() for c in changes], indent=2, ensure_ascii=False
        )
        return f"""You are a senior software architect planning implementation batches.

## Task
Group these design changes into implementable chunks, ordered by dependency.

## Chunking Rules
1. Schema changes (ALTER_SCHEMA) → always in chunk-1 (foundation layer)
2. Backend CRUD for NEW_MODULE → one chunk per module
3. MODIFY_BEHAVIOR on existing modules → group by module
4. NEW_FRONTEND → after its backend dependency, group by page/feature
5. Independent features (like PWA) → separate chunk, can run last
6. Each chunk should produce max {self.max_tasks_per_chunk} implementation tasks
7. Minimize cross-chunk dependencies

## Output Format
Respond ONLY with a JSON array of chunk objects. No other text.
{{
  "chunk_id": "chunk-N-short-name",
  "title": "human-readable title",
  "change_ids": ["list of change_ids in this chunk"],
  "wave_order": 1,
  "depends_on_chunks": ["chunk-ids this depends on"],
  "estimated_tasks": 5
}}

## DESIGN CHANGES
{changes_json}

## FULL SPEC (for context)
{spec_content[:20000]}
"""

    def _build_adjustment_prompt(
        self,
        completed_chunk: DesignChunk,
        design_issues: list,
        drift_notes: str,
        remaining_chunks: list[DesignChunk],
    ) -> str:
        remaining_info = json.dumps(
            [{"chunk_id": c.chunk_id, "title": c.title,
              "change_ids": [ch.change_id for ch in c.changes]}
             for c in remaining_chunks],
            indent=2, ensure_ascii=False,
        )
        return f"""You are a software architect adjusting implementation plans after discovering issues.

## Context
Chunk "{completed_chunk.chunk_id}" ({completed_chunk.title}) has been implemented.
During validation, these issues were found:

### Design Issues
{json.dumps(design_issues, indent=2, ensure_ascii=False)}

### Drift Notes
{drift_notes}

## Task
Determine if any of these remaining chunks need adjustment:
{remaining_info}

## Output Format
Respond ONLY with a JSON array. No other text.
For each chunk that needs adjustment:
{{
  "chunk_id": "the-chunk-id",
  "spec_additions": "additional spec text to append (if any)",
  "new_estimated_tasks": null or updated number,
  "status": "pending" or "needs_redesign"
}}

If no chunks need adjustment, return an empty array: []
"""

    async def _call_claude(self, prompt: str) -> str:
        """Call Claude CLI with prompt."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --model {self.model} --output-format text',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=600
            )
            result = stdout.decode().strip()
            if proc.returncode != 0:
                logger.warning(
                    f"[SpecRefiner] Claude returned code {proc.returncode}: "
                    f"{stderr.decode()[:200]}"
                )
            return result
        except asyncio.TimeoutError:
            logger.error("[SpecRefiner] Claude CLI timed out (600s)")
            return "[]"
        except Exception as e:
            logger.error(f"[SpecRefiner] Claude CLI error: {e}")
            return "[]"
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    def _parse_chunks(self, raw: str) -> list[DesignChunk]:
        """Parse chunking result from LLM and map change_ids back to DesignChange objects."""
        data = _parse_json_array(raw)
        chunks = []

        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                chunk = DesignChunk(
                    chunk_id=item["chunk_id"],
                    title=item.get("title", ""),
                    wave_order=item.get("wave_order", 0),
                    depends_on_chunks=item.get("depends_on_chunks", []),
                    estimated_tasks=item.get("estimated_tasks", 0),
                    # changes will be populated by caller after matching change_ids
                )
                # Store change_ids temporarily for later mapping
                chunk._change_ids = item.get("change_ids", [])
                chunks.append(chunk)
            except (KeyError, TypeError) as e:
                logger.warning(f"[SpecRefiner] Skipping malformed chunk: {e}")

        return chunks

    def map_changes_to_chunks(
        self, chunks: list[DesignChunk], changes: list[DesignChange]
    ):
        """Map DesignChange objects to chunks by change_id."""
        change_map = {c.change_id: c for c in changes}
        for chunk in chunks:
            change_ids = getattr(chunk, '_change_ids', [])
            for cid in change_ids:
                if cid in change_map:
                    chunk.changes.append(change_map[cid])
                else:
                    logger.warning(
                        f"[SpecRefiner] change_id '{cid}' in {chunk.chunk_id} "
                        f"not found in changes list"
                    )
            # Clean up temp attr
            if hasattr(chunk, '_change_ids'):
                delattr(chunk, '_change_ids')
