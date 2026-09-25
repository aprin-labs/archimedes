"""Operator-triggered KnowledgeBase pipeline runner.

Wraps the existing ``submodules/KnowledgeBase/papers_analysis/`` pipeline
(SPECTER2 embeddings + HDBSCAN/BERTopic clustering + REBEL/SciSpacy KG)
and writes outputs to:

  /srv/corpus-artifact/embeddings.npy + ids.json
  /srv/corpus-artifact/clusters.json
  /srv/corpus-artifact/topics.json
  /srv/corpus-artifact/kg_triples.jsonl
  /srv/corpus-artifact/kg_graph.json
  /srv/corpus-artifact/manifest.json
  Postgres papers.cluster_id + topic_label (denormalized)
  Postgres kg_entities + kg_relations

Per docs/specs/kb-integration-spec.md:
  - No re-implementation: invokes the submodule's existing code.
  - Atomic-swap: writes to a tmpdir, then symlinks on success.

This skeleton wires the entry point and persistence schema; the actual
pipeline invocation is gated behind a feature flag while the docker
container with the SPECTER2 / REBEL / SciSpacy deps is built out.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIR = Path(os.getenv("KB_ARTIFACT_DIR", "/srv/corpus-artifact"))
DEFAULT_CORPUS_DIR = Path(os.getenv("KB_CORPUS_DIR", "/srv/corpus-text"))


def _kb_submodule_path() -> Path:
    """Locate submodules/KnowledgeBase/ relative to this file."""
    here = Path(__file__).resolve()
    # backend/archimedes/scripts/run_kb_pipeline.py → repo root → submodules/KB/
    return here.parents[3] / "submodules" / "KnowledgeBase"


def run_pipeline(
    *,
    corpus_dir: Path | None = None,
    artifact_dir: Path | None = None,
    lease_lost: threading.Event | None = None,
) -> dict:
    """Run the full KB pipeline. Returns a manifest dict.

    The actual model-call code is gated behind ``KB_PIPELINE_ENABLED`` until
    the dedicated container with SPECTER2/REBEL/SciSpacy weights is built
    out. Without the flag we still write an empty manifest so the runner's
    "have we ever run?" check has a stable target.

    ``lease_lost`` (#1046 review, exactly-once #1043): a ``threading.Event``
    the caller's independent renewal thread sets the instant it can no
    longer prove this process still holds the runner lease (CAS loss, or an
    error that leaves renewal unconfirmed). When set, this function still
    computes and returns a manifest dict (so the caller's return value stays
    informative) but REFUSES TO WRITE manifest.json — a run whose lease
    already lapsed must not clobber the authoritative instance's manifest
    with a lost-update race, since another kb_runner copy may already be
    running its own pipeline under a fresh lease.

    This is a guard-the-write strategy rather than a fully cooperative
    mid-pipeline abort because today's real pipeline invocation below is
    still a skeleton (raises ``NotImplementedError``) with exactly one
    finalization step — the manifest write. It IS checked as an early
    cooperative-abort checkpoint before that (currently unimplemented, ~6GB
    of model weights) stage begins, so once the real SPECTER2/HDBSCAN/REBEL
    stages are wired in, prefer adding further ``lease_lost`` checks at their
    natural checkpoints rather than relying solely on this final guard.
    """
    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    artifact_dir = artifact_dir or DEFAULT_ARTIFACT_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)

    time.time()
    started_iso = datetime.now(UTC).isoformat()
    logger.info("kb_pipeline: starting (corpus=%s, artifact=%s)", corpus_dir, artifact_dir)

    if not os.getenv("KB_PIPELINE_ENABLED"):
        manifest = {
            "run_ts": started_iso,
            "duration_s": 0.0,
            "paper_count": 0,
            "cluster_count": 0,
            "kg_node_count": 0,
            "kg_edge_count": 0,
            "status": "skipped — set KB_PIPELINE_ENABLED=1 to invoke the full pipeline",
        }
        if lease_lost is not None and lease_lost.is_set():
            logger.error(
                "kb_pipeline: lease lost during this run — refusing to write manifest.json "
                "(another kb_runner copy may already hold a fresh lease and be running its "
                "own pipeline; writing now would risk a lost-update race on the authoritative "
                "manifest)"
            )
            return manifest
        (artifact_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        logger.info("kb_pipeline: skipped (KB_PIPELINE_ENABLED not set)")
        return manifest

    if lease_lost is not None and lease_lost.is_set():
        logger.error(
            "kb_pipeline: lease already lost before the real pipeline stage started — "
            "aborting without running the multi-GB model pipeline or writing a manifest"
        )
        return {
            "run_ts": started_iso,
            "duration_s": 0.0,
            "paper_count": 0,
            "cluster_count": 0,
            "kg_node_count": 0,
            "kg_edge_count": 0,
            "status": "aborted — lease lost before pipeline start",
        }

    # Real pipeline path — gated by feature flag. Invocation pattern lives in
    # kb-integration-spec.md § "Pipeline invocation". Adding to sys.path so
    # the submodule's `papers_analysis` package is importable.
    kb_root = _kb_submodule_path()
    if str(kb_root) not in sys.path:
        sys.path.insert(0, str(kb_root))

    # Real papers_analysis entry points (verified against the submodule pin;
    # these are not a drop-in wiring job — see remaining-work notes below):
    #   papers_analysis.vectorize.run_vectorize(
    #       cache_dir, metadata_db, embeddings_dir, tfidf, specter2, …
    #   ) → dict
    #   papers_analysis.cluster.run_clustering(
    #       embeddings, docs, sha256s, …
    #   ) → dict
    #   papers_analysis.cluster._extract_topic_records(topic_model)
    #       → dict[int, dict]  # BERTopic labels; called from run_clustering,
    #                          # not a standalone public API
    #   papers_analysis.knowledge_graph.run_knowledge_graph(
    #       db_path, out_dir, …, device="mps"
    #   ) → None  # writes JSONL/GraphML/TTL to disk; does not return triples
    #   papers_analysis.knowledge_graph.build_networkx_graph(...)
    #       → nx.MultiDiGraph
    #   papers_analysis.knowledge_graph.build_rdf_graph(G) → RDFLib Graph
    # papers_analysis.graph.run_graph / papers_analysis.graph.build_graph exist
    # but build a SPECTER2 cosine-similarity graph, not a KG from triples.
    #
    # Three mismatches remain before this skeleton can call those entry
    # points (this is remaining work, not a wiring job — see #1090):
    #   1. Data model. papers_analysis is built around data/metadata.db
    #      (sqlite) plus data/embeddings/, both resolved relative to the
    #      submodule. This runner and kb-integration-spec.md assume a
    #      corpus_text_dir of text files and /srv/corpus-artifact/. There
    #      is no adapter between them.
    #   2. Identity. The submodule keys on sha256; Archimedes keys on
    #      paper id. A mapping layer is required before any output can be
    #      persisted to kg_entities / kg_relations.
    #   3. Device. run_knowledge_graph defaults device="mps" and returns
    #      None, writing to disk. On Fargate that has to be "cpu"
    #      (infra/variables.tf already records GPU-appropriate compute as
    #      out of scope there).
    #
    # The intended artifact layout (atomic-swap, output schema) is in
    # docs/specs/kb-integration-spec.md § Pipeline invocation — that spec
    # names modules and prose steps, not these functions. The real path is
    # gated behind KB_PIPELINE_ENABLED because it pulls in ~6 GB of model
    # weights (SPECTER2, REBEL, SciSpacy) and runs meaningfully only inside
    # the dedicated container. Once that container ships, set the flag and
    # this NotImplementedError will be replaced with the real invocation.
    raise NotImplementedError(
        "KB_PIPELINE_ENABLED set, but the full pipeline invocation is not yet wired. "
        "See docs/specs/kb-integration-spec.md § Pipeline invocation for the canonical shape. "
        "Until the dedicated docker container with SPECTER2/REBEL/SciSpacy weights ships, "
        "leave KB_PIPELINE_ENABLED unset and rely on the skip path."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    manifest = run_pipeline(corpus_dir=args.corpus, artifact_dir=args.artifact)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
